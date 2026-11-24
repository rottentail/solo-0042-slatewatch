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

    corrected_scan = tour.scan('picture-hole')
    corrected_route = f'/desk/scans/{corrected_scan["id"]}'
    black = tour.hit(corrected_scan, 'black')
    silence = next(hit for hit in corrected_scan['hits'] if hit['kind'] == 'silence' and hit['channel'] == 1)
    hole = tour.hit(corrected_scan, 'picture_gap')
    first_intent = tour.call(f'/scans/{corrected_scan["id"]}/intents', {
        'kind': 'black', 'start_us': black['start_us'], 'end_us': black['end_us'],
        'reason': '制作记录 A：片头黑场', 'record': 'PROD-A-17'}, 201)
    tour.call(f'/scans/{corrected_scan["id"]}/intents', {
        'kind': 'black', 'start_us': black['start_us'], 'end_us': black['end_us'] - 80000,
        'reason': '制作记录 B：交接单', 'record': 'PROD-B-04'}, 201)
    page_origin, _, ledger_page = request(origin, corrected_route + '?' + urlencode({'hit': black['id']}))
    assert '采信依据片段'.encode() in ledger_page and '制作意图台账'.encode() in ledger_page
    assert 'PROD-A-17'.encode() in ledger_page and '登记制作方更正'.encode() in ledger_page
    cut = black['start_us'] + 120000
    _, _, corrected_page = request(origin, corrected_route + '/intent-correction', {
        'hit': black['id'], 'intent_seq': first_intent['seq'], 'start_us': black['start_us'],
        'end_us': cut, 'record': 'PROD-A-17-R1', 'reason': '制作方更正：黑场提前收束'})
    assert '已被更正'.encode() in corrected_page and '恢复待查'.encode() in corrected_page
    assert '仍有采信依据'.encode() in corrected_page and '更正自'.encode() in corrected_page
    # ruling history stays visible after accept then reset
    request(origin, corrected_route + '/ruling', {
        'hit': black['id'], 'outcome': 'accept', 'reason': '交接前人工放行', 'reviewer': '甲班'})
    request(origin, corrected_route + '/ruling', {
        'hit': black['id'], 'outcome': 'reset', 'reason': '制作方更正撤回', 'reviewer': '乙班'})
    _, _, trail_page = request(origin, corrected_route + '?' + urlencode({'hit': black['id']}))
    assert '人工裁决与操作史'.encode() in trail_page
    assert '交接前人工放行'.encode() in trail_page and '制作方更正撤回'.encode() in trail_page
    # channel-qualified silence and unmeasured hole render their basis
    _, _, silence_page = request(origin, corrected_route + '?' + urlencode({'hit': silence['id']}))
    assert '恢复待查'.encode() in silence_page
    request(origin, corrected_route + '/ruling', {
        'hit': hole['id'], 'outcome': 'accept', 'reason': '缺口候选放行', 'reviewer': '甲班'})
    _, _, hole_page = request(origin, corrected_route + '?' + urlencode({'hit': hole['id']}))
    assert '从无可证实画面'.encode() in hole_page and '人工通过'.encode() in hole_page
    print('审片台：实测时间轴、证据画格与 WAV、裁决、冻结校验、版次比对均通过')
    print('审片台：意图台账、更正事件、采信片段与操作史交接视图均通过')


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
