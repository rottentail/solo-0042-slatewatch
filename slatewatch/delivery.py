"""Pre-delivery check: does the selected material testify each conclusion?

A delivery plan describes a resource-limited recipient: how many frames per
excerpt they can view and whether they receive the WAV. The check reads the
actually extracted mappings — captured source frames, native channels and
zero-filled ranges — never the count of generated files. When a combination
falls short it names the missing testimony capability:

- ``picture``: a real source frame inside the finding span (black).
- ``picture_pair``: real frames on both sides of the segment — the two halves
  of a long freeze, or the flanks of a picture hole.
- ``sound``: the shipped WAV carries real samples over the finding span, or
  documents a sound hole through its filled ranges.
- ``stereo``: the anomaly is only judgeable from a specific channel of the
  dual-channel WAV, so the shipped WAV must carry that channel.
"""
from .clock import SECOND, Span

PICTURE_KINDS = frozenset(('black', 'freeze', 'picture_gap'))
CAPABILITIES = ('picture', 'picture_pair', 'sound', 'stereo')


def _picture_missing(hit, pictures):
    span = Span(hit['start_us'], hit['end_us'])
    captured = [item for item in pictures if item['source_us'] is not None]
    if hit['kind'] == 'black':
        inside = any(span.start <= item['target_us'] < span.end for item in captured)
        return [] if inside else ['picture']
    if hit['kind'] == 'freeze':
        middle = span.start + span.duration // 2
        early = any(span.start <= item['target_us'] < middle for item in captured)
        late = any(middle <= item['target_us'] < span.end for item in captured)
        return [] if early and late else ['picture_pair']
    before = any(item['target_us'] < span.start for item in captured)
    after = any(item['target_us'] >= span.end for item in captured)
    planned_before = any(item['target_us'] < span.start for item in pictures)
    planned_after = any(item['target_us'] >= span.end for item in pictures)
    flanked = (before or not planned_before) and (after or not planned_after)
    return [] if flanked and (before or after) else ['picture_pair']


def _sound_missing(hit, sound, shipped):
    capability = 'stereo' if (hit['channel'] or 0) >= 2 else 'sound'
    if not shipped or sound is None or sound['channels'] < (hit['channel'] or 1):
        return [capability]
    rate = sound['sample_rate']
    span = Span(hit['start_us'], hit['end_us'])
    filled = [Span(round(item['first'] * SECOND / rate), round(item['end'] * SECOND / rate))
              for item in sound['filled_sample_ranges']]
    overlaps = any(item.intersect(span) for item in filled)
    if hit['kind'] == 'sound_gap':
        return [] if overlaps else [capability]
    return [capability] if overlaps else []


def _observed(mapping, plan):
    pictures = mapping['picture']
    sound = mapping['sound']
    return {
        'picture_targets': len(pictures),
        'picture_captured': sum(1 for item in pictures if item['source_us'] is not None),
        'sound': None if sound is None else {
            'shipped': plan['sound'],
            'channels': sound['channels'],
            'filled_ranges': len(sound['filled_sample_ranges']),
        },
    }


def assess(scan, hits, excerpts, plan):
    """Grade each finding against the materials selected under ``plan``."""
    mappings = {mapping['hit_id']: mapping for _members, mapping in excerpts}
    rows = []
    missing = {}
    for hit in hits:
        mapping = mappings[hit['id']]
        if hit['kind'] in PICTURE_KINDS:
            lacks = _picture_missing(hit, mapping['picture'])
        else:
            lacks = _sound_missing(hit, mapping['sound'], plan['sound'])
        for capability in lacks:
            missing.setdefault(capability, []).append(hit['id'])
        rows.append({
            'hit_id': hit['id'], 'kind': hit['kind'], 'channel': hit['channel'],
            'start_us': hit['start_us'], 'end_us': hit['end_us'],
            'status': 'supported' if not lacks else 'unsupported',
            'missing': lacks,
            'evidence': _observed(mapping, plan),
        })
    return {
        'scan_id': scan['id'],
        'ready': not missing,
        'delivery': {'frames': plan['frames'], 'sound': plan['sound'], 'padding_ms': plan['padding_ms']},
        'hits': rows,
        'missing': missing,
    }
