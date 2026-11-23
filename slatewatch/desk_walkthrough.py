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

    # Recipient disclosure: only the granted channel and window leave the desk.
    silence = tour.hit(first, 'silence')
    detail = request(origin, route + '?' + urlencode({'hit': silence['id']}))[2]
    assert '制作授权披露件'.encode() in detail and '只授权源声道'.encode() in detail
    disclosure_page, _, created = request(origin, route + '/disclosure', {
        'hit': silence['id'], 'recipient': '外包声学承包商', 'padding_ms': 0, 'frames': 3,
        'channel': silence['channel'], 'note': '仅授权左声道静音位置',
    })
    assert '/desk/disclosures/'.encode() in disclosure_page.encode()
    disclosure_key = disclosure_page.rsplit('/', 1)[1]
    record = tour.call(f'/disclosures/{disclosure_key}')
    # A hold ruling was recorded earlier, so the frozen review status is the
    # current whole-reel status; the disclosure does not reset it to pending.
    assert record['recipient'] == '外包声学承包商' and record['review_status'] == 'hold'
    detail_html = request(origin, disclosure_page)[2]
    assert '源母带 SHA-256'.encode() in detail_html and disclosure_key.encode() in detail_html
    disclosure_zip = tour.call(f'/disclosures/{disclosure_key}/bundle')
    assert sha256(disclosure_zip).hexdigest() == record['sha256']
    with ZipFile(BytesIO(disclosure_zip)) as archive:
        item = json.loads(archive.read('disclosure.json'))['items'][0]
        assert item['sound']['channels'] == [silence['channel']]
        assert item['authorized_window'] == {'start_us': silence['start_us'],
                                             'end_us': silence['end_us']}
        names = archive.namelist()
        assert all('frame' not in name or name.endswith('.pgm') for name in names)
    checked = request(origin, f'/desk/disclosures/{disclosure_key}/verify', {'verify': 1})[2]
    assert '均已通过校验'.encode() in checked
    listing = request(origin, '/desk/disclosures')[2]
    assert disclosure_key.encode() in listing and '外包声学承包商'.encode() in listing
    # A picture finding offers channel checkboxes instead of a hidden single lane.
    black = tour.hit(first, 'black')
    black_page = request(origin, route + '?' + urlencode({'hit': black['id']}))[2]
    assert '授权声道'.encode() in black_page and b'channel-1' in black_page and b'channel-2' in black_page
    query = urlencode({'before': first['id'], 'after': second['id']})
    compared = request(origin, '/desk/compare?' + query)[2]
    assert '前版独有'.encode() in compared and first['id'].encode() in compared
    assert '找不到'.encode() in request(origin, route + '?hit=missing', expected=404)[2]
    print('审片台：实测时间轴、证据画格与 WAV、裁决、冻结校验、版次比对均通过')


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
