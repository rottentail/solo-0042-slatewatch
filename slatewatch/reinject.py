"""Re-inject field hold marks written against a low-bitrate proxy into a master scan.

代理审片与母带扫描不共享容器时标：代理可能被首尾切除（整体平移）、更换伴音轨
（声道编号变化）或在中段出现时间漂移。本模块只做只读的同一性判定，给出建议、
冲突与无法证明同一性的段，绝不直接改动既有裁决。所有换算依据保留在结果中，
供状况变化后重新对照。
"""
from statistics import median

from .clock import InputError, LIMIT, Span, integer, shape, text
from .rules import CHANNEL_KINDS, KINDS, identifier
from .review import adjudicate

OUTCOMES = {'accept', 'hold'}
# 建议匹配至少要保留 30% 的交并比，避免把相邻但不同的候选错认成同一事件。
MIN_LOCATE_PERMILLE = 300
# 两个候选得分接近时无法二选一，按不能证明同一性处理。
AMBIGUITY_PERMILLE = 50
# 共模平移判定：各条最优平移相对中位数的偏差不超过该值才视为首尾切除。
GLOBAL_OFFSET_TOLERANCE_US = 50_000
# 首尾切除造成的整体平移默认容差与上限（微秒）。
DEFAULT_DRIFT_US = 1_500_000
MAX_DRIFT_US = 5_000_000
# 单条代理标注的长度与编号上限，防止异常输入拖垮配对。
MAX_MARKS = 60
MAX_REFERENCE = 40


def review_state(scan, vault):
    """当前母带扫描的复核投影，用于冻结基线与再次对照。"""
    intents, rulings = vault.timeline(scan['id'])
    return adjudicate(scan, intents, rulings)


def _check_mark(scan, mark, index, drift):
    prefix = f'marks[{index}]'
    shape(mark, ('kind', 'start_us', 'end_us'), ('channel', 'reference', 'note', 'outcome'))
    kind = mark['kind']
    if not isinstance(kind, str) or kind not in KINDS:
        raise InputError(f'{prefix}.kind must be one of the measured finding kinds')
    # 代理标注运行在代理自身时钟上：首尾切除后标注可能带小幅负时刻，允许落在
    # [-漂移容差, 通用上限] 内；能否对回母带由配对阶段判定。
    start = integer(mark['start_us'], 'start_us', -drift, LIMIT)
    end = integer(mark['end_us'], 'end_us', start + 1, LIMIT)
    span = Span(start, end)
    channel = mark.get('channel')
    if kind in CHANNEL_KINDS:
        sound = scan['tracks']['sound']
        if channel is None:
            raise InputError(f'{prefix}.channel is required for {kind} marks')
        if not sound:
            raise InputError(f'{prefix} cannot be located: the master scan selected no sound lane')
        integer(channel, f'{prefix}.channel', 1, sound['channels'])
    elif channel is not None:
        raise InputError(f'{prefix}.channel only applies to sound findings')
    reference = mark.get('reference')
    if reference is not None:
        text(reference, f'{prefix}.reference', MAX_REFERENCE)
    note = mark.get('note')
    if note is not None:
        text(note, f'{prefix}.note', 350)
    outcome = mark.get('outcome', 'hold')
    if outcome not in OUTCOMES:
        raise InputError(f'{prefix}.outcome must be accept or hold')
    return {'kind': kind, 'channel': channel, **span.as_dict(),
            'reference': reference, 'note': note, 'outcome': outcome}


def parse_request(scan, data):
    """校验并规范化一次回灌请求；不读取也不修改任何裁决。"""
    shape(data, ('marks',), ('proxy_label', 'reviewer', 'note', 'drift_tolerance_us'))
    marks = data['marks']
    if not isinstance(marks, list) or not 1 <= len(marks) <= MAX_MARKS:
        raise InputError(f'provide 1 to {MAX_MARKS} proxy marks')
    drift = integer(data.get('drift_tolerance_us', DEFAULT_DRIFT_US),
                    'drift_tolerance_us', 0, MAX_DRIFT_US)
    checked = [_check_mark(scan, mark, index, drift) for index, mark in enumerate(marks)]
    refs = [mark['reference'] for mark in checked if mark['reference'] is not None]
    if len(refs) != len(set(refs)):
        raise InputError('proxy reference numbers must be unique within one re-injection')
    payload = {'marks': checked, 'drift_tolerance_us': drift,
               'proxy_label': text(data['proxy_label'], 'proxy_label') if data.get('proxy_label') else None,
               'reviewer': text(data['reviewer'], 'reviewer') if data.get('reviewer') else None,
               'note': text(data['note'], 'note', 350) if data.get('note') else None}
    return payload


def _compatible(mark, hit):
    if mark['kind'] != hit['kind']:
        return False
    if mark['channel'] is None:
        return hit['channel'] is None
    return hit['channel'] is not None


def _iou(left, right):
    shared = left.intersect(right)
    if shared is None:
        return 0.0
    return shared.duration / (left.duration + right.duration - shared.duration)


