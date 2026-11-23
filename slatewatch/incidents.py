"""Independent incident timeline from measured coverage and dropout findings."""
from .clock import Span, uncovered

INTERRUPTED = 'interrupted'
CLEAR = 'clear'
UNMEASURED = 'unmeasured'

MEASURED_KINDS = {'picture': ('black',), 'sound': ('silence',)}
GAP_KIND = {'picture': 'picture_gap', 'sound': 'sound_gap'}
SUPPORTS = ('joint', 'picture', 'sound')


def lane_intervals(scan, lane, extent):
    """Interruption ranges of one lane, each tied to its measured basis."""
    coverage = [Span.parse(span) for span in scan['coverage'][lane]]
    threshold = scan['thresholds'][f'{lane}_gap_ms'] * 1000
    gap_kind = GAP_KIND[lane]
    intervals = []
    for hole in uncovered(coverage, extent):
        hit_ids = [hit['id'] for hit in scan['hits']
                   if hit['kind'] == gap_kind and Span.parse(hit).intersect(hole)]
        intervals.append({'lane': lane, 'kind': gap_kind, 'measured': False, 'channel': None,
                          **hole.as_dict(), 'below_threshold': hole.duration < threshold,
                          'hit_ids': hit_ids})
    for hit in scan['hits']:
        if hit['kind'] in MEASURED_KINDS[lane]:
            intervals.append({'lane': lane, 'kind': hit['kind'], 'measured': True,
                              'channel': hit['channel'], 'start_us': hit['start_us'],
                              'end_us': hit['end_us'], 'below_threshold': False,
                              'hit_ids': [hit['id']]})
    return sorted(intervals, key=lambda item: (item['start_us'], item['end_us']))


def _state(intervals, start, end):
    if intervals is None:
        return UNMEASURED
    return INTERRUPTED if any(iv['start_us'] < end and start < iv['end_us'] for iv in intervals) else CLEAR


def _support(picture, sound):
    if picture == INTERRUPTED and sound == INTERRUPTED:
        return 'joint'
    return 'picture' if picture == INTERRUPTED else 'sound'


def timeline(scan):
    """Partition the programme extent into repairable incident segments.

    A segment is incident while at least one selected lane is interrupted,
    either by a measured dropout finding or by a coverage hole of any size;
    holes shorter than the candidate threshold still count because they are
    ranges without any measurement. An unselected sound lane is unmeasured,
    never clear, so its segments cannot read as jointly supported.
    """
    extent = Span(0, scan['extent']['end_us'])
    selected = scan['coverage']['sound'] is not None
    lanes = {'picture': lane_intervals(scan, 'picture', extent),
             'sound': lane_intervals(scan, 'sound', extent) if selected else None}
    points = {extent.start, extent.end}
    for intervals in lanes.values():
        for item in intervals or ():
            points.update((item['start_us'], item['end_us']))
    ordered = sorted(points)
    merged = []
    for start, end in zip(ordered, ordered[1:]):
        states = {lane: _state(intervals, start, end) for lane, intervals in lanes.items()}
        if INTERRUPTED not in states.values():
            continue
        if merged and merged[-1]['end_us'] == start and merged[-1]['lanes'] == states:
            merged[-1]['end_us'] = end
        else:
            merged.append({'start_us': start, 'end_us': end, 'lanes': states})
    segments = []
    for segment in merged:
        evidence = [item for lane in ('picture', 'sound') for item in lanes[lane] or ()
                    if item['start_us'] < segment['end_us'] and segment['start_us'] < item['end_us']]
        segments.append({**segment, 'support': _support(segment['lanes']['picture'], segment['lanes']['sound']),
                         'evidence': evidence})
    counts = {key: sum(1 for segment in segments if segment['support'] == key) for key in SUPPORTS}
    return {'scan_id': scan['id'], 'extent': scan['extent'], 'sound_selected': selected,
            'tracks': {'picture': scan['tracks']['picture']['index'],
                       'sound': scan['tracks']['sound']['index'] if scan['tracks']['sound'] else None},
            'counts': counts, 'segments': segments}
