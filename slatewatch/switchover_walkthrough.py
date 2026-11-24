"""Rehearse the preview-cache cutover across a real software restart.

The drill uses real generated media and loopback HTTP servers sharing one
on-disk vault, and walks the exact migration story:

1. the *old program* (in-process memory cache) mints a preview and a seal;
2. the service is stopped and the *new program* starts against the same vault
   with file-backed previews — the old memory-only token is explained as
   missing while the frozen ZIP still verifies;
3. a file preview keeps the exact same address, excerpt options, PNG/WAV
   bytes and ``filled_sample_ranges`` after another restart;
4. expiry and missing source masters produce distinct ``expired`` and
   ``source_unavailable`` states;
5. switching the server back to ``memory`` mode (the old program behaviour)
   still verifies the sealed package, and the committed pre-change code can
   verify the same bytes via ``git archive``.

Run::

    python -m slatewatch.switchover_walkthrough
    python -m slatewatch.switchover_walkthrough --root /tmp/sw-drill
"""

from hashlib import sha256
from http.client import HTTPConnection
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlencode
from zipfile import ZipFile

from .desk.preview import png
from .http import Server
from .walkthrough import Tour

PASS_MARK = '切换演练通过'


class Response:
    def __init__(self, status, location, content_type, body):
        self.status = status
        self.location = location
        self.content_type = content_type
        self.body = body

    @property
    def text(self):
        return self.body.decode('utf-8', errors='replace')

    def json(self):
        return json.loads(self.body)


class Browser:
    """Minimal origin-bound browser client; redirects are handled explicitly."""

    def __init__(self, address):
        self.host, self.port = address
        self.origin = f'http://{self.host}:{self.port}'

    def send(self, method, path, form=None):
        connection = HTTPConnection(self.host, self.port, timeout=30)
        body = None
        headers = {'Origin': self.origin}
        if form is not None:
            body = urlencode(form).encode('ascii')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        result = Response(response.status, response.getheader('Location'),
                          response.getheader('Content-Type'), data)
        connection.close()
        return result

    def get(self, path):
        return self.send('GET', path)

    def post(self, path, form=None):
        return self.send('POST', path, form or {})

    def post_form_location(self, path, form):
        response = self.post(path, form)
        assert response.status == 303, (path, response.status, response.text[:300])
        assert response.location, response.__dict__
        return response.location

    def follow_post(self, path, form):
        location = self.post_form_location(path, form)
        return location, self.get(location)


def start_server(vault, mode='file', ttl=None):
    server = Server(('127.0.0.1', 0), str(vault), preview_mode=mode, preview_ttl=ttl)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=10)


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def digest(data):
    return sha256(data).hexdigest()


def make_preview(browser, scan_id, hit_id, padding, frames):
    location, response = browser.follow_post(
        f'/desk/scans/{scan_id}/preview',
        {'hit': hit_id, 'padding_ms': padding, 'frames': frames})
    expect(response.status == 200, (location, response.status, response.text[:400]))
    expect(f'data-preview-state="ready"'.encode() in response.body, response.text[:400])
    expect(location.startswith('/desk/previews/') and len(location.rsplit('/', 1)[-1]) == 32,
           f'expected legacy-shaped 32-hex token, got {location}')
    return location, response


def state_document(browser, address, expected_status=200):
    base = address if address.startswith('/desk/previews/') else f'/desk/previews/{address}'
    response = browser.get(base + '/state')
    expect(response.status == expected_status,
           (base, response.status, expected_status, response.text[:400]))
    return response.json()


def semantic_status(browser, address, expected):
    base = address if address.startswith('/desk/previews/') else f'/desk/previews/{address}'
    response = browser.get(base + '/status')
    expect(response.status == expected, (base, response.status, expected))
    return response.json()


