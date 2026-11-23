"""Repair plans for missing-frame holes, reviewed on a target frame-rate grid.

The damaged master (A) stays read-only: plans only read its scan facts.  A
second scan (B) is consulted on the shared programme clock.  Every missing
frame position inside an A-side picture hole is classified as:

* ``solid``      - B has a distinct, verifiable content cell for the position;
* ``held``       - B covers the position only by dwelling on one frozen cell;
* ``unverified``- neither side has verifiable content for the position.

Target grid positions are exact rationals so any frame rate can be re-checked;
source times are integer microseconds from the B-side decoded display clock and
uncertain boundary edges are reported instead of being silently assumed.
"""
from dataclasses import dataclass
from fractions import Fraction

from .clock import InputError, SECOND, Span, coalesce, integer, shape, text
from .media import decode
from .rules import identifier

PLAN_VERSION = 1
SOLID, HELD, UNVERIFIED = 'solid', 'held', 'unverified'
CLASSES = (SOLID, HELD, UNVERIFIED)
MIN_FRAME_RATE, MAX_FRAME_RATE = 1, 240
# A cell may be replayed for one extra display period without counting as held.
HELD_REPEAT_TOLERANCE = 1


@dataclass(frozen=True)
class Rate:
    numerator: int
    denominator: int

    @classmethod
    def parse(cls, value, name):
        if isinstance(value, int):
            numerator, denominator = value, 1
        elif isinstance(value, str):
            text_value = value.strip()
            if not text_value:
                raise InputError(f'{name} must be a positive frame rate')
            parts = text_value.split('/')
            if len(parts) > 2 or any(not part.strip().isdigit() for part in parts):
                raise InputError(f'{name} must be an integer rate or N/D fraction')
            numerator = int(parts[0].strip())
            denominator = int(parts[1].strip()) if len(parts) == 2 else 1
        else:
            raise InputError(f'{name} must be an integer rate or N/D fraction')
        integer(numerator, name, MIN_FRAME_RATE, MAX_FRAME_RATE * 1000)
        integer(denominator, f'{name} denominator', 1, 1_000_000)
        rate = Fraction(numerator, denominator)
        if not MIN_FRAME_RATE <= rate <= MAX_FRAME_RATE:
            raise InputError(f'{name} must be between {MIN_FRAME_RATE} and {MAX_FRAME_RATE} fps')
        return cls(numerator, denominator)

    @property
    def fraction(self):
        return Fraction(self.numerator, self.denominator)

    def as_dict(self):
        return {'numerator': self.numerator, 'denominator': self.denominator}


@dataclass(frozen=True)
class SourceCell:
    """One decoded B-side picture with its actual source display time."""

    timestamp_us: int
    next_us: int
    pixels: bytes


def source_cells(path, tracks):
    """Decode B once, keeping real source times and thumbnails for identity."""
    cells, previous = [], None
    for block in decode(path, tracks):
        if previous is not None:
            cells.append(SourceCell(previous.timestamp_us, block.timestamp_us, previous.pixels))
        previous = block
    if previous is None:
        raise InputError('repair source has no pictures in the programme clock')
    nominal = max(1, round(SECOND / Fraction(tracks['picture'].get('average_rate') or '25')))
    cells.append(SourceCell(previous.timestamp_us, previous.timestamp_us + nominal, previous.pixels))
    return cells


