"""Show exact evidence mappings and browser views of packaged assets.

The view layer is store-agnostic: it works with the legacy in-process
:class:`~slatewatch.desk.cache.PreviewCache` and the restart-safe
:class:`~slatewatch.desk.cache.FilePreviewStore`.  Persisted descriptors keep
the original excerpt ``selection`` options, the full clock mapping (including
``filled_sample_ranges``) and SHA-256 indexes for the PNG/WAV assets.
"""

from io import BytesIO
import json
from struct import pack
from zipfile import ZipFile
from zlib import compress, crc32

from ..clock import Missing
from ..media import HEIGHT, WIDTH
from .cache import (
    STATE_EXPIRED,
    STATE_READY,
    STATE_SOURCE_UNAVAILABLE,
    PreviewUnavailable,
)
from .render import page
from .text import interval, safe, timestamp

STATE_LABEL = {
    STATE_READY: '可复听',
    STATE_EXPIRED: '材料已过期',
    STATE_SOURCE_UNAVAILABLE: '源不可用',
}


def png(pgm):
    prefix = f'P5\n{WIDTH} {HEIGHT}\n255\n'.encode()
    if not pgm.startswith(prefix) or len(pgm) != len(prefix) + WIDTH * HEIGHT:
        from ..clock import InputError

        raise InputError('无法显示此证据画格。')
    pixels = pgm[len(prefix):]

    def chunk(name, payload):
        return pack('!I', len(payload)) + name + payload + pack('!I', crc32(name + payload))

    rows = b''.join(b'\0' + pixels[i:i + WIDTH] for i in range(0, len(pixels), WIDTH))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', pack('!2I5B', WIDTH, HEIGHT, 8, 0, 0, 0, 0))
            + chunk(b'IDAT', compress(rows)) + chunk(b'IEND', b''))


# ---------------------------------------------------------------------------
# Issuing previews
# ---------------------------------------------------------------------------

def issue(app, cache, scan_id, hit_id, padding, frames, key=None):
    """Extract one excerpt and put it into the active preview store.

    Pass ``key`` to regenerate an existing file-backed address in place.
    """
    selection = {'hit_ids': [hit_id], 'padding_ms': padding, 'frames': frames}
    raw = app.evidence(scan_id, selection)
    with ZipFile(BytesIO(raw)) as archive:
        from .cache import PREVIEW_LIMIT

        if sum(item.file_size for item in archive.infolist()) > PREVIEW_LIMIT:
            from ..clock import InputError

            raise InputError('摘片超过浏览器预览容量，请下载完整证据包。')
        mapping = json.loads(archive.read(f'{hit_id}/clock.json'))
        assets = {frame['file'].removesuffix('.pgm') + '.png':
                  (png(archive.read(f'{hit_id}/{frame["file"]}')), 'image/png')
                  for frame in mapping['picture'] if frame['file']}
        if mapping['sound']:
            assets['sound.wav'] = (archive.read(f'{hit_id}/sound.wav'), 'audio/wav')
    mapping['scan_id'] = scan_id
    source = None
    scan = app.vault.get('scan', scan_id, required=False)
    if scan:
        master = app.vault.get('master', scan['master_id'], required=False)
        if master:
            source = {'master_id': master['id'], 'sha256': master['sha256']}
    return cache.put(mapping, assets, selection=selection, source=source, key=key)


# Backwards-compatible name used by the desk routes and older walkthroughs.
def create(app, cache, scan_id, hit_id, padding, frames):
    return issue(app, cache, scan_id, hit_id, padding, frames)


def source_status(app, source):
    """Return whether the master backing this preview is currently usable."""
    if not source:
        return None
    sha = source.get('sha256')
    master_id = source.get('master_id')
    if not sha and not master_id:
        return None
    try:
        if master_id and app.vault.get('master', master_id, required=False) is None:
            return False
        if sha and not app.vault.object_path(sha).is_file():
            return False
    except OSError:
        return False
    return True


