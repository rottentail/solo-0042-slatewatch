"""Browser pages for proxy re-injection precheck and later re-verification."""

from ..reinject import MAX_MARKS
from .library import scan_choices
from .render import page
from .text import KINDS, STATUS, safe, timestamp

CLASS_LABEL = {'suggestion': '建议', 'conflict': '冲突', 'unproven': '无法证明同一性'}
CLASS_STATE = {'suggestion': 'expected', 'conflict': 'held', 'unproven': 'pending'}
CHANGE_LABEL = {'status_changed': '结论已变化', 'finding_gone': '候选已不存在'}


def index(app, scan_id=''):
    reports = app.vault.recent('reinject')
    rows = []
    for item in reports:
        counts = item['counts']
        rows.append(
            f'<tr><th><a href="/desk/reinjects/{safe(item["id"])}">{safe(item["id"])}</a></th>'
            f'<td><a href="/desk/scans/{safe(item["scan_id"])}">{safe(item["scan_id"][-7:])}</a></td>'
            f'<td>{safe(item["proxy_label"] or "未命名代理")}</td>'
            f'<td>{counts["suggestion"]}</td><td>{counts["conflict"]}</td><td>{counts["unproven"]}</td></tr>'
        )
    table = ''.join(rows) or '<tr><td colspan="6">暂无回灌预检报告。</td></tr>'
    return page('回灌预检', 'reinjects', 'reinjects', choices=scan_choices(app.vault, scan_id),
                max_marks=MAX_MARKS, rows=table)


def _mark_brief(mark):
    channel = f' · 声道 {mark["channel"]}' if mark.get('channel') is not None else ''
    ref = f' · 现场编号 {safe(mark["reference"])}' if mark.get('reference') else ''
    return f'{KINDS[mark["kind"]]}{channel}{ref}'


def detail(app, key, message=''):
    report = app.vault.get('reinject', key)
    confirmations = {event['mark_index']: event for event in app.vault.reinject_timeline(key)}
    notice = f'<p role="status" class="good">{safe(message)}</p>' if message else ''
    located_rows = []
    for entry in report['located']:
        mark = entry['mark']
        state = CLASS_STATE[entry['classification']]
        label = CLASS_LABEL[entry['classification']]
        applied = confirmations.get(entry['mark_index'])
        action = (f'已确认（裁决事件 #{applied["ruling_seq"]}）' if applied
                  else _confirm_form(key, entry, report, label))
        located_rows.append(
            f'<tr><td><span class="state {state}">{label}</span></td>'
            f'<th>{_mark_brief(mark)}</th>'
            f'<td class="time">{timestamp(mark["start_us"])} – {timestamp(mark["end_us"])}</td>'
            f'<td><a class="time" href="/desk/scans/{safe(report["scan_id"])}?hit={safe(entry["hit_id"])}#finding">'
            f'{safe(entry["hit_id"][-7:])}</a><br><span class="muted">{STATUS[entry["master_status_at_check"]]}</span></td>'
            f'<td class="time">{entry["overlap_permille"]}‰<br><span class="muted">平移 {entry["shift_us"]} µs</span></td>'
            f'<td>{action}</td></tr>'
        )
    unproven_rows = ''.join(
        f'<tr><td><span class="state pending">无法证明同一性</span></td><th>{_mark_brief(mark)}</th>'
        f'<td class="time">{timestamp(mark["start_us"])} – {timestamp(mark["end_us"])}</td>'
        f'<td colspan="3">{safe(mark.get("note") or "")} '
        f'<span class="muted">{_reason(mark["reason"])}</span></td></tr>'
        for mark in report['unproven']
    ) or '<tr><td colspan="6" class="muted">没有无法证明同一性的段。</td></tr>'
    return page('回灌预检详情', 'reinjects', 'reinject_detail',
                key=safe(key), scan=safe(report['scan_id']),
                proxy=safe(report['proxy_label'] or '未命名代理'),
                reviewer=safe(report['reviewer'] or '未登记'),
                model='整体平移' if report['alignment_model'] == 'global_offset' else '逐段漂移',
                offset=safe(report['global_offset_us']), notice=notice,
                located=''.join(located_rows) or '<tr><td colspan="6" class="muted">没有可定位的段。</td></tr>',
                unproven=unproven_rows)


def _reason(code):
    return {
        'no_compatible_finding': '母带扫描中没有同指标、同声道候选',
        'below_overlap_threshold': '平移后交并比仍低于定位阈值',
        'ambiguous_candidates': '多个候选得分接近，无法一对一确认',
    }.get(code, code)


def _confirm_form(key, entry, report, label):
    override = '<input name="override" value="1" type="hidden">' if label == '冲突' else ''
    return (f'<form method="post" action="/desk/reinjects/{safe(key)}/confirmations" class="actions">'
            f'<input name="selections" value="{entry["mark_index"]}" type="hidden">{override}'
            f'<input name="reviewer" placeholder="审片员" required maxlength="120">'
            f'<input name="reason" placeholder="确认理由" required maxlength="350">'
            f'<button class="primary">人工确认</button></form>')


def recheck_page(app, key):
    result = app.reinject_recheck(key)
    change_rows = ''.join(
        f'<tr><td><code>{safe(item["hit_id"])}</code></td><td>{STATUS[item["baseline_status"]]}</td>'
        f'<td>{STATUS[item["current_status"]] if item["current_status"] else "—"}</td>'
        f'<td>{CHANGE_LABEL[item["change"]]}</td></tr>'
        for item in result['changes']
    ) or '<tr><td colspan="4" class="muted">自预检以来母带侧状况没有变化。</td></tr>'
    mark_rows = ''.join(
        f'<tr><td>{item["mark_index"]}</td>'
        f'<td><code>{safe(item.get("hit_id") or "—")}</code></td>'
        f'<td>{"已确认" if item["state"] == "applied" else "待人工确认" if item["state"] == "pending_confirmation" else "无法证明"}</td>'
        f'<td>{STATUS[item["current_status"]] if item.get("current_status") else "—"}</td></tr>'
        for item in result['marks']
    )
    return page('回灌复核对照', 'reinjects', 'reinject_recheck', key=safe(key),
                scan=safe(result['scan_id']), changes=change_rows, marks=mark_rows,
                count=result['confirmation_count'])
