"""Report exact, checkable recovery conditions for an evicted temporary preview.

A preview is identified by (scan, finding, padding, frames). When the bounded
in-process cache drops an entry — or the process restarts — the preview URL still
carries that identity, so this module can re-evaluate the vault and classify the
loss precisely:

- regenerable: scan, finding and master source bytes are all in the vault;
- verify_only: regeneration is impossible, but a sealed bundle covering the
  finding is present and can be verified on its own terms;
- insufficient: neither path is available.

Other previews and differently-parameterised bundles are never presented as a
substitute for the requested preview.
"""

import json
from zipfile import BadZipFile, ZipFile

from ..evidence import excerpt_span
from .preview import parse_token
from .render import page
from .text import KINDS, RECOVERY, STATUS, interval, lane, safe


def seal_coverage(app, scan_id, hit_id):
    """Split this scan's seals into usable coverage of the finding and unusable records."""
    covering, unusable = [], []
    for record in app.vault.recent('seal', limit=None):
        if record['scan_id'] != scan_id:
            continue
        path = app.vault.object_path(record['sha256'])
        if not path.exists():
            unusable.append((record, '包字节不在保管库'))
            continue
        try:
            with ZipFile(path) as archive:
                mappings = json.loads(archive.read('evidence.json'))
        except (OSError, BadZipFile, KeyError, ValueError):
            unusable.append((record, '包内容无法读取'))
            continue
        mapping = next((item for item in mappings if item.get('hit_id') == hit_id), None)
        if mapping is not None:
            covering.append((record, mapping))
    return covering, unusable


def evaluate(app, scan_id, hit_id, padding_ms, frames):
    vault = app.vault
    scan = vault.get('scan', scan_id, required=False)
    hit = next((item for item in scan['hits'] if item['id'] == hit_id), None) if scan else None
    master = vault.get('master', scan['master_id'], required=False) if scan else None
    source = master is not None and vault.object_path(master['sha256']).exists()
    covering, unusable = seal_coverage(app, scan_id, hit_id)
    expected = None
    if scan and hit:
        span = excerpt_span(hit, scan['extent']['end_us'], padding_ms)
        expected = (span.start, span.end, frames)
    matches = []
    for record, mapping in covering:
        actual = (mapping['start_us'], mapping['end_us'], len(mapping['picture']))
        exact = None if expected is None else actual == expected
        matches.append({'record': record, 'mapping': mapping, 'exact': exact})
    regenerable = bool(scan and hit and master and source)
    if regenerable:
        state = 'regenerable'
    elif matches:
        state = 'verify_only'
    else:
        state = 'insufficient'
    return {'scan_id': scan_id, 'hit_id': hit_id, 'padding_ms': padding_ms, 'frames': frames,
            'scan': scan, 'hit': hit, 'master': master, 'source': source,
            'matches': matches, 'unusable': unusable, 'state': state}


def fact(items, ok, text):
    items.append(f'<li class="{"ok" if ok else "bad"}">{text}</li>')


def conditions(model):
    items = []
    scan_id, hit_id = safe(model['scan_id']), safe(model['hit_id'])
    scan, hit, master = model['scan'], model['hit'], model['master']
    fact(items, scan is not None,
         f'扫描记录 <code>{scan_id}</code> 在库' if scan else f'扫描记录 <code>{scan_id}</code> 不在库')
    if scan is None:
        fact(items, False, '候选归属与母带归属无法核对（扫描记录缺失）')
    else:
        detail = f'（{KINDS[hit["kind"]]} · {lane(hit)} · {interval(hit)}）' if hit else ''
        fact(items, hit is not None,
             f'候选 <code>{hit_id}</code> 属于该扫描{detail}' if hit else f'候选 <code>{hit_id}</code> 不在该扫描中')
        fact(items, master is not None,
             f'母带描述 <code>{safe(scan["master_id"])}</code> 在库' if master
             else f'母带描述 <code>{safe(scan["master_id"])}</code> 不在库')
    if master is not None:
        digest = f'SHA-256 <code>{safe(master["sha256"])}</code>'
        fact(items, model['source'], f'母带源字节在保管库（{digest}）' if model['source'] else f'母带源字节不在保管库（{digest}）')
    note = f'；另有 {len(model["unusable"])} 个冻结包的包字节不在库或不可读，无法核验' if model['unusable'] else ''
    if model['matches']:
        fact(items, True, f'收录此候选的冻结包 {len(model["matches"])} 个，包字节在库可独立核验{note}')
    else:
        fact(items, False, '没有收录此候选的可用冻结包' + (note if note else ''))
    return ''.join(items)


