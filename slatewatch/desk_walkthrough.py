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
        response = urlopen(Request(origin + path, data=body, headers=headers), timeout=60)
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
    assert '同页预览选中候选'.encode() in details
    assert details.count(b'type="checkbox"') == len(first['hits'])
    preview, _, page = request(origin, route + '/preview', {'hit': hit['id'], 'padding_ms': 80, 'frames': 3})
    assert '补零区间'.encode() in page and '源采样'.encode() in page
    png = re.search(rb'src="(/desk/previews/[^"]+\.png)"', page)[1]
    assert request(origin, png.decode())[2].startswith(b'\x89PNG')
    wav = re.search(rb'src="(/desk/previews/[^"]+\.wav)"', page)[1]
    assert request(origin, wav.decode())[2].startswith(b'RIFF')

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
    capacity(tour, origin)
    print('审片台：实测时间轴、证据画格与 WAV、裁决、冻结校验、版次比对、同页复核与容量降级均通过')


def capacity(tour, origin):
    """Review overlapping findings on one page while an oversized excerpt degrades."""
    record = tour.scan('overrun')
    by_kind = {item['kind']: item for item in record['hits']}
    black, silence, clip = by_kind['black'], by_kind['silence'], by_kind['clip']
    route = f'/desk/scans/{record["id"]}'
    fields = [('hit', black['id']), ('hit', silence['id']), ('hit', clip['id']),
              ('padding_ms', '2000'), ('frames', '3')]
    _, _, solo = request(origin, route + '/preview', {'hit': black['id'], 'padding_ms': 2000, 'frames': 3})
    assert '无法在线播放'.encode() not in solo
    wav = re.search(rb'src="(/desk/previews/[^"]+\.wav)"', solo)[1]
    assert len(request(origin, wav.decode())[2]) == 66 * 48000 * 4 + 44
    _, _, board = request(origin, route + '/preview', fields)
    for item in (black, silence, clip):
        assert item['id'].encode() in board
    assert board.count('无法在线播放'.encode()) == 2
    assert len(re.findall(rb'src="/desk/previews/[^"]+\.png"', board)) == 9
    sizes = {}
    for url in re.findall(rb'src="(/desk/previews/[^"]+\.wav)"', board):
        key = url.rsplit(b'/', 1)[1].removesuffix(b'-sound.wav').decode()
        sizes[key] = len(request(origin, url.decode())[2])
    assert sizes[clip['id']] == 6 * 48000 * 4 + 44
    assert 11 * 1024 * 1024 < sizes[black['id']] < 66 * 48000 * 4 + 44
    assert 11 * 1024 * 1024 < sizes[silence['id']] < 66 * 48000 * 4 + 44
    _, mime, bundle = request(origin, route + '/evidence', fields)
    assert mime == 'application/zip'
    with ZipFile(BytesIO(bundle)) as archive:
        for item, seconds in ((black, 66), (silence, 66), (clip, 6)):
            mapping = json.loads(archive.read(f"{item['id']}/clock.json"))
            sound = mapping['sound']
            assert 'dropped_sample_ranges' not in sound
            assert sound['end_source_sample'] - sound['first_source_sample'] == seconds * 48000
            assert len(archive.read(f"{item['id']}/sound.wav")) == seconds * 48000 * 4 + 44


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