def _candidate_scores(mark, hits, drift):
    """同一指标/声道下，每个母带候选在各自最优平移下的得分。"""
    span = Span(mark['start_us'], mark['end_us'])
    scored = []
    for hit in hits:
        if not _compatible(mark, hit):
            continue
        hit_span = Span.parse(hit)
        implied = hit_span.start - span.start
        if abs(implied) > drift:
            continue
        shifted = Span(span.start + implied, span.end + implied)
        ratio = _iou(shifted, hit_span)
        if ratio * 1000 >= MIN_LOCATE_PERMILLE:
            scored.append({'hit': hit, 'permille': round(ratio * 1000),
                           'shift_us': implied,
                           'end_delta_us': hit_span.end - shifted.end})
    scored.sort(key=lambda item: (-item['permille'], abs(item['shift_us']), item['hit']['start_us']))
    return scored


def _global_shift(assignments):
    """首尾切除对应一个公共平移；用中位位移稳健估计，返回 None 表示漂移不共模。"""
    if not assignments:
        return None
    shifts = [item['shift_us'] for item in assignments]
    shift = int(round(median(shifts)))
    if any(abs(value - shift) > GLOBAL_OFFSET_TOLERANCE_US for value in shifts):
        return None
    return shift


def _greedy(rows):
    """按得分做稳定一对一分配，返回 {mark_index: 候选行}。"""
    ordered = sorted(rows, key=lambda row: (-row['permille'], abs(row['shift_us']),
                                            row['mark_index'], row['hit']['start_us']))
    used_marks, used_hits, chosen = set(), set(), {}
    for row in ordered:
        if row['mark_index'] in used_marks or row['hit']['id'] in used_hits:
            continue
        used_marks.add(row['mark_index'])
        used_hits.add(row['hit']['id'])
        chosen[row['mark_index']] = row
    return chosen


class _EmptyTimeline:
    """供不接触持久层的单元级调用复用复核投影。"""

    def timeline(self, scan_id):
        return [], []


def locate(scan, payload, vault=None):
    """把代理标注分成可定位建议、冲突与无法证明同一性三类。"""
    review = review_state(scan, vault or _EmptyTimeline())
    statuses = {item['id']: item['status'] for item in review['hits']}
    hits = scan['hits']
    drift = payload['drift_tolerance_us']
    per_mark, all_rows = [], []
    for index, mark in enumerate(payload['marks']):
        scored = _candidate_scores(mark, hits, drift)
        per_mark.append(scored)
        for row in scored:
            all_rows.append({'mark_index': index, **row})
    independent = _greedy(all_rows)
    common = _global_shift(list(independent.values()))
    if common is not None:
        shifted_rows = []
        for index, mark in enumerate(payload['marks']):
            span = Span(mark['start_us'], mark['end_us'])
            moved = Span(span.start + common, span.end + common)
            for hit in hits:
                if not _compatible(mark, hit):
                    continue
                ratio = _iou(moved, Span.parse(hit))
                if ratio * 1000 >= MIN_LOCATE_PERMILLE:
                    shifted_rows.append({'mark_index': index, 'hit': hit,
                                         'permille': round(ratio * 1000), 'shift_us': common,
                                         'end_delta_us': hit['end_us'] - moved.end})
        common_assign = _greedy(shifted_rows)
        # 共模平移能解释的标注不少于独立配对时才采用整体模型，否则按中段漂移逐条处理。
        if len(common_assign) >= len(independent):
            assignments, model = common_assign, 'global_offset'
        else:
            assignments, model = independent, 'per_mark_drift'
    else:
        assignments, model = independent, 'per_mark_drift'

    located, unproven = [], []
    matched_hit_ids = set()
    for index, mark in enumerate(payload['marks']):
        choice = assignments.get(index)
        scored = per_mark[index]
        ambiguous = (len(scored) > 1
                     and scored[0]['permille'] - scored[1]['permille'] <= AMBIGUITY_PERMILLE)
        if choice is None or ambiguous:
            unproven.append({'mark_index': index,
                             'reason': 'ambiguous_candidates' if ambiguous else _unproven_reason(scored),
                             'candidates': [_candidate_brief(row, statuses) for row in scored[:3]], **mark})
            continue
        hit = choice['hit']
        matched_hit_ids.add(hit['id'])
        current = statuses[hit['id']]
        entry = {'mark_index': index, 'mark': mark, 'hit_id': hit['id'], 'kind': hit['kind'],
                 'channel': hit['channel'], 'master_span': Span.parse(hit).as_dict(),
                 'proxy_span': {'start_us': mark['start_us'], 'end_us': mark['end_us']},
                 'overlap_permille': choice['permille'], 'shift_us': choice['shift_us'],
                 'end_delta_us': choice['end_delta_us'], 'alignment': model,
                 'master_status_at_check': current,
                 'classification': 'conflict' if _is_conflict(mark, current) else 'suggestion'}
        located.append(entry)

    master_only = [
        {'hit_id': hit['id'], 'kind': hit['kind'], 'channel': hit['channel'],
         **Span.parse(hit).as_dict(), 'status': statuses[hit['id']]}
        for hit in hits if hit['id'] not in matched_hit_ids
    ]
    return {'alignment_model': model, 'global_offset_us': common if model == 'global_offset' else None,
            'located': located, 'unproven': unproven, 'master_only': master_only,
            'counts': {'suggestion': sum(1 for item in located if item['classification'] == 'suggestion'),
                       'conflict': sum(1 for item in located if item['classification'] == 'conflict'),
                       'unproven': len(unproven), 'master_only': len(master_only)}}


