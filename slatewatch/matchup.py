"""Cross-check two scans at requested programme instants outside fixed candidates."""
from fractions import Fraction
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from .clock import InputError, LIMIT, Span, integer
from .media import HEIGHT, WIDTH, Picture, decode
from .picture import differences, nominal_duration
from .rules import canonical

MAX_POINTS = 24
DEFAULT_TOLERANCE_MILLI = 4000
VERDICTS = ('same_frame', 'timestamp_offset', 'different_content',
            'left_missing', 'right_missing', 'both_missing')


def parse_points(values):
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_POINTS:
        raise InputError(f'provide 1 to {MAX_POINTS} programme instants')
    points = [integer(value, 'points_us', 0, LIMIT) for value in values]
    if len(set(points)) != len(points):
        raise InputError('programme instants must be unique')
    return points


class Side:
    """Frames and coverage facts of one scan at the requested instants."""

    def __init__(self, scan, points):
        self.points = points
        self.nominal = nominal_duration(scan['tracks']['picture'])
        self.gap_cutoff = scan['thresholds']['picture_gap_ms'] * 1000
        self.coverage = [Span.parse(value) for value in scan['coverage']['picture']]
        self.frames = {}

    def capture(self, frame, next_us):
        start = frame.timestamp_us
        hint = frame.duration_us or self.nominal
        if next_us is None:
            end = start + hint
        else:
            step = next_us - start
            cutoff = max(self.gap_cutoff, hint + self.nominal)
            end = min(start + hint, next_us) if step > cutoff else next_us
        for point in self.points:
            if start <= point < end:
                self.frames[point] = (frame, Span(start, end))

    def hole(self, point):
        previous, following = None, None
        for span in self.coverage:
            if span.end <= point:
                previous = span.end
            elif span.start > point:
                following = span.start
                break
        return {'start_us': previous, 'end_us': following}

    def describe(self, point):
        found = self.frames.get(point)
        if found is None:
            return {'covered': False, 'source_us': None, 'window': None, 'duration_us': None,
                    'nominal_us': self.nominal, 'long_frame': False, 'hole': self.hole(point)}
        frame, window = found
        return {'covered': True, 'source_us': frame.timestamp_us, 'window': window.as_dict(),
                'duration_us': window.duration, 'nominal_us': self.nominal,
                'long_frame': window.duration * 2 >= self.nominal * 3, 'hole': None}


def collect(scan, path, points):
    """Decode once, keeping only the frames that cover a requested instant."""
    side = Side(scan, points)
    prior = None
    for block in decode(path, scan['tracks']):
        if isinstance(block, Picture):
            if prior is not None:
                side.capture(prior, block.timestamp_us)
            prior = block
    if prior is not None:
        side.capture(prior, None)
    return side


def metrics(left, right):
    """Structural difference tolerating a uniform grading lift, plus raw measures."""
    delta, edge = differences(left, right)
    count = len(left)
    shift = Fraction(sum(left) - sum(right), count)
    centered = sum(abs(a - b - shift) for a, b in zip(left, right))
    return {'delta_milli': delta, 'edge_milli': edge,
            'centered_delta_milli': int(centered * 1000) // count,
            'luma_shift_milli': round(shift * 1000)}


def classify(left, right, measures, tolerance):
    flags = []
    for tag, side in (('left', left), ('right', right)):
        if not side['covered']:
            flags.append(f'{tag}_hole')
        elif side['long_frame']:
            flags.append(f'{tag}_long_frame')
    if not left['covered'] or not right['covered']:
        if not left['covered'] and not right['covered']:
            return 'both_missing', flags
        return ('left_missing' if not left['covered'] else 'right_missing'), flags
    if measures['centered_delta_milli'] > tolerance or measures['edge_milli'] > tolerance:
        return 'different_content', flags
    if left['source_us'] == right['source_us']:
        return 'same_frame', flags
    return 'timestamp_offset', flags + ['timestamp_offset']


def run(left_scan, left_path, right_scan, right_path, points, tolerance):
    ordered = sorted(points)
    left = collect(left_scan, left_path, ordered)
    right = collect(right_scan, right_path, ordered)
    frames, rows = {}, []
    for point in points:
        left_side, right_side = left.describe(point), right.describe(point)
        left_frame, right_frame = (side.frames.get(point, (None, None))[0] for side in (left, right))
        measures = metrics(left_frame.pixels, right_frame.pixels) if left_frame and right_frame else None
        verdict, flags = classify(left_side, right_side, measures, tolerance)
        frames[point] = {'left': left_frame, 'right': right_frame}
        rows.append({'point_us': point, 'verdict': verdict, 'flags': flags,
                     'source_delta_us': (right_side['source_us'] - left_side['source_us'])
                     if left_frame and right_frame else None,
                     'left': left_side, 'right': right_side, 'metrics': measures})
    summary = {'total': len(rows), 'flagged': sum(bool(row['flags']) for row in rows)}
    summary.update({verdict: sum(row['verdict'] == verdict for row in rows) for verdict in VERDICTS})
    report = {'left_scan': left_scan['id'], 'right_scan': right_scan['id'],
              'left_master': left_scan['master_id'], 'right_master': right_scan['master_id'],
              'tolerance_milli': tolerance, 'points': rows, 'summary': summary}
    return report, frames


def bundle(report, frames):
    """Package sampled frames and the clock mapping for comparison outside the desk."""
    buf = BytesIO()
    with ZipFile(buf, 'w', compression=ZIP_DEFLATED) as archive:
        rows = []
        for index, row in enumerate(report['points']):
            files = {}
            for tag in ('left', 'right'):
                frame = frames[row['point_us']][tag]
                name = None
                if frame is not None:
                    name = f'point-{index + 1:02d}/{tag}.pgm'
                    archive.writestr(name, f'P5\n{WIDTH} {HEIGHT}\n255\n'.encode() + frame.pixels)
                files[tag] = name
            rows.append({**row, 'files': files})
        archive.writestr('matchup.json', canonical({**report, 'points': rows}))
    return buf.getvalue()