def assert_preview_intact(browser, address, selection, evidence_zip=None):
    key = address.rsplit('/', 1)[-1]
    doc = state_document(browser, address, 200)
    expect(doc['state'] == 'ready', doc)
    expect(doc['selection'] == selection, (doc['selection'], selection))
    expect(doc['options'] == selection, doc.get('options'))
    expect(doc['persistence'] == 'file', doc.get('persistence'))
    ranges = doc['filled_sample_ranges']
    expect(isinstance(ranges, list) and ranges, 'basalt sound_gap should record fill ranges')

    page = browser.get(address)
    expect(page.status == 200, page.status)
    expect(f'data-preview-state="ready"'.encode() in page.body, 'ready marker missing')
    expect(f'data-padding-ms="{selection["padding_ms"]}"'.encode() in page.body, 'padding option missing')
    expect(f'data-frames="{selection["frames"]}"'.encode() in page.body, 'frames option missing')
    expect(selection['hit_ids'][0].encode() in page.body, 'hit id missing on page')
    expect('原摘片选项'.encode() in page.body, 'options heading missing')
    expect('补零区间'.encode() in page.body and '源采样'.encode() in page.body, 'range copy missing')

    for name, meta in doc['assets'].items():
        asset = browser.get(meta['url'])
        expect(asset.status == 200, (name, asset.status))
        expect(digest(asset.body) == meta['sha256'], f'{name} sha256 mismatch after restart')
        expect(len(asset.body) == meta['size'], f'{name} size mismatch')
        if name.endswith('.png'):
            expect(asset.body.startswith(b'\x89PNG\r\n\x1a\n'), f'{name} is not a PNG')
        if name == 'sound.wav':
            expect(asset.body.startswith(b'RIFF') and asset.body[8:12] == b'WAVE', 'not a WAV')

    verification = browser.get(f'/desk/previews/{key}/verify')
    expect(verification.status == 200, (verification.status, verification.text[:200]))
    expect(verification.json()['verified'] is True, verification.json())

    if evidence_zip is not None:
        hit_id = selection['hit_ids'][0]
        with ZipFile(BytesIO(evidence_zip)) as archive:
            mapping = json.loads(archive.read(f'{hit_id}/clock.json'))
            expect(mapping['sound']['filled_sample_ranges'] ==
                   doc['mapping']['sound']['filled_sample_ranges'], 'fill ranges drifted')
            wav = browser.get(f'/desk/previews/{key}/sound.wav')
            expect(wav.body == archive.read(f'{hit_id}/sound.wav'), 'WAV differs from fresh evidence ZIP')
            for frame in mapping['picture']:
                if not frame['file']:
                    continue
                png_name = frame['file'].removesuffix('.pgm') + '.png'
                served = browser.get(f'/desk/previews/{key}/{png_name}').body
                expect(served == png(archive.read(f'{hit_id}/{frame["file"]}')),
                       f'{png_name} differs from evidence PGM conversion')
    return doc


def verify_with_committed_old_code(vault, seal_id, expected_sha):
    """Export HEAD (the pre-change program) and verify the seal with it."""
    workspace = Path(__file__).resolve().parents[1]
    try:
        archived = subprocess.check_output(['git', 'archive', '--format=tar', 'HEAD'],
                                           cwd=workspace, stderr=subprocess.DEVNULL, timeout=30)
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        return f'skipped（无法导出 HEAD 旧程序：{exc}）'
    with TemporaryDirectory(prefix='sw-oldprog-') as target:
        old_root = Path(target) / 'old'
        run_dir = Path(target) / 'cwd'
        old_root.mkdir()
        run_dir.mkdir()
        with tarfile.open(fileobj=BytesIO(archived)) as tar:
            try:
                tar.extractall(old_root, filter='data')
            except TypeError:  # Python < 3.11.4 / older tarfile
                tar.extractall(old_root)
        script = (
            "import json\n"
            "from slatewatch.seal import verify\n"
            "from slatewatch.store import Vault\n"
            f"vault = Vault({str(vault)!r})\n"
            f"record = vault.get('seal', {seal_id!r})\n"
            "out = verify(vault.object_path(record['sha256']), record)\n"
            "assert out['status'] == 'verified', out\n"
            f"assert record['sha256'] == {expected_sha!r}\n"
            "print(json.dumps(out, ensure_ascii=False))\n"
        )
        env = {**os.environ, 'PYTHONPATH': str(old_root)}
        completed = subprocess.run([sys.executable, '-c', script], cwd=run_dir, env=env,
                                   capture_output=True, text=True, timeout=60)
        if completed.returncode != 0:
            raise AssertionError(f'old program failed to verify seal:\n{completed.stdout}\n{completed.stderr}')
        return completed.stdout.strip().splitlines()[-1]


