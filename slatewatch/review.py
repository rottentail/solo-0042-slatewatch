"""Reconcile immutable findings with programme intent, corrections and reviewer decisions.

Intent events are append-only. A production correction does not delete or rewrite the
earlier note: it appends an ``intent_correction`` event that supersedes the earlier
intent and contributes a narrower (or otherwise changed) active span. The full ledger,
including why a candidate was once released, stays readable. Review adjudication also
splits every finding into microsecond segments so a handoff can tell evidence-backed
picture from coverage holes that no ruling can turn into measured material.
"""
from .clock import InputError, Missing, Span, coalesce, integer, partition, shape, text
from .rules import CHANNEL_KINDS, INTENT_KINDS, INTENT_STATES

OUTCOMES = {'accept', 'hold', 'reset'}
ACTIVE, SUPERSEDED = INTENT_STATES
BASIS_ORDER = ('unmeasured', 'expected', 'pending')


def find_hit(scan, hit_id):
    for hit in scan['hits']:
        if hit['id'] == hit_id:
            return hit
    raise Missing('finding does not belong to the selected scan')


def _record_ref(data):
    record = data.get('record')
    if record is None:
        return None
    return text(record, 'record', 120)


def intent(scan, data):
    shape(data, ('kind', 'start_us', 'end_us', 'reason'), ('channel', 'record'))
    kind = data['kind']
    if not isinstance(kind, str) or kind not in INTENT_KINDS:
        raise InputError('intent must describe black, freeze or silence')
    span = Span.parse(data, scan['extent']['end_us'])
    channel = data.get('channel')
    if kind in CHANNEL_KINDS:
        if not scan['tracks']['sound']:
            raise InputError('selected scan has no sound lane')
        if channel is None:
            raise InputError(f'{kind} intent must name its channel')
        integer(channel, 'channel', 1, scan['tracks']['sound']['channels'])
    elif channel is not None:
        raise InputError('picture intent does not take a channel')
    entry = {'kind': kind, 'channel': channel, **span.as_dict(),
             'reason': text(data['reason'], 'reason', 350)}
    record = _record_ref(data)
    if record is not None:
        entry['record'] = record
    return entry


def correction(scan, intents, corrections, data):
    shape(data, ('intent_seq', 'start_us', 'end_us', 'reason'), ('record',))
    sequence = integer(data['intent_seq'], 'intent_seq', 1, 2 ** 63 - 1)
    target = next((item for item in intents if item['seq'] == sequence), None)
    if target is None:
        raise Missing('intent sequence does not belong to the selected scan')
    if any(item['supersedes_seq'] == sequence for item in corrections):
        raise InputError('intent has already been corrected')
    span = Span.parse(data, scan['extent']['end_us'])
    entry = {'supersedes_seq': sequence, 'kind': target['kind'],
             'channel': target['channel'], **span.as_dict(),
             'reason': text(data['reason'], 'reason', 350)}
    record = data.get('record')
    if record is not None:
        entry['record'] = text(record, 'record', 120)
    elif 'record' in target:
        entry['record'] = target['record']
    return entry


def reconstruct(intents, corrections=()):
    """Project the append-only events into the current intent ledger.

    Original intent documents keep their stored span and reason. A correction adds a
    new active ledger entry carrying the corrected span and a lineage link; the entry
    it supersedes remains in the ledger, marked ``superseded``. Legacy scans without
    correction events simply read every intent as active.
    """
    superseded = {event['supersedes_seq'] for event in corrections}
    ledger = []
    for mark in intents:
        state = SUPERSEDED if mark['seq'] in superseded else ACTIVE
        ledger.append({'state': state, 'record': None, 'corrected_from_seq': None, **mark})
    for event in corrections:
        ledger.append({'state': ACTIVE, 'seq': event['seq'], 'kind': event['kind'],
                       'channel': event['channel'], 'start_us': event['start_us'],
                       'end_us': event['end_us'], 'reason': event['reason'],
                       'record': event.get('record'), 'corrected_from_seq': event['supersedes_seq']})
    return ledger


