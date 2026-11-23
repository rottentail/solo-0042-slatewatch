"""Content-only inspection for cross-field temporal misalignment (combing).

The inspection never reads a container or codec progressive/interlaced flag:
the flags on archived masters are known to be unreliable.  A frame is reported
only when two independent content tests agree:

1. spatial comb signature — a woven row sits well outside the mean of its two
   adjacent rows while those adjacent rows (the opposite field, captured at the
   other field instant) stay close to each other;
2. temporal field pairing — the FFmpeg-idet style pairing MSE confirms that one
   field of the current frame matches the opposite-parity field of the previous
   frame better than same-parity fields match, which is only possible when the
   two woven fields come from different time instants.

Stationary fine detail in a progressive frame fails test 1; a properly
deinterlaced (line-doubled or merged) frame fails test 1 as well because the
weave is gone.  Both controls therefore stay clear while genuinely interlaced
motion is flagged, and on a mixed-standard programme only the woven ranges
produce hits.
"""
from dataclasses import dataclass
from fractions import Fraction

from .clock import InputError, LIMIT, SECOND, Span
from .hits import candidates
from .rules import FieldThresholds, identifier

FIELD_INSPECTION_VERSION = 1


@dataclass
class FieldObservation:
    span: Span
    comb_permille: int
    motion_mse: int
    pair_gap: int
    order: str
    comb_points: list


def _field_rows(frame, parity):
    return [frame.pixels[y * frame.line_size:y * frame.line_size + frame.width]
            for y in range(parity, frame.height, 2)]


def _pair_mse(left, right):
    total = 0
    cells = 0
    for row_a, row_b in zip(left, right):
        for a, b in zip(row_a, row_b):
            delta = a - b
            total += delta * delta
            cells += 1
    return total // cells if cells else 0


