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

    # 补证：申请表单、历史/当前来源、列表、独立校验，且原冻结包哈希不变
    seal_page = request(origin, seal)[2]
    assert '申请补证'.encode() in seal_page and '按历史决定补充同一次观察'.encode() in seal_page
    supplement_target, _, supplement_page = request(
        origin, seal + '/supplement',
        {'basis': 'historical', 'hit': hit['id'], 'reason': '接收方需要多两格上下文',
         'padding_ms': 160, 'frames': 5})
    assert '补证详情'.encode() in supplement_page and '按历史决定补充同一次观察'.encode() in supplement_page
    supplement_key = supplement_target.rsplit('/', 1)[1]
    supplement_descriptor = tour.call(f'/supplements/{supplement_key}')
    assert supplement_descriptor['seal_sha256'] == descriptor['sha256']
    assert tour.call(f'/supplements/{supplement_key}/verify')['status'] == 'verified'
    assert request(origin, f'/desk/supplements/{supplement_key}/verify', {'verify': 1})[2] \
        .find('已通过校验'.encode()) >= 0

    # 后版扫描候选不能混入历史补证；按当前审片另做则显式记录来源
    later_hit = tour.hit(second, 'sound_gap')
    assert '找不到'.encode() in request(
        origin, seal + '/supplement',
        {'basis': 'historical', 'hit': later_hit['id'], 'reason': '混用来源'}, expected=404)[2]
    current_target, _, current_page = request(
        origin, seal + '/supplement',
        {'basis': 'current', 'hit': later_hit['id'], 'reason': '按后版复审',
         'scan_id': second['id']})
    assert '按当前审片另做'.encode() in current_page
    current_key = current_target.rsplit('/', 1)[1]
    current_descriptor = tour.call(f'/supplements/{current_key}')
    assert current_descriptor['basis'] == 'current' and current_descriptor['scan_id'] == second['id']

    # 原冻结包字节与描述符均未被补证改动
    assert tour.call(f'/seals/{key}')['sha256'] == descriptor['sha256']
    refreshed = request(origin, seal)[2]
    assert supplement_key.encode() in refreshed and current_key.encode() in refreshed

    query = urlencode({'before': first['id'], 'after': second['id']})
    compared = request(origin, '/desk/compare?' + query)[2]
    assert '前版独有'.encode() in compared and first['id'].encode() in compared
    assert '找不到'.encode() in request(origin, route + '?hit=missing', expected=404)[2]
    print('审片台：实测时间轴、证据画格与 WAV、裁决、冻结校验、补证来源区分、版次比对均通过')


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