def describe(app, cache, key):
    """Resolve the public state document for any preview store."""
    if getattr(cache, 'file_backed', False):
        raw = cache.peek(key)
        available = source_status(app, raw.get('source'))
        document = cache.describe(key, source_available=available)
        document.setdefault('http_status', 200)
    else:
        document = cache.describe(key)
        document['source']['available'] = source_status(app, document.get('source'))
        document['http_status'] = 200
    # Uniform aliases across both store implementations.
    state = document['state']
    document.setdefault('status', state)
    document.setdefault('ok', state == STATE_READY)
    document.setdefault('ready', state == STATE_READY)
    document.setdefault('expired', state == STATE_EXPIRED)
    document.setdefault('source_unavailable', state == STATE_SOURCE_UNAVAILABLE)
    return document


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _frames(doc):
    mapping = doc['mapping']
    base = doc['page_url']
    assets = doc.get('assets', {})
    items = []
    for item in mapping['picture']:
        label = timestamp(item['target_us'])
        name = item['file'].removesuffix('.pgm') + '.png' if item['file'] else None
        asset = assets.get(name) if name else None
        if item['file'] and asset and asset.get('available', True):
            picture = f'<img src="{base}/{name}" alt="证据画格">'
            fact = f'源帧 {timestamp(item["source_us"])}'
        elif item['file']:
            picture = '<div class="missing-frame">预览画格文件已移除</div>'
            fact = f'源帧 {timestamp(item["source_us"])}（材料已过期）' if item['source_us'] is not None else '此采样点无源帧'
        else:
            picture = '<div class="missing-frame">无画格覆盖</div>'
            fact = '此采样点无源帧'
        items.append(f'<figure>{picture}<figcaption><strong>{label}</strong><small>{fact}</small></figcaption></figure>')
    return f'<section aria-label="证据画格" class="frames">{ "".join(items) }</section>'


def _sound(doc):
    track = doc['mapping']['sound']
    if track is None:
        return '<section class="form-section" aria-label="原轨伴音"><h2>原轨伴音</h2><p class="muted">未选伴音轨</p></section>'
    base = doc['page_url']
    asset = doc.get('assets', {}).get('sound.wav')
    playable = asset and asset.get('available', True) and doc['state'] != STATE_EXPIRED
    player = (f'<audio controls preload="metadata" src="{base}/sound.wav"></audio>') if playable else (
        '<p class="muted">伴音文件已随过期材料移除，下列源采样与补零范围仍留档。</p>')
    filled = ', '.join(f'[{item["first"]}, {item["end"]})' for item in track['filled_sample_ranges'])
    return ('<section class="form-section" aria-label="原轨伴音"><h2>原轨伴音</h2>'
            f'{player}'
            f'<p>{track["sample_rate"]} Hz · {track["channels"]} 声道 · 源采样 '
            f'[{track["first_source_sample"]}, {track["end_source_sample"]})</p>'
            f'<p>补零区间 <code>{filled or "无"}</code></p></section>')


def _options(doc):
    selection = doc.get('selection') or {}
    hit_id = doc.get('hit_id') or (selection.get('hit_ids') or [''])[0]
    padding = selection.get('padding_ms')
    frames = selection.get('frames')
    selection_json = json.dumps(selection, ensure_ascii=False).replace('<', '\\u003c').replace('&', '\\u0026')
    created = _clock_text(doc.get('created_at'))
    expires = _clock_text(doc.get('expires_at')) if doc.get('expires_at') else '长期保留（不过期）'
    source = doc.get('source') or {}
    source_state = source.get('available')
    if source_state is True:
        source_note = '源母带对象在库'
    elif source_state is False:
        source_note = '源母带对象不可用'
    else:
        source_note = '旧进程内存记录（不校验源）'
    return f'''<section class="preview-options" aria-label="原摘片选项">
<h2>原摘片选项</h2>
<script type="application/json" id="preview-selection">{selection_json}</script>
<dl data-hit-id="{safe(hit_id)}" data-padding-ms="{safe(padding if padding is not None else '')}" data-frames="{safe(frames if frames is not None else '')}">
<div><dt>候选</dt><dd><code>{safe(hit_id)}</code></dd></div>
<div><dt>hit_ids</dt><dd><code>{safe(json.dumps(selection.get('hit_ids', []), ensure_ascii=False))}</code></dd></div>
<div><dt>留边 padding_ms</dt><dd>{safe(padding)} 毫秒</dd></div>
<div><dt>画格数 frames</dt><dd>{safe(frames)}</dd></div>
<div><dt>建立时间</dt><dd>{created}</dd></div>
<div><dt>到期时间</dt><dd>{expires}</dd></div>
<div><dt>来源</dt><dd><code>{safe((source.get('sha256') or '未记录')[:16])}</code> · {source_note} · {safe(doc.get('persistence', ''))}</dd></div>
</dl></section>'''