def comb_points(frame, thresholds):
    """Locate pixels whose row alternates against the opposite-field rows."""
    points = []
    raw, ls, width, height = frame.pixels, frame.line_size, frame.width, frame.height
    luma_gate, field_gate = thresholds.comb_luma, thresholds.comb_field_gate
    for y in range(1, height - 1):
        center_row = y * ls
        up_row = (y - 1) * ls
        down_row = (y + 1) * ls
        for x in range(width):
            center = raw[center_row + x]
            up = raw[up_row + x]
            down = raw[down_row + x]
            if abs(center - (up + down) // 2) >= luma_gate and abs(up - down) <= field_gate:
                points.append([y, x])
    return points


def pairing_metrics(frame, previous):
    """Return idet-style MSEs plus the interlace-vs-progressive gap."""
    top = _field_rows(frame, 0)
    bottom = _field_rows(frame, 1)
    prev_top = _field_rows(previous, 0)
    prev_bottom = _field_rows(previous, 1)
    # Naming follows ffmpeg's idet filter: a top-field-first stream makes the
    # current top field temporally adjacent to the previous bottom field.
    tff = _pair_mse(prev_bottom, top)
    bff = _pair_mse(prev_top, bottom)
    progressive = (_pair_mse(prev_top, top) + _pair_mse(prev_bottom, bottom)) // 2
    return tff, bff, progressive, progressive - min(tff, bff)


def inspect_frame(frame, previous, thresholds):
    points = comb_points(frame, thresholds)
    comb_permille = len(points) * 1000 // (frame.width * frame.height)
    if previous is None:
        return FieldObservation(Span(frame.timestamp_us, frame.timestamp_us + frame.duration_us),
                                comb_permille, 0, 0, 'undetermined', points)
    tff, bff, motion_mse, gap = pairing_metrics(frame, previous)
    order = 'undetermined'
    if gap > thresholds.pair_gap:
        order = 'top_field_first' if tff < bff else 'bottom_field_first'
    return FieldObservation(Span(frame.timestamp_us, frame.timestamp_us + frame.duration_us),
                            comb_permille, motion_mse, gap, order, points)


def is_combing(observation, thresholds):
    return (observation.comb_permille >= thresholds.comb_permille
            and observation.pair_gap >= thresholds.pair_gap
            and observation.motion_mse >= thresholds.motion_mse
            and observation.order != 'undetermined')


def bob_deinterlace(frame):
    """Reference line-doubling deinterlace: each field fills its missing rows."""
    raw, ls, width, height = frame.pixels, frame.line_size, frame.width, frame.height
    output = bytearray(height * width)
    for y in range(height):
        source_y = y - 1 if y % 2 == 1 else y
        start = y * width
        output[start:start + width] = raw[source_y * ls:source_y * ls + width]
    return bytes(output)


def pgm(width, height, pixels):
    header = f'P5\n{width} {height}\n255\n'.encode()
    return header + pixels


def field_pgm(frame, parity):
    rows = _field_rows(frame, parity)
    return pgm(frame.width, len(rows), b''.join(bytes(row) for row in rows))


def crop(frame, points, margin=12):
    """Crop a full-raster frame around its comb evidence, half-open bounds."""
    if not points:
        return None
    ys = [y for y, _ in points]
    xs = [x for _, x in points]
    top = max(0, min(ys) - margin)
    bottom = min(frame.height, max(ys) + margin + 1)
    left = max(0, min(xs) - margin)
    right = min(frame.width, max(xs) + margin + 1)
    raw, ls = frame.pixels, frame.line_size
    pixels = b''.join(raw[y * ls + left:y * ls + right] for y in range(top, bottom))
    return {'x': left, 'y': top, 'width': right - left, 'height': bottom - top,
            'pixels': pixels, 'points': [(y - top, x - left) for y, x in points
                                         if top <= y < bottom and left <= x < right]}


class FieldInspector:
    def __init__(self, track, thresholds):
        self.thresholds = thresholds
        rate = Fraction(track['average_rate'] or '25')
        # Keep the fractional rate exact: rounding a 29.97 fps step to whole
        # microseconds would insert phantom gaps that break run coalescing.
        self.nominal_step = (SECOND / rate) if rate > 0 else Fraction(40000)
        self.observations = []
        self.previous = None
        self.frames = 0
        self.order_votes = {'top_field_first': 0, 'bottom_field_first': 0}

    def push(self, frame):
        if frame.height % 2:
            raise InputError('field inspection requires an even picture height')
        if frame.timestamp_us + frame.duration_us > LIMIT:
            raise InputError('picture extent exceeds 120 seconds')
        observation = inspect_frame(frame, self.previous, self.thresholds)
        if observation.order != 'undetermined':
            self.order_votes[observation.order] += 1
        self.observations.append((frame, observation))
        self.previous = frame
        self.frames += 1

    def finish(self):
        if not self.observations:
            raise InputError('selected video has no pictures in the programme clock')
        bins = []
        observed = []
        for index, (frame, observation) in enumerate(self.observations):
            # The display extent ends at the next presentation timestamp; a
            # stored constant duration is only quantised frame-rate metadata and
            # must not manufacture gaps on fractional-rate programmes.
            if index + 1 < len(self.observations):
                end = self.observations[index + 1][0].timestamp_us
            elif frame.duration_us:
                end = frame.timestamp_us + frame.duration_us
            else:
                end = round(frame.timestamp_us + self.nominal_step)
            span = Span(frame.timestamp_us, max(end, frame.timestamp_us + 1))
            combing = is_combing(observation, self.thresholds)
            bins.append((span, observation.comb_permille if combing else None))
            observed.append({
                'start_us': span.start, 'end_us': span.end,
                'comb_permille': observation.comb_permille,
                'pair_gap': observation.pair_gap,
                'motion_mse': observation.motion_mse,
                'field_order': observation.order,
                'combing': combing,
            })
        findings = list(candidates(bins, 'combing', self.thresholds.combing_min_ms * 1000))
        frames = [frame for frame, _ in self.observations]
        points = {str(frame.timestamp_us): observation.comb_points
                  for frame, observation in self.observations if observation.comb_points}
        dominant = max(self.order_votes, key=self.order_votes.get)
        if not self.order_votes[dominant]:
            dominant = 'undetermined'
        stats = {'frames': self.frames, 'field_order_votes': self.order_votes,
                 'dominant_field_order': dominant,
                 'combing_frames': sum(1 for _, value in bins if value is not None),
                 'comb_points': points}
        return findings, observed, stats, frames


def inspection_identity(asset, policy):
    return identifier('field-inspection', [asset['id'], policy['id'], FIELD_INSPECTION_VERSION])
