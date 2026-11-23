"""Production chapter tables projected onto a scan's programme clock.

A chapter table can be authored against two different zero points. A row may
start at the first programme picture (``picture`` origin, the same clock used
by the measured findings) or at the player's whole-reel zero (``reel``
origin), which also covers the audio-only head before the first picture.

Rows whose origin is not declared are kept honest: when both readings land on
playable picture the chapter can only be presented as a candidate range, never
silently resolved. A reading that falls outside picture coverage (the
audio-only head, a picture hole or past the end) is not playable, so a single
surviving reading determines the chapter. Links to findings always carry a
scan identity and are never remapped onto another scan's findings.
"""
from .clock import InputError, Missing, Span, coalesce, integer, shape, text
from .rules import identifier

ORIGINS = ('picture', 'reel')
MAX_CHAPTERS = 200


def parse_sheet(payload):
    """Validate a production chapter table and return an immutable sheet record."""
    shape(payload, ('chapters',), ('label',))
    label = text(payload['label'], 'label') if payload.get('label') is not None else ''
    raw = payload['chapters']
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_CHAPTERS:
        raise InputError(f'chapter table must contain 1 to {MAX_CHAPTERS} chapters')
    chapters = []
    seen = set()
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise InputError(f'chapter {index} must be an object')
        shape(entry, ('title',), ('origin', 'picture_time_us', 'reel_time_us'))
        title = text(entry['title'], f'chapter {index} title', 120)
        origin = entry.get('origin')
        if origin is not None and origin not in ORIGINS:
            raise InputError(f'chapter {index} origin must be picture or reel')
        picture_time = entry.get('picture_time_us')
        reel_time = entry.get('reel_time_us')
        if origin is None and (picture_time is None or reel_time is None):
            raise InputError(f'chapter {index} needs both time readings when its origin is not declared')
        if origin == 'picture' and picture_time is None:
            raise InputError(f'chapter {index} declares a picture origin but omits picture_time_us')
        if origin == 'reel' and reel_time is None:
            raise InputError(f'chapter {index} declares a reel origin but omits reel_time_us')
        values = {}
        for name, value in (('picture_time_us', picture_time), ('reel_time_us', reel_time)):
            if value is not None:
                values[name] = integer(value, name, 0, 120 * 1_000_000)
        if len(set(values.values())) != len(values):
            raise InputError(f'chapter {index} picture and reel readings must differ or be dropped')
        for reading in values.values():
            if reading in seen:
                raise InputError(f'chapter {index} repeats a time reading already used by another chapter')
            seen.add(reading)
        chapters.append({'seq': index, 'title': title, 'origin': origin, **values})
    record = {'label': label, 'chapters': chapters}
    return {'id': identifier('sheet', record), **record}


def picture_start(scan):
    """First playable picture timestamp on the programme clock."""
    coverage = scan['coverage']['picture']
    if not coverage:
        raise InputError('selected scan has no picture coverage to anchor chapters')
    return coverage[0]['start_us']


def _playable(scan, when):
    if not 0 <= when < scan['extent']['end_us']:
        return False
    point = Span(when, when + 1)
    return any(point.intersect(Span(area['start_us'], area['end_us']))
               for area in coalesce(scan['coverage']['picture']))


def _readings(chapter):
    if chapter['origin'] == 'picture':
        return [('picture', chapter['picture_time_us'])]
    if chapter['origin'] == 'reel':
        return [('reel', chapter['reel_time_us'])]
    return [('picture', chapter['picture_time_us']), ('reel', chapter['reel_time_us'])]


def project(scan, sheet):
    """Place every sheet row on this scan, separating determined from candidate."""
    head = picture_start(scan)
    determined, candidates = [], []
    for chapter in sheet['chapters']:
        placements = []
        for origin, reading in _readings(chapter):
            when = reading if origin == 'picture' else reading - head
            if _playable(scan, when):
                placements.append({'origin': origin, 'reading_us': reading, 'time_us': when})
        if not placements:
            raise InputError(
                f'chapter {chapter["seq"]} 「{chapter["title"]}」 has no playable picture position; '
                'its reading falls in the audio-only head, a picture hole or past the end')
        if chapter['origin'] is not None or len(placements) == 1:
            chosen = placements[0]
            determined.append({
                'seq': chapter['seq'], 'title': chapter['title'], 'time_us': chosen['time_us'],
                'origin': chosen['origin'], 'declared_origin': chapter['origin'],
                'reading_us': chosen['reading_us'], 'head_offset_us': head,
            })
        else:
            times = sorted(item['time_us'] for item in placements)
            candidates.append({
                'seq': chapter['seq'], 'title': chapter['title'],
                'range': {'start_us': times[0], 'end_us': times[-1]},
                'placements': sorted(placements, key=lambda item: item['time_us']),
                'head_offset_us': head,
            })
    return {'head_offset_us': head, 'determined': determined, 'candidates': candidates}


def known_hit(scan, hit_id):
    for hit in scan['hits']:
        if hit['id'] == hit_id:
            return hit
    raise Missing('finding does not belong to the selected scan')


def known_chapter(import_event, chapter_seq):
    sequences = {chapter['seq'] for chapter in import_event['chapters']}
    if chapter_seq not in sequences:
        raise Missing('chapter does not belong to the imported chapter table')


def link_event(scan, import_event, data):
    """Validate a manual chapter-to-finding link against the live scan."""
    shape(data, ('chapter_seq',), ('hit_id',))
    seq = integer(data['chapter_seq'], 'chapter_seq', 1, MAX_CHAPTERS)
    known_chapter(import_event, seq)
    hit_id = data.get('hit_id')
    hit = None
    if hit_id is not None:
        hit = known_hit(scan, text(hit_id, 'hit_id'))
    return {'sheet_id': import_event['sheet_id'], 'chapter_seq': seq,
            'hit_id': hit['id'] if hit else None}


def carry_links(scan, sheet_id, links):
    """Split imported legacy links into live links and explicitly unresolved ones.

    A link only survives when it names a finding of *this* scan and a chapter of
    this sheet. Nothing is reattached by proximity, so a trimmed programme keeps
    the old reference visible as unresolved instead of moving it to another hit.
    """
    if not isinstance(links, list) or len(links) > MAX_CHAPTERS:
        raise InputError('chapter links must be a list no larger than the chapter table')
    chapter_seqs = {chapter['seq'] for chapter in sheet_id['chapters']}
    hit_ids = {hit['id'] for hit in scan['hits']}
    active, unresolved = [], []
    for index, link in enumerate(links, start=1):
        if not isinstance(link, dict):
            raise InputError(f'chapter link {index} must be an object')
        shape(link, ('chapter_seq', 'hit_id'), ('from_scan_id',))
        seq = integer(link['chapter_seq'], 'chapter_seq', 1, MAX_CHAPTERS)
        hit_id = text(link['hit_id'], 'hit_id')
        from_scan = link.get('from_scan_id')
        if from_scan is not None:
            text(from_scan, 'from_scan_id')
        if seq not in chapter_seqs:
            raise InputError(f'chapter link {index} names a chapter absent from the imported table')
        record = {'sheet_id': sheet_id['id'], 'chapter_seq': seq, 'hit_id': hit_id,
                  'from_scan_id': from_scan}
        if hit_id in hit_ids:
            active.append(record)
        else:
            unresolved.append({**record, 'reason': 'finding is not part of this scan'})
    return active, unresolved
