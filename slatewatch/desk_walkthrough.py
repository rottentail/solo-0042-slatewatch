"""Observe the browser workbench over HTTP using real generated media."""

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import re
from zipfile import ZipFile

from .http import Server
from .walkthrough import Tour


def request(origin, path, data=None, expected=200):
    headers = {'Origin': origin}
    body = None if data is None else urlencode(data).encode()
    try:
        response = urlopen(Request(origin + path, data=body, headers=headers), timeout=20)
    except HTTPError as exc:
        response = exc
    with response:
        assert response.status == expected, (path, response.status, response.read())
        return response.url.removeprefix(origin), response.headers.get_content_type(), response.read()


def run(server, root):
    tour = Tour(server.server_address, root)
    origin = tour.origin.removesuffix('/slatewatch/v1')
    assert '暂无扫描'.encode() in request(origin, '/desk')[2]
    assert '开始扫描'.encode() in request(origin, '/desk/intake')[2]
    first = tour.scan()
    second = tour.scan('revision')
    singular = tour.scan('picture-hole', sound='none')
    held = tour.scan('long-frame')
    route = f'/desk/scans/{first["id"]}'
    picture_only = request(origin, f'/desk/scans/{singular["id"]}')[2]
    assert '未选轨'.encode() in picture_only and '画面缺口'.encode() in picture_only

    hit = tour.hit(first, 'sound_gap')
    details = request(origin, route + '?' + urlencode({'hit': hit['id']}))[2]
    assert '节目时间轴'.encode() in details and hit['id'].encode() in details
    assert '技术证据板'.encode() not in details
    preview, _, page = request(origin, route + '/preview', {'hit': hit['id'], 'padding_ms': 80, 'frames': 3})
    assert '补零区间'.encode() in page and '源采样'.encode() in page
    png = re.search(rb'src="(/desk/previews/[^"]+\.png)"', page)[1]
    assert request(origin, png.decode())[2].startswith(b'\x89PNG')
    assert request(origin, preview + '/sound.wav')[2].startswith(b'RIFF')

    steady = tour.hit(first, 'freeze')
    panel = request(origin, route + '?' + urlencode({'hit': steady['id']}))[2]
    assert '技术证据板'.encode() in panel
    _, _, steady_board = request(origin, route + '/board', {'hit': steady['id'], 'context': 2})
    assert '适合取差'.encode() in steady_board and '省略'.encode() in steady_board
    assert '长格间隔 · '.encode() not in steady_board and '覆盖缺口 · '.encode() not in steady_board

    gap = tour.hit(singular, 'picture_gap')
    _, _, gap_board = request(origin, f'/desk/scans/{singular["id"]}/board', {'hit': gap['id'], 'context': 2})
    assert '覆盖缺口 · 无法支持同一连续镜头的判断'.encode() in gap_board

    black = tour.hit(held, 'black')
    board_url, _, held_board = request(origin, f'/desk/scans/{held["id"]}/board', {'hit': black['id'], 'context': 2})
    assert '长格间隔 · 无法支持同一连续镜头的判断'.encode() in held_board
    assert '适合取差'.encode() in held_board and '事件内'.encode() in held_board
    for src in re.findall(rb'src="(/desk/boards/[^"]+\.png)"', held_board):
        assert request(origin, src.decode())[2].startswith(b'\x89PNG')
    assert re.search(rb'/desk/boards/[^"]+diff', held_board)

    seal, _, frozen = request(origin, route + '/seal', {'hit': hit['id']})
    assert '待审'.encode() in frozen
    _, _, updated = request(origin, route + '/ruling', {
        'hit': hit['id'], 'outcome': 'hold', 'reason': '伴音缺口待确认', 'reviewer': '审片员',
    })
    assert '人工扣留'.encode() in updated
    verified = request(origin, seal + '/verify', {'verify': 1})[2]
    assert '已通过校验'.encode() in verified
    key = seal.rsplit('/', 1)[1]
    descriptor = tour.call(f'/seals/{key}')
    bundle = tour.call(f'/seals/{key}/bundle')
    assert sha256(bundle).hexdigest() == descriptor['sha256']
    with ZipFile(BytesIO(bundle)) as archive:
        assert json.loads(archive.read('review.json'))['status'] == 'pending'
    query = urlencode({'before': first['id'], 'after': second['id']})
    compared = request(origin, '/desk/compare?' + query)[2]
    assert '前版独有'.encode() in compared and first['id'].encode() in compared
    assert '找不到'.encode() in request(origin, route + '?hit=missing', expected=404)[2]
    print('审片台：实测时间轴、证据画格与 WAV、技术证据板、裁决、冻结校验、版次比对均通过')


def main():
    with TemporaryDirectory(prefix='slatewatch-desk-') as directory:
        root = Path(directory)
        with Server(('127.0.0.1', 0), root / 'vault') as server:
            runner = Thread(target=server.serve_forever)
            runner.start()
            try:
                run(server, root)
            finally:
                server.shutdown()
                runner.join()


if __name__ == '__main__':
    main()
