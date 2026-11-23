"""Display the current comparison result with independent links to both scans."""

from ..clock import InputError
from .library import scan_choices
from .render import page
from .text import KINDS, interval


def cell(scan, hit):
    if hit is None:
        return '<td class="muted">无匹配候选</td>'
    return f'<td><a class="time" href="/desk/scans/{scan}?hit={hit["id"]}#finding">{interval(hit)}</a></td>'


def show(app, before='', after=''):
    records = ''
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
        records = ''.join(rows) or '<tr><td colspan="4">没有可显示的技术候选。</td></tr>'
    return page('版次比对', 'compare', 'compare',
                left=scan_choices(app.vault, before), right=scan_choices(app.vault, after),
                rows=records or '<tr><td colspan="4">请选择两版扫描。</td></tr>')