def _clock_text(value):
    if not isinstance(value, (int, float)):
        return safe(value or '—')
    import datetime

    return datetime.datetime.fromtimestamp(value).strftime('%Y-%m-%d %H:%M:%S')


def _banner(doc):
    state = doc['state']
    if state == STATE_EXPIRED:
        reason = doc.get('expiry_reason') or doc.get('reason') or 'ttl'
        return (f'<p class="preview-banner expired" role="status" data-reason="{safe(reason)}">'
                '预览材料已过期：PNG/WAV 已按保留策略清除，原摘片选项、时标映射与补样范围仍留档。'
                '源母带可用时可按原选项重新摘片，地址保持不变。</p>')
    if state == STATE_SOURCE_UNAVAILABLE:
        intact = all(item.get('available') for item in doc.get('assets', {}).values()) if doc.get('assets') else False
        if intact:
            detail = '已落盘的 PNG/WAV 仍可核对，但无法重新摘片或续期；请恢复 vault/objects 中的源母带对象后重试。'
        else:
            detail = '缓存材料不完整，且 vault/objects 中的源母带对象缺失，无法恢复摘片；请恢复源对象后重试。'
        return f'<p class="preview-banner source" role="status">源不可用：{detail}</p>'
    return ''


def _actions(doc):
    key = doc['id']
    if doc['state'] == STATE_READY:
        return ('<section class="preview-actions"><form method="post" '
                f'action="/desk/previews/{key}/expire" class="inline">'
                '<button type="submit">将此预览标记为过期（演练）</button></form></section>')
    label = '恢复来源并重新核对 / 摘片' if doc['state'] == STATE_SOURCE_UNAVAILABLE else '按原选项重新摘片'
    return ('<section class="preview-actions"><form method="post" '
            f'action="/desk/previews/{key}/rebuild" class="actions">'
            f'<button class="primary" type="submit">{label}</button></form></section>')


def _audit(doc):
    key = doc['id']
    rows = ''.join(
        f'<li><code>{safe(name)}</code> · {safe(meta.get("size"))} B · '
        f'<code>{safe(meta.get("sha256", "")[:24])}</code>… · '
        f'{"在盘" if meta.get("available", True) else "已移除"}</li>'
        for name, meta in sorted(doc.get('assets', {}).items())
    )
    ranges = doc.get('filled_sample_ranges')
    return f'''<details class="preview-audit form-section"><summary>核对信息：资产 SHA-256、补样范围与机器状态</summary>
<p><a href="/desk/previews/{key}/state">state</a> · <a href="/desk/previews/{key}/status">status</a>
 · <a href="/desk/previews/{key}/verify">重新计算哈希核对 verify</a>
 · <a href="/desk/previews/{key}/manifest.json">manifest.json</a></p>
<p>补样范围（filled_sample_ranges）：<code>{safe(json.dumps(ranges, ensure_ascii=False))}</code></p>
<ul class="asset-index">{rows}</ul></details>'''


