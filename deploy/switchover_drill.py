#!/usr/bin/env python3
"""Switchover drill for two operator positions sharing one local review room.

Two real ``python -m slatewatch.http`` processes are started from
deploy/positions.json against the same vault, then the drill verifies:

1. desk-a registers a master, publishes a scan, freezes a seal and opens a
   browser preview (the clip's browse address).
2. A long scan occupies every inspection slot of desk-a; meanwhile desk-b can
   still open the existing scan, read original frames and listen to the audio.
3. desk-a registers a second master and exits before its scan is published.
   desk-b takes over and confirms: which scans remain reviewable, that the
   orphaned master can be published, which preview browse address has lapsed,
   and that the old seal still points at the original bytes and verdict.

The drill is re-runnable: by default the vault starts fresh; --keep reuses the
existing room. Generated media is cached under the configured media directory.
"""
import argparse
from fractions import Fraction
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import sys
from threading import Thread
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zipfile import ZipFile

DEPLOY = Path(__file__).resolve().parent
REPO = DEPLOY.parent
sys.path.insert(0, str(DEPLOY))
sys.path.insert(0, str(REPO))

import roomctl  # noqa: E402

import av  # noqa: E402
from slatewatch.reels import SAMPLE_RATE, audio_frame, make  # noqa: E402
from slatewatch.scan import scan_identity  # noqa: E402

API = '/slatewatch/v1'
LONG_REEL_SECONDS = 100
LONG_REEL_SIZE = (3840, 2160)
LONG_REEL_FPS = 25


class Client:
    """Minimal HTTP client for one position, following redirects like a browser."""

    def __init__(self, position):
        self.position = position
        self.origin = position.origin

    def raw(self, method, path, body=None, headers=None, timeout=60):
        request = Request(self.origin + path, data=body, method=method, headers=headers or {})
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.status, response.geturl(), response.read()
        except HTTPError as exc:
            return exc.code, self.origin + path, exc.read()

    def expect(self, method, path, body=None, headers=None, status=200, timeout=60):
        code, url, data = self.raw(method, path, body, headers, timeout)
        if code != status:
            raise AssertionError(f'{method} {path}: expected HTTP {status}, got {code}: {data[:200]!r}')
        return url, data

    def api(self, method, path, payload=None, status=200, timeout=60):
        body = None if payload is None else json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json'} if payload is not None else {}
        _url, data = self.expect(method, API + path, body, headers, status, timeout)
        return json.loads(data)

    def upload(self, query, data, timeout=180):
        _url, body = self.expect('POST', f'{API}/masters?{query}', data,
                                 {'Content-Type': 'application/octet-stream'}, 201, timeout)
        return json.loads(body)

    def desk(self, path, form=None, status=200, timeout=60):
        body = None if form is None else urlencode(form).encode()
        headers = {'Origin': self.origin}
        if form is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        return self.expect('POST' if form is not None else 'GET', path, body, headers, status, timeout)


class Drill:
    def __init__(self):
        self.failures = 0
        self.count = 0

    def phase(self, title):
        print(f'\n== {title} ==', flush=True)

    def check(self, label, condition, detail=''):
        self.count += 1
        if not condition:
            self.failures += 1
        print(f'[{"PASS" if condition else "FAIL"}] {label}' + (f' — {detail}' if detail else ''), flush=True)
        return condition


def make_long_reel(path, seconds=LONG_REEL_SECONDS, size=LONG_REEL_SIZE, fps=LONG_REEL_FPS):
    """Flat 4K master: small container that still takes seconds to decode and measure."""
    width, height = size
    rate = Fraction(fps)
    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=rate)
        video.width, video.height = size
        video.pix_fmt = 'gray'
        audio = container.add_stream('pcm_s16le', rate=SAMPLE_RATE)
        audio.layout = 'stereo'
        frame = av.VideoFrame(width, height, 'gray')
        plane = frame.planes[0]
        raw = bytearray(plane.buffer_size)
        row = bytes([128]) * width
        for y in range(height):
            start = y * plane.line_size
            raw[start:start + width] = row
        plane.update(raw)
        next_sample = 0
        total = seconds * SAMPLE_RATE
        for i in range(seconds * fps):
            frame.pts = i
            frame.time_base = 1 / rate
            container.mux(video.encode(frame))
            while next_sample < total and Fraction(next_sample, SAMPLE_RATE) <= Fraction(i, fps):
                container.mux(audio.encode(audio_frame(next_sample, 160, False)))
                next_sample += 160
        container.mux(video.encode())
        container.mux(audio.encode())
    return path


