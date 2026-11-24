"""Keep frozen review states separate from current scan states."""

from .render import page
from .text import STATUS, SUPPLEMENT_BASES, safe


def index(app):
    records = app.vault.recent('seal')
    rows = []
    for item in records:
        key = item['id']
        rows.append(f'<tr><th><a href="/desk/seals/{key}">{key}</a></th><td>{STATUS[item["review_status"]]}</td>'
                    f'<td><code>{item["sha256"][:16]}</code></td></tr>')
    return page('冻结包', 'seals', 'seals', rows=''.join(rows) or '<tr><td colspan="3">暂无冻结包</td></tr>')


def _supplement_rows(app, seal_key):
    records = [item for item in app.vault.recent('supplement', limit=200) if item['seal_id'] == seal_key]
    rows = []
    for item in records:
        key = item['id']
        rows.append(
            f'<tr><th><a href="/desk/supplements/{key}">{key}</a></th>'
            f'<td>{SUPPLEMENT_BASES[item["basis"]]}</td>'
            f'<td><code>{safe(item["scan_id"][-12:])}</code></td>'
            f'<td>{STATUS[item["review_status"]]}</td>'
            f'<td><code>{safe(item["sha256"][:16])}</code></td></tr>'
        )
    if not rows:
        return '<tr><td colspan="5">暂无补证。</td></tr>'
    return ''.join(rows)


def _request_form(app, item):
    scan = app.vault.get('scan', item['scan_id'])
    hits = scan['hits']
    options = ''.join(f'<option value="{safe(hit["id"])}">{safe(hit["id"][-12:])}</option>' for hit in hits)
    return f'''
<section class="supplement-request" aria-labelledby="supplement-title">
  <h2 id="supplement-title">申请补证</h2>
  <p class="muted">补证生成独立新包，不改动原冻结包，原包哈希保持不变。</p>
  <form method="post" action="/desk/seals/{safe(item['id'])}/supplement" class="actions">
    <label>来源
      <select name="basis" id="supplement-basis" required>
        <option value="historical" selected>{SUPPLEMENT_BASES['historical']}</option>
        <option value="current">{SUPPLEMENT_BASES['current']}</option>
      </select>
    </label>
    <label>候选
      <select name="hit" required>{options}</select>
    </label>
    <label id="current-scan-field" hidden>当前扫描
      <input name="scan_id" type="text" maxlength="120" placeholder="后版扫描编号">
    </label>
    <label>留边（毫秒）<input name="padding_ms" type="number" min="0" max="2000" value="120" required></label>
    <label>画格数<input name="frames" type="number" min="1" max="8" value="3" required></label>
    <label class="wide">补证理由<input name="reason" type="text" required maxlength="350"></label>
    <button class="primary">生成补证包</button>
  </form>
  <script src="/desk/static/supplement.js"></script>
</section>'''


def detail(app, key, verified=False):
    item = app.vault.get('seal', key)
    if verified:
        app.verify(key)
    state = '<p role="status" class="good">包哈希和成员索引已通过校验。</p>' if verified else ''
    table = f'''
<section aria-labelledby="supplement-list">
  <h2 id="supplement-list">补证记录</h2>
  <div class="table-wrap"><table>
    <thead><tr><th scope="col">补证编号</th><th scope="col">来源</th><th scope="col">所用扫描</th>
    <th scope="col">评审结论</th><th scope="col">哈希前缀</th></tr></thead>
    <tbody>{_supplement_rows(app, key)}</tbody>
  </table></div>
</section>'''
    return page('冻结包详情', 'seals', 'seal', key=key, digest=safe(item['sha256']),
                status=STATUS[item['review_status']], scan=safe(item['scan_id']), verified=state,
                supplements=table, request_form=_request_form(app, item))


def supplement_detail(app, key, verified=False):
    item = app.vault.get('supplement', key)
    if verified:
        app.verify_supplement(key)
    state = '<p role="status" class="good">补证哈希、成员索引与来源绑定已通过校验。</p>' if verified else ''
    seal_key = item['seal_id']
    return page('补证详情', 'seals', 'supplement', key=safe(key), seal_key=safe(seal_key),
                seal_digest=safe(item['seal_sha256']), digest=safe(item['sha256']),
                basis=SUPPLEMENT_BASES[item['basis']], scan=safe(item['scan_id']),
                status=STATUS[item['review_status']], verified=state)