def render_page(doc):
    state = doc['state']
    heading = {
        STATE_READY: '证据预览',
        STATE_EXPIRED: '预览材料已过期',
        STATE_SOURCE_UNAVAILABLE: '源不可用',
    }.get(state, '证据预览')
    badge_cls = {'ready': 'accepted', 'expired': 'pending', 'source_unavailable': 'held'}.get(state, 'pending')
    badge = f'<span class="state {badge_cls}" data-state-badge="{state}">{STATE_LABEL.get(state, state)}</span>'
    frames = _frames(doc) if doc.get('mapping') else ''
    sound = _sound(doc) if doc.get('mapping') else ''
    return page('证据预览', 'scan', 'preview',
                state=state,
                scan=safe(doc.get('scan_id') or ''),
                hit=safe(doc.get('hit_id') or ''),
                span=interval((doc.get('mapping') or doc.get('span') or {'start_us': 0, 'end_us': 0})),
                heading=heading, banner=_banner(doc), state_badge=badge,
                options=_options(doc), frames=frames, sound=sound,
                audit=_audit(doc), actions=_actions(doc))


def show(key, mapping):
    """Backwards-compatible renderer for callers holding a raw mapping."""
    assets = {
        frame['file'].removesuffix('.pgm') + '.png': {'available': True}
        for frame in mapping.get('picture', []) if frame['file']
    }
    if mapping.get('sound'):
        assets['sound.wav'] = {'available': True}
    doc = {
        'id': key, 'page_url': f'/desk/previews/{key}', 'state': STATE_READY,
        'scan_id': mapping.get('scan_id'), 'hit_id': mapping.get('hit_id'),
        'selection': {'hit_ids': [mapping.get('hit_id')], 'padding_ms': None, 'frames': None},
        'mapping': mapping, 'assets': assets, 'source': {}, 'persistence': 'memory',
    }
    return render_page(doc)


def index_page(cache):
    if getattr(cache, 'file_backed', False):
        rows = cache.summaries()
        items = ''.join(
            f'<tr><th><a href="/desk/previews/{row["id"]}">{row["id"]}</a></th>'
            f'<td data-preview-state="{row["state"]}">{STATE_LABEL.get(row["state"], row["state"])}</td>'
            f'<td><code>{safe(row.get("hit_id") or "")}</code></td>'
            f'<td>{_clock_text(row.get("expires_at")) if row.get("expires_at") else "—"}</td></tr>'
            for row in rows
        ) or '<tr><td colspan="4">暂无文件预览。</td></tr>'
        kind = '文件支持（重启后保留）'
    else:
        names = cache.names() if hasattr(cache, 'names') else list(getattr(cache, 'entries', {}))
        items = ''.join(
            f'<tr><th><a href="/desk/previews/{key}">{key}</a></th>'
            f'<td data-preview-state="ready">{STATE_LABEL[STATE_READY]}</td>'
            '<td>进程内</td><td>进程退出即失效</td></tr>'
            for key in names
        ) or '<tr><td colspan="4">暂无进程内预览。</td></tr>'
        kind = '进程内存（旧程序模式）'
    return page('预览存储', 'seals', 'preview_index', rows=items, kind=kind)


# ---------------------------------------------------------------------------
# HTTP response helpers (4-tuples consumed by desk.routes)
# ---------------------------------------------------------------------------

def _json(document, status=None):
    body = json.dumps(document, ensure_ascii=False, indent=2).encode('utf-8')
    return status or document.get('http_status', 200), body, 'application/json; charset=utf-8', {}


def page_response(app, cache, key):
    # Descriptor-known states render inline panels with HTTP 200 so a browser
    # or simple script can read the state; machine clients can use /status
    # for the semantic 410/503 codes.  Unknown keys still raise Missing -> 404.
    doc = describe(app, cache, key)
    return 200, render_page(doc), 'text/html; charset=utf-8', {}


def state_response(app, cache, key):
    try:
        doc = describe(app, cache, key)
    except Missing as exc:
        return _json({'state': 'missing', 'id': key, 'error': str(exc),
                      'status': 'missing', 'ok': False, 'http_status': 404}, 404)
    # Envelope always carries the discriminative state with 200; the advisory
    # http_status field reports the REST-semantic code for the condition.
    return _json(doc, 200)


def status_response(app, cache, key):
    """Same document as state_response, but with semantic HTTP status."""
    try:
        doc = describe(app, cache, key)
    except Missing as exc:
        return _json({'state': 'missing', 'id': key, 'error': str(exc),
                      'status': 'missing', 'ok': False, 'http_status': 404}, 404)
    return _json(doc, int(doc.get('http_status', 200)))


