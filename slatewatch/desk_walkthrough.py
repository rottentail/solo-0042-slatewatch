"""Observe the browser workbench over HTTP using real generated media."""

from array import array
from bisect import bisect_right
from fractions import Fraction
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
import wave
from zipfile import ZipFile

from .clock import sample_boundary
from .http import Server
from .media import Picture, decode
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


def wav_pcm(data):
    with wave.open(BytesIO(data), 'rb') as audio:
        pcm = array('h')
        pcm.frombytes(audio.readframes(audio.getnframes()))
        return pcm, audio.getframerate(), audio.getnchannels(), audio.getnframes()


def source_pcm(directory, master):
    """Decode a registered master independently of the audition preparation."""
    path = directory / 'vault' / 'objects' / master['sha256']
    blocks = [entry for entry in decode(path, master['media']) if not isinstance(entry, Picture)]
    starts = [block.first_sample for block in blocks]

    def at(index, channel):
        block = blocks[bisect_right(starts, index) - 1]
        return block.samples[(index - block.first_sample) * block.channels + channel]

    return at


def unzipped(data):
    with ZipFile(BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def audition(tour, origin):
    """主混 / 参考轨对听：异速率、异录音起点、参考轨中段缺包与实录为零。"""
    main = tour.master('mix-main')
    reference = tour.master('mix-ref')
    assert (main['media']['sound']['rate'], reference['media']['sound']['rate']) == (44100, 48000)
    assert (main['media']['origin_us'], reference['media']['origin_us']) == (0, 800000)
    policy = tour.policy()
    scan = tour.call('/scans', {'master_id': main['id'], 'policy_id': policy['id']}, 201)
    hit = tour.hit(scan, 'clip')
    assert (hit['start_us'], hit['end_us']) == (2500000, 3500000)
    route = f'/desk/scans/{scan["id"]}'
    selection = {'hit_ids': [hit['id']], 'padding_ms': 120, 'frames': 3}
    before_scan = tour.call(f'/scans/{scan["id"]}')
    before_review = tour.call(f'/scans/{scan["id"]}/review')
    before_evidence = tour.call(f'/scans/{scan["id"]}/evidence', selection)

    plain, _, plain_page = request(origin, route + '/preview', {'hit': hit['id'], 'padding_ms': 120, 'frames': 3})
    assert '原轨伴音'.encode() in plain_page and '准备对听'.encode() in plain_page
    script = request(origin, '/desk/static/audition.js')
    assert script[1] == 'text/javascript' and 'currentTime' in script[2].decode()

    form = {'hit': hit['id'], 'padding_ms': 120, 'frames': 3, 'reference': reference['id']}
    pair, _, pair_page = request(origin, route + '/preview', form)
    assert '主混 / 参考轨对听'.encode() in pair_page and '准备对听'.encode() not in pair_page
    mapping = json.loads(request(origin, pair + '/clock.json')[2])
    box = mapping['audition']
    assert box['rate'] == 48000
    main_track, ref_track = box['main'], box['reference']
    assert (main_track['source_rate'], main_track['converted']) == (44100, True)
    assert (ref_track['source_rate'], ref_track['converted']) == (48000, False)
    assert (main_track['master_id'], ref_track['master_id']) == (main['id'], reference['id'])
    assert (main_track['sha256'], ref_track['sha256']) == (main['sha256'], reference['sha256'])
    assert ref_track['filled_sample_ranges'] == [{'first': 124800, 'end': 134400}]
    assert main_track['filled_sample_ranges'] == []

    first_out = sample_boundary(mapping['start_us'], box['rate'])
    out_count = sample_boundary(mapping['end_us'], box['rate']) - first_out
    main_pcm = wav_pcm(request(origin, pair + '/main.wav')[2])
    ref_pcm = wav_pcm(request(origin, pair + '/reference.wav')[2])
    assert main_pcm[1:] == (48000, 2, out_count) and ref_pcm[1:] == (48000, 2, out_count)
    main_pcm, ref_pcm = main_pcm[0], ref_pcm[0]

    main_at = source_pcm(tour.directory, main)
    ref_at = source_pcm(tour.directory, reference)
    filled = ref_track['filled_sample_ranges']

    def is_filled(sample):
        return any(item['first'] <= sample < item['end'] for item in filled)

    first_main = main_track['first_source_sample']
    end_main = main_track['end_source_sample']
    for j in range(out_count):
        position = first_out + j
        for channel in range(2):
            # 参考轨为原采样率直达：缺口内为零，其余逐样本等于原轨
            expect = 0 if is_filled(position) else ref_at(position, channel)
            assert ref_pcm[j * 2 + channel] == expect, ('reference', j, channel)
            # 主混经 44.1k→48k 试听转换：按映射用有理数插值独立复算
            base, rem = divmod(position * 44100 - first_main * 48000, 48000)
            if base < 0:
                expect = main_at(first_main, channel)
            else:
                low = main_at(first_main + base, channel)
                high = main_at(first_main + base + 1, channel) \
                    if first_main + base + 1 < end_main else low
                expect = round(low + (high - low) * Fraction(rem, 48000))
            assert main_pcm[j * 2 + channel] == expect, ('main', j, channel)

    # 游标刚贴在缺口边界：节目 2.6s 处主混是削顶样本，参考轨恰为补零起点
    edge = 124800 - first_out
    assert (first_out + edge) * 1_000_000 == 2_600_000 * 48000
    assert (ref_pcm[edge * 2], ref_pcm[edge * 2 + 1]) == (0, 0)
    assert (ref_pcm[(edge - 1) * 2], ref_pcm[(edge - 1) * 2 + 1]) == \
        (ref_at(124799, 0), ref_at(124799, 1))
    assert 124800 * 44100 % 48000 == 0 and main_pcm[edge * 2 + 1] == 32767

    # 参考轨实录为零：[3.0s, 3.3s) 源采样确为零但不属补零；主混同位置仍在削顶
    for j in range(144000 - first_out, 158400 - first_out):
        assert (ref_pcm[j * 2], ref_pcm[j * 2 + 1]) == (0, 0)
        assert not is_filled(first_out + j)
        assert main_pcm[j * 2 + 1] == 32767

    # 试听不改变主混扫描、复核状态与既有证据
    assert tour.call(f'/scans/{scan["id"]}') == before_scan
    assert tour.call(f'/scans/{scan["id"]}/review') == before_review
    assert unzipped(tour.call(f'/scans/{scan["id"]}/evidence', selection)) == unzipped(before_evidence)

    # 误用与容量边界
    own = request(origin, route + '/preview', {**form, 'reference': main['id']}, expected=400)[2]
    assert '另一卷母带'.encode() in own
    silent = tour.master('no-sound')
    no_track = request(origin, route + '/preview', {**form, 'reference': silent['id']}, expected=400)[2]
    assert '未选伴音轨'.encode() in no_track
    request(origin, route + '/preview', {**form, 'reference': 'master-missing'}, expected=404)
    long_master = tour.master('mix-long')
    long_scan = tour.call('/scans', {'master_id': long_master['id'], 'policy_id': policy['id']}, 201)
    long_hit = tour.hit(long_scan, 'clip')
    over = {'hit': long_hit['id'], 'padding_ms': 0, 'frames': 1, 'reference': reference['id']}
    too_big = request(origin, f'/desk/scans/{long_scan["id"]}/preview', over, expected=400)[2]
    assert '对听摘片超过浏览器预览容量'.encode() in too_big
    print('对听：44.1k/48k 异起点对切、缺口边界与实录为零逐样本核对、扫描与证据未被改变、容量上限生效')


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
    assert json.loads(request(origin, preview + '/clock.json')[2])['hit_id'] == hit['id']

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
    print('审片台：实测时间轴、证据画格与 WAV、裁决、冻结校验、版次比对均通过')
    audition(tour, origin)


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
