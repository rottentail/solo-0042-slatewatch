"""Splice-aware version comparison driven by confirmed same-picture points.

The legacy :mod:`slatewatch.compare` flow assumes both scans share one programme
clock.  A re-cut timeline does not: the same real picture appears at different
timestamps in each version and a whole sentence may be missing from the later
version.  Operators therefore register *conformation points*: rows stating that
one genuine frame is seen at ``before_us`` in the earlier scan and at
``after_us`` in the later scan.

Two neighbouring conformation points open a mapping segment.  Whether the
segment is retained or a cut is decided from the real frames each scan decoded
inside it, never from candidate names or pixel-identical title cards.  A
retained segment holds the same number of frames on both sides; a cut segment
holds markedly fewer frames in the later version, i.e. material known to have
been removed.  Nothing is extrapolated beyond the outermost points, so regions
the sparse points cannot reach stay explicitly unlocated instead of being
guessed from matching content.
"""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from .clock import InputError, Span, coalesce, integer, shape, text
from .rules import identifier

MAX_EVIDENCE = 200
MAX_LABEL = 60
# A cut must remove at least two thirds of the frames the earlier scan decoded
# inside the bracketed segment.  Retained material keeps a 1:1 frame count even
# when container rounding shifts a boundary by a single frame.
CUT_FRAME_RATIO = 3


@dataclass(frozen=True)
class Point:
    before_us: int
    after_us: int
    label: str
    evidence: str

    def as_dict(self):
        return {'id': identifier('point', [self.before_us, self.after_us, self.label, self.evidence]),
                'before_us': self.before_us, 'after_us': self.after_us,
                'label': self.label, 'evidence': self.evidence}


@dataclass(frozen=True)
class Segment:
    before_start: int
    before_end: int
    after_start: int
    after_end: int
    before_frames: int
    after_frames: int

    @property
    def removed(self):
        return self.before_frames >= self.after_frames * CUT_FRAME_RATIO

    def forward(self, old_us):
        if self.removed:
            # Deleted material has no surviving frame; pin every inside instant
            # to the cut's output boundary instead of inventing frame times.
            return self.after_start
        ratio = (old_us - self.before_start) / (self.before_end - self.before_start)
        return self.after_start + round(ratio * (self.after_end - self.after_start))

    def inverse(self, new_us):
        if self.removed:
            # The only surviving after-side instant is the cut boundary frame.
            return self.before_start
        ratio = (new_us - self.after_start) / (self.after_end - self.after_start)
        return self.before_start + round(ratio * (self.before_end - self.before_start))

    def as_dict(self):
        return {'before': {'start_us': self.before_start, 'end_us': self.before_end},
                'after': {'start_us': self.after_start, 'end_us': self.after_end},
                'before_frames': self.before_frames, 'after_frames': self.after_frames,
                'status': 'removed' if self.removed else 'confirmed'}


def _frame_times(scan):
    """Real decoded frame presentation times recorded by the scan."""
    recorded = scan.get('frame_times')
    if recorded is not None:
        return sorted(recorded)
    # Older scans predate frame-time recording; fall back to coverage edges.
    times = {span['start_us'] for span in scan['coverage']['picture']}
    return sorted(times)


def _count_frames(frame_times, start_us, end_us):
    """Frames whose presentation time lies in the half-open [start, end)."""
    return bisect_left(frame_times, end_us) - bisect_left(frame_times, start_us)


def parse_points(payload, before_scan, after_scan):
    shape(payload, ('before', 'after', 'conformation_points'), ('minimum_permille',))
    rows = payload['conformation_points']
    if not isinstance(rows, list) or not 2 <= len(rows) <= 200:
        raise InputError('conformation_points must contain 2 to 200 confirmed rows')
    left_frames, right_frames = _frame_times(before_scan), _frame_times(after_scan)
    points = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise InputError(f'conformation point {index} must be an object')
        shape(row, ('before_us', 'after_us'), ('label', 'evidence'))
        before_us = integer(row['before_us'], 'before_us', 0, _extent(before_scan))
        after_us = integer(row['after_us'], 'after_us', 0, _extent(after_scan))
        label = text(row.get('label', f'确认点 {index + 1}'), 'label', MAX_LABEL)
        evidence = text(row.get('evidence', ''), 'evidence', MAX_EVIDENCE) if row.get('evidence') else ''
        if before_us not in left_frames:
            raise InputError(f'conformation point {index} before_us is not a real frame time')
        if after_us not in right_frames:
            raise InputError(f'conformation point {index} after_us is not a real frame time')
        points.append(Point(before_us, after_us, label, evidence))
    points.sort(key=lambda point: (point.before_us, point.after_us))
    for previous, current in zip(points, points[1:]):
        if previous.before_us == current.before_us:
            raise InputError('conformation points must reference distinct before frames')
        if previous.after_us >= current.after_us:
            raise InputError('after-frame times must stay in the same order as before frames')
    return points