def ensure_media(room):
    room.media_dir.mkdir(parents=True, exist_ok=True)
    media = {}
    for name in ('basalt', 'revision'):
        path = room.media_dir / f'{name}.mkv'
        if not path.exists():
            print(f'generating {name} master -> {path}', flush=True)
            make(path, name)
        media[name] = path
    long_path = room.media_dir / 'long-4k.mkv'
    if not long_path.exists():
        print(f'generating long 4K master -> {long_path}', flush=True)
        make_long_reel(long_path)
    media['long'] = long_path
    return media


def unreachable(client, path):
    try:
        client.raw('GET', path, timeout=3)
    except URLError:
        return True
    return False


def run_drill(room, busy, spare, media, nonce):
    drill = Drill()
    A = Client(busy)
    B = Client(spare)
    vault_objects = room.root / 'objects'

    drill.phase(f'第 1 阶段：{busy.name} 登记、扫描、冻结并打开摘片预览')
    basalt_bytes = media['basalt'].read_bytes()
    master1 = A.upload(urlencode({'label': 'basalt', 'sound': 'first'}), basalt_bytes)
    drill.check('源件 basalt 已登记', master1['sha256'] == sha256(basalt_bytes).hexdigest(),
                master1['id'])
    policy = A.api('POST', '/policies', {'label': '工位默认'}, 201)
    scan1 = A.api('POST', '/scans', {'master_id': master1['id'], 'policy_id': policy['id']}, 201)
    hit1 = next(hit for hit in scan1['hits'] if hit['kind'] == 'sound_gap')
    drill.check('扫描已发布且带有候选', len(scan1['hits']) > 0,
                f"{scan1['id']} · {len(scan1['hits'])} 项候选")
    seal1 = A.api('POST', f"/scans/{scan1['id']}/seals",
                  {'hit_ids': [hit1['id']], 'padding_ms': 80, 'frames': 3}, 201)
    drill.check('冻结包已生成', seal1['review_status'] in ('pending', 'hold', 'pass'),
                f"{seal1['id']} · 冻结时结论 {seal1['review_status']}")
    preview_url, page = A.desk(f"/desk/scans/{scan1['id']}/preview",
                               {'hit': hit1['id'], 'padding_ms': 80, 'frames': 3})
    preview_path = urlsplit(preview_url).path
    png_rel = re.search(rb'src="(/desk/previews/[^"]+\.png)"', page).group(1).decode()
    _url, png = A.desk(png_rel)
    _url, wav = A.desk(preview_path + '/sound.wav')
    drill.check('摘片预览可浏览（画格 PNG 与伴音 WAV）',
                png.startswith(b'\x89PNG') and wav.startswith(b'RIFF'), preview_path)

    drill.phase(f'第 2 阶段：长扫描占满 {busy.name} 处理槽，{spare.name} 继续审片')
    long_bytes = media['long'].read_bytes()
    master_long = A.upload(urlencode({'label': f'long-4k-{nonce}', 'sound': 'first'}), long_bytes)
    review_path = f"{API}/scans/{scan1['id']}/review"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status, _url, _body = A.raw('GET', review_path, timeout=10)
        if status == 200:
            break
        time.sleep(0.05)
    outcome = {}

    def long_scan():
        started = time.monotonic()
        give_up = started + 60
        body = json.dumps({'master_id': master_long['id'], 'policy_id': policy['id']}).encode()
        while True:
            status, _url, data = A.raw('POST', f'{API}/scans', body,
                                       {'Content-Type': 'application/json'}, 300)
            if status == 503 and time.monotonic() < give_up:
                time.sleep(0.1)
                continue
            if status != 201:
                raise AssertionError(f'long scan: expected HTTP 201, got {status}: {data[:200]!r}')
            outcome['scan'] = json.loads(data)
            outcome['seconds'] = time.monotonic() - started
            return

    def busy_probe():
        status, _url, _body = A.raw('GET', review_path, timeout=10)
        return status == 503

    worker = Thread(target=long_scan)
    worker.start()
    saturated = False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and worker.is_alive():
        if busy_probe():
            time.sleep(0.3)
            if busy_probe():
                saturated = True
                break
        time.sleep(0.05)
    spare_health = roomctl.health(spare)
    drill.check(f'长扫描进行中，{busy.name} 处理槽已满（持续 HTTP 503）', saturated,
                f'同期 {spare.name} 健康={spare_health}')
    drill.check(f'长扫描占槽期间 {spare.name} 仍就绪', spare_health == 200)
    if saturated:
        _url, opened = B.desk(f"/desk/scans/{scan1['id']}")
        drill.check(f'{spare.name} 仍能打开已有扫描', scan1['id'].encode() in opened, scan1['id'])
        b_preview_url, b_page = B.desk(f"/desk/scans/{scan1['id']}/preview",
                                       {'hit': hit1['id'], 'padding_ms': 80, 'frames': 3})
        b_preview = urlsplit(b_preview_url).path
        b_png_rel = re.search(rb'src="(/desk/previews/[^"]+\.png)"', b_page).group(1).decode()
        _url, b_png = B.desk(b_png_rel)
        _url, b_wav = B.desk(b_preview + '/sound.wav')
        drill.check(f'{spare.name} 仍能读取原画格和试听',
                    b_png.startswith(b'\x89PNG') and b_wav.startswith(b'RIFF'), b_preview)
        _url, excerpt = B.expect('POST', f"{API}/scans/{scan1['id']}/evidence",
                                 json.dumps({'hit_ids': [hit1['id']], 'padding_ms': 80,
                                             'frames': 3}).encode(),
                                 {'Content-Type': 'application/json'}, 200)
        with ZipFile(BytesIO(excerpt)) as archive:
            names = set(archive.namelist())
            mapping = json.loads(archive.read(f"{hit1['id']}/clock.json"))
            pgm = archive.read(f"{hit1['id']}/frame-01.pgm")
            sound = archive.read(f"{hit1['id']}/sound.wav")
        drill.check(f'{spare.name} 摘片 ZIP 含原画格 PGM、WAV 与时标映射',
                    f"{hit1['id']}/clock.json" in names and pgm.startswith(b'P5\n')
                    and sound.startswith(b'RIFF') and mapping['sound'] is not None,
                    f"{len(names)} 个成员")
    worker.join()
    scan_long = outcome.get('scan', {})
    drill.check('长扫描最终完成并发布', bool(scan_long.get('id')),
                f"耗时 {outcome.get('seconds', 0):.1f}s · {scan_long.get('id', '无')}")
    status, _url, _body = A.raw('GET', review_path)
    drill.check(f'{busy.name} 空闲后恢复服务', status == 200)

    drill.phase(f'第 3 阶段：{busy.name} 登记源件后退出，{spare.name} 接管')
    revision_bytes = media['revision'].read_bytes()
    master2 = A.upload(urlencode({'label': f'revision-{nonce}', 'sound': 'first'}), revision_bytes)
    expected_scan2 = scan_identity(master2, policy)
    print(f'-- {busy.name} 已登记源件 {master2["id"]}（扫描 {expected_scan2} 尚未发布），现在退出',
          flush=True)
    if not roomctl.stop(room, busy):
        drill.check(f'{busy.name} 退出', False, '无法停止进程')
    drill.check(f'{busy.name} 已退出（端口不再应答）', unreachable(A, f'{API}/health'))
    drill.check(f'{spare.name} 仍在服务', roomctl.health(spare) == 200)

    _url, home = B.desk('/desk')
    discovered = sorted(set(re.findall(rb'/desk/scans/([a-z]+-[0-9a-f]{24})', home)))
    reviewable = {}
    for key in discovered:
        view = B.api('GET', f'/scans/{key.decode()}/review')
        reviewable[key.decode()] = view['status']
    drill.check('接手人核对可继续审的扫描',
                scan1['id'] in reviewable and scan_long.get('id') in reviewable,
                '；'.join(f'{key}→{state}' for key, state in reviewable.items()))

    stored = B.api('GET', f"/masters/{master2['id']}")
    object_path = vault_objects / stored['sha256']
    original = object_path.read_bytes() if object_path.exists() else b''
    drill.check('退出前登记的源件仍指向原字节',
                stored['sha256'] == sha256(revision_bytes).hexdigest()
                and sha256(original).hexdigest() == stored['sha256'],
                stored['sha256'][:16])
    status, _url, _body = B.raw('GET', f'{API}/scans/{expected_scan2}')
    drill.check('退出前该源件的扫描确实尚未发布', status == 404, expected_scan2)
    scan2 = B.api('POST', '/scans', {'master_id': master2['id'], 'policy_id': policy['id']}, 201)
    view2 = B.api('GET', f"/scans/{scan2['id']}/review")
    drill.check('接手人发布该扫描并可继续审', scan2['id'] == expected_scan2 and 'status' in view2,
                f"{scan2['id']} → {view2['status']}")

    status, _url, body = B.raw('GET', preview_path)
    drill.check('旧摘片的浏览地址已失效', status == 404 and '预览已过期'.encode() in body,
                f'{preview_path}（候选 {hit1["id"]}，由 {busy.name} 生成）')
    re_url, _page = B.desk(f"/desk/scans/{scan1['id']}/preview",
                           {'hit': hit1['id'], 'padding_ms': 80, 'frames': 3})
    drill.check('接手人可对同一候选重新摘片', urlsplit(re_url).path != preview_path,
                urlsplit(re_url).path)

    desc = B.api('GET', f"/seals/{seal1['id']}")
    verified = B.api('GET', f"/seals/{seal1['id']}/verify")
    _url, bundle = B.expect('GET', f"{API}/seals/{seal1['id']}/bundle")
    with ZipFile(BytesIO(bundle)) as archive:
        index = json.loads(archive.read('index.json'))
        frozen_scan = json.loads(archive.read('scan.json'))
        frozen_review = json.loads(archive.read('review.json'))
    master1_object = vault_objects / master1['sha256']
    drill.check('旧冻结包仍指向原字节',
                verified['status'] == 'verified'
                and sha256(bundle).hexdigest() == desc['sha256']
                and index['master_sha256'] == master1['sha256']
                and sha256(master1_object.read_bytes()).hexdigest() == index['master_sha256'],
                f"包 {desc['sha256'][:16]} · 源 {master1['sha256'][:16]}")
    drill.check('旧冻结包仍指向原扫描与原裁决',
                frozen_scan['id'] == scan1['id'] and frozen_review['status'] == desc['review_status'],
                f"扫描 {frozen_scan['id']} · 冻结结论 {frozen_review['status']}")
    marker = f'接管复核 {nonce}'
    B.api('POST', f"/scans/{scan1['id']}/rulings",
          {'hit_id': hit1['id'], 'outcome': 'hold', 'reason': marker, 'reviewer': spare.name}, 201)
    live = B.api('GET', f"/scans/{scan1['id']}/review")
    _url, bundle_after = B.expect('GET', f"{API}/seals/{seal1['id']}/bundle")
    with ZipFile(BytesIO(bundle_after)) as archive:
        frozen_after = json.loads(archive.read('review.json'))
    drill.check('新裁决已生效且旧冻结包裁决不变',
                any(marker in record['reason'] for record in live['rulings'])
                and frozen_after['status'] == desc['review_status']
                and marker.encode() not in bundle_after,
                f"实时 {live['status']} · 冻结仍为 {frozen_after['status']}")
    return drill


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=roomctl.DEPLOY / 'positions.json')
    parser.add_argument('--busy', default='desk-a', help='被长扫描占满并退出的操作位')
    parser.add_argument('--takeover', default='desk-b', help='继续审片并接管的操作位')
    parser.add_argument('--keep', action='store_true', help='复用现有保管库而不是重建')
    parser.add_argument('--leave-running', action='store_true', help='演练结束后保留两个进程')
    args = parser.parse_args()
    room = roomctl.Room(args.config)
    for name in (args.busy, args.takeover):
        if name not in room.positions:
            print(f'配置中没有操作位 {name!r}')
            return 2
    busy, spare = room.positions[args.busy], room.positions[args.takeover]
    nonce = f'{int(time.time())}'

    roomctl.stop(room, busy)
    roomctl.stop(room, spare)
    if not args.keep and room.root.exists():
        shutil.rmtree(room.root)
    room.root.mkdir(parents=True, exist_ok=True)
    media = ensure_media(room)
    if not roomctl.start(room, busy) or not roomctl.start(room, spare):
        print('无法启动两个操作位，演练中止')
        return 1
    try:
        drill = run_drill(room, busy, spare, media, nonce)
    finally:
        if not args.leave_running:
            roomctl.stop(room, busy)
            roomctl.stop(room, spare)
    total = drill.count
    print(f'\n演练结果：{"通过" if drill.failures == 0 else "失败"} '
          f'({total - drill.failures}/{total} 项核对通过)')
    print(f'保管库 {room.root} · 日志 {room.run_dir} · 样片 {room.media_dir}')
    if args.leave_running:
        print(f'进程保留运行：{busy.origin} 与 {spare.origin}（停止：python3 deploy/roomctl.py stop all）')
    return 0 if drill.failures == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
