"""Observe the browser workbench over HTTP using real generated media."""

from hashlib import sha256
from io import BytesIO
from collections import OrderedDict
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

from .clock import InputError
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


def assert_no_zip_path(server, action):
    """A browser preview must consume raw materials, never build an archive."""
    app = server.app
    originals = (app.evidence, app.evidence_materials)
    calls = {'zip': 0, 'materials': 0}

    def blocked_evidence(*args, **kwargs):
        calls['zip'] += 1
        raise AssertionError('preview must not enter the ZIP packaging path')

    def counting_materials(*args, **kwargs):
        calls['materials'] += 1
        return originals[1](*args, **kwargs)

    app.evidence = blocked_evidence
    app.evidence_materials = counting_materials
    try:
        action()
    finally:
        app.evidence, app.evidence_materials = originals
    assert calls['zip'] == 0 and calls['materials'] == 1, calls


def verify_preview_boundary(origin, server, route, hit):
    """Single preview renders PNG/WAV directly and never touches the ZIP path."""

    def open_preview():
        _path, _mime, page = request(
            origin, route + '/preview', {'hit': hit['id'], 'padding_ms': 80, 'frames': 3})
        assert request(origin, re.search(rb'src="([^"]+\.png)"', page)[1].decode())[2].startswith(b'\x89PNG')
        assert request(origin, _path + '/sound.wav')[2].startswith(b'RIFF')
        return page

    assert_no_zip_path(server, open_preview)


def verify_hole_frames(origin, server, tour, scan):
    """Targets inside a picture hole stay unattributed instead of inventing frames."""
    hit = tour.hit(scan, 'picture_gap')
    page_path, _mime, page = request(
        origin, f'/desk/scans/{scan["id"]}/preview',
        {'hit': hit['id'], 'padding_ms': 40, 'frames': 4})
    members, mapping = server.app.evidence_materials(
        scan['id'], {'hit_ids': [hit['id']], 'padding_ms': 40, 'frames': 4})[0]
    assert all(frame['file'] is None and frame['source_us'] is None for frame in mapping['picture'])
    assert '无画格覆盖'.encode() in page
    # An unselected sound lane is reported as such rather than fabricated.
    if mapping['sound'] is None:
        assert '未选伴音轨'.encode() in page
    else:
        assert request(origin, page_path + '/sound.wav')[2].startswith(b'RIFF')
    return mapping


def verify_duplicate_source_frame(origin, server, tour, scan):
    """Repeated hits on one source frame keep distinct targets and unique members."""
    hit = tour.hit(scan, 'sound_gap')
    _members, mapping = server.app.evidence_materials(
        scan['id'], {'hit_ids': [hit['id']], 'padding_ms': 0, 'frames': 8})[0]
    targets = [frame['target_us'] for frame in mapping['picture']]
    sources = [frame['source_us'] for frame in mapping['picture']]
    files = [frame['file'] for frame in mapping['picture']]
    assert len(targets) == len(set(targets)), 'sample targets must stay distinct'
    assert len(sources) != len(set(sources)), 'this window must reuse a source frame'
    assert len(files) == len(set(files)) and all(files), 'output members stay uniquely named'
    # The browser preview exposes one PNG per output member, attribution preserved.
    page_path, _mime, page = request(
        origin, f'/desk/scans/{scan["id"]}/preview',
        {'hit': hit['id'], 'padding_ms': 0, 'frames': 8})
    pngs = re.findall(rb'src="([^"]+\.png)"', page)
    assert len(pngs) == len(files)
    return mapping


def verify_padded_sound(origin, server, tour, scan):
    """Padding extends the WAV window; holes are zero-filled and listed separately."""
    hit = tour.hit(scan, 'sound_gap')
    members, mapping = server.app.evidence_materials(
        scan['id'], {'hit_ids': [hit['id']], 'padding_ms': 80, 'frames': 3})[0]
    sound = mapping['sound']
    with wave.open(BytesIO(members['sound.wav'])) as audio:
        assert audio.getframerate() == scan['tracks']['sound']['rate']
        assert audio.getnchannels() == scan['tracks']['sound']['channels']
        assert audio.getnframes() == sound['end_source_sample'] - sound['first_source_sample']
    filled = sound['filled_sample_ranges']
    covered = sum(item['end'] - item['first'] for item in filled)
    assert covered < audio.getnframes() and filled, 'the interior hole must remain listed as fill'
    _path, _mime, page = request(
        origin, f'/desk/scans/{scan["id"]}/preview',
        {'hit': hit['id'], 'padding_ms': 80, 'frames': 3})
    assert '补零区间'.encode() in page and '源采样'.encode() in page
    return mapping