def _extent(scan):
    return scan['extent']['end_us']


def build_segments(points, before_frames, after_frames):
    segments = []
    for left, right in zip(points, points[1:]):
        segments.append(Segment(
            left.before_us, right.before_us, left.after_us, right.after_us,
            _count_frames(before_frames, left.before_us, right.before_us),
            _count_frames(after_frames, left.after_us, right.after_us),
        ))
    return segments


def _containing(segments, starts, old_us):
    index = bisect_right(starts, old_us) - 1
    if 0 <= index < len(segments) and old_us <= segments[index].before_end:
        return segments[index]
    return None


def _inverse_containing(segments, starts, new_us):
    index = bisect_right(starts, new_us) - 1
    if 0 <= index < len(segments) and new_us <= segments[index].after_end:
        return segments[index]
    return None


def _piece(segment, old_start, old_end):
    return {'before': {'start_us': old_start, 'end_us': old_end},
            'after': {'start_us': segment.forward(old_start), 'end_us': segment.forward(old_end)},
            'status': 'removed' if segment.removed else 'confirmed'}


def map_before_hit(segments, starts, hit):
    """Split an earlier-version finding along the confirmed segments."""
    span = Span.parse(hit)
    pieces, cursor = [], span.start
    while cursor < span.end:
        segment = _containing(segments, starts, cursor)
        if segment is None:
            pieces.append({'before': {'start_us': cursor, 'end_us': span.end}, 'after': None,
                           'status': 'unlocated'})
            break
        cut = min(span.end, segment.before_end)
        pieces.append(_piece(segment, cursor, cut))
        cursor = cut
    mapped = [Span(piece['after']['start_us'], piece['after']['end_us'])
              for piece in pieces
              if piece['status'] == 'confirmed' and piece['after']['end_us'] > piece['after']['start_us']]
    if any(piece['status'] == 'unlocated' for piece in pieces):
        status = 'unlocated'
    elif any(piece['status'] == 'removed' for piece in pieces):
        status = 'removed' if not mapped else 'mixed'
    else:
        status = 'confirmed'
    return {'side': 'before', 'status': status, 'kind': hit['kind'], 'channel': hit['channel'],
            'before': span.as_dict(),
            'after': [span.as_dict() for span in coalesce(mapped)] or None,
            'pieces': pieces, 'hit_id': hit['id']}


def map_after_hit(segments, after_starts, hit):
    """Place a later-version finding back onto the earlier timeline."""
    span = Span.parse(hit)
    pieces, cursor = [], span.start
    while cursor < span.end:
        segment = _inverse_containing(segments, after_starts, cursor)
        if segment is None:
            pieces.append({'before': None, 'after': {'start_us': cursor, 'end_us': span.end},
                           'status': 'unlocated'})
            break
        cut = min(span.end, segment.after_end)
        old_span = Span(segment.inverse(cursor), segment.inverse(cut))
        pieces.append({'before': old_span.as_dict(),
                       'after': {'start_us': cursor, 'end_us': cut},
                       'status': 'removed' if segment.removed else 'confirmed'})
        cursor = cut
    mapped = [Span(piece['before']['start_us'], piece['before']['end_us'])
              for piece in pieces
              if piece['status'] == 'confirmed' and piece['before']['end_us'] > piece['before']['start_us']]
    if any(piece['status'] == 'unlocated' for piece in pieces):
        status = 'unlocated'
    elif any(piece['status'] == 'removed' for piece in pieces):
        status = 'mixed' if mapped else 'removed'
    else:
        status = 'confirmed'
    return {'side': 'after', 'status': status, 'kind': hit['kind'], 'channel': hit['channel'],
            'before': [span.as_dict() for span in coalesce(mapped)] or None,
            'after': span.as_dict(), 'pieces': pieces, 'hit_id': hit['id']}


def compare_splice(before_scan, after_scan, payload):
    points = parse_points(payload, before_scan, after_scan)
    before_frames, after_frames = _frame_times(before_scan), _frame_times(after_scan)
    segments = build_segments(points, before_frames, after_frames)
    before_starts = [segment.before_start for segment in segments]
    after_starts = [segment.after_start for segment in segments]
    findings = [map_before_hit(segments, before_starts, hit) for hit in before_scan['hits']]
    findings.extend(map_after_hit(segments, after_starts, hit) for hit in after_scan['hits'])
    def _start(item):
        anchor = item['before'] or item['after']
        return anchor[0]['start_us'] if isinstance(anchor, list) else anchor['start_us']

    findings.sort(key=lambda item: (item['side'], item['kind'], item['channel'] or 0, _start(item)))
    return {'mode': 'splice', 'before_scan': before_scan['id'], 'after_scan': after_scan['id'],
            'conformation_points': [point.as_dict() for point in points],
            'segments': [segment.as_dict() for segment in segments],
            'findings': findings}
