"""Rolling delivery checks over equal-length, sample-aligned sound windows.

The delivery party verifies the companion audio in fixed-length sliding
windows.  Unlike the scan-time RMS bins, every window also keeps its largest
absolute sample, so an unusually short peak cannot be diluted by a long calm
stretch.  Coverage holes are never interpreted as silence: the digital-silence
ratio has the *measured* sample count as its denominator and a window reports
which share of its length was actually observed.  Window state is one of
``measured`` (the full window comes from real samples), ``bounded`` (only a
lower bound is known because part of the window is a coverage hole) or
``blank`` (nothing was measured).  Every window carries its source sample
range, so a reported interval can be traced back to the original PCM grid.
"""
from dataclasses import dataclass
from math import isqrt

from .clock import InputError, LIMIT, SECOND, Span, coalesce, integer, sample_boundary, shape
from .media import PCM, decode

WINDOW_MS_MIN, WINDOW_MS_MAX = 50, 10_000
HOP_MS_MIN = 10
# A window that is mostly a hole cannot support a delivery decision even when
# the observed fragment itself looks clean.
MIN_MEASURED_PERMILLE_DEFAULT = 900
PEAK_LIMIT_DEFAULT = 32767

# Stable machine-readable blocker tokens; the desk maps them to review text.
REASON_BLANK = 'unmeasured_window'
REASON_HOLE = 'coverage_hole'
REASON_CLIP = 'clip_ratio'
REASON_PEAK = 'peak_code'

STATE_MEASURED = 'measured'
STATE_BOUNDED = 'bounded'
STATE_BLANK = 'blank'


@dataclass
class RollingWindow:
    index: int
    first_sample: int
    end_sample: int
    channel: int
    energy: int = 0
    silent: int = 0
    clipped: int = 0
    peak: int = 0
    measured: int = 0
    covered: list = None

    def __post_init__(self):
        if self.covered is None:
            self.covered = []

    @property
    def length(self):
        return self.end_sample - self.first_sample

    def absorb(self, block, begin, end):
        """Fold the block's samples for ``[begin, end)`` into this window."""
        offset = (begin - block.first_sample) * block.channels
        size = end - begin
        values = block.samples[offset + self.channel:offset + size * block.channels:block.channels]
        self.energy += sum(x * x for x in values)
        self.silent += sum(1 for x in values if x == 0)
        self.clipped += sum(1 for x in values if x == 32767 or x == -32768)
        self.peak = max(self.peak, max((abs(x) for x in values), default=0))
        self.measured += size
        self.covered.append(Span(begin, end))


