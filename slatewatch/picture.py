"""Picture measurement without rebuilding the presentation clock from fps."""
from fractions import Fraction

from .clock import InputError, LIMIT, SECOND, Span, coalesce
from .hits import candidates
from .media import WIDTH


def black_ratio(pixels, threshold):
    return sum(pixels.count(i) for i in range(threshold + 1)) * 1000 // len(pixels)


def differences(previous, current, width=WIDTH):
    delta = sum(abs(old - new) for old, new in zip(previous, current))
    edge_delta = 0
    for row in range(0, len(current), width):
        for x in range(row + 1, row + width):
            edge_delta += abs((current[x] - current[x - 1]) - (previous[x] - previous[x - 1]))
    return delta * 1000 // len(current), edge_delta * 1000 // (len(current) - len(current) // width)


def nominal_duration(track):
    rate = Fraction(track['average_rate'] or '25')
    return max(1, round(SECOND / rate)) if rate > 0 else 40000


class PictureMeter:
    def __init__(self, track, thresholds):
        self.nominal = nominal_duration(track)
        self.thresholds = thresholds
        self.pending = None
        self.previous = None
        self.coverage = []
        self.black_bins = []
        self.freeze_bins = []
        self.frames = 0
        self.max_step = 0

    def push(self, frame):
        if self.pending is not None:
            self._measure(frame.timestamp_us)
        self.pending = frame
        self.frames += 1

    def _measure(self, next_us=None):
        frame = self.pending
        start = frame.timestamp_us
        duration_hint = frame.duration_us or self.nominal
        if next_us is None:
            end = start + duration_hint
        else:
            step = next_us - start
            self.max_step = max(step, self.max_step)
            cutoff = max(self.thresholds.picture_gap_ms * 1000, duration_hint + self.nominal)
            end = min(start + duration_hint, next_us) if step > cutoff else next_us
        if end > LIMIT or end <= start:
            raise InputError('picture extent exceeds 120 seconds or is empty')
        span = Span(start, end)
        black = black_ratio(frame.pixels, self.thresholds.black_luma_max)
        self.black_bins.append((span, black if black >= self.thresholds.black_permille else None))
        still = None
        if self.previous is not None and self.coverage[-1].end == start:
            delta, edge = differences(self.previous.pixels, frame.pixels, frame.width)
            if delta <= self.thresholds.freeze_delta_milli and edge <= self.thresholds.freeze_edge_milli:
                still = delta
        self.freeze_bins.append((span, still))
        self.coverage.append(span)
        self.previous = frame

    def finish(self):
        if not self.pending:
            raise InputError('selected video has no pictures in the programme clock')
        self._measure()
        hits = list(candidates(self.black_bins, 'black', self.thresholds.black_min_ms * 1000))
        hits.extend(candidates(self.freeze_bins, 'freeze', self.thresholds.freeze_min_ms * 1000))
        return coalesce(self.coverage), hits, {'frames': self.frames, 'max_step_us': self.max_step}