def _grid_positions(hole, rate):
    """Target frame-rate slots [t, t+period) intersecting the hole, in order.

    Grid instants are exact rationals; slot edges are rounded to integer
    microseconds, matching the integer microsecond clock used by the scans.
    """
    period = SECOND / rate.fraction
    first = -((-Fraction(hole.start) * rate.numerator) // (rate.denominator * SECOND))
    index = first
    while True:
        point = index * period
        if point >= hole.end:
            return
        start = max(hole.start, point)
        end = min(hole.end, point + period)
        if start < end:
            yield index, Span(round(start), round(end))
        index += 1


def classify_slot(slot, cells):
    overlaps = [cell for cell in cells if cell.timestamp_us < slot.end and slot.start < cell.next_us]
    if not overlaps:
        return UNVERIFIED, None
    midpoint = (slot.start + slot.end) // 2
    cell = next((candidate for candidate in overlaps
                 if candidate.timestamp_us <= midpoint < candidate.next_us), overlaps[0])
    repeats = sum(1 for other in cells if other.pixels == cell.pixels)
    if repeats <= HELD_REPEAT_TOLERANCE + 1:
        return SOLID, cell
    return HELD, cell


def _uncertain_edges(hole, cells, slots):
    """Report grid edges where B's source boundary is not exactly on the grid."""
    edges = []
    boundaries = sorted({edge for cell in cells for edge in (cell.timestamp_us, cell.next_us)
                         if hole.start < edge < hole.end})
    for edge in boundaries:
        for _, slot in slots:
            if slot.start < edge < slot.end:
                edges.append({'edge_us': edge, 'slot_start_us': slot.start, 'slot_end_us': slot.end})
                break
    return edges


def plan_hole(hole, rate, cells):
    slots = list(_grid_positions(hole, rate))
    positions, counts = [], dict.fromkeys(CLASSES, 0)
    for index, slot in slots:
        kind, cell = classify_slot(slot, cells)
        counts[kind] += 1
        positions.append({
            'frame_index': index,
            'target_start_us': slot.start,
            'target_end_us': slot.end,
            'classification': kind,
            'b_source_start_us': cell.timestamp_us if cell else None,
            'b_source_end_us': cell.next_us if cell else None,
        })
    return {
        'start_us': hole.start,
        'end_us': hole.end,
        'target_slots': len(slots),
        'counts': counts,
        'uncertain_edges': _uncertain_edges(hole, cells, slots),
        'positions': positions,
    }


def build_plan(damaged_scan, donor_scan, donor_path, rate):
    picture_gaps = [hit for hit in damaged_scan['hits'] if hit['kind'] == 'picture_gap']
    if not picture_gaps:
        raise InputError('the damaged scan has no picture gap to repair')
    if damaged_scan['id'] == donor_scan['id']:
        raise InputError('repair donor must be a different scan from the damaged master')
    damaged_extent = Span.parse(damaged_scan['extent'])
    donor_extent = Span.parse(donor_scan['extent'])
    holes = coalesce(Span.parse(hit) for hit in picture_gaps)
    cells = source_cells(donor_path, donor_scan['tracks'])
    donor_coverage = [Span.parse(span) for span in donor_scan['coverage']['picture']]
    plans = []
    for hole in holes:
        hole_plans = plan_hole(hole, rate, cells)
        # Positions outside any B-side covered span cannot be verified either.
        hole_coverage = [part for span in donor_coverage
                         if (part := span.intersect(hole)) is not None]
        for position in hole_plans['positions']:
            slot = Span(position['target_start_us'], position['target_end_us'])
            if not any(span.contains(slot) for span in hole_coverage):
                if position['classification'] != UNVERIFIED:
                    hole_plans['counts'][position['classification']] -= 1
                    hole_plans['counts'][UNVERIFIED] += 1
                position['classification'] = UNVERIFIED
                position['b_source_start_us'] = None
                position['b_source_end_us'] = None
        plans.append(hole_plans)
    totals = dict.fromkeys(CLASSES, 0)
    for entry in plans:
        for kind in CLASSES:
            totals[kind] += entry['counts'][kind]
    record = {
        'damaged_scan': damaged_scan['id'],
        'donor_scan': donor_scan['id'],
        'damaged_master_id': damaged_scan['master_id'],
        'donor_master_id': donor_scan['master_id'],
        'plan_version': PLAN_VERSION,
        'target_frame_rate': rate.as_dict(),
        'damaged_extent_us': damaged_extent.as_dict(),
        'donor_extent_us': donor_extent.as_dict(),
        'counts': totals,
        'holes': plans,
    }
    record['id'] = identifier('repair', [record['damaged_scan'], record['donor_scan'],
                                         record['plan_version'], record['target_frame_rate']])
    return record


def request_payload(data):
    shape(data, ('damaged_scan', 'donor_scan'), ('target_frame_rate',))
    damaged = text(data['damaged_scan'], 'damaged_scan')
    donor = text(data['donor_scan'], 'donor_scan')
    if damaged == donor:
        raise InputError('repair donor must be a different scan from the damaged master')
    rate = Rate.parse(data.get('target_frame_rate', 25), 'target_frame_rate')
    return damaged, donor, rate