def verify_document(app, cache, key):
    """Recompute PNG/WAV hashes against the persisted index.

    This is the preview analogue of seal verification: it proves the bytes
    served at the stable address are exactly the ones written at issue time.
    """
    import hashlib

    if not getattr(cache, 'file_backed', False):
        try:
            mapping, assets = cache.get(key)
        except Missing as exc:
            return None, str(exc)
        checks = {name: {'ok': True, 'sha256': hashlib.sha256(data).hexdigest()}
                  for name, (data, _mime) in assets.items()}
        return {'id': key, 'state': STATE_READY, 'verified': True,
                'checked': len(checks), 'assets': checks}, None
    record = cache.peek(key)
    checks = {}
    all_ok = record.get('state') != STATE_EXPIRED
    for name, meta in record.get('assets', {}).items():
        path = cache.directory(key) / 'assets' / name
        if not path.is_file():
            checks[name] = {'ok': False, 'reason': 'missing'}
            all_ok = False
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        ok = digest == meta.get('sha256') and path.stat().st_size == meta.get('size')
        checks[name] = {'ok': ok, 'sha256': digest}
        all_ok = all_ok and ok
    state = record.get('state', STATE_READY)
    return {'id': key, 'state': state,
            'verified': bool(all_ok and state == STATE_READY),
            'checked': len(checks), 'assets': checks}, None


def verify_response(app, cache, key):
    try:
        document, error = verify_document(app, cache, key)
    except Missing as exc:
        return _json({'state': 'missing', 'id': key, 'error': str(exc)}, 404)
    if error:
        return _json({'state': 'missing', 'id': key, 'error': error}, 404)
    status = 200 if document['verified'] else 409
    return _json(document, status)


def asset_response(app, cache, key, name):
    try:
        if getattr(cache, 'file_backed', False):
            data, mime, _meta = cache.asset(key, name)
        else:
            _mapping, assets = cache.get(key)
            if name not in assets:
                raise Missing('没有此项预览材料。')
            data, mime = assets[name]
    except PreviewUnavailable as exc:
        return _json({'state': exc.state, 'id': key, 'asset': name,
                      'error': str(exc), 'reason': exc.reason}, exc.http_status)
    return 200, data, mime, {}


def action_response(app, cache, key, action):
    """POST expire/rebuild.  Returns a route tuple (often a 303 redirect)."""
    from .routes import redirect

    if action in ('expire', 'retire', 'invalidate', 'revoke'):
        if getattr(cache, 'file_backed', False):
            cache.expire(key, 'manual')
        # Memory cache has no tombstone; dropping the token is its "expiry".
        elif key in getattr(cache, 'entries', {}):
            with cache.lock:
                _m, _a, size = cache.entries.pop(key)
                cache.metadata.pop(key, None)
                cache.total -= size
        return redirect(f'/desk/previews/{key}')
    if action in ('rebuild', 'refresh', 'regenerate', 'retry'):
        selection = None
        scan_id = None
        source = None
        if getattr(cache, 'file_backed', False):
            record = cache.peek(key)
            selection = record.get('selection')
            scan_id = record.get('scan_id')
            source = record.get('source')
        else:
            try:
                doc = cache.describe(key)
                selection = doc.get('selection')
                scan_id = doc.get('scan_id')
                source = doc.get('source')
            except Missing:
                selection = None
        if not selection:
            raise Missing('没有可用于重新摘片的原选项。')
        if source and source_status(app, source) is False:
            # Do not let the decoder turn a missing vault object into a 500.
            doc = describe(app, cache, key)
            return int(doc.get('http_status', 503)), render_page(doc), 'text/html; charset=utf-8', {}
        hit_id = (selection.get('hit_ids') or [None])[0]
        padding = selection.get('padding_ms')
        frames = selection.get('frames')
        new_key = issue(app, cache, scan_id, hit_id,
                        120 if padding is None else padding,
                        3 if frames is None else frames,
                        key=key if getattr(cache, 'file_backed', False) else None)
        return redirect(f'/desk/previews/{new_key}')
    raise Missing('工作台没有此预览操作。')
