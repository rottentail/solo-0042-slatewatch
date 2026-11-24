"""Render a scan alongside its live adjudication and selected finding."""

from . import handoff, library, timeline
from .render import fragment, page
from .text import KINDS, badge, interval, lane, safe


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


def details(scan_id, view, hit):
    if not hit:
        return fragment('clear', scan=scan_id)
    intent_form = ''
    if hit['kind'] in ('black', 'freeze', 'silence'):
        intent_form = fragment('intent', scan=scan_id, hit=hit['id'],
                               start=hit['start_us'], end=hit['end_us'])
    ruling = hit['ruling']
    note = f'{safe(ruling["reviewer"])} · {safe(ruling["reason"])}' if ruling else '尚无人工裁决'
    history = [event for event in view['rulings'] if event['hit_id'] == hit['id']]
    trail = ''.join(
        f'<li><code>{safe(event["outcome"])}</code> · {safe(event["reviewer"])} · '
        f'{safe(event["reason"])} <small>事件 #{event["seq"]}</small></li>'
        for event in history
    ) or '<li>尚无裁决记录。</li>'
    return fragment('finding', scan=scan_id, hit=hit['id'], title=KINDS[hit['kind']],
                    lane=lane(hit), span=interval(hit), status=badge(hit['status']),
                    note=note, intent_form=intent_form,
                    segments=handoff.basis_rows(scan_id, hit), ruling_trail=trail)


def show(app, scan_id, selected):
    scan = app.vault.get('scan', scan_id)
    master = app.vault.get('master', scan['master_id'])
    view = app.review(scan_id)
    hit = next((item for item in view['hits'] if item['id'] == selected), None)
    if selected and hit is None:
        from ..clock import Missing

        raise Missing('finding does not belong to the selected scan')
    hit = hit or next(iter(view['hits']), None)
    intent_panel = handoff.panel(scan_id, hit, view['intents']) if hit else ''
    return page('审片', 'scan', 'scan', sidebar=library.sidebar(app.vault, scan_id),
                master=safe(master['label']), policy=safe(library.policy_name(app.vault, scan)), state=badge(view['status']),
                source=safe(master['sha256'][:12]), clip=timeline.draw(scan, hit['id'] if hit else ''),
                table=rows(scan_id, view['hits'], hit['id'] if hit else ''),
                detail=details(scan_id, view, hit), intent_panel=intent_panel)
