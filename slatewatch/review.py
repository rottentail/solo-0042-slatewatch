"""Reconcile immutable findings with programme intent and reviewer decisions."""
from .clock import InputError, Missing, Span, coalesce, integer, shape, text
from .rules import CHANNEL_KINDS, INTENT_KINDS

OUTCOMES = {'accept', 'hold', 'reset'}


def find_hit(scan, hit_id):
    for hit in scan['hits']:
        if hit['id'] == hit_id:
            return hit
    raise Missing('finding does not belong to the selected scan')


def intent(scan, data):
    shape(data, ('kind', 'start_us', 'end_us', 'reason'), ('channel',))
    kind = data['kind']
    if not isinstance(kind, str) or kind not in INTENT_KINDS:
        raise InputError('intent must describe black, freeze or silence')
    span = Span.parse(data, scan['extent']['end_us'])
    channel = data.get('channel')
    if kind in CHANNEL_KINDS:
        if not scan['tracks']['sound']:
            raise InputError('selected scan has no sound lane')
        if channel is not None:
            integer(channel, 'channel', 1, scan['tracks']['sound']['channels'])
    elif channel is not None:
        raise InputError('picture intent does not take a channel')
    return {'kind': kind, 'channel': channel, **span.as_dict(), 'reason': text(data['reason'], 'reason', 350)}


def ruling(scan, data):
    shape(data, ('hit_id', 'outcome', 'reason', 'reviewer'))
    find_hit(scan, text(data['hit_id'], 'hit_id'))
    if not isinstance(data['outcome'], str) or data['outcome'] not in OUTCOMES:
        raise InputError('outcome must be accept, hold or reset')
    return {'hit_id': data['hit_id'].strip(), 'outcome': data['outcome'],
            'reason': text(data['reason'], 'reason', 350), 'reviewer': text(data['reviewer'], 'reviewer')}


def applicable(hit, intents):
    span = Span.parse(hit)
    matching = [(mark, Span.parse(mark)) for mark in intents
                if mark['kind'] == hit['kind'] and mark['channel'] in (None, hit['channel'])]
    overlapping = [(mark, area) for mark, area in matching if area.intersect(span)]
    covered = coalesce(area for _, area in overlapping)
    return any(area.contains(span) for area in covered), [mark['seq'] for mark, _ in overlapping]


def adjudicate(scan, intents, rulings):
    last_ruling = {record['hit_id']: record for record in rulings}
    results = []
    counts = dict.fromkeys(('pending', 'expected', 'accepted', 'held'), 0)
    for hit in scan['hits']:
        decision = last_ruling.get(hit['id'])
        expected, marks = applicable(hit, intents)
        if decision and decision['outcome'] in ('accept', 'hold'):
            status = 'accepted' if decision['outcome'] == 'accept' else 'held'
        else:
            status = 'expected' if expected else 'pending'
        counts[status] += 1
        results.append({**hit, 'status': status, 'intent_sequences': marks, 'ruling': decision})
    status = 'hold' if counts['held'] else 'pending' if counts['pending'] else 'pass'
    return {'scan_id': scan['id'], 'status': status, 'counts': counts, 'hits': results,
            'intents': intents, 'rulings': rulings}