class RollingCheck:
    """Consume the native-rate PCM stream once and keep per-window facts."""

    def __init__(self, track, channel, window_ms, hop_ms):
        self.rate = track['rate']
        self.channels = track['channels']
        self.channel = channel
        self.window = sample_boundary(window_ms * 1000, self.rate)
        self.hop = sample_boundary(hop_ms * 1000, self.rate)
        if self.window <= 0 or self.hop <= 0:
            raise InputError('rolling window and hop must contain at least one sample')
        self.windows = []

    def window_at(self, index):
        return RollingWindow(index, index * self.hop, index * self.hop + self.window, self.channel)

    def push(self, block):
        if not block.count:
            return
        if block.span.end > LIMIT:
            raise InputError('sound extent exceeds 120 seconds')
        index = max(0, (block.first_sample - self.window) // self.hop + 1)
        while True:
            window = self.window_at(index)
            if window.first_sample >= block.end_sample:
                break
            begin = max(window.first_sample, block.first_sample)
            end = min(window.end_sample, block.end_sample)
            if begin < end:
                while len(self.windows) <= index:
                    self.windows.append(self.window_at(len(self.windows)))
                self.windows[index].absorb(block, begin, end)
            index += 1

    def report(self, extent_sample, clip_permille, peak_limit, min_measured_permille):
        """Project the accumulated facts onto the complete equal windows."""
        complete = max(0, (extent_sample - self.window) // self.hop + 1)
        while len(self.windows) < complete:
            self.windows.append(self.window_at(len(self.windows)))
        windows = [self._verdict(window, clip_permille, peak_limit, min_measured_permille)
                   for window in self.windows[:complete]]
        return windows, [window['index'] for window in windows if window['blocking']]

    def _verdict(self, window, clip_permille, peak_limit, min_measured_permille):
        measured_permille = window.measured * 1000 // window.length
        covered = coalesce(window.covered)
        reasons = []
        if window.measured == 0:
            state = STATE_BLANK
            reasons.append(REASON_BLANK)
        elif measured_permille < min_measured_permille:
            state = STATE_BOUNDED
            reasons.append(REASON_HOLE)
        else:
            state = STATE_MEASURED if measured_permille == 1000 else STATE_BOUNDED
        metrics = None
        if window.measured:
            observed_clip = window.clipped * 1000 // window.measured
            silence_permille = window.silent * 1000 // window.measured
            rms = isqrt(window.energy // window.measured)
            metrics = {
                'rms_code': rms,
                'peak_code': window.peak,
                'silence_permille': silence_permille,
                'clip_permille': observed_clip,
                'measured_samples': window.measured,
                'silent_samples': window.silent,
                'clipped_samples': window.clipped,
                # A partially observed window can only bound the true values.
                'bounds': 'exact' if state == STATE_MEASURED else 'lower_bound',
            }
            # Delivery limits can only fail on fully observed, exact windows;
            # a lower-bound fragment never confirms a signal violation.
            if state == STATE_MEASURED:
                if observed_clip >= clip_permille:
                    reasons.append(REASON_CLIP)
                if window.peak >= peak_limit:
                    reasons.append(REASON_PEAK)
        return {
            'index': window.index,
            'state': state,
            'blocking': bool(reasons),
            'reasons': reasons,
            'measured_permille': measured_permille,
            'start_us': round(window.first_sample * SECOND / self.rate),
            'end_us': round(window.end_sample * SECOND / self.rate),
            'first_source_sample': window.first_sample,
            'end_source_sample': window.end_sample,
            'covered_sample_ranges': [span.as_dict() for span in covered],
            'hole_sample_ranges': _gaps(window, covered),
            'metrics': metrics,
        }


def _gaps(window, covered):
    """Sample-domain holes inside a window, mirroring clock.uncovered."""
    cursor = window.first_sample
    gaps = []
    for span in covered:
        if span.start > cursor:
            gaps.append(Span(cursor, span.start).as_dict())
        cursor = max(cursor, span.end)
    if cursor < window.end_sample:
        gaps.append(Span(cursor, window.end_sample).as_dict())
    return gaps


def request_parameters(data, track):
    shape(data, (), ('channel', 'window_ms', 'hop_ms', 'min_measured_permille',
                     'clip_permille', 'peak_code_limit'))
    channel = integer(data.get('channel', 1), 'channel', 1, track['channels'])
    window_ms = integer(data.get('window_ms', 250), 'window_ms', WINDOW_MS_MIN, WINDOW_MS_MAX)
    hop_ms = integer(data.get('hop_ms', window_ms), 'hop_ms', HOP_MS_MIN, WINDOW_MS_MAX)
    min_measured = integer(data.get('min_measured_permille', MIN_MEASURED_PERMILLE_DEFAULT),
                           'min_measured_permille', 1, 1000)
    if hop_ms > window_ms:
        raise InputError('hop_ms must not exceed window_ms, otherwise windows leave unchecked gaps')
    return channel, window_ms, hop_ms, min_measured


def check(scan, path, data):
    """Run a rolling delivery check for one selected channel of a scan."""
    track = scan['tracks']['sound']
    if track is None:
        raise InputError('the selected scan has no companion sound lane')
    channel, window_ms, hop_ms, min_measured = request_parameters(data, track)
    clip_permille = integer(data.get('clip_permille', scan['thresholds']['clip_permille']),
                            'clip_permille', 1, 1000)
    peak_limit = integer(data.get('peak_code_limit', PEAK_LIMIT_DEFAULT),
                         'peak_code_limit', 1, 32768)
    meter = RollingCheck(track, channel - 1, window_ms, hop_ms)
    for block in decode(path, scan['tracks']):
        if isinstance(block, PCM):
            meter.push(block)
    extent_sample = sample_boundary(scan['extent']['end_us'], track['rate'])
    windows, blockers = meter.report(extent_sample, clip_permille, peak_limit, min_measured)
    return {
        'scan_id': scan['id'],
        'channel': channel,
        'sample_rate': track['rate'],
        'window_ms': window_ms,
        'hop_ms': hop_ms,
        'min_measured_permille': min_measured,
        'clip_permille': clip_permille,
        'peak_code_limit': peak_limit,
        'extent_sample': extent_sample,
        'window_samples': meter.window,
        'hop_samples': meter.hop,
        'state_counts': {
            STATE_MEASURED: sum(1 for w in windows if w['state'] == STATE_MEASURED),
            STATE_BOUNDED: sum(1 for w in windows if w['state'] == STATE_BOUNDED),
            STATE_BLANK: sum(1 for w in windows if w['state'] == STATE_BLANK),
        },
        'blocking': bool(blockers),
        'blocked_window_indexes': blockers,
        'windows': windows,
    }
