"""Find faint periodic luma bands on the reduced grayscale raster.

A frame qualifies when one spatial period explains most of the profile's
variance and repeats for at least a few cycles, in rows (horizontal bands) or in
columns (vertical bands).  Faintness is not the gate: a one-luma band counts as
long as the periodicity is clean, while a smooth gradient or a busy texture does
not qualify however strong it is.  A true episode keeps that period, direction
and a steady phase step from frame to frame; real repeating texture holds its
phase while scrolling bands advance it.  Any gap, lost period or abrupt phase
jump ends the run, so a hard cut between two striped shots is never stitched
into a single technical finding.
"""
from dataclasses import dataclass
from math import atan2, cos, hypot, pi, sin, tau

from .hits import Run
from .media import HEIGHT, WIDTH

MIN_PERIOD = 4
MIN_CYCLES = 3.0
PHASE_TOLERANCE = 0.15  # cycles between measured and predicted phase step
STATIC_TOLERANCE = 0.1  # a resting texture drifts less than this per frame
_TABLES = {}


@dataclass(frozen=True)
class Band:
    direction: str
    period: int
    strength: int   # permille of profile variance explained by the period
    amplitude: float
    phase: float


def row_profile(pixels):
    return [sum(pixels[y * WIDTH:(y + 1) * WIDTH]) / WIDTH for y in range(HEIGHT)]


def column_profile(pixels):
    return [sum(pixels[x::WIDTH]) / HEIGHT for x in range(WIDTH)]


def _table(period, size):
    """Cosine and sine at each sample of one spatial period, cached across frames."""
    key = (period, size)
    table = _TABLES.get(key)
    if table is None:
        step = 2 * pi / period
        table = ([cos(step * i) for i in range(size)], [sin(step * i) for i in range(size)])
        _TABLES[key] = table
    return table


def harmonic(profile, period):
    """Least-squares fit of one spatial frequency; returns amplitude, purity, phase."""
    size = len(profile)
    mean = sum(profile) / size
    cosines, sines = _table(period, size)
    real = sum((value - mean) * cosine for value, cosine in zip(profile, cosines)) * 2 / size
    imaginary = sum((value - mean) * sine for value, sine in zip(profile, sines)) * 2 / size
    variance = sum((value - mean) ** 2 for value in profile) / size
    if variance <= 0:
        return 0.0, 0, 0.0
    amplitude = hypot(real, imaginary)
    purity = min(1000, round(1000 * (amplitude * amplitude / 2) / variance))
    phase = atan2(imaginary, real) / (2 * pi) % 1.0
    return amplitude, purity, phase


def _strongest(profile, direction, luma_span, purity_min):
    """Cleanest qualifying period; on a tie the shortest period wins."""
    floor = luma_span / 2000  # half of the minimum peak-to-peak band, in luma
    mean = sum(profile) / len(profile)
    if sum((value - mean) ** 2 for value in profile) / len(profile) < floor * floor:
        return None
    best = None
    for period in range(MIN_PERIOD, len(profile) // 2 + 1):
        if len(profile) / period < MIN_CYCLES:
            break
        amplitude, purity, phase = harmonic(profile, period)
        if amplitude >= floor and purity >= purity_min and (best is None or purity > best.strength):
            best = Band(direction, period, purity, amplitude, phase)
    return best


def measure(pixels, luma_span, purity_min):
    """Return the strongest periodic band in the frame, or None when decomposed."""
    bands = [_strongest(row_profile(pixels), 'horizontal', luma_span, purity_min),
             _strongest(column_profile(pixels), 'vertical', luma_span, purity_min)]
    bands = [band for band in bands if band is not None]
    return max(bands, key=lambda band: band.strength) if bands else None


def _step(state, band):
    return (band.phase - state['phase']) % 1.0


def _resting(delta):
    """Distance from a phase step to a resting texture, in cycles."""
    return min(delta % 1.0, 1.0 - delta % 1.0)


def _circular(state, delta):
    """Angle of the running phase-step mean, without the 0/1 wrap artefact."""
    state['turn'] += sin(tau * delta)
    state['straight'] += cos(tau * delta)
    state['steps'] += 1
    return atan2(state['turn'], state['straight']) / tau % 1.0


def _continues(state, band):
    if band.direction != state['direction'] or band.period != state['period']:
        return False
    if state['steps'] == 0:
        return True
    delta = _step(state, band)
    mean = state['mean']
    if abs((delta - mean + 0.5) % 1.0 - 0.5) > PHASE_TOLERANCE:
        return False
    # Real texture rests while scrolling bands move; a cut between the two
    # states must not continue the same run even when both share a period.
    return (_resting(delta) <= STATIC_TOLERANCE) == (_resting(mean) <= STATIC_TOLERANCE)


def fold(bins, minimum_us):
    """Fold qualifying bands into findings, breaking on gaps, texture or phase jumps."""
    run, state = None, None

    def emit():
        return {'kind': 'stripe', 'channel': None, **run.span.as_dict(),
                'bins': run.bins, 'minimum': run.minimum, 'maximum': run.maximum,
                'direction': state['direction'], 'period': state['period'],
                'purity_permille': state['purity'], 'peak_us': state['peak_us']}

    for span, band in bins:
        if run is not None and (band is None or span.start != run.span.end or not _continues(state, band)):
            if run.span.duration >= minimum_us:
                yield emit()
            run, state = None, None
        if band is None:
            continue
        level = band.strength
        if run is None:
            run = Run(span, 1, level, level)
            state = {'direction': band.direction, 'period': band.period, 'phase': band.phase,
                     'turn': 0.0, 'straight': 0.0, 'steps': 0, 'mean': 0.0,
                     'purity': band.strength, 'peak': band.amplitude, 'peak_us': span.start}
        else:
            state['mean'] = _circular(state, _step(state, band))
            state['phase'] = band.phase
            run.extend(span, level)
            state['purity'] = level
            if band.amplitude > state['peak']:
                state['peak'], state['peak_us'] = band.amplitude, span.start
    if run is not None and run.span.duration >= minimum_us:
        yield emit()
