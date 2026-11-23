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
    route = f'/desk/scans/{first["id"]}'
    picture_only = request(origin, f'/desk/scans/{singular["id"]}')[2]
    assert '未选轨'.encode() in picture_only and '画面缺口'.encode() in picture_only

    hit = tour.hit(first, 'sound_gap')
    details = request(origin, route + '?' + urlencode({'hit': hit['id']}))[2]
    assert '节目时间轴'.encode() in details and hit['id'].encode() in details
    preview, _, page = request(origin, route + '/preview', {'hit': hit['id'], 'padding_ms': 80, 'frames': 3})
    assert '补零区间'.encode() in page and '源采样'.encode() in page
    assert '回到异常'.encode() in page and b'role="slider"' in page and b'lane-fill' in page
    assert page.count('试听此画格'.encode()) == 3
    clock = json.loads(re.search(rb'id="evidence-clock">([^<]+)</script>', page)[1])
    assert clock['rate'] == 8000 and (clock['first'], clock['end']) == (15360, 18240)
    assert clock['span'] == [1920000, 2280000] and clock['hit'] == [2000000, 2200000]
    assert clock['filled'] == [[16000, 17600]] and clock['picture'] == [[1920000, 2280000]]
    assert clock['frames'] == [[1980000, 1960000], [2100000, 2080000], [2220000, 2200000]]
    png = re.search(rb'src="(/desk/previews/[^"]+\.png)"', page)[1]
    assert request(origin, png.decode())[2].startswith(b'\x89PNG')
    assert request(origin, preview + '/sound.wav')[2].startswith(b'RIFF')

    holed = tour.scan('picture-hole')
    gap = tour.hit(holed, 'picture_gap')
    _, _, hole_page = request(origin, f'/desk/scans/{holed["id"]}/preview',
                              {'hit': gap['id'], 'padding_ms': 80, 'frames': 3})
    hole_clock = json.loads(re.search(rb'id="evidence-clock">([^<]+)</script>', hole_page)[1])
    assert hole_clock['hit'] == [2000000, 2520000]
    assert hole_clock['picture'] == [[1920000, 2000000], [2520000, 2600000]]
    assert hole_clock['frames'] == [[2033333, None], [2260000, None], [2486666, None]]
    assert hole_page.count('无画格覆盖'.encode()) == 3

    silent = tour.hit(singular, 'picture_gap')
    _, _, silent_page = request(origin, f'/desk/scans/{singular["id"]}/preview',
                                {'hit': silent['id'], 'padding_ms': 80, 'frames': 3})
    assert '未选伴音轨'.encode() in silent_page and b'evidence-clock' not in silent_page
    assert '试听此画格'.encode() not in silent_page

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
    print('审片台：实测时间轴、证据画格与 WAV、试听光标坐标、裁决、冻结校验、版次比对均通过')


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
