"""Intent ledger, corrections and segment-level evidence basis for the browser desk."""

from .render import fragment
from .text import BASIS, INTENT_STATE, KINDS, interval, safe, timestamp


def basis_rows(scan_id, hit):
    rows = []
    for segment in hit['segments']:
        marks = '、'.join(f'#{number}' for number in segment['intent_sequences']) or '—'
        rows.append(
            f'<tr class="basis {safe(segment["basis"])}"><td>{BASIS[segment["basis"]]}</td>'
            f'<td class="time">{interval(segment)}</td><td>{safe(marks)}</td></tr>'
        )
    return fragment(
        'segments', scan=safe(scan_id), hit=safe(hit['id']),
        rows=''.join(rows) or '<tr><td colspan="3">该候选没有可分解的时标片段。</td></tr>',
    )


def ledger_rows(scan_id, marks):
    rows = []
    for mark in marks:
        title = KINDS[mark['kind']]
        origin = f'更正自 #{mark["corrected_from_seq"]}' if mark['corrected_from_seq'] else '原始登记'
        record = safe(mark.get('record') or '未注明制作记录')
        rows.append(
            f'<tr class="ledger {safe(mark["state"])}"><th scope="row">#{mark["seq"]}</th>'
            f'<td>{safe(title)}</td><td class="time">{interval(mark)}</td>'
            f'<td><span class="state {safe(mark["state"])}">{INTENT_STATE[mark["state"]]}</span></td>'
            f'<td>{record}<small>{safe(origin)} · {safe(mark["reason"])}</small></td></tr>'
        )
    return fragment('ledger', scan=safe(scan_id), rows=''.join(rows) or '<tr><td colspan="5">暂无制作意图。</td></tr>')


def correction_form(scan_id, hit, marks):
    active = [mark for mark in marks if mark['state'] == 'active' and mark['kind'] == hit['kind']]
    options = ''.join(
        f'<option value="{mark["seq"]}">#{mark["seq"]} · {timestamp(mark["start_us"])} – '
        f'{timestamp(mark["end_us"])} · {safe(mark.get("record") or mark["reason"])}</option>'
        for mark in active
    )
    if not options:
        return ''
    return fragment('correction', scan=safe(scan_id), hit=safe(hit['id']), options=options)


def panel(scan_id, hit, marks):
    return fragment('intents', ledger=ledger_rows(scan_id, marks),
                    correction=correction_form(scan_id, hit, marks))
