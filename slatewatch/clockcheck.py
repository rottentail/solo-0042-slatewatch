"""Clock self-check between two capture-machine masters using operator anchors.

Equal container duration alone cannot prove that two masters share a programme
clock.  The duty officer supplies a few scene anchors that can be recognised in
both masters.  From those paired timestamps we distinguish:

* ``constant_offset`` – one straight mapping with a unit rate: only the origin
  of the two clocks differs;
* ``rate_difference`` – one straight mapping whose slope is not 1: the two
  capture clocks run at different speeds;
* ``cut_jump`` – two straight segments, the second one shifted by a jump: an
  early stretch lines up and a later stretch lines up after a removed block;
* ``inconclusive`` – the anchors do not contain enough evidence for any of the
  models above.

All arithmetic uses exact rationals; only the reported residuals are rounded to
integer microseconds.  Ranges that the anchors cannot substantiate are returned
explicitly instead of being silently treated as proven.
"""
from dataclasses import dataclass
from fractions import Fraction

from .clock import InputError, Span, coalesce, integer, shape, text, uncovered
from .rules import identifier

MAX_ANCHORS = 12
DEFAULT_TOLERANCE_US = 5_000
RATE_EPSILON = Fraction(1, 1000)
CUT_EPSILON = Fraction(1, 1000)
MIN_CUT_ANCHORS = 4
SECOND = 1_000_000

VERDICTS = ('constant_offset', 'rate_difference', 'cut_jump', 'inconclusive')
VERDICT_LABELS = {
    'constant_offset': '常量起点差',
    'rate_difference': '时钟比例差',
    'cut_jump': '中段跳变（剪去一块）',
    'inconclusive': '证据不足',
}


@dataclass(frozen=True)
class Anchor:
    label: str
    before_us: int
    after_us: int


def _permille(value):
    return int(value * 1000)


def _round_us(value):
    return int(value + Fraction(1, 2)) if value >= 0 else -int(-value + Fraction(1, 2))


def linear_fit(points):
    """Least-squares line after = slope * before + intercept over rationals."""
    count = len(points)
    mean_x = sum(x for x, _ in points) / count
    mean_y = sum(y for _, y in points) / count
    variance = sum((x - mean_x) ** 2 for x, _ in points)
    if variance == 0:
        raise InputError('anchor timestamps must be distinct on each master')
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / variance
    intercept = mean_y - slope * mean_x
    residuals = [y - (slope * x + intercept) for x, y in points]
    return slope, intercept, residuals


def _max_abs_residual(residuals):
    return max(abs(value) for value in residuals)


def _model_dict(slope, intercept, residuals, native_anchors, programme_anchors):
    """Fit values stay on native container clocks; anchor rows use programme clocks."""
    return {
        'rate_permille': _permille(slope),
        'origin_offset_us': _round_us(intercept),
        'max_abs_residual_us': _round_us(_max_abs_residual(residuals)),
        'residuals_us': [_round_us(value) for value in residuals],
        'anchors': [
            {'label': anchor.label, 'before_us': anchor.before_us, 'after_us': anchor.after_us,
             'residual_us': _round_us(residual)}
            for anchor, residual in zip(programme_anchors, residuals)
        ],
    }


def _covered(scan, when):
    return any(span['start_us'] <= when < span['end_us'] for span in scan['coverage']['picture'])


def _origin_us(scan):
    """Container shared start on the native clock; zero when the record omits it."""
    origin = scan['tracks'].get('origin_us', 0)
    return origin if isinstance(origin, int) else 0


