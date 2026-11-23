"""Render the chapter reference track and its bounded browser forms."""

import json

from ..clock import InputError
from .render import fragment
from .text import KINDS, interval, safe, timestamp


def _origin(chapter):
    if chapter['state'] != 'determined':
        return '候选范围（画面/整卷两读均可取）'
    label = '节目画面起点' if chapter['origin'] == 'picture' else '播放器整卷零点'
    declared = '' if chapter['declared_origin'] is None else '（章节表已声明）'
    return label + declared


def _hit_link(scan_id, hit):
    return (f'<a class="time" href="/desk/scans/{scan_id}?hit={hit["id"]}#finding">'
            f'{safe(KINDS[hit["kind"]])} · {safe(interval(hit))}</a>')


def _available(scan_id, chapter):
    hits = chapter['available_hits']
    if not hits:
        return '<span class="muted">该位置没有可取的技术异常。</span>'
    return '<ul class="hitlinks">' + ''.join(
        f'<li>{_hit_link(scan_id, hit)}</li>' for hit in hits) + '</ul>'


def _link_cell(scan_id, chapter):
    link = chapter['link']
    if link is None:
        return '<span class="muted">未关联异常</span>'
    if link['status'] == 'unresolved':
        return (f'<span class="state pending">旧关联已失效</span>'
                f'<code class="block">{safe(link["hit_id"])}</code>'
                f'<p class="muted">该异常不属于本扫描，未自动改挂到其它异常。</p>')
    return _hit_link(scan_id, link['hit'])


def _rows(scan_id, chapters, state):
    rows = []
    for chapter in chapters:
        if chapter['state'] != state:
            continue
        if state == 'determined':
            when = timestamp(chapter['time_us'])
        else:
            when = f'{timestamp(chapter["range"]["start_us"])} – {timestamp(chapter["range"]["end_us"])}'
        rows.append(
            '<tr>'
            f'<th scope="row">{safe(chapter["title"])}<small class="block">第 {chapter["seq"]} 节</small></th>'
            f'<td class="time">{safe(when)}</td>'
            f'<td>{safe(_origin(chapter))}</td>'
            f'<td>{_link_cell(scan_id, chapter)}</td>'
            f'<td>{_available(scan_id, chapter)}</td>'
            '</tr>')
    return ''.join(rows)


def _link_forms(scan_id, chapters):
    forms = []
    for chapter in chapters:
        options = '<option value="">不关联异常</option>' + ''.join(
            f'<option value="{safe(hit["id"])}"'
            f'{" selected" if chapter["link"] and chapter["link"].get("hit_id") == hit["id"] else ""}>'
            f'{safe(KINDS[hit["kind"]])} · {safe(interval(hit))}</option>'
            for hit in chapter['available_hits'])
        forms.append(fragment(
            'chapter_link', scan=scan_id, seq=chapter['seq'], title=safe(chapter['title']), options=options))
    return ''.join(forms)


def panel(app, scan_id):
    track = app.chapter_track(scan_id)
    if track['sheet_id'] is None:
        return fragment('chapters_empty', scan=scan_id)
    chapters = track['chapters']
    determined = _rows(scan_id, chapters, 'determined') or '<tr><td colspan="5" class="muted">没有已确定章节。</td></tr>'
    candidates = _rows(scan_id, chapters, 'candidate') or '<tr><td colspan="5" class="muted">没有候选范围章节。</td></tr>'
    unresolved = ''
    if track['unresolved_links']:
        items = ''.join(
            f'<li>第 {link["chapter_seq"]} 节 → <code>{safe(link["hit_id"])}</code>'
            f'<span class="muted">（{safe(link["reason"])}）</span></li>'
            for link in track['unresolved_links'])
        unresolved = f'<div class="unresolved" role="status"><strong>未能承接的旧关联</strong><ul>{items}</ul></div>'
    return fragment(
        'chapters',
        scan=scan_id, head=timestamp(track['head_offset_us']), sheet=safe(track['sheet_id']),
        determined=determined, candidates=candidates, unresolved=unresolved,
        link_forms=_link_forms(scan_id, chapters))


def parse_sheet_form(data):
    """Decode the browser JSON textarea into the same payload the JSON API accepts."""
    raw = data.get('sheet_json', '')
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise InputError('章节表必须是合法 JSON。') from exc
    if not isinstance(payload, dict) or 'chapters' not in payload:
        raise InputError('章节表需包含 chapters 数组。')
    return payload