def _is_conflict(mark, status):
    # 现场待扣留（hold）撞上母带侧已人工通过即冲突；其余为待人工确认的建议。
    if mark['outcome'] == 'hold' and status == 'accepted':
        return True
    if mark['outcome'] == 'accept' and status == 'held':
        return True
    return False


def _unproven_reason(scored):
    if not scored:
        return 'no_compatible_finding'
    return 'below_overlap_threshold'


def _candidate_brief(row, statuses):
    hit = row['hit']
    return {'hit_id': hit['id'], 'overlap_permille': row['permille'],
            'shift_us': row['shift_us'], 'status': statuses[hit['id']],
            **Span.parse(hit).as_dict()}


def baseline(scan, vault):
    """冻结预检当时的母带候选状态，作为状况变化后的复核对照。"""
    view = review_state(scan, vault)
    return [{'hit_id': item['id'], 'kind': item['kind'], 'channel': item['channel'],
             'start_us': item['start_us'], 'end_us': item['end_us'], 'status': item['status']}
            for item in view['hits']]


def build_report(scan, payload, vault):
    result = locate(scan, payload, vault)
    report = {'id': identifier('reinject', [scan['id'], payload, result['located'], result['unproven']]),
              'scan_id': scan['id'], 'master_id': scan['master_id'],
              'proxy_label': payload['proxy_label'], 'reviewer': payload['reviewer'],
              'note': payload['note'], 'drift_tolerance_us': payload['drift_tolerance_us'],
              'marks': payload['marks'], **result,
              'baseline': baseline(scan, vault)}
    return report


def confirmation_request(scan, report, data, current_statuses):
    """校验人工确认请求；实际裁决写入仍由 service 走既有只追加通道。"""
    shape(data, ('selections', 'reviewer', 'reason'), ('override',))
    selections = data['selections']
    if not isinstance(selections, list) or not 1 <= len(selections) <= MAX_MARKS:
        raise InputError(f'select 1 to {MAX_MARKS} located mark indexes')
    indexes = []
    for value in selections:
        integer(value, 'selections', 0, MAX_MARKS - 1)
        indexes.append(value)
    if len(set(indexes)) != len(indexes):
        raise InputError('selections must be unique')
    located = {entry['mark_index']: entry for entry in report['located']}
    override = bool(data.get('override', False))
    actions, conflicts = [], []
    for index in indexes:
        entry = located.get(index)
        if entry is None:
            raise InputError(f'mark {index} is not located in the master scan and cannot be applied')
        status = current_statuses[entry['hit_id']]
        mark = entry['mark']
        if _is_conflict(mark, status):
            conflicts.append(index)
            if not override:
                raise InputError(f'mark {index} conflicts with the existing ruling; confirm with override')
        actions.append({'mark_index': index, 'hit_id': entry['hit_id'], 'outcome': mark['outcome'],
                        'prior_status': status})
    return {'selections': indexes, 'actions': actions, 'override': override,
            'reviewer': text(data['reviewer'], 'reviewer'),
            'reason': text(data['reason'], 'reason', 350), 'conflict_indexes': conflicts}


def recheck(report, scan, vault, confirmations):
    """对照冻结基线，指出自预检以来母带侧状况的变化。"""
    view = review_state(scan, vault)
    current = {item['id']: item for item in view['hits']}
    baseline_states = {item['hit_id']: item['status'] for item in report['baseline']}
    changes = []
    for hit_id, old_status in baseline_states.items():
        item = current.get(hit_id)
        if item is None:
            changes.append({'hit_id': hit_id, 'baseline_status': old_status,
                            'current_status': None, 'change': 'finding_gone'})
        elif item['status'] != old_status:
            changes.append({'hit_id': hit_id, 'baseline_status': old_status,
                            'current_status': item['status'], 'change': 'status_changed'})
    located = {entry['mark_index']: entry for entry in report['located']}
    applied = {event['mark_index']: event for event in confirmations}
    marks = []
    for index, mark in enumerate(report['marks']):
        entry = located.get(index)
        if entry is None:
            marks.append({'mark_index': index, 'state': 'unproven'})
            continue
        event = applied.get(index)
        now = current.get(entry['hit_id'])
        marks.append({'mark_index': index, 'hit_id': entry['hit_id'],
                      'state': 'applied' if event else 'pending_confirmation',
                      'confirmation_seq': event['seq'] if event else None,
                      'current_status': now['status'] if now else None})
    return {'reinject_id': report['id'], 'scan_id': scan['id'], 'changes': changes,
            'marks': marks, 'confirmation_count': len(confirmations)}