def parse_request(data, before_scan, after_scan):
    shape(data, ('before', 'after', 'anchors'), ('tolerance_us',))
    anchors_data = data['anchors']
    if not isinstance(anchors_data, list) or not 1 <= len(anchors_data) <= MAX_ANCHORS:
        raise InputError(f'provide 1 to {MAX_ANCHORS} scene anchors')
    anchors = []
    for index, item in enumerate(anchors_data):
        if not isinstance(item, dict):
            raise InputError('each anchor must be an object')
        shape(item, ('label', 'before_us', 'after_us'))
        label = text(item['label'], f'anchors[{index}].label', 60)
        before_us = integer(item['before_us'], f'anchors[{index}].before_us', 0, before_scan['extent']['end_us'])
        after_us = integer(item['after_us'], f'anchors[{index}].after_us', 0, after_scan['extent']['end_us'])
        if not _covered(before_scan, before_us):
            raise InputError(f'anchor {label!r} falls outside the before master picture coverage')
        if not _covered(after_scan, after_us):
            raise InputError(f'anchor {label!r} falls outside the after master picture coverage')
        anchors.append(Anchor(label, before_us, after_us))
    anchors.sort(key=lambda anchor: anchor.before_us)
    if any(left.before_us == right.before_us for left, right in zip(anchors, anchors[1:])):
        raise InputError('anchor timestamps on the before master must be distinct')
    if any(left.after_us == right.after_us for left, right in zip(anchors, anchors[1:])):
        raise InputError('anchor timestamps on the after master must be distinct')
    tolerance = integer(data.get('tolerance_us', DEFAULT_TOLERANCE_US), 'tolerance_us', 100, 60 * SECOND)
    return anchors, tolerance


def _single_segment(native_anchors, programme_anchors, tolerance):
    points = [(Fraction(anchor.before_us), Fraction(anchor.after_us)) for anchor in native_anchors]
    slope, intercept, residuals = linear_fit(points)
    model = _model_dict(slope, intercept, residuals, native_anchors, programme_anchors)
    if _max_abs_residual(residuals) > tolerance:
        return None
    if abs(slope - 1) <= RATE_EPSILON:
        return 'constant_offset', {'tolerance_us': tolerance, 'segments': [model]}
    return 'rate_difference', {'tolerance_us': tolerance, 'segments': [model]}


def _piecewise(native_anchors, programme_anchors, tolerance):
    """Two unit-rate segments separated by a single jump, if the evidence shows one."""
    anchors = native_anchors
    points = [(Fraction(anchor.before_us), Fraction(anchor.after_us)) for anchor in anchors]
    candidates = []
    for split in range(2, len(anchors) - 1):
        left = points[:split]
        right = points[split:]
        slope_left, intercept_left, residual_left = linear_fit(left)
        slope_right, intercept_right, residual_right = linear_fit(right)
        if abs(slope_left - 1) > CUT_EPSILON or abs(slope_right - 1) > CUT_EPSILON:
            continue
        max_residual = max(_max_abs_residual(residual_left), _max_abs_residual(residual_right))
        if max_residual > tolerance:
            continue
        boundary_before = Fraction(anchors[split - 1].before_us + anchors[split].before_us, 2)
        left_end = slope_left * boundary_before + intercept_left
        right_start = slope_right * boundary_before + intercept_right
        jump = right_start - left_end
        if abs(jump) <= tolerance:
            continue
        candidates.append((max_residual, split, slope_left, intercept_left, residual_left,
                           slope_right, intercept_right, residual_right, boundary_before, jump))
    if not candidates:
        return None
    _, split, slope_left, intercept_left, residual_left, slope_right, intercept_right, residual_right, \
        boundary_before, jump = min(candidates, key=lambda row: (row[0], row[1]))
    boundary_programme = boundary_before - Fraction(
        native_anchors[0].before_us - programme_anchors[0].before_us)
    first_model = _model_dict(slope_left, intercept_left, residual_left,
                              native_anchors[:split], programme_anchors[:split])
    second_model = _model_dict(slope_right, intercept_right, residual_right,
                               native_anchors[split:], programme_anchors[split:])
    detail = {
        'tolerance_us': None,
        'segments': [
            {'role': 'before_cut', **first_model},
            {'role': 'after_cut', **second_model},
        ],
        'cut': {
            'boundary_before_us': _round_us(boundary_programme),
            'boundary_native_before_us': _round_us(boundary_before),
            'jump_us': _round_us(jump),
            'anchor_gap_before_us': [programme_anchors[split - 1].before_us,
                                     programme_anchors[split].before_us],
            'anchor_gap_after_us': [programme_anchors[split - 1].after_us,
                                    programme_anchors[split].after_us],
        },
    }
    return 'cut_jump', detail


