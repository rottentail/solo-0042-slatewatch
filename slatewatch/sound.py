"""Accumulate native PCM into coverage-aware, per-channel time windows."""
from dataclasses import dataclass
from math import isqrt

from .clock import InputError, LIMIT, SECOND, Span, coalesce, sample_boundary
from .hits import candidates


@dataclass
class Window:
    start_sample: int
    end_sample: int
    covered: int
    energy: list[int]
    clipped: list[int]


class SoundMeter:
    def __init__(self, track, thresholds, window_us=None):
        self.rate = track['rate']
        self.channels = track['channels']
        self.thresholds = thresholds
        self.step = window_us if window_us is not None else thresholds.audio_window_ms * 1000
        self.windows = {}
        self.blocks = []
        self.samples = 0
        self.peak = [0] * self.channels

    def push(self, pcm):
        if not pcm.count:
            return
        if pcm.span.end > LIMIT:
            raise InputError('sound extent exceeds 120 seconds')
        self.blocks.append(pcm.span)
        self.samples += pcm.count
        current = pcm.first_sample
        while current < pcm.end_sample:
            key = current * SECOND // (self.step * self.rate)
            begin = sample_boundary(key * self.step, self.rate)
            boundary = sample_boundary((key + 1) * self.step, self.rate)
            window = self.windows.get(key)
            if window is None:
                window = Window(begin, boundary, 0, [0] * self.channels, [0] * self.channels)
                self.windows[key] = window
            end = min(pcm.end_sample, window.end_sample)
            offset = (current - pcm.first_sample) * self.channels
            size = (end - current) * self.channels
            for channel in range(self.channels):
                values = pcm.samples[offset + channel:offset + size:self.channels]
                window.energy[channel] += sum(x * x for x in values)
                window.clipped[channel] += sum(x == 32767 or x == -32768 for x in values)
                self.peak[channel] = max(self.peak[channel], max(abs(x) for x in values))
            window.covered += end - current
            current = end

    def _bins(self, channel, kind):
        for key, window in sorted(self.windows.items()):
            span = Span(key * self.step, (key + 1) * self.step)
            value = None
            if window.covered == window.end_sample - window.start_sample:
                if kind == 'silence':
                    rms = isqrt(window.energy[channel] // window.covered)
                    if rms <= self.thresholds.silence_rms:
                        value = rms
                else:
                    permille = window.clipped[channel] * 1000 // window.covered
                    if permille >= self.thresholds.clip_permille:
                        value = permille
            yield span, value

    def finish(self):
        hits = []
        for channel in range(self.channels):
            hits.extend(candidates(self._bins(channel, 'silence'), 'silence',
                                   self.thresholds.silence_min_ms * 1000, channel + 1))
            hits.extend(candidates(self._bins(channel, 'clip'), 'clip',
                                   self.thresholds.clip_min_ms * 1000, channel + 1))
        return coalesce(self.blocks), hits, {'samples': self.samples, 'peak_codes': self.peak}
