"""Use the single Slatewatch service from HTML pages and bounded browser forms."""

import json
from urllib.parse import urlencode, urlsplit

from ..clock import Conflict, InputError, Missing
from ..media import MAX_ASSET
from ..review import BasisConflict, find_hit
from . import compare, forms, library, preview, scans, seals
from .render import ASSETS, HERE, page
from .text import safe


def html(data):
    return 200, data, 'text/html; charset=utf-8', {}


def redirect(path):
    return 303, b'', 'text/plain', {'Location': path}


def allow_origin(handler):
    received = handler.headers.get('Origin')
    host = handler.headers.get('Host', '')
    origin = urlsplit(received or '')
    if origin.scheme not in ('http', 'https') or origin.netloc != host or origin.path or origin.query or origin.fragment:
        raise InputError('请从当前审片台地址提交。')


def read_form(handler):
    if handler.headers.get_content_type() != 'application/x-www-form-urlencoded':
        raise InputError('预期审片表单。')
    return forms.decode(handler.body(16 * 1024).decode('ascii'))


def selected_url(scan_id, hit_id):
    return f'/desk/scans/{scan_id}?' + urlencode({'hit': hit_id}) + '#finding'


def upload(handler, app, query):
    if handler.headers.get_content_type() != 'application/octet-stream':
        raise InputError('请选择要上传的母带。')
    picture = forms.number(query, 'picture') if query.get('picture') else None
    sound = query.get('sound') or 'first'
    if sound == 'none':
        sound = None
    elif sound != 'first':
        sound = forms.number(query, 'sound')
    record = app.master(handler.body(MAX_ASSET), query.get('label', ''), picture, sound)
    return 201, json.dumps({'id': record['id']}, ensure_ascii=False).encode(), 'application/json', {}


def route(handler, parsed):
    app = handler.server.app
    cache = handler.server.preview_cache
    method = handler.command
    parts = parsed.path.strip('/').split('/')
    params = forms.decode(parsed.query)
    if method == 'POST':
        allow_origin(handler)
        if parts == ['desk', 'upload']:
            return upload(handler, app, params)
        data = read_form(handler)
    match method, parts:
        case 'GET', [''] | ['desk']:
            latest = scans.home(app.vault)
            return redirect(f'/desk/scans/{latest}') if latest else html(page('审片', 'scan', 'empty'))
        case 'GET', ['desk', 'static', name] if name in ASSETS:
            mime = 'text/css' if name.endswith('.css') else 'text/javascript'
            return 200, (HERE / 'static' / name).read_bytes(), mime, {}
        case 'GET', ['desk', 'intake']:
            return html(library.intake(app.vault))
        case 'POST', ['desk', 'policy']:
            forms.policy(app, data)
            return redirect('/desk/intake')
        case 'POST', ['desk', 'scan']:
            record = app.scan({'master_id': data.get('master', ''), 'policy_id': data.get('policy', '')})
            return redirect(f'/desk/scans/{record["id"]}')
        case 'GET', ['desk', 'scans', key]:
            return html(scans.show(app, key, params.get('hit', '')))
        case 'POST', ['desk', 'scans', key, operation]:
            return scan_action(app, cache, key, operation, data)
        case 'GET', ['desk', 'previews', key]:
            mapping, _assets = cache.get(key)
            return html(preview.show(key, mapping))
        case 'GET', ['desk', 'previews', key, name]:
            _mapping, assets = cache.get(key)
            if name not in assets:
                raise Missing('没有此项预览材料。')
            content, mime = assets[name]
            return 200, content, mime, {}
        case 'GET', ['desk', 'compare']:
            return html(compare.show(app, params.get('before', ''), params.get('after', '')))
        case 'GET', ['desk', 'seals']:
            return html(seals.index(app))
        case 'GET', ['desk', 'seals', key]:
            return html(seals.detail(app, key))
        case 'POST', ['desk', 'seals', key, 'verify']:
            return html(seals.detail(app, key, verified=True))
        case _:
            raise Missing('工作台没有此页面。')


def scan_action(app, cache, key, operation, data):
    hit_id = data.get('hit', '')
    if operation == 'ruling':
        request = {'hit_id': hit_id, 'outcome': data.get('outcome'), 'reason': data.get('reason'),
                   'reviewer': data.get('reviewer')}
        if data.get('basis'):
            request['basis'] = forms.number(data, 'basis')
        try:
            app.add_ruling(key, request)
        except BasisConflict:
            return 409, scans.show(app, key, hit_id, attempt=request), 'text/html; charset=utf-8', {}
        return redirect(selected_url(key, hit_id))
    if operation == 'intent':
        hit = find_hit(app.vault.get('scan', key), hit_id)
        request = {'kind': hit['kind'], 'start_us': forms.number(data, 'start_us'),
                   'end_us': forms.number(data, 'end_us'), 'reason': data.get('reason')}
        if hit['channel'] is not None:
            request['channel'] = hit['channel']
        app.add_intent(key, request)
        return redirect(selected_url(key, hit_id))
    if operation not in ('preview', 'evidence', 'seal'):
        raise Missing('工作台没有此操作。')
    padding, frames = forms.number(data, 'padding_ms', 120), forms.number(data, 'frames', 3)
    if operation == 'preview':
        token = preview.create(app, cache, key, hit_id, padding, frames)
        return redirect(f'/desk/previews/{token}')
    request = {'hit_ids': [hit_id], 'padding_ms': padding, 'frames': frames}
    if operation == 'seal':
        record = app.seal(key, request)
        return redirect(f'/desk/seals/{record["id"]}')
    return 200, app.evidence(key, request), 'application/zip', {'Content-Disposition': 'attachment; filename="slatewatch-evidence.zip"'}


def handle(handler, parsed):
    try:
        return route(handler, parsed)
    except (Conflict, InputError, Missing, TimeoutError, ValueError) as exc:
        code = 409 if isinstance(exc, Conflict) else 404 if isinstance(exc, Missing) else 408 if isinstance(exc, TimeoutError) else 400
        if parsed.path == '/desk/upload':
            return code, b'{"error":"upload rejected"}', 'application/json', {}
        message = str(exc) if any('\u4e00' <= char <= '\u9fff' for char in str(exc)) else {
            400: '输入无效，或母带不符合此工位的媒体约束。',
            404: '找不到指定的扫描、证据或冻结包。',
            408: '工位读取超时。',
            409: '来源、扫描或制品在本次核对中发生冲突。',
        }[code]
        return code, page('操作未完成', '', 'error', message=safe(message), code=code), 'text/html; charset=utf-8', {}