def drill(work):
    vault = work / 'vault'
    summary = {}

    # ---- Phase 1: old program, process memory ---------------------------
    old_server, old_thread = start_server(vault, mode='memory')
    try:
        tour = Tour(old_server.server_address, work)
        scan = tour.scan()
        hit = tour.hit(scan, 'sound_gap')
        browser = Browser(old_server.server_address)
        old_preview, old_page = browser.follow_post(
            f'/desk/scans/{scan["id"]}/preview',
            {'hit': hit['id'], 'padding_ms': 80, 'frames': 3})
        expect(old_page.status == 200, old_page.status)
        expect(browser.get(old_preview + '/sound.wav').body.startswith(b'RIFF'), 'old WAV missing')
        sealed = tour.call(f'/scans/{scan["id"]}/seals', tour.selection(scan), 201)
        seal_id = sealed['id']
        seal_sha = sealed['sha256']

        # Graceful in-process handoff path: a still-resident old cache can be
        # drained to disk preserving tokens (separate dir; the hard-restart
        # path below intentionally loses the memory-only token).
        from slatewatch.desk.cache import FilePreviewStore, migrate_entries

        drain = FilePreviewStore(work / 'drained-previews')
        moved = migrate_entries(old_server.previews, drain)
        expect(old_preview.rsplit('/', 1)[-1] in moved, 'migration did not preserve token')
        expect(drain.describe(old_preview.rsplit('/', 1)[-1], True)['state'] == 'ready',
               'drained preview unreadable')
        print(f'1) 旧程序（memory）：进程内预览 {old_preview} 与冻结包 {seal_id[:18]} 已建立；在线缓存可排空落盘')
    finally:
        stop_server(old_server, old_thread)

    # ---- Phase 2: new program first boot on the same vault --------------
    server, thread = start_server(vault, mode='file')
    try:
        tour = Tour(server.server_address, work)
        browser = Browser(server.server_address)
        gone = browser.get(old_preview)
        expect(gone.status == 404, ('legacy token should be 404', gone.status))
        expect('旧进程'.encode() in gone.body and '内存'.encode() in gone.body,
               'legacy memory hint missing')
        verified = tour.call(f'/seals/{seal_id}/verify')
        expect(verified['status'] == 'verified', verified)
        bundle = tour.call(f'/seals/{seal_id}/bundle')
        expect(digest(bundle) == seal_sha, 'bundle sha mismatch')
        print('2) 切换到文件存储：旧内存地址明确提示不可迁移，旧冻结包在新程序下验真通过')

        selection_p = {'hit_ids': [hit['id']], 'padding_ms': 160, 'frames': 4}
        key_p, _ = make_preview(browser, scan['id'], hit['id'], 160, 4)
        selection_q = {'hit_ids': [hit['id']], 'padding_ms': 40, 'frames': 2}
        key_q, _ = make_preview(browser, scan['id'], hit['id'], 40, 2)
        selection_r = {'hit_ids': [hit['id']], 'padding_ms': 0, 'frames': 1}
        key_r, _ = make_preview(browser, scan['id'], hit['id'], 0, 1)
        evidence_zip = tour.call(f'/scans/{scan["id"]}/evidence', selection_p)
        summary['preview_address'] = key_p
        print(f'3) 文件预览已落盘：{key_p} 等三个地址（uuid 令牌与旧程序同形）')
    finally:
        stop_server(server, thread)

    # ---- Phase 3: restart, same browser address -------------------------
    server, thread = start_server(vault, mode='file')
    try:
        tour = Tour(server.server_address, work)
        browser = Browser(server.server_address)
        expect(browser.get(key_p).status == 200, 'preview did not survive restart')
        doc_p = assert_preview_intact(browser, key_p, selection_p, evidence_zip)
        expect(state_document(browser, key_q)['selection'] == selection_q, 'Q options drifted')
        print('4) 重启后同地址复听：原选项、PNG/WAV 哈希与补样范围逐项核对一致')

        # ---- Phase 4a: explicit expiry keeps options as a tombstone -----
        response = browser.post(f'{key_p}/expire', {'confirm': 'expire'})
        expect(response.status == 303, response.status)
        expired_page = browser.get(key_p)
        expect(expired_page.status == 200, expired_page.status)
        expect('data-preview-state="expired"'.encode() in expired_page.body, 'expired marker missing')
        expect('预览材料已过期'.encode() in expired_page.body, 'expired heading missing')
        expect(f'data-padding-ms="160"'.encode() in expired_page.body, 'options lost on expiry')
        expired_doc = state_document(browser, key_p)
        expect(expired_doc['state'] == 'expired', expired_doc)
        expect(expired_doc['http_status'] == 410, expired_doc)
        expect(semantic_status(browser, key_p, 410)['state'] == 'expired', 'semantic /status mismatch')
        expect(expired_doc['selection'] == selection_p, 'expired selection not retained')
        wav_gone = browser.get(f'{key_p}/sound.wav')
        expect(wav_gone.status == 410 and wav_gone.json()['state'] == 'expired', 'asset should be 410')
        descriptor = vault / 'previews' / key_p.rsplit('/', 1)[-1] / 'preview.json'
        on_disk = json.loads(descriptor.read_text(encoding='utf-8'))
        expect(on_disk['state'] == 'expired', 'descriptor not tombstoned')
        expect(not (descriptor.parent / 'assets' / 'sound.wav').exists(), 'expired WAV not removed')

        # In-place rebuild restores the SAME address from the retained options.
        rebuild = browser.post(f'{key_p}/rebuild', {'confirm': 'rebuild'})
        expect(rebuild.status == 303 and rebuild.location == key_p,
               (rebuild.status, rebuild.location))
        renewed = browser.get(key_p)
        expect(renewed.status == 200 and b'data-preview-state="ready"' in renewed.body, 'rebuild failed')

        # Re-submitting the excerpt form mints a fresh token; the renewed
        # address stays valid and carries the identical original options.
        new_address, new_page = browser.follow_post(
            f'/desk/scans/{scan["id"]}/preview',
            {'hit': hit['id'], 'padding_ms': 160, 'frames': 4})
        expect(new_address != key_p, 'form re-excerpt should mint a new token')
        expect(new_page.status == 200 and b'data-preview-state="ready"' in new_page.body, 'new preview failed')
        expect(state_document(browser, new_address)['selection'] == selection_p, 'new options differ')
        print('5) 过期状态独立（410 expired）：选项留档、媒体清除；原地重建保地址，表单重摘发新地址')

        # ---- Phase 4b: source master unavailable ------------------------
        master = tour.call(f'/masters/{scan["master_id"]}')
        source_object = vault / 'objects' / master['sha256']
        held_source = work / f'{master["sha256"]}.held'
        expect(source_object.is_file(), 'source object missing before drill')
        source_object.rename(held_source)
        try:
            missing_doc = state_document(browser, key_q)
            expect(missing_doc['state'] == 'source_unavailable', missing_doc)
            expect(missing_doc['http_status'] == 503, missing_doc)
            expect(semantic_status(browser, key_q, 503)['source']['available'] is False,
                   'semantic source status mismatch')
            expect(missing_doc['source']['available'] is False, missing_doc['source'])
            unavailable_page = browser.get(key_q)
            expect(unavailable_page.status == 200, unavailable_page.status)
            expect(b'data-preview-state="source_unavailable"' in unavailable_page.body, 'state marker missing')
            expect('源不可用'.encode() in unavailable_page.body, 'source label missing')
            # File-backed assets stay verifiable even while the source is gone.
            still_png = browser.get(f'{key_q}/' + next(iter(missing_doc['assets'])))
            expect(still_png.status == 200 and still_png.body.startswith(b'\x89PNG'),
                   'persisted PNG should survive source loss')
            rebuild = browser.post(f'{key_q}/rebuild', {'confirm': 'rebuild'})
            expect(rebuild.status == 503, ('rebuild must report source state', rebuild.status))
            expect(b'source_unavailable' in rebuild.body, 'rebuild body missing state')
        finally:
            held_source.rename(source_object)
        rebuilt = browser.post(f'{key_q}/rebuild', {'confirm': 'rebuild'})
        expect(rebuilt.status == 303 and rebuilt.location == key_q, rebuilt.status)
        expect(state_document(browser, key_q)['state'] == 'ready', 'source did not recover')
        print('6) 源不可用状态独立（503 source_unavailable）：现存 PNG/WAV 仍可核对，源恢复后可重建')

        # ---- Phase 4c: operator-edited expiry timestamp -----------------
        key_r_id = key_r.rsplit('/', 1)[-1]
        record_r_path = vault / 'previews' / key_r_id / 'preview.json'
        record_r = json.loads(record_r_path.read_text(encoding='utf-8'))
        record_r['expires_at'] = 0
        record_r_path.write_text(json.dumps(record_r, ensure_ascii=False, indent=2), encoding='utf-8')
        ttl_doc = state_document(browser, key_r)
        expect(ttl_doc['state'] == 'expired' and ttl_doc['reason'] == 'ttl'
               and ttl_doc['http_status'] == 410, ttl_doc)
        print('7) 直接把 descriptor 的 expires_at 改到过去，新请求即进入 expired（TTL 可演练）')
    finally:
        stop_server(server, thread)

    # ---- Phase 5: rollback to the old program ---------------------------
    old_server, old_thread = start_server(vault, mode='memory')
    try:
        tour = Tour(old_server.server_address, work)
        browser = Browser(old_server.server_address)
        expect(tour.call(f'/seals/{seal_id}/verify')['status'] == 'verified', 'rollback verify failed')
        desk_verify = browser.post(f'/desk/seals/{seal_id}/verify', {'verify': '1'})
        expect(desk_verify.status == 200 and '已通过校验'.encode() in desk_verify.body,
               'desk seal verification failed after rollback')
        rollback_preview, page = browser.follow_post(
            f'/desk/scans/{scan["id"]}/preview', {'hit': hit['id'], 'padding_ms': 80, 'frames': 3})
        expect(page.status == 200, 'old memory preview broken')
        expect(state_document(browser, rollback_preview)['persistence'] == 'memory', 'not memory mode')
        print('8) 切回老程序（memory 模式）：工作台验真通过，旧的进程内摘片行为如常')
    finally:
        stop_server(old_server, old_thread)

    # ---- Phase 6: package bytes + true committed old code ---------------
    object_path = vault / 'objects' / seal_sha
    raw_zip = object_path.read_bytes()
    expect(digest(raw_zip) == seal_sha, 'seal object hash drifted')
    with ZipFile(BytesIO(raw_zip)) as archive:
        expect(archive.testzip() is None, 'seal zip corrupt')
        expect('index.json' in archive.namelist(), 'index missing')
    old_result = verify_with_committed_old_code(vault, seal_id, seal_sha)
    summary['old_program_verify'] = old_result
    print('9) 冻结 ZIP 原字节完整；HEAD 旧程序独立 verify 结果：' + old_result)

    summary.update({'seal_id': seal_id, 'seal_sha256': seal_sha,
                    'persistent_preview': key_p, 'legacy_preview': old_preview})
    return summary


def run(root=None):
    if root is not None:
        work = Path(root)
        work.mkdir(parents=True, exist_ok=True)
        summary = drill(work)
        print(json.dumps({'drill': 'switchover', 'root': str(work), **summary},
                         ensure_ascii=False, indent=2))
    else:
        with TemporaryDirectory(prefix='slatewatch-switchover-') as directory:
            summary = drill(Path(directory))
            print(json.dumps({'drill': 'switchover', **summary}, ensure_ascii=False, indent=2))
    print(PASS_MARK + '：文件预览跨重启保地址，过期/源不可用状态分离，回退后旧冻结包仍验真')
    return summary


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Rehearse preview file-storage cutover and rollback')
    parser.add_argument('--root', default=None, help='persistent drill directory (temporary by default)')
    args = parser.parse_args()
    run(args.root)


if __name__ == '__main__':
    main()
