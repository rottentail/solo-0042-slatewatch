"""Locate other programme positions that reuse the picture content of one finding.

The lookup reports association leads, never a merged verdict.  Every similar
position keeps its own findings, review status and audio facts; sharing a
picture sequence is not evidence that a technical anomaly is shared as well.
Identical still pictures, such as the station ident repeated at every programme
head, are listed separately and never counted as content repeats.
"""
from bisect import bisect_left
from collections import defaultdict
from math import sqrt

from .clock import SECOND, Span, sample_boundary
from .media import HEIGHT, WIDTH, Picture, decode

GRID_W, GRID_H = 10, 6
CELL_W, CELL_H = WIDTH // GRID_W, HEIGHT // GRID_H
AUDIO_STEP = 250_000

MOTION_SEED = 0.6
MOTION_MIN = 1.0
ACTIVE_FRACTION = 0.5
STILL_MOTION = 0.3
STILL_SIMILARITY = 995
RUN_MIN_US = 250_000
GAP_FRAMES = 3
MERGE_US = 300_000
ALIGN_FRAMES = 8
TABLE_COUNT = 4
TABLE_BITS = 20
BUCKET_LIMIT = 64
PAIR_LIMIT = 200_000
DEVIATION_LIMIT = 24
AUDIO_SIMILAR_PERMILLE = 800
POSITION_LIMIT = 24


def _normalize(values):
    mean = sum(values) / len(values)
    centred = [value - mean for value in values]
    norm = sqrt(sum(value * value for value in centred))
    return tuple(value / norm for value in centred) if norm else tuple(0.0 for _ in centred)


def _profile(pixels):
    cells = []
    for gy in range(GRID_H):
        for gx in range(GRID_W):
            total = 0
            for y in range(gy * CELL_H, (gy + 1) * CELL_H):
                row = y * WIDTH
                total += sum(pixels[row + gx * CELL_W:row + (gx + 1) * CELL_W])
            cells.append(total / (CELL_W * CELL_H))
    return _normalize(cells)


def _similarity(first, second):
    return sum(a * b for a, b in zip(first, second))


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _projections(seed):
    tables = []
    state = seed & 0xFFFFFFFF
    for _ in range(TABLE_COUNT):
        table = []
        for _bit in range(TABLE_BITS):
            vector = []
            for _index in range(GRID_W * GRID_H):
                state ^= (state << 13) & 0xFFFFFFFF
                state ^= state >> 17
                state ^= (state << 5) & 0xFFFFFFFF
                vector.append(1.0 if state & 0x80000000 else -1.0)
            table.append(vector)
        tables.append(table)
    return tables


_TABLES = _projections(20260101)


def _key(signature, table):
    return tuple(1 if sum(a * b for a, b in zip(signature, vector)) >= 0 else 0 for vector in table)


