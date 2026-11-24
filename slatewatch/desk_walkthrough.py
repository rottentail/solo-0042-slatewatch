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
    selection = {'hit': hit['id'], 'padding_ms': 80, 'frames': 3}
    preview, _, page = request(origin, route + '/preview', selection)
    assert preview == f'/desk/previews/{first["id"]}.{hit["id"]}.80.3'
    assert '补零区间'.encode() in page and '源采样'.encode() in page and '留边 80 毫秒'.encode() in page
    again, _, _ = request(origin, route + '/preview', selection)
    assert again == preview
    shifted, _, _ = request(origin, route + '/preview', {'hit': hit['id'], 'padding_ms': 120, 'frames': 3})
    assert shifted != preview and '留边 120 毫秒'.encode() in request(origin, shifted)[2]
    png = re.search(rb'src="(/desk/previews/[^"]+\.png)"', page)[1]
    assert request(origin, png.decode())[2].startswith(b'\x89PNG')
    assert request(origin, preview + '/sound.wav')[2].startswith(b'RIFF')

    with server.preview_cache.lock:  # 模拟缓存挤出或进程重启
        server.preview_cache.entries.clear()
        server.preview_cache.total = 0
    _, _, recovery = request(origin, preview, expected=404)
    assert '可再生成'.encode() in recovery and '恢复条件核对'.encode() in recovery
    restored, _, page = request(origin, route + '/preview', selection)
    assert restored == preview and '补零区间'.encode() in page

    seal, _, frozen = request(origin, route + '/seal', selection)
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

    master = tour.call(f'/masters/{first["master_id"]}')
    (root / 'vault' / 'objects' / master['sha256']).unlink()  # 保管库只剩描述与冻结包
    with server.preview_cache.lock:
        server.preview_cache.entries.clear()
        server.preview_cache.total = 0
    assert '找不到'.encode() in request(origin, route + '/preview', selection, expected=404)[2]
    _, _, recovery = request(origin, preview, expected=404)
    assert '只能验证旧内容'.encode() in recovery and key.encode() in recovery
    assert '摘片范围与抽样点相同'.encode() in recovery
    assert '已通过校验'.encode() in request(origin, seal + '/verify', {'verify': 1})[2]
    clip = tour.hit(first, 'clip')
    lost = f'/desk/previews/{first["id"]}.{clip["id"]}.120.3'
    _, _, recovery = request(origin, lost, expected=404)
    assert '材料不足'.encode() in recovery and master['sha256'].encode() in recovery
    assert '预览链接不完整'.encode() in request(origin, '/desk/previews/unknown', expected=404)[2]
    print('审片台：实测时间轴、证据画格与 WAV、裁决、冻结校验、版次比对、预览恢复均通过')


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
