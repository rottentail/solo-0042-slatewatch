"""Render a scan alongside its live adjudication and selected finding."""

from . import library, timeline
from .render import fragment, page
from .text import KINDS, OUTCOMES, badge, interval, lane, safe


def home(vault):
    records = vault.recent('scan', limit=1)
    if not records:
        return None
    return records[0]['id']


def rows(scan_id, findings, selected):
    return ''.join(
        f'<tr{" class=selected" if hit["id"] == selected else ""}>'
        f'<th scope="row"><a href="/desk/scans/{scan_id}?hit={hit["id"]}#finding">{KINDS[hit["kind"]]}</a></th>'
        f'<td>{lane(hit)}</td><td class="time">{interval(hit)}</td><td>{badge(hit["status"])}</td></tr>'
        for hit in findings
    ) or '<tr><td colspan="4">没有技术异常。</td></tr>'


def note(ruling):
    if not ruling:
        return '尚无人工裁决'
    return (f'第 {ruling["seq"]} 条 · {safe(ruling["reviewer"])} · '
            f'{OUTCOMES[ruling["outcome"]]} · {safe(ruling["reason"])}')


def notice(hit):
    ruling = hit['ruling']
    if ruling and ruling['outcome'] in ('accept', 'hold'):
        lead = (f'正在覆写第 {ruling["seq"]} 条裁决：{safe(ruling["reviewer"])} 记录为'
                f'「{OUTCOMES[ruling["outcome"]]}」，理由：{safe(ruling["reason"])}。')
    elif ruling:
        lead = f'第 {ruling["seq"]} 条裁决已撤回，候选回到规则判定。'
    else:
        lead = '候选新增了适用的制作意图。'
    marks = '、'.join(f'#{seq}' for seq in hit['intent_sequences'])
    covered = f'<p>当前适用的制作意图：{marks}</p>' if marks else ''
    return (f'<div class="notice" role="alert"><p><strong>裁决依据已变化，本次结论未保存。</strong>{lead}</p>'
            f'{covered}<p>核对当前依据后，勾选确认并重新记录裁决。</p></div>')


def ruling_form(hit, attempt):
    state = {'notice': '', 'confirm': '', 'reviewer': '', 'reason': '', 'open': '',
             'sel_accept': '', 'sel_hold': '', 'sel_reset': ''}
    if attempt is None:
        return state
    state['open'] = ' open'
    if attempt.get('outcome') in OUTCOMES:
        state[f"sel_{attempt['outcome']}"] = ' selected'
    state['reviewer'] = safe(attempt.get('reviewer') or '')
    state['reason'] = safe(attempt.get('reason') or '')
    state['notice'] = notice(hit)
    state['confirm'] = ('<label class="confirm"><input name="confirm" type="checkbox" value="yes" required>'
                        '已核对当前依据，确认覆写并记录</label>')
    return state


def details(scan, hit, attempt=None):
    if not hit:
        return fragment('clear', scan=scan['id'])
    intent_form = ''
    if hit['kind'] in ('black', 'freeze', 'silence'):
        intent_form = fragment('intent', scan=scan['id'], hit=hit['id'],
                               start=hit['start_us'], end=hit['end_us'])
    return fragment('finding', scan=scan['id'], hit=hit['id'], title=KINDS[hit['kind']],
                    lane=lane(hit), span=interval(hit), status=badge(hit['status']),
                    note=note(hit['ruling']), intent_form=intent_form, basis=hit['basis'],
                    **ruling_form(hit, attempt))


def show(app, scan_id, selected, attempt=None):
    scan = app.vault.get('scan', scan_id)
    master = app.vault.get('master', scan['master_id'])
    view = app.review(scan_id)
    hit = next((item for item in view['hits'] if item['id'] == selected), None)
    if selected and hit is None:
        from ..clock import Missing

        raise Missing('finding does not belong to the selected scan')
    hit = hit or next(iter(view['hits']), None)
    active = hit['id'] if hit else ''
    return page('审片', 'scan', 'scan', sidebar=library.sidebar(app.vault, scan_id),
                master=safe(master['label']), policy=safe(library.policy_name(app.vault, scan)), state=badge(view['status']),
                source=safe(master['sha256'][:12]), clip=timeline.draw(scan, active),
                table=rows(scan_id, view['hits'], active), detail=details(scan, hit, attempt))