def verify_rejection_and_eviction(origin, server, route, hit):
    """Preview refusal and cache eviction leave every formal delivery untouched."""
    cache = server.preview_cache
    descriptors_before = {item['id']: item for item in server.app.vault.recent('seal')}

    # Real cache semantics: filling the bounded entry count evicts oldest first.
    saved_entries, saved_total = cache.entries, cache.total
    cache.entries, cache.total = OrderedDict(), 0
    try:
        keys = [
            cache.put({'hit_id': f'probe-{index}', 'picture': [], 'sound': None, 'scan_id': 'probe'},
                      {f'frame-{index}.png': (b'x' * 16, 'image/png')})
            for index in range(13)
        ]
        assert len(cache.entries) == 12
        assert keys[0] not in cache.entries and keys[-1] in cache.entries
        cache.get(keys[-1])  # most recent stays readable
    finally:
        cache.entries, cache.total = saved_entries, saved_total

    # Genuine over-capacity refusal through the browser form still leaves deliveries intact.
    from .desk.cache import PREVIEW_LIMIT
    oversized = PREVIEW_LIMIT + 1
    try:
        cache.put({'hit_id': 'too-large', 'picture': [], 'sound': None, 'scan_id': 'probe'},
                  {'frame.png': (b'x' * oversized, 'image/png')})
        raise AssertionError('oversized preview should have been refused')
    except InputError:
        pass

    # Real admission followed by expiry: an evicted token 404s without touching the vault.
    cache.entries, cache.total = OrderedDict(), 0
    try:
        first, _mime, _page = request(
            origin, route + '/preview', {'hit': hit['id'], 'padding_ms': 40, 'frames': 2})
        admitted = next(iter(cache.entries))
        cache.entries.pop(admitted)
        assert request(origin, first, expected=404)[2]
    finally:
        cache.entries, cache.total = saved_entries, saved_total

    # Sealed descriptors and bytes are independent of the process-local cache.
    assert descriptors_before.keys() <= {item['id'] for item in server.app.vault.recent('seal')}
    for seal_id, record in descriptors_before.items():
        assert server.app.verify(seal_id)['status'] == 'verified'
        assert sha256(server.app.download(seal_id)).hexdigest() == record['sha256']


def verify_frozen_members_independent(origin, tour, scan):
    """One frozen batch delivers each member separately with unchanged snapshots."""
    review_before = tour.call(f'/scans/{scan["id"]}/review')
    chosen = [hit['id'] for hit in scan['hits'][:2]]
    selection = {'hit_ids': chosen, 'padding_ms': 80, 'frames': 3}
    sealed = tour.call(f'/scans/{scan["id"]}/seals', selection, 201)
    bundle = tour.call(f'/seals/{sealed["id"]}/bundle')
    materials = tour.call(f'/scans/{scan["id"]}/evidence', selection)
    with ZipFile(BytesIO(bundle)) as archive:
        names = set(archive.namelist())
        maps = json.loads(archive.read('evidence.json'))
        folders = {name.split('/')[1] for name in names if name.startswith('evidence/')}
        assert folders == set(chosen) and len(maps) == len(chosen)
        for mapping in maps:
            hit_id = mapping['hit_id']
            assert hit_id in chosen
            for frame in mapping['picture']:
                if frame['file']:
                    assert f'evidence/{hit_id}/{frame["file"]}' in names
            if mapping['sound']:
                assert f'evidence/{hit_id}/sound.wav' in names
        frozen_review = json.loads(archive.read('review.json'))
        assert frozen_review['status'] == review_before['status']
    # The instant archive and the frozen bundle carry the same per-member bytes.
    instant = {}
    with ZipFile(BytesIO(materials)) as archive:
        for name in archive.namelist():
            instant[name] = archive.read(name)
    with ZipFile(BytesIO(bundle)) as archive:
        for mapping in maps:
            hit_id = mapping['hit_id']
            for frame in mapping['picture']:
                if frame['file']:
                    frozen = archive.read(f'evidence/{hit_id}/{frame["file"]}')
                    assert frozen == instant[f'{hit_id}/{frame["file"]}']
            if mapping['sound']:
                assert archive.read(f'evidence/{hit_id}/sound.wav') == instant[f'{hit_id}/sound.wav']
    # A later ruling changes the live review but not the already-frozen snapshot.
    target = chosen[0]
    tour.call(f'/scans/{scan["id"]}/rulings',
              {'hit_id': target, 'outcome': 'hold', 'reason': '封包后追加扣留', 'reviewer': '审片员'}, 201)
    assert tour.call(f'/scans/{scan["id"]}/review')['status'] != review_before['status']
    with ZipFile(BytesIO(tour.call(f'/seals/{sealed["id"]}/bundle'))) as archive:
        assert json.loads(archive.read('review.json'))['status'] == review_before['status']
    assert tour.call(f'/seals/{sealed["id"]}/verify')['status'] == 'verified'
    return sealed


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

    verify_preview_boundary(origin, server, route, hit)
    verify_hole_frames(origin, server, tour, singular)
    verify_duplicate_source_frame(origin, server, tour, first)
    verify_padded_sound(origin, server, tour, first)

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

    verify_rejection_and_eviction(origin, server, route, hit)
    fresh = tour.scan()
    verify_frozen_members_independent(origin, tour, fresh)

    query = urlencode({'before': first['id'], 'after': second['id']})
    compared = request(origin, '/desk/compare?' + query)[2]
    assert '前版独有'.encode() in compared and first['id'].encode() in compared
    assert '找不到'.encode() in request(origin, route + '?hit=missing', expected=404)[2]
    print('审片台：预览不经 ZIP、洞内抽图、同源格复用、补段伴音与冻结成员独立均通过')


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
