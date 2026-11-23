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
    png = re.search(rb'src="(/desk/previews/[^"]+\.png)"', page)[1]
    assert request(origin, png.decode())[2].startswith(b'\x89PNG')
    assert request(origin, preview + '/sound.wav')[2].startswith(b'RIFF')

    assert 'region-tool'.encode() in page
    source_us = re.search(rb'name="source_us" value="(\d+)"', page)[1].decode()
    region_url, _, region_page = request(origin, route + '/region', {
        'hit': hit['id'], 'source_us': source_us, 'x': 4, 'y': 3, 'width': 12, 'height': 9})
    assert '局部源像素复查'.encode() in region_page and '源像素矩形'.encode() in region_page
    assert request(origin, region_url + '/region.png')[2].startswith(b'\x89PNG')
    with ZipFile(BytesIO(request(origin, region_url + '/region.zip')[2])) as archive:
        mapping = json.loads(archive.read('region.json'))
        assert mapping['source_us'] == int(source_us)
        rect = mapping['source']['rect']
        assert archive.read('region.pgm').startswith(f"P5\n{rect['width']} {rect['height']}\n255\n".encode())
    gap = next(item for item in singular['hits'] if item['kind'] == 'picture_gap')
    missing = request(origin, f'/desk/scans/{singular["id"]}/region',
                      {'hit': gap['id'], 'source_us': 2040000, 'x': 0, 'y': 0, 'width': 4, 'height': 4}, expected=404)[2]
    assert '没有源画格覆盖'.encode() in missing
    request(origin, route + '/region',
            {'hit': hit['id'], 'source_us': source_us, 'x': 75, 'y': 0, 'width': 10, 'height': 4}, expected=400)

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
    print('审片台：实测时间轴、证据画格与 WAV、局部源像素复查、裁决、冻结校验、版次比对均通过')


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
