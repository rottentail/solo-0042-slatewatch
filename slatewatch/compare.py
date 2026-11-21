"""One-to-one time-overlap comparison for versions on the same programme clock."""
from collections import defaultdict
from fractions import Fraction

from .clock import Span


def lanes(hits):
    result = defaultdict(list)
    for hit in hits:
        result[(hit['kind'], hit['channel'])].append(hit)
    return result


def pairs(before, after, minimum):
    left_lanes, right_lanes = lanes(before), lanes(after)
    for lane, left_group in left_lanes.items():
        right_group = right_lanes.get(lane, [])
        right_start = 0
        for left in left_group:
            old_span = Span.parse(left)
            while right_start < len(right_group) and right_group[right_start]['end_us'] <= old_span.start:
                right_start += 1
            for index in range(right_start, len(right_group)):
                right = right_group[index]
                if right['start_us'] >= old_span.end:
                    break
                new_span = Span.parse(right)
                shared = old_span.intersect(new_span)
                if not shared:
                    continue
                overlap = Fraction(shared.duration, old_span.duration + new_span.duration - shared.duration)
                if overlap >= minimum:
                    yield overlap, left, right


def compare(before, after, minimum_permille):
    scored = sorted(pairs(before['hits'], after['hits'], Fraction(minimum_permille, 1000)),
                    key=lambda row: (-row[0], row[1]['start_us'], row[2]['start_us'], row[1]['id'], row[2]['id']))
    used_left, used_right, matches = set(), set(), []
    for ratio, left, right in scored:
        if left['id'] in used_left or right['id'] in used_right:
            continue
        used_left.add(left['id'])
        used_right.add(right['id'])
        matches.append({'before': left, 'after': right, 'overlap_permille': int(ratio * 1000),
                        'start_delta_us': right['start_us'] - left['start_us'],
                        'end_delta_us': right['end_us'] - left['end_us']})
    return {'before_scan': before['id'], 'after_scan': after['id'], 'matches': matches,
            'resolved': [h for h in before['hits'] if h['id'] not in used_left],
            'introduced': [h for h in after['hits'] if h['id'] not in used_right],
            'minimum_permille': minimum_permille}