def _inconclusive(native_anchors, programme_anchors, tolerance, reason):
    points = [(Fraction(anchor.before_us), Fraction(anchor.after_us)) for anchor in native_anchors]
    if len(points) < 2:
        slope, intercept, residuals = Fraction(1), points[0][1] - points[0][0], [Fraction(0)]
    else:
        slope, intercept, residuals = linear_fit(points)
    return {
        'tolerance_us': tolerance,
        'reason': reason,
        'segments': [_model_dict(slope, intercept, residuals, native_anchors, programme_anchors)],
    }


def _unproven_ranges(scan, anchors, side):
    """Spans on one master clock that no accepted anchor can substantiate.

    Two kinds of ranges stay unproven: material outside the outermost anchors
    and every picture coverage hole inside the anchor-supported stretch.
    """
    coverage = [Span.parse(item) for item in scan['coverage']['picture']]
    key = 'before_us' if side == 'before' else 'after_us'
    first = getattr(anchors[0], key)
    last = getattr(anchors[-1], key)
    extent = Span(0, scan['extent']['end_us'])
    result = [Span(0, first), Span(last, extent.end)]
    supported = Span(first, last)
    covered = coalesce(span for span in coverage if span.intersect(supported) is not None)
    result.extend(uncovered(covered, supported))
    return [span.as_dict() for span in coalesce(span for span in result if span.end > span.start)]


def evaluate(before_scan, after_scan, data):
    anchors, tolerance = parse_request(data, before_scan, after_scan)
    origin_before = _origin_us(before_scan)
    origin_after = _origin_us(after_scan)
    origin_delta = origin_after - origin_before
    # Anchors arrive on the programme clocks (origin removed).  Restore the
    # native container timestamps so a pure start-offset is visible to the fit.
    native = [Anchor(anchor.label, anchor.before_us + origin_before,
                     anchor.after_us + origin_after) for anchor in anchors]
    if len(native) < 2:
        verdict, detail = 'inconclusive', _inconclusive(
            native, anchors, tolerance, 'only one scene anchor: a clock mapping needs at least two')
    else:
        single = _single_segment(native, anchors, tolerance)
        if single is not None:
            verdict, detail = single
        else:
            piece = _piecewise(native, anchors, tolerance) if len(native) >= MIN_CUT_ANCHORS else None
            if piece is not None:
                verdict, detail = piece
                detail['tolerance_us'] = tolerance
            else:
                reason = ('no single straight mapping fits within tolerance and no two-segment jump '
                          'is supported by at least four anchors')
                verdict, detail = 'inconclusive', _inconclusive(native, anchors, tolerance, reason)
    record = {
        'before_scan': before_scan['id'],
        'after_scan': after_scan['id'],
        'verdict': verdict,
        'verdict_label': VERDICT_LABELS[verdict],
        'tolerance_us': tolerance,
        'anchor_count': len(anchors),
        'container_origins_us': {'before': origin_before, 'after': origin_after,
                                 'start_delta_us': origin_delta},
        'anchors': [{'label': anchor.label, 'before_us': anchor.before_us, 'after_us': anchor.after_us}
                    for anchor in anchors],
        'unproven_ranges': {
            'before': _unproven_ranges(before_scan, anchors, 'before'),
            'after': _unproven_ranges(after_scan, anchors, 'after'),
        },
        **detail,
    }
    record['id'] = identifier('clockcheck', [
        record['before_scan'], record['after_scan'], record['tolerance_us'], record['anchors'],
    ])
    return record