def ruling(scan, data):
    shape(data, ('hit_id', 'outcome', 'reason', 'reviewer'))
    find_hit(scan, text(data['hit_id'], 'hit_id'))
    if not isinstance(data['outcome'], str) or data['outcome'] not in OUTCOMES:
        raise InputError('outcome must be accept, hold or reset')
    return {'hit_id': data['hit_id'].strip(), 'outcome': data['outcome'],
            'reason': text(data['reason'], 'reason', 350), 'reviewer': text(data['reviewer'], 'reviewer')}


def matching_intents(hit, marks):
    span = Span.parse(hit)
    overlapping = []
    for mark in marks:
        if mark['kind'] != hit['kind'] or mark['channel'] not in (None, hit['channel']):
            continue
        area = Span.parse(mark).intersect(span)
        if area:
            overlapping.append((mark, area))
    return overlapping


def applicable(hit, intents):
    span = Span.parse(hit)
    overlaps = matching_intents(hit, intents)
    covered = coalesce(area for _, area in overlaps)
    return any(area.contains(span) for area in covered), [mark['seq'] for mark, _ in overlaps]


def coverage_lanes(scan, hit):
    if hit['kind'] in CHANNEL_KINDS or hit['kind'] == 'sound_gap':
        return [Span.parse(item) for item in scan['coverage']['sound']]
    return [Span.parse(item) for item in scan['coverage']['picture']]


def segments(scan, hit, active_marks):
    """Split a finding into evidence basis segments on the programme clock.

    Boundaries come from every individual active intent (not just the coalesced
    union) so the receiver can see which production record backs each fragment,
    including fragments where a corrected note and an independent note overlap.
    """
    span = Span.parse(hit)
    coverage = coalesce(coverage_lanes(scan, hit))
    overlaps = matching_intents(hit, active_marks)
    supported = coalesce(area for _, area in overlaps)
    boundaries = {span.start, span.end}
    for area in (*coverage, *(area for _, area in overlaps)):
        boundaries.update(point for point in (area.start, area.end) if span.start < point < span.end)
    result = []
    for piece in partition(boundaries, span):
        has_picture = any(area.contains(piece) for area in coverage)
        backed = any(area.contains(piece) for area in supported)
        if not has_picture:
            basis = 'unmeasured'
        else:
            basis = 'expected' if backed else 'pending'
        marks = sorted({mark['seq'] for mark, area in overlaps if area.contains(piece)})
        result.append({'basis': basis, **piece.as_dict(), 'duration_us': piece.duration,
                       'intent_sequences': marks})
    return result


def adjudicate(scan, intents, rulings, corrections=()):
    last_ruling = {record['hit_id']: record for record in rulings}
    ledger = reconstruct(intents, corrections)
    active_marks = [mark for mark in ledger if mark['state'] == ACTIVE]
    results = []
    counts = dict.fromkeys(('pending', 'expected', 'accepted', 'held'), 0)
    basis_totals = dict.fromkeys(BASIS_ORDER, 0)
    for hit in scan['hits']:
        decision = last_ruling.get(hit['id'])
        expected, marks = applicable(hit, active_marks)
        pieces = segments(scan, hit, active_marks)
        for piece in pieces:
            basis_totals[piece['basis']] += piece['duration_us']
        if decision and decision['outcome'] in ('accept', 'hold'):
            status = 'accepted' if decision['outcome'] == 'accept' else 'held'
        else:
            status = 'expected' if expected else 'pending'
        counts[status] += 1
        results.append({**hit, 'status': status, 'intent_sequences': marks,
                        'segments': pieces, 'ruling': decision})
    status = 'hold' if counts['held'] else 'pending' if counts['pending'] else 'pass'
    return {'scan_id': scan['id'], 'status': status, 'counts': counts,
            'basis_us': basis_totals, 'hits': results, 'intents': ledger,
            'intent_events': intents, 'intent_corrections': list(corrections),
            'rulings': rulings}
