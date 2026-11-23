"""Spot fake dual-channel intervals where one sound channel copies the other."""
from array import array
from math import isqrt, sqrt

from .clock import SECOND, Span, sample_boundary
from .hits import candidates

ANALYSIS_RATE = 8000


class Window:
    def __init__(self, start_sample, end_sample):
        self.start_sample = start_sample
        self.end_sample = end_sample
        self.covered = 0
        self.pcm = array('h')


def correlation(left, right, lag):
    """Mean-centred normalized correlation of the overlap at one lag."""
    start = max(0, -lag)
    stop = min(len(left), len(right) - lag)
    count = stop - start
    if count < 16:
        return 0.0
    sa = sb = saa = sbb = sab = 0
    for index in range(start, stop):
        a = left[index]
        b = right[index + lag]
        sa += a
        sb += b
        saa += a * a
        sbb += b * b
        sab += a * b
    covariance = sab - sa * sb / count
    var_left = saa - sa * sa / count
    var_right = sbb - sb * sb / count
    if var_left <= 0 or var_right <= 0:
        return 0.0
    return covariance / sqrt(var_left * var_right)


def sign_lags(left, right, bound, picks=3):
    """Best candidate lags from one-bit correlation over the whole search range."""
    count = len(left)
    mean_left = sum(left) // count
    mean_right = sum(right) // count
    bits_left = bits_right = 0
    for index in range(count):
        bits_left |= (left[index] > mean_left) << index
        bits_right |= (right[index] > mean_right) << index
    scores = []
    for lag in range(-bound, bound + 1):
        overlap = count - abs(lag)
        if overlap < 16:
            continue
        if lag >= 0:
            mismatched = ((bits_left ^ (bits_right >> lag)) & ((1 << overlap) - 1)).bit_count()
        else:
            mismatched = ((bits_right ^ (bits_left >> -lag)) & ((1 << overlap) - 1)).bit_count()
        scores.append((abs(overlap - 2 * mismatched) * 1000 // overlap, lag))
    scores.sort(key=lambda score: (-score[0], score[1]))
    picked = []
    for _score, lag in scores:
        if all(abs(lag - other) > 2 for other in picked):
            picked.append(lag)
        if len(picked) == picks:
            break
    return picked


class DuplicateMeter:
    """Measure fully covered stereo windows and fold copy runs into findings."""

    def __init__(self, track, thresholds):
        self.rate = track['rate']
        self.thresholds = thresholds
        self.step = thresholds.audio_window_ms * 1000
        self.open = {}
        self.bins = {}
        self.lag = 0

    def push(self, pcm):
        if not pcm.count:
            return
        for key, window in list(self.open.items()):
            if window.end_sample <= pcm.first_sample:
                self._close(key, window)
        current = pcm.first_sample
        while current < pcm.end_sample:
            key = current * SECOND // (self.step * self.rate)
            begin = sample_boundary(key * self.step, self.rate)
            boundary = sample_boundary((key + 1) * self.step, self.rate)
            window = self.open.get(key)
            if window is None:
                window = Window(begin, boundary)
                self.open[key] = window
            end = min(pcm.end_sample, boundary)
            offset = (current - pcm.first_sample) * 2
            window.pcm.extend(pcm.samples[offset:offset + (end - current) * 2])
            window.covered += end - current
            current = end

    def finish(self):
        for key in sorted(self.open):
            self._close(key, self.open[key])
        bins = ((Span(key * self.step, (key + 1) * self.step), value)
                for key, value in sorted(self.bins.items()))
        return candidates(bins, 'duplicate', self.thresholds.duplicate_min_ms * 1000)

    def _close(self, key, window):
        del self.open[key]
        value = None
        if window.covered == window.end_sample - window.start_sample:
            value = self._measure(window.pcm)
        self.bins[key] = value

    def _measure(self, pcm):
        stride = 2 * max(1, self.rate // ANALYSIS_RATE)
        left, right = pcm[0::stride], pcm[1::stride]
        count = len(left)
        if count < 16:
            return None
        if min(isqrt(sum(x * x for x in left) // count),
               isqrt(sum(x * x for x in right) // count)) <= self.thresholds.silence_rms:
            return None
        bound = self.thresholds.duplicate_lag_ms * self.rate // 1000 // (stride // 2)
        best, lag = 0.0, 0
        for candidate in {0, self.lag, *sign_lags(left, right, bound)}:
            value = abs(correlation(left, right, candidate))
            if value > best:
                best, lag = value, candidate
        permille = round(best * 1000)
        if permille >= self.thresholds.duplicate_corr_permille:
            self.lag = lag
            return permille
        self.lag = 0
        return None