class _Audio:
    """Per-channel RMS and zero-crossing facts on a fixed programme grid."""

    def __init__(self, rate, channels):
        self.rate = rate
        self.channels = channels
        self.windows = {}

    def push(self, pcm):
        if not pcm.count:
            return
        for channel in range(self.channels):
            values = pcm.samples[channel::self.channels]
            total = len(values)
            index = 0
            while index < total:
                key = (pcm.first_sample + index) * SECOND // (AUDIO_STEP * self.rate)
                boundary = sample_boundary((key + 1) * AUDIO_STEP, self.rate) - pcm.first_sample
                stop = min(total, max(index + 1, boundary))
                chunk = values[index:stop]
                entry = self.windows.setdefault(
                    key, [{'square': 0.0, 'count': 0, 'crossings': 0, 'previous': None}
                          for _ in range(self.channels)])[channel]
                entry['square'] += sum(float(value) * value for value in chunk)
                entry['count'] += len(chunk)
                previous = entry['previous']
                crossings = 0
                for value in chunk:
                    if previous is not None and (value >= 0) != (previous >= 0):
                        crossings += 1
                    previous = value
                entry['crossings'] += crossings
                entry['previous'] = previous
                index = stop

    def facts(self, span):
        first = span.start // AUDIO_STEP
        last = max(first, (span.end - 1) // AUDIO_STEP)
        windows = []
        for key in range(first, last + 1):
            begin = key * AUDIO_STEP
            end = min(span.end, begin + AUDIO_STEP)
            expected = sample_boundary(end, self.rate) - sample_boundary(begin, self.rate)
            stored = self.windows.get(key)
            channels = []
            covered = 0
            for channel in range(self.channels):
                entry = stored[channel] if stored else None
                count = entry['count'] if entry else 0
                covered = max(covered, count)
                channels.append({
                    'rms': round(sqrt(entry['square'] / count)) if count else None,
                    'zero_crossings_permille': round(entry['crossings'] * 1000 / (count - 1))
                    if count > 1 else None,
                })
            windows.append({'start_us': begin, 'end_us': end,
                            'coverage_permille': min(1000, round(covered * 1000 / expected)) if expected else 0,
                            'channels': channels})
        return windows


def _sound_facts(audio, span):
    if audio is None:
        return {'state': 'unselected', 'windows': [], 'rms': None, 'zero_crossings_permille': None}
    windows = audio.facts(span)
    present = [window for window in windows if window['coverage_permille'] > 0]
    full = [window for window in windows if window['coverage_permille'] >= 1000]
    if not present:
        state = 'missing'
    elif len(full) < len(windows):
        state = 'partial'
    elif all(channel['rms'] == 0 for window in full for channel in window['channels']
             if channel['rms'] is not None):
        state = 'silent'
    else:
        state = 'covered'
    levels = [channel['rms'] for window in full for channel in window['channels'] if channel['rms'] is not None]
    crossings = [channel['zero_crossings_permille'] for window in full for channel in window['channels']
                 if channel['zero_crossings_permille'] is not None]
    return {'state': state, 'windows': windows,
            'rms': round(_mean([float(value) for value in levels])) if levels else None,
            'zero_crossings_permille': round(_mean([float(value) for value in crossings])) if crossings else None}


def _interior(facts, span):
    """Windows wholly inside ``span`` so boundary partials do not mask a hole."""
    inside = [window for window in facts['windows']
              if window['start_us'] >= span.start and window['end_us'] <= span.end]
    return inside or facts['windows']


def _sound_relation(anchor_facts, anchor_span, position_facts, position_span, shift_us):
    left, right = anchor_facts['state'], position_facts['state']
    if left == 'unselected' or right == 'unselected':
        return 'unselected', None
    if left in ('missing', 'partial') or right in ('missing', 'partial'):
        if left in ('missing', 'partial') and right in ('missing', 'partial'):
            return 'both-incomplete', None
        return ('anchor-incomplete' if left in ('missing', 'partial') else 'position-incomplete'), None
    if left == 'silent' or right == 'silent':
        return ('both-silent', 1000) if left == right == 'silent' else ('level-differs', 0)
    first = {window['start_us']: window for window in _interior(anchor_facts, anchor_span)}
    second = {window['start_us']: window for window in _interior(position_facts, position_span)}
    keys = sorted(second)
    scores = []
    for begin, anchor_window in first.items():
        shifted = begin + shift_us
        position = bisect_left(keys, shifted)
        choices = [index for index in (position - 1, position) if 0 <= index < len(keys)]
        if not choices:
            continue
        match_key = min((keys[index] for index in choices),
                        key=lambda key: abs(key - shifted))
        if abs(match_key - shifted) > AUDIO_STEP:
            continue
        match = second[match_key]
        for a, b in zip(anchor_window['channels'], match['channels']):
            if a['rms'] is None or b['rms'] is None:
                continue
            crossings = 1000 - min(1000, abs((a['zero_crossings_permille'] or 0)
                                            - (b['zero_crossings_permille'] or 0)) * 4)
            loudest = max(a['rms'], b['rms'])
            level = round(min(a['rms'], b['rms']) * 1000 / loudest) if loudest else 1000
            scores.append(min(crossings, level))
    if not scores:
        return 'incomparable', None
    score = round(_mean(scores))
    return ('similar' if score >= AUDIO_SIMILAR_PERMILLE else 'differs'), score


class _Timeline:
    def __init__(self, audio):
        self.audio = audio
        self.frames = []
        self.previous = None

    def picture(self, picture):
        motion = 0.0 if self.previous is None else _mean(
            [abs(a - b) for a, b in zip(self.previous, picture.pixels)])
        self.frames.append({'timestamp_us': picture.timestamp_us,
                            'duration_us': picture.duration_us,
                            'signature': _profile(picture.pixels),
                            'motion': motion,
                            'luma': round(sum(picture.pixels) / len(picture.pixels), 2)})
        self.previous = picture.pixels

    def sound(self, pcm):
        self.audio.push(pcm)


def _seed_pairs(frames, still_frames):
    tables = [defaultdict(list) for _ in range(TABLE_COUNT)]
    for index, frame in enumerate(frames):
        if frame['motion'] <= MOTION_SEED or index in still_frames:
            continue
        for number, table in enumerate(_TABLES):
            tables[number][_key(frame['signature'], table)].append(index)
    seen = set()
    emitted = 0
    for table in tables:
        for bucket in table.values():
            if len(bucket) < 2:
                continue
            entries = bucket[:BUCKET_LIMIT]
            for position, first in enumerate(entries):
                for second in entries[position + 1:]:
                    if (first, second) in seen:
                        continue
                    seen.add((first, second))
                    yield first, second
                    emitted += 1
                    if emitted >= PAIR_LIMIT:
                        return


def _extend(frames, first, second, minimum, covered, still_frames):
    if _similarity(frames[first]['signature'], frames[second]['signature']) < minimum:
        return None

    def usable(first_index, second_index):
        return (first_index not in still_frames and second_index not in still_frames
                and _similarity(frames[first_index]['signature'],
                                frames[second_index]['signature']) >= minimum)

    low = 0
    while first - low - 1 >= 0 and second - low - 1 >= 0:
        if not usable(first - low - 1, second - low - 1):
            break
        low += 1
    high = 0
    while first + high + 1 < len(frames) and second + high + 1 < len(frames):
        if not usable(first + high + 1, second + high + 1):
            break
        high += 1
    start_first, start_second = first - low, second - low
    end_first, end_second = first + high + 1, second + high + 1
    if end_first > start_second:
        return None
    for offset in range(end_first - start_first):
        covered.add((start_first + offset, start_second + offset))
    return start_first, end_first, start_second, end_second


def _runs(frames, minimum, still_frames):
    covered = set()
    runs = []
    for first, second in _seed_pairs(frames, still_frames):
        if (first, second) in covered:
            continue
        extended = _extend(frames, first, second, minimum, covered, still_frames)
        if extended is None:
            continue
        start_first, end_first, start_second, end_second = extended
        duration = sum(frame['duration_us'] for frame in frames[start_first:end_first])
        active = sum(1 for frame in frames[start_first:end_first]
                     if frame['motion'] > MOTION_SEED) / (end_first - start_first)
        strength = _mean([frame['motion'] for frame in frames[start_first:end_first]])
        if duration < RUN_MIN_US or active < ACTIVE_FRACTION or strength < MOTION_MIN:
            continue
        runs.append({'first_start': start_first, 'first_end': end_first,
                     'second_start': start_second, 'second_end': end_second,
                     'offset': start_second - start_first})
    by_offset = defaultdict(list)
    for run in runs:
        by_offset[run['offset']].append(run)
    merged = []
    for group in by_offset.values():
        group.sort(key=lambda item: item['first_start'])
        current = None
        for run in group:
            if current is not None and run['first_start'] - current['first_end'] <= GAP_FRAMES:
                current['first_end'] = max(current['first_end'], run['first_end'])
                current['second_end'] = max(current['second_end'], run['second_end'])
                continue
            if current is not None:
                merged.append(current)
            current = dict(run)
        if current is not None:
            merged.append(current)
    return merged


def _frame_span(frames, start, end):
    last = frames[end - 1]
    return Span(frames[start]['timestamp_us'], last['timestamp_us'] + last['duration_us'])


def _subtract(span, cuts):
    pieces = [span]
    for cut in cuts:
        remaining = []
        for piece in pieces:
            if cut.end <= piece.start or piece.end <= cut.start:
                remaining.append(piece)
                continue
            if piece.start < cut.start:
                remaining.append(Span(piece.start, cut.start))
            if cut.end < piece.end:
                remaining.append(Span(cut.end, piece.end))
        pieces = remaining
    return [piece for piece in pieces if piece.duration >= RUN_MIN_US]


def _occurrences(frames, runs, stills):
    intervals = [_frame_span(frames, run['first_start'], run['first_end']) for run in runs]
    intervals += [_frame_span(frames, run['second_start'], run['second_end']) for run in runs]
    intervals.sort()
    merged = []
    for span in intervals:
        if merged and span.start - merged[-1].end <= MERGE_US:
            merged[-1] = Span(merged[-1].start, max(merged[-1].end, span.end))
        else:
            merged.append(span)
    cuts = [stretch['span'] for stretch in stills]
    pieces = []
    for span in merged:
        pieces.extend(_subtract(span, cuts))
    pieces.sort()
    merged = []
    for span in pieces:
        if merged and span.start <= merged[-1].end:
            merged[-1] = Span(merged[-1].start, max(merged[-1].end, span.end))
        else:
            merged.append(span)
    return merged


def _still_stretches(frames):
    stretches = []
    start = None
    for index in range(1, len(frames)):
        still = (frames[index]['motion'] <= STILL_MOTION
                 and _similarity(frames[index]['signature'], frames[index - 1]['signature']) * 1000
                 >= STILL_SIMILARITY)
        if still and start is None:
            start = index - 1
        elif not still and start is not None:
            stretches.append((start, index))
            start = None
    if start is not None:
        stretches.append((start, len(frames)))
    result = []
    for begin, end in stretches:
        span = _frame_span(frames, begin, end)
        if span.duration >= RUN_MIN_US:
            result.append({'start': begin, 'end': end, 'span': span,
                           'signature': frames[(begin + end) // 2]['signature']})
    return result


def _still_groups(stretches):
    groups = []
    for stretch in stretches:
        for group in groups:
            if _similarity(stretch['signature'], group[0]['signature']) * 1000 >= STILL_SIMILARITY:
                group.append(stretch)
                break
        else:
            groups.append([stretch])
    return [group for group in groups if len(group) >= 2]


def _hits_inside(hits, span):
    return [{'id': hit['id'], 'kind': hit['kind'], 'channel': hit['channel'],
             'start_us': hit['start_us'], 'end_us': hit['end_us'],
             'status': hit['status'], 'ruling': hit.get('ruling')}
            for hit in hits
            if hit['start_us'] < span.end and span.start < hit['end_us']]


def _alignment(frames, anchor, position):
    """Locate the anchor clip inside a repeated position, frame by frame.

    A repeat may enter a little early or late in one recording, so the search
    looks around the expected offset instead of trusting occurrence bounds.
    """
    anchor_indexes = [index for index, frame in enumerate(frames)
                      if anchor.start <= frame['timestamp_us'] < anchor.end]
    position_indexes = [index for index, frame in enumerate(frames)
                        if position.start <= frame['timestamp_us'] < position.end]
    fallback = {'offset': 0, 'delta': 0, 'shift_us': position.start - anchor.start, 'similarity': 0.0}
    if not anchor_indexes or not position_indexes:
        return fallback
    expected = position_indexes[0] - anchor_indexes[0]
    low, high = position_indexes[0], position_indexes[-1]
    best = None
    for delta in range(-ALIGN_FRAMES, ALIGN_FRAMES + 1):
        offset = expected + delta
        targets = [index + offset for index in anchor_indexes]
        if any(target < low or target > high for target in targets):
            continue
        scores = [_similarity(frames[index]['signature'], frames[target]['signature'])
                  for index, target in zip(anchor_indexes, targets)]
        value = _mean(scores)
        if best is None or value > best[1]:
            best = (offset, value)
    if best is None:
        return fallback
    offset, value = best
    shift = frames[anchor_indexes[0] + offset]['timestamp_us'] - frames[anchor_indexes[0]]['timestamp_us']
    return {'offset': offset, 'delta': offset - expected, 'shift_us': shift, 'similarity': value}


def _lead(frames, audio, anchor, occurrence, hits, minimum):
    alignment = _alignment(frames, anchor, occurrence)
    anchor_indexes = [index for index, frame in enumerate(frames)
                      if anchor.start <= frame['timestamp_us'] < anchor.end]
    scores, deviations = [], []
    for index in anchor_indexes:
        target = index + alignment['offset']
        if not 0 <= target < len(frames):
            continue
        other = frames[target]
        score = _similarity(frames[index]['signature'], other['signature'])
        scores.append(score)
        if score * 1000 < minimum:
            deviations.append({'timestamp_us': other['timestamp_us'],
                               'anchor_timestamp_us': frames[index]['timestamp_us'],
                               'picture_similarity_permille': round(score * 1000),
                               'local': _hits_inside(hits, Span(other['timestamp_us'],
                                                                other['timestamp_us'] + 1)),
                               'mean_luma': other['luma']})
    anchor_sound = _sound_facts(audio, anchor)
    position_sound = _sound_facts(audio, occurrence)
    relation, score = _sound_relation(anchor_sound, anchor, position_sound, occurrence,
                                      alignment['shift_us'])
    return {
        'start_us': occurrence.start,
        'end_us': occurrence.end,
        'relation': 'picture-repeat',
        'same_anomaly': False,
        'picture_similarity_permille': round(_mean(scores) * 1000) if scores else 0,
        'matched_frames': len(scores),
        'frame_offset': alignment['delta'],
        'deviations': deviations[:DEVIATION_LIMIT],
        'deviation_count': len(deviations),
        'sound_relation': relation,
        'sound_similarity_permille': score,
        'local': {'findings': _hits_inside(hits, occurrence), 'sound': position_sound},
        'anchor_facts': {'sound': anchor_sound},
    }


def _still_entry(frames, group, hits):
    inner = [found for stretch in group for found in _hits_inside(hits, stretch['span'])]
    return {'kind': 'still-picture', 'similarity_permille': 1000,
            'local': {'findings': inner},
            'occurrences': [{'start_us': stretch['span'].start,
                             'end_us': stretch['span'].end,
                             'frames': len(frames[stretch['start']:stretch['end']])}
                            for stretch in group],
            'note': 'Identical still picture at several positions, such as a station ident repeated at '
                    'each programme head. It is listed for reference only, is excluded from content '
                    'repeats and never inherits a finding or ruling.'}


def locate(path, scan, anchor_hit, hits, minimum_permille, minimum_ms):
    tracks = scan['tracks']
    audio = _Audio(tracks['sound']['rate'], tracks['sound']['channels']) if tracks['sound'] else None
    timeline = _Timeline(audio)
    for block in decode(path, tracks):
        if isinstance(block, Picture):
            timeline.picture(block)
        elif audio is not None:
            timeline.sound(block)
    frames = timeline.frames
    response = {
        'scan_id': scan['id'],
        'anchor': {'hit_id': anchor_hit['id'], 'kind': anchor_hit['kind'],
                   'channel': anchor_hit['channel'],
                   'start_us': anchor_hit['start_us'], 'end_us': anchor_hit['end_us'],
                   'status': anchor_hit.get('status'), 'content': None, 'still': None},
        'positions': [],
        'excluded': [],
        'parameters': {'minimum_permille': minimum_permille, 'minimum_ms': minimum_ms},
        'source': {'picture_frames': len(frames), 'duration_us': scan['extent']['end_us']},
        'truncated': False,
        'adjudication': 'independent',
        'same_anomaly': False,
        'note': 'Shared content is an association lead only. Each position keeps its own findings, '
                'status, ruling and audio facts; a repeat is never treated as the anchor anomaly.',
    }
    if not frames:
        return response
    hit_span = Span(anchor_hit['start_us'], anchor_hit['end_us'])
    stills = _still_stretches(frames)
    groups = _still_groups(stills)
    still_frames = {index for stretch in stills
                    for index in range(stretch['start'], stretch['end'])}
    response['excluded'] = [_still_entry(frames, group, hits) for group in groups]
    runs = _runs(frames, minimum_permille / 1000, still_frames)
    occurrences = _occurrences(frames, runs, stills)
    anchor = next((span for span in occurrences
                   if span.start < hit_span.end and hit_span.start < span.end), None)
    if anchor is None:
        anchor = next((span for span in occurrences
                       if abs(span.start - hit_span.start) <= 500_000), None)
    still_overlap = [(min(stretch['span'].end, hit_span.end)
                      - max(stretch['span'].start, hit_span.start), stretch)
                     for stretch in stills]
    anchor_still = next((stretch for size, stretch in still_overlap
                         if size * 2 >= hit_span.duration), None)
    if anchor is None and anchor_still is not None:
        response['anchor']['still'] = {'start_us': anchor_still['span'].start,
                                       'end_us': anchor_still['span'].end,
                                       'note': 'The anchor lies on a still picture shared by several '
                                               'positions; no content repeat is derived from it.'}
        return response
    if anchor is None:
        return response
    response['anchor']['content'] = {'start_us': anchor.start, 'end_us': anchor.end,
                                     'sound': _sound_facts(audio, anchor)}
    for span in occurrences:
        if span is anchor or span.duration < minimum_ms * 1000:
            continue
        if span.start < anchor.end and anchor.start < span.end:
            continue
        if len(response['positions']) >= POSITION_LIMIT:
            response['truncated'] = True
            break
        response['positions'].append(_lead(frames, audio, anchor, span, hits, minimum_permille))
    response['positions'].sort(key=lambda item: item['start_us'])
    return response
