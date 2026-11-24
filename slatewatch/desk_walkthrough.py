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
    rework(origin, tour)
    assert '找不到'.encode() in request(origin, route + '?hit=missing', expected=404)[2]
    print('审片台：实测时间轴、证据画格与 WAV、裁决、冻结校验、版次比对、逐窗核销均通过')


def rework(origin, tour):
    """Exercise the browser recheck desk on the equal-length rework pair."""
    policy = tour.policy()
    earlier = tour.master('rework-before')
    later = tour.master('rework-after', sound='2')
    before = tour.call('/scans', {'master_id': earlier['id'], 'policy_id': policy['id']}, 201)
    after = tour.call('/scans', {'master_id': later['id'], 'policy_id': policy['id']}, 201)
    hit = next(item for item in before['hits']
               if item['kind'] == 'clip' and item['channel'] == 2)
    workspace = f'/desk/scans/{after["id"]}/recheck'
    plan = request(origin, workspace + '?' + urlencode(
        {'before': before['id'], 'hit': hit['id'], 'after_channel': '2'}))[2]
    assert '待核销旧窗'.encode() in plan and '沿用前版第 4 窗'.encode() in plan
    landing, _, page = request(origin, workspace, {
        'before': before['id'], 'hit': hit['id'], 'after_channel': '2',
        'reviewer': '审片员', 'reason': '等长换轨逐窗复核'})
    assert '可销'.encode() in page and '仍削顶'.encode() in page and '证据不足'.encode() in page
    assert '前版伴音 2 → 后版伴音 2'.encode() in page
    replay = re.search(rb'href="(/desk/scans/[^"]+/listen\?[^"]+)"', page)[1].decode()
    assert request(origin, replay.replace('&amp;', '&'))[2].startswith(b'RIFF')
    switch = request(origin, landing)[2]
    assert '核销记录'.encode() in switch and '前版伴音 2 → 后版伴音 2'.encode() in switch
    assert f'/desk/scans/{before["id"]}?hit={hit["id"]}'.encode() in switch
    return landing


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
