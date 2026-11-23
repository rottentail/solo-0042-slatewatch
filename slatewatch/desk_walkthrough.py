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
    assert '找不到'.encode() in request(origin, route + '?hit=missing', expected=404)[2]

    headline = tour.scan('headline')
    head_route = f'/desk/scans/{headline["id"]}'
    empty = request(origin, head_route)[2]
    assert '导入制作章节表'.encode() in empty and '章节参考轨'.encode() in empty
    sheet = {
        'label': '制作部章节表',
        'chapters': [
            {'title': '片头标志', 'picture_time_us': 1600000, 'reel_time_us': 900000},
            {'title': '正片开场', 'picture_time_us': 1300000, 'reel_time_us': 2500000},
            {'title': '静帧段落', 'origin': 'picture', 'picture_time_us': 2600000},
        ],
    }
    _, _, imported = request(origin, head_route + '/chapters', {'sheet_json': json.dumps(sheet, ensure_ascii=False)})
    assert '已确定章节'.encode() in imported and '候选范围章节'.encode() in imported
    assert '片头标志'.encode() in imported and '节目画面起点'.encode() in imported
    assert '候选范围（画面/整卷两读均可取）'.encode() in imported
    # The determined chapter offers a real selectable anomaly link back to the finding page.
    freeze = next(hit for hit in headline['hits'] if hit['kind'] == 'freeze')
    assert f'/desk/scans/{headline["id"]}?hit={freeze["id"]}'.encode() in imported
    _, _, linked = request(origin, head_route + '/chapter-link',
                           {'chapter_seq': 3, 'hit_id': freeze['id']})
    assert '旧关联已失效'.encode() not in linked
    # A foreign-scan finding cannot be attached from the browser either.
    foreign = next(hit for hit in second['hits'] if hit['kind'] == 'freeze')['id']
    assert request(origin, head_route + '/chapter-link',
                   {'chapter_seq': 3, 'hit_id': foreign}, expected=404)[1].startswith('text/html')
    print('审片台：实测时间轴、证据画格与 WAV、裁决、冻结校验、版次比对、章节参考轨均通过')


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
