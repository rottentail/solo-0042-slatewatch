"""Display version comparisons with independent links back to both scans."""

from ..clock import InputError
from .library import scan_choices
from .render import page
from .text import KINDS, interval, safe, timestamp

SPLICE_STATUS = {'confirmed': '已证实可成对', 'removed': '已知删去', 'mixed': '跨切口',
                 'unlocated': '确认点间无法定位'}


def cell(scan, hit):
    if hit is None:
        return '<td class="muted">无匹配候选</td>'
    return f'<td><a class="time" href="/desk/scans/{scan}?hit={hit["id"]}#finding">{interval(hit)}</a></td>'


def _clock(spans):
    if not spans:
        return '<span class="muted">—</span>'
    if isinstance(spans, dict):
        spans = [spans]
    return '<br>'.join(f'<code class="time">{timestamp(span["start_us"])} – {timestamp(span["end_us"])}</code>'
                       for span in spans)


def _piece_rows(pieces):
    rows = []
    for piece in pieces:
        before = _clock(piece['before'])
        after = _clock(piece['after'])
        rows.append(f'<tr><td>{SPLICE_STATUS[piece["status"]]}</td><td>{before}</td><td>{after}</td></tr>')
    return ''.join(rows)


def splice_page(app, before_id, after_id, raw_points):
    if not before_id or not after_id:
        raise InputError('需同时选定两版扫描。')
    points = _parse_points(raw_points)
    result = app.compare({'before': before_id, 'after': after_id, 'conformation_points': points})
    point_rows = ''.join(
        f'<tr><td><code class="time">{timestamp(p["before_us"])}</code></td>'
        f'<td><code class="time">{timestamp(p["after_us"])}</code></td>'
        f'<td>{safe(p["label"])}</td><td>{safe(p["evidence"]) or "—"}</td></tr>'
        for p in result['conformation_points']
    )
    segment_rows = ''.join(
        f'<tr><td>{SPLICE_STATUS[s["status"]]}</td>'
        f'<td><code class="time">{timestamp(s["before"]["start_us"])} – {timestamp(s["before"]["end_us"])}</code></td>'
        f'<td><code class="time">{timestamp(s["after"]["start_us"])} – {timestamp(s["after"]["end_us"])}</code></td>'
        f'<td>{s["before_frames"]} / {s["after_frames"]}</td></tr>'
        for s in result['segments']
    )
    finding_rows = []
    for item in result['findings']:
        kind = KINDS[item['kind']] + (f'（声道 {item["channel"]}）' if item['channel'] is not None else '')
        side = '前版候选' if item['side'] == 'before' else '后版候选'
        link = f'/desk/scans/{before_id if item["side"] == "before" else after_id}?hit={item["hit_id"]}#finding'
        finding_rows.append(
            f'<tr><td>{SPLICE_STATUS[item["status"]]}</td><td>{side}</td>'
            f'<td><a href="{link}">{kind}</a></td>'
            f'<td>{_clock(item["before"])}</td><td>{_clock(item["after"])}</td>'
            f'<td><details><summary>逐段证据（{len(item["pieces"])}）</summary>'
            f'<table class="nested"><thead><tr><th>判定</th><th>前版时标</th><th>后版时标</th></tr></thead>'
            f'<tbody>{_piece_rows(item["pieces"])}</tbody></table></details></td></tr>'
        )
    body = f'''
<h2>确认点（同一真实画格的两侧时间）</h2>
<div class="table-wrap"><table><thead><tr><th>前版时标</th><th>后版时标</th><th>名称</th><th>证据</th></tr></thead>
<tbody>{point_rows}</tbody></table></div>
<h2>映射段（按段内真实画格数判定删剪）</h2>
<div class="table-wrap"><table><thead><tr><th>判定</th><th>前版区间</th><th>后版区间</th><th>画格数 前/后</th></tr></thead>
<tbody>{segment_rows}</tbody></table></div>
<h2>候选局部对照</h2>
<div class="table-wrap"><table><thead><tr><th>归属判定</th><th>来源</th><th>异常</th><th>前版时标</th><th>后版时标</th><th>逐条证据</th></tr></thead>
<tbody>{''.join(finding_rows)}</tbody></table></div>'''
    return _render(app, before_id, after_id, body)


def _parse_points(raw):
    points = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        fields = [part.strip() for part in line.split(',')]
        if len(fields) < 2 or len(fields) > 4:
            raise InputError(f'第 {line_no} 行需为“前版微秒, 后版微秒, 名称, 证据”，含 2 至 4 列。')
        try:
            before_us = int(fields[0])
            after_us = int(fields[1])
        except ValueError as exc:
            raise InputError(f'第 {line_no} 行的时刻必须是整数微秒。') from exc
        row = {'before_us': before_us, 'after_us': after_us}
        if len(fields) >= 3 and fields[2]:
            row['label'] = fields[2]
        if len(fields) == 4 and fields[3]:
            row['evidence'] = fields[3]
        points.append(row)
    if len(points) < 2:
        raise InputError('剪接对照至少需要两个确认点。')
    return points


def show(app, before='', after=''):
    body = ''
    if before or after:
        if not before or not after:
            raise InputError('需同时选定两版扫描。')
        result = app.compare({'before': before, 'after': after})
        rows = []
        for pair in result['matches']:
            a, b = pair['before'], pair['after']
            rows.append(f'<tr><td>匹配</td><th>{KINDS[a["kind"]]}</th>{cell(before, a)}{cell(after, b)}</tr>')
        for key, title in (('resolved', '前版独有'), ('introduced', '后版独有')):
            for hit in result[key]:
                a, b = (hit, None) if key == 'resolved' else (None, hit)
                rows.append(f'<tr><td>{title}</td><th>{KINDS[hit["kind"]]}</th>{cell(before, a)}{cell(after, b)}</tr>')
        body = ('<h2>共享节目时线比对</h2><div class="table-wrap"><table>'
                '<thead><tr><th scope="col">归属</th><th scope="col">异常</th>'
                '<th scope="col">前版节目时标</th><th scope="col">后版节目时标</th></tr></thead>'
                f'<tbody>{"".join(rows) or "<tr><td colspan=4>没有可显示的技术候选。</td></tr>"}</tbody></table></div>')
    else:
        body = '<p class="muted">请选择两版扫描。</p>'
    return _render(app, before, after, body)


def _render(app, before, after, body):
    return page('版次比对', 'compare', 'compare',
                left=scan_choices(app.vault, before), right=scan_choices(app.vault, after),
                before_id=safe(before), after_id=safe(after), body=body)
