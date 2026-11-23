"""Keep frozen review states separate from current scan states."""

from .render import page
from .text import STATUS, safe


def index(app):
    records = app.vault.recent('seal')
    rows = []
    for item in records:
        key = item['id']
        rows.append(f'<tr><th><a href="/desk/seals/{key}">{key}</a></th><td>{STATUS[item["review_status"]]}</td>'
                    f'<td><code>{item["sha256"][:16]}</code></td></tr>')
    return page('冻结包', 'seals', 'seals', rows=''.join(rows) or '<tr><td colspan="3">暂无冻结包</td></tr>')


def detail(app, key, verified=False):
    item = app.vault.get('seal', key)
    if verified:
        app.verify(key)
    state = '<p role="status" class="good">包哈希和成员索引已通过校验。</p>' if verified else ''
    return page('冻结包详情', 'seals', 'seal', key=key, digest=safe(item['sha256']),
                status=STATUS[item['review_status']], scan=safe(item['scan_id']), verified=state)
