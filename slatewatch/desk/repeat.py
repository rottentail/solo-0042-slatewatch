"""Show other positions that reuse one finding's picture, without merging verdicts."""

from ..clock import InputError, Missing
from .render import page
from .text import KINDS, badge, interval, safe, timestamp

SOUND = {
    'similar': '伴音相近',
    'differs': '伴音不同',
    'both-incomplete': '两处均无完整伴音',
    'anchor-incomplete': '基准处伴音不完整',
    'position-incomplete': '此处伴音不完整',
    'both-silent': '两处均为数字静音',
    'level-differs': '伴音电平不同',
    'unselected': '未选伴音',
    'incomparable': '无法并比伴音',
}


def number(params, key, default):
    raw = params.get(key, '')
    if not raw:
        return default
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 7:
        raise InputError('比对门槛需要非负整数字面值。')
    return int(raw)


def findings(scan_id, items):
    if not items:
        return '<span class="muted">无本地候选</span>'
    links = []
    for hit in items:
        channel = f' · 伴音 {hit["channel"]}' if hit['channel'] else ''
        ruling = hit.get('ruling')
        note = f'（{safe(ruling["reviewer"])}：{safe(ruling["reason"])}）' if ruling else ''
        links.append(f'<a href="/desk/scans/{scan_id}?hit={hit["id"]}#finding">{KINDS[hit["kind"]]}'
                     f'{channel} · {interval(hit)}</a> {badge(hit["status"])}{note}')
    return '<br>'.join(links)


def span_cell(scan_id, position):
    local = position['local']['findings']
    if local:
        first = local[0]['id']
        return (f'<a class="time" href="/desk/scans/{scan_id}?hit={first}#finding">'
                f'{interval(position)}</a>')
    return f'<span class="time">{interval(position)}</span>'


def rows(scan_id, positions):
    if not positions:
        return ('<tr><td colspan="6">除基准候选外没有找到其他同内容的画面位置。'
                '相同台标、静帧或配音不同的画面以各自事实单列。</td></tr>')
    result = []
    for position in positions:
        detail = (f'，{position["deviation_count"]} 帧与基准不同'
                  if position['deviation_count'] else '')
        local = position['local']['findings']
        if not local and position['deviations']:
            marks = '、'.join(timestamp(item['timestamp_us'])
                              for item in position['deviations'][:3])
            detail += f'（不同帧 {marks}，本地无候选）'
        sound = SOUND.get(position['sound_relation'], position['sound_relation'])
        result.append(
            f'<tr><td>{safe(position["relation"])}</td>'
            f'<td>{span_cell(scan_id, position)}</td>'
            f'<td>{position["picture_similarity_permille"] / 10:.1f}%{safe(detail)}</td>'
            f'<td>{safe(sound)}</td>'
            f'<td>{findings(scan_id, position["local"]["findings"])}</td>'
            f'<td>{position["matched_frames"]} 帧</td></tr>'
        )
    return ''.join(result)


def excluded(groups):
    if not groups:
        return '<p class="muted">没有发现多处完全相同的静帧。</p>'
    blocks = []
    for group in groups:
        spans = '、'.join(interval(item) for item in group['occurrences'])
        local = group.get('local', {}).get('findings') or []
        note = f'；相关候选 {len(local)} 条，结论各自保留' if local else ''
        blocks.append(f'<li>静帧 {len(group["occurrences"])} 处：<code>{spans}</code>{note}</li>')
    return ('<ul>' + ''.join(blocks) + '</ul>'
            '<p class="muted">相同台标或静帧只作位置参考，不计入同内容复播，也不继承任何候选或裁决。</p>')


def show(app, scan_id, params):
    scan = app.vault.get('scan', scan_id)
    hit_id = params.get('hit', '')
    view = app.review(scan_id)
    if not hit_id:
        raise InputError('请先选定一条候选。')
    hit = next((item for item in view['hits'] if item['id'] == hit_id), None)
    if hit is None:
        raise Missing('finding does not belong to the selected scan')
    minimum = min(1000, max(500, number(params, 'minimum_permille', 900)))
    length = min(scan['extent']['end_us'] // 1000, max(100, number(params, 'minimum_ms', 500)))
    try:
        result = app.repeats(scan_id, {'hit_id': hit_id, 'minimum_permille': minimum,
                                       'minimum_ms': length})
    except InputError as exc:
        raise InputError(f'回查参数超出范围：{exc}') from exc
    anchor = result['anchor']
    content = anchor['content']
    if anchor['still'] is not None:
        anchor_note = ('<p class="muted">此候选位于多处相同的静帧（如台标）上。'
                       '静帧不是同内容复播，已单列在下方。</p>')
        content_text = '静帧 ' + interval(anchor['still'])
    elif content is not None:
        anchor_note = ''
        content_text = interval(content)
    else:
        anchor_note = '<p class="muted">没有找到可对齐的画面内容，可能候选落在覆盖缺口内。</p>'
        content_text = '无对齐内容'
    return page('重复位置', 'scan', 'repeat', scan=safe(scan_id), hit=safe(hit_id),
                kind=KINDS[hit['kind']], span=interval(hit), status=badge(hit['status']),
                content=safe(content_text), anchor_note=anchor_note,
                rows=rows(scan_id, result['positions']),
                excluded=excluded(result['excluded']),
                minimum=minimum, length=length,
                origin=f'/desk/scans/{scan_id}?hit={hit_id}#finding')