def regenerable(model):
    scan_id, hit_id = safe(model['scan_id']), safe(model['hit_id'])
    extra = ''
    if model['matches']:
        extra = (f'<p class="muted">另有 {len(model["matches"])} 个冻结包收录此候选，'
                 '需要长期交接时可在冻结包页核验。</p>')
    return f'''<section class="form-section" aria-label="重新生成">
  <h2>可再生成</h2>
  <p class="good">再生成条件齐备：扫描记录、候选与母带源字节均在库。按原参数重新生成的预览与原内容一致。</p>
  <form class="actions" method="post" action="/desk/scans/{scan_id}/preview">
    <input name="hit" value="{hit_id}" type="hidden">
    <input name="padding_ms" value="{model["padding_ms"]}" type="hidden">
    <input name="frames" value="{model["frames"]}" type="hidden">
    <button class="primary">按原参数重新生成</button>
    <a class="button" href="/desk/scans/{scan_id}?hit={hit_id}#finding">返回候选复核</a>
  </form>{extra}
</section>'''


def verify_only(model):
    rows = []
    for item in model['matches']:
        record, mapping = item['record'], item['mapping']
        sound = '含伴音' if mapping['sound'] else '无伴音'
        if item['exact'] is None:
            check = '扫描记录缺失，无法与原预览参数核对'
        elif item['exact']:
            check = '摘片范围与抽样点相同'
        else:
            check = '范围或画格数不同，不能替代原预览'
        rows.append(f'<tr><th scope="row"><a href="/desk/seals/{record["id"]}">{record["id"]}</a></th>'
                    f'<td>{STATUS[record["review_status"]]}</td><td class="time">{interval(mapping)}</td>'
                    f'<td>{len(mapping["picture"])} 张</td><td>{sound}</td><td>{check}</td></tr>')
    master = model['master']
    if master is not None and not model['source']:
        restore = f'若找回母带源字节（SHA-256 <code>{safe(master["sha256"])}</code>）放回保管库，仍可重新生成原预览。'
    else:
        restore = '若恢复扫描记录与母带源件，仍可重新生成原预览。'
    return f'''<section class="form-section" aria-label="验证旧内容">
  <h2>只能验证旧内容</h2>
  <p>源材料不足，无法重新生成此预览。以下冻结包收录了此候选，可打开冻结包页校验其哈希与成员索引，或下载留存。</p>
  <div class="table-wrap"><table>
    <thead><tr><th scope="col">冻结包</th><th scope="col">冻结时裁决</th><th scope="col">摘片范围</th>
    <th scope="col">画格</th><th scope="col">伴音</th><th scope="col">参数核对</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
  <p class="muted">冻结包保持冻结当时的内容，参数不一致的包不能替代原预览。{restore}</p>
</section>'''


def insufficient(model):
    advice = []
    master = model['master']
    if model['scan'] is None:
        advice.append(f'<li>扫描记录 <code>{safe(model["scan_id"])}</code> 不在库：从备份恢复保管库描述符；'
                      '重新登记母带并按同一规则版扫描会产生新的扫描与候选编号。</li>')
    elif model['hit'] is None:
        advice.append(f'<li>候选 <code>{safe(model["hit_id"])}</code> 不在扫描中：核对预览地址是否完整未被截断。</li>')
    if model['scan'] is not None and master is None:
        advice.append(f'<li>母带描述 <code>{safe(model["scan"]["master_id"])}</code> 不在库：'
                      '重新登记同一母带文件可同时恢复描述与源字节。</li>')
    if master is not None and not model['source']:
        advice.append(f'<li>母带源字节不在保管库：将 SHA-256 为 <code>{safe(master["sha256"])}</code> 的字节对象恢复到 '
                      'objects/，或重新登记同一母带文件，之后可按原参数重新生成。</li>')
    for record, reason in model['unusable']:
        advice.append(f'<li>冻结包 <code>{record["id"]}</code> 的描述在库但{reason}：'
                      f'恢复字节对象 <code>{record["sha256"]}</code> 后可校验其冻结内容。</li>')
    return f'''<section class="form-section" aria-label="材料不足">
  <h2>材料不足</h2>
  <p class="error">此预览既无法重新生成，也没有收录此候选且可核验的冻结包。</p>
  <ul>{''.join(advice)}</ul>
</section>'''


CONCLUSION = {'regenerable': regenerable, 'verify_only': verify_only, 'insufficient': insufficient}


def miss(app, token):
    scan_id, hit_id, padding_ms, frames = parse_token(token)
    model = evaluate(app, scan_id, hit_id, padding_ms, frames)
    hit = model['hit']
    detail = f' · {KINDS[hit["kind"]]} · {lane(hit)} · {interval(hit)}' if hit else ''
    if model['scan'] is not None:
        back = f'<a href="/desk/scans/{safe(scan_id)}?hit={safe(hit_id)}#finding">← 返回候选复核</a>'
    else:
        back = '<a href="/desk">← 返回审片</a>'
    state = model['state']
    body = page('预览恢复', 'scan', 'recover', back=back, scan_id=safe(scan_id), hit_id=safe(hit_id),
                hit_detail=detail, padding_ms=padding_ms, frames=frames,
                badge=f'<span class="state {state}">{RECOVERY[state]}</span>',
                conditions=conditions(model), conclusion=CONCLUSION[state](model))
    return 404, body, 'text/html; charset=utf-8', {}
