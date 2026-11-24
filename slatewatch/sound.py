"""Accumulate native PCM into coverage-aware per-channel windows.

Two internal boundaries live in this module:

* :class:`WindowGrid` decides which fixed time window a source sample belongs
  to and only accumulates coverage, energy and full-scale counts.  It knows
  nothing about silence, clipping or minimum event lengths, so the same
  accumulation code serves every window grid phase.
* :func:`window_reading` and :func:`channel_hits` apply the immutable rule
  thresholds to already accumulated :class:`Window` values.  They never touch
  PCM or media, which lets the rule semantics be checked with hand-built
  windows and reused for every grid without a second decode pass.

:class:`SoundMeter` is the scan-facing coordinator: it feeds each decoded PCM
block once into the registered grids and then asks the same rule functions to
synthesize candidates for each of them.  The default scan always uses the
phase-zero grid, so its windows, statistics and findings are unchanged.
"""
from dataclasses import dataclass
from math import isqrt

from .clock import InputError, LIMIT, SECOND, Span, coalesce, sample_boundary
from .hits import candidates


@dataclass
class Window:
    """Accumulated source samples for one fixed grid slot of one grid."""

    start_sample: int
    end_sample: int
    covered: int
    energy: list[int]
    clipped: list[int]

    @property
    def capacity(self):
        return self.end_sample - self.start_sample

    @property
    def complete(self):
        return self.covered == self.capacity


class WindowGrid:
    """Fold native-rate PCM blocks into windows on one phase of the grid.

    ``phase_us`` shifts every window boundary by a fixed number of
    microseconds.  Phase zero reproduces the programme-clock-aligned scan
    grid; other phases are used only by internal verification and never
    change the stored scan.  A negative phase is allowed so that every
    programme-clock sample still lands in a full-capacity window.
    """

    def __init__(self, rate, channels, step_us, phase_us=0):
        if not 0 <= abs(phase_us) < step_us:
            raise InputError('sound grid phase must stay inside one window')
        self.rate = rate
        self.channels = channels
        self.step = step_us
        self.phase = phase_us
        self.windows = {}

    def _slot(self, sample):
        return (sample * SECOND - self.phase * self.rate) // (self.step * self.rate)

    def _window(self, key):
        window = self.windows.get(key)
        if window is None:
            begin = sample_boundary(key * self.step + self.phase, self.rate)
            boundary = sample_boundary((key + 1) * self.step + self.phase, self.rate)
            window = Window(begin, boundary, 0, [0] * self.channels, [0] * self.channels)
            self.windows[key] = window
        return window

    def span(self, key):
        return Span(key * self.step + self.phase, (key + 1) * self.step + self.phase)

    def push(self, pcm):
        """Accumulate one decoded PCM block without applying any rule."""
        current = pcm.first_sample
        while current < pcm.end_sample:
            window = self._window(self._slot(current))
            end = min(pcm.end_sample, window.end_sample)
            offset = (current - pcm.first_sample) * self.channels
            size = (end - current) * self.channels
            for channel in range(self.channels):
                values = pcm.samples[offset + channel:offset + size:self.channels]
                window.energy[channel] += sum(x * x for x in values)
                window.clipped[channel] += sum(x == 32767 or x == -32768 for x in values)
            window.covered += end - current
            current = end

    def rows(self):
        """Yield ``(programme-clock span, window)`` in grid order."""
        for key, window in sorted(self.windows.items()):
            yield self.span(key), window


def window_reading(window, channel, kind, thresholds):
    """Apply one metric's rule to a single window.

    Windows that do not contain their full source sample set return ``None``
    and can never decide a metric.  This is the single place that interprets
    the silence and clipping thresholds, so every grid and every hand-built
    verification input shares the same semantics.
    """
    if not window.complete:
        return None
    if kind == 'silence':
        rms = isqrt(window.energy[channel] // window.capacity)
        return rms if rms <= thresholds.silence_rms else None
    permille = window.clipped[channel] * 1000 // window.capacity
    return permille if permille >= thresholds.clip_permille else None


def channel_hits(rows, channels, thresholds):
    """Fold accumulated windows into findings using the shared run logic.

    ``rows`` is any iterable of ``(Span, Window)``; it does not have to come
    from a media decode.  Missing grid slots naturally break runs because the
    caller only supplies windows that received samples.
    """
    rows = list(rows)
    findings = []
    for channel in range(channels):
        silence_bins = ((span, window_reading(window, channel, 'silence', thresholds))
                        for span, window in rows)
        clip_bins = ((span, window_reading(window, channel, 'clip', thresholds))
                     for span, window in rows)
        findings.extend(candidates(silence_bins, 'silence',
                                   thresholds.silence_min_ms * 1000, channel + 1))
        findings.extend(candidates(clip_bins, 'clip',
                                   thresholds.clip_min_ms * 1000, channel + 1))
    return findings


class SoundMeter:
    """Coordinate the scan grid and optional verification grids in one pass."""

    def __init__(self, track, thresholds, extra_phases=()):
        self.rate = track['rate']
        self.channels = track['channels']
        self.thresholds = thresholds
        step = thresholds.audio_window_ms * 1000
        self.step = step
        self.grids = [WindowGrid(self.rate, self.channels, step)]
        for phase in extra_phases:
            if phase == 0:
                raise InputError('the phase-zero sound grid is reserved for the scan')
            self.grids.append(WindowGrid(self.rate, self.channels, step, phase))
        self.blocks = []
        self.samples = 0
        self.peak = [0] * self.channels

    @property
    def scan_grid(self):
        return self.grids[0]

    def push(self, pcm):
        if not pcm.count:
            return
        if pcm.span.end > LIMIT:
            raise InputError('sound extent exceeds 120 seconds')
        self.blocks.append(pcm.span)
        self.samples += pcm.count
        for grid in self.grids:
            grid.push(pcm)
        index = 0
        for _ in range(pcm.count):
            for channel in range(self.channels):
                self.peak[channel] = max(self.peak[channel], abs(pcm.samples[index]))
                index += 1

    def findings(self, grid):
        return channel_hits(grid.rows(), self.channels, self.thresholds)

    def readings(self, grid):
        """Expose per-window support and measured values for verification."""
        result = []
        for span, window in grid.rows():
            per_channel = []
            for channel in range(self.channels):
                per_channel.append({
                    'covered': window.covered,
                    'capacity': window.capacity,
                    'complete': window.complete,
                    'rms': isqrt(window.energy[channel] // window.capacity) if window.complete else None,
                    'clip_permille': (window.clipped[channel] * 1000 // window.capacity
                                      if window.complete else None),
                    'silence_value': window_reading(window, channel, 'silence', self.thresholds),
                    'clip_value': window_reading(window, channel, 'clip', self.thresholds),
                })
            result.append({'span': span.as_dict(), 'channels': per_channel})
        return result

    def finish(self):
        hits = self.findings(self.scan_grid)
        return coalesce(self.blocks), hits, {'samples': self.samples, 'peak_codes': self.peak}
