"""Short production walkthroughs using a private vault and real HTTP requests."""
import argparse
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import wave
from zipfile import ZipFile

from .http import PREFIX, Server
from .reels import make


class Tour:
    def __init__(self, address, directory):
        self.origin = f'http://{address[0]}:{address[1]}{PREFIX}'
        self.directory = directory

    def call(self, route, value=None, expected=200):
        body = value if isinstance(value, bytes) else json.dumps(value).encode() if value is not None else None
        mime = 'application/octet-stream' if isinstance(value, bytes) else 'application/json'
        request = Request(self.origin + route, data=body, headers={'Content-Type': mime})
        try:
            response = urlopen(request, timeout=20)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            result = json.loads(body) if response.headers.get_content_type() == 'application/json' else body
            if response.status != expected:
                raise RuntimeError(f'{route}: expected HTTP {expected}, got {response.status}: {result!r}')
            return result

    def master(self, name='basalt', **streams):
        path = make(self.directory / f'{name}.mkv', name)
        query = urlencode({'label': name, **streams})
        return self.call(f'/masters?{query}', path.read_bytes(), 201)

    def policy(self, **thresholds):
        return self.call('/policies', {'label': 'Basalt Hour', 'thresholds': thresholds}, 201)

    def scan(self, name='basalt', **streams):
        master = self.master(name, **streams)
        policy = self.policy()
        return self.call('/scans', {'master_id': master['id'], 'policy_id': policy['id']}, 201)

    def selection(self, scan):
        return {'hit_ids': [self.hit(scan, 'sound_gap')['id']], 'padding_ms': 80, 'frames': 3}

    @staticmethod
    def hit(scan, kind):
        return next(hit for hit in scan['hits'] if hit['kind'] == kind)


def intake(tour):
    master = tour.master()
    stored = tour.call(f"/masters/{master['id']}")
    error = tour.call('/masters?label=invalid', b'not a movie', 400)
    assert stored == master and master['media']['sound']['channels'] == 2
    return {'tracks': master['media']['tracks'], 'invalid_media': error}


def policy(tour):
    base = tour.policy()
    changed = tour.policy(black_min_ms=600)
    error = tour.call('/policies', {'label': 'invalid', 'thresholds': {'audio_window_ms': 450}}, 400)
    assert changed['id'] != base['id'] and tour.call(f"/policies/{base['id']}") == base
    return {'baseline': base['thresholds']['black_min_ms'], 'revision': changed['thresholds']['black_min_ms'], 'invalid': error}


def scan(tour):
    record = tour.scan()
    kinds = sorted({hit['kind'] for hit in record['hits']})
    assert kinds == ['black', 'clip', 'freeze', 'silence', 'sound_gap']
    error = tour.call('/scans', {'master_id': record['master_id'], 'policy_id': 'missing'}, 404)
    return {'kinds': kinds, 'hit_count': len(record['hits']), 'missing_policy': error}


def intent(tour):
    record = tour.scan()
    hit = tour.hit(record, 'black')
    uri = f"/scans/{record['id']}"
    base = {'kind': 'black', 'start_us': hit['start_us'], 'end_us': hit['end_us'], 'reason': 'opening slate'}
    shortened = {**base, 'end_us': hit['end_us'] - 1}
    tour.call(uri + '/intents', shortened, 201)
    before = next(value for value in tour.call(uri + '/review')['hits'] if value['id'] == hit['id'])
    tour.call(uri + '/intents', base, 201)
    after = next(value for value in tour.call(uri + '/review')['hits'] if value['id'] == hit['id'])
    error = tour.call(uri + '/intents', {**base, 'channel': 1}, 400)
    assert (before['status'], after['status']) == ('pending', 'expected')
    return {'partial': before['status'], 'covered': after['status'], 'invalid_channel': error}


def review(tour):
    record = tour.scan()
    hit = tour.hit(record, 'clip')
    uri = f"/scans/{record['id']}"
    payload = {'outcome': 'hold', 'reason': 'right channel at full scale', 'reviewer': 'technical QC', 'hit_id': hit['id']}
    tour.call(uri + '/rulings', payload, 201)
    held = tour.call(uri + '/review')
    tour.call(uri + '/rulings', {**payload, 'outcome': 'reset'}, 201)
    reset = tour.call(uri + '/review')
    error = tour.call(uri + '/rulings', {**payload, 'hit_id': 'other'}, 404)
    assert held['status'] == 'hold' and reset['status'] == 'pending'
    return {'hold': held['counts'], 'reset': reset['counts'], 'foreign_hit': error}


def compare(tour):
    left, right = tour.scan(), tour.scan('revision')
    data = {'before': left['id'], 'after': right['id']}
    result = tour.call('/compare', data)
    assert any(item['kind'] == 'clip' for item in result['resolved']) and result['matches']
    return {'matches': len(result['matches']), 'resolved': [hit['kind'] for hit in result['resolved']],
            'invalid_threshold': tour.call('/compare', {**data, 'minimum_permille': 0}, 400)}


def reinject(tour):
    record = tour.scan()
    uri = f"/scans/{record['id']}"
    by_kind = {hit['kind']: hit for hit in record['hits']}
    head_cut = 800_000
    black = by_kind['black']
    freeze = by_kind['freeze']
    clip = by_kind['clip']
    marks = [
        # 首尾切除：整体前移 0.8 秒，应在同一平移下定位为建议。
        {'kind': 'black', 'start_us': black['start_us'] - head_cut,
         'end_us': black['end_us'] - head_cut, 'reference': 'P-001', 'outcome': 'hold'},
        # 中段漂移：冻帧只在局部偏移 1.2 秒，仍应逐段定位。
        {'kind': 'freeze', 'start_us': freeze['start_us'] + 1_200_000,
         'end_us': freeze['end_us'] + 1_200_000, 'reference': 'P-002'},
        # 削顶段：先制造一条已人工通过的母带裁决，再让代理请求扣留以形成冲突。
        {'kind': 'clip', 'channel': clip['channel'], 'start_us': clip['start_us'] - head_cut,
         'end_us': clip['end_us'] - head_cut, 'reference': 'P-003', 'outcome': 'hold'},
        # 现场编号不同但无法在母带定位：母带没有画面缺口候选，同指标下无可配对项。
        {'kind': 'picture_gap', 'start_us': record['extent']['end_us'] - 400_000,
         'end_us': record['extent']['end_us'] - 100_000, 'reference': 'P-004'},
    ]
    tour.call(uri + '/rulings', {'hit_id': clip['id'], 'outcome': 'accept',
                                 'reason': 'master review passed', 'reviewer': 'master QC'}, 201)
    payload = {'marks': marks, 'proxy_label': '外场低码率代理', 'reviewer': '现场同事',
               'drift_tolerance_us': 2_000_000}
    report = tour.call(uri + '/reinjects', payload, 201)
    assert report['counts']['conflict'] == 1 and report['counts']['unproven'] == 1
    assert report['counts']['suggestion'] == 2
    conflict = next(item for item in report['located'] if item['classification'] == 'conflict')
    suggestion = next(item for item in report['located'] if item['classification'] == 'suggestion')
    assert conflict['hit_id'] == clip['id'] and conflict['master_status_at_check'] == 'accepted'
    assert suggestion['shift_us'] in (head_cut, -1_200_000)
    assert report['baseline'] and {item['hit_id'] for item in report['baseline']} == \
        {hit['id'] for hit in record['hits']}
    # 预检本身不得改动任何裁决。
    review = tour.call(uri + '/review')
    assert next(item for item in review['hits'] if item['id'] == clip['id'])['status'] == 'accepted'
    # 冲突未显式 override 时拒绝写入；无法证明的段不能被选择。
    blocked = tour.call(f"/reinjects/{report['id']}/confirmations",
                        {'selections': [conflict['mark_index']], 'reviewer': '复核员',
                         'reason': '尝试覆盖'}, 400)
    bad = tour.call(f"/reinjects/{report['id']}/confirmations",
                    {'selections': [3], 'reviewer': '复核员', 'reason': '无法定位项'}, 400)
    # 人工确认建议段后，裁决经既有只追加通道落到母带候选上。
    confirmed = tour.call(f"/reinjects/{report['id']}/confirmations",
                          {'selections': [suggestion['mark_index']], 'reviewer': '复核员',
                           'reason': '对照母带画格确认'}, 201)
    applied = confirmed['confirmations'][0]
    assert applied['hit_id'] == suggestion['hit_id'] and applied['outcome'] == 'hold'
    after = tour.call(uri + '/review')
    assert next(item for item in after['hits'] if item['id'] == applied['hit_id'])['status'] == 'held'
    # 冲突带 override 后可确认。
    overridden = tour.call(f"/reinjects/{report['id']}/confirmations",
                           {'selections': [conflict['mark_index']], 'override': True,
                            'reviewer': '复核员', 'reason': '代理证据成立，改判扣留'}, 201)
    assert overridden['confirmations'][0]['prior_status'] == 'accepted'
    # 复核对照能看到结论变化与已确认段。
    checked = tour.call(f"/reinjects/{report['id']}/recheck")
    changed = {item['hit_id']: item['change'] for item in checked['changes']}
    assert applied['hit_id'] in changed and clip['id'] in changed
    assert sum(1 for item in checked['marks'] if item['state'] == 'applied') == 2
    invalid = tour.call(uri + '/reinjects',
                        {'marks': [{'kind': 'clip', 'start_us': 1, 'end_us': 2}]}, 400)
    return {'counts': report['counts'], 'alignment': report['alignment_model'],
            'blocked_conflict': blocked, 'unproven_rejected': bad,
            'recheck_changes': len(checked['changes']), 'missing_channel': invalid}


def evidence(tour):
    record = tour.scan()
    uri = f"/scans/{record['id']}/evidence"
    selection = tour.selection(record)
    output = tour.call(uri, selection)
    with ZipFile(BytesIO(output)) as archive:
        label = selection['hit_ids'][0]
        mapping = json.loads(archive.read(label + '/clock.json'))
        with wave.open(BytesIO(archive.read(label + '/sound.wav')), 'rb') as audio:
            assert audio.getnframes() == mapping['sound']['end_source_sample'] - mapping['sound']['first_source_sample']
        assert mapping['sound']['filled_sample_ranges'] and all(frame['file'] for frame in mapping['picture'])
    return {'map': mapping, 'invalid_selection': tour.call(uri, {'hit_ids': []}, 400)}


def seal(tour):
    record = tour.scan()
    uri = f"/scans/{record['id']}"
    selection = tour.selection(record)
    first = tour.call(uri + '/seals', selection, 201)
    tour.call(uri + '/rulings', {'hit_id': selection['hit_ids'][0], 'outcome': 'hold', 'reviewer': 'operator',
                               'reason': 'empty programme interval'}, 201)
    second = tour.call(uri + '/seals', selection, 201)
    assert first['id'] != second['id'] and first['review_status'] == 'pending' and second['review_status'] == 'hold'
    older = tour.call(f"/seals/{first['id']}")
    assert older == first
    error = tour.call(uri + '/seals', {'hit_ids': ['other']}, 404)
    return {'sealed_then': first['review_status'], 'sealed_now': second['review_status'], 'missing_finding': error}


def catalog(tour):
    record = tour.scan('picture-hole', sound='none')
    observed = tour.call(f"/scans/{record['id']}")
    assert observed['coverage']['sound'] is None
    assert observed['coverage']['picture'] == [{'start_us': 0, 'end_us': 2000000}, {'start_us': 2520000, 'end_us': 4000000}]
    return {'coverage': observed['coverage'], 'unknown_scan': tour.call('/scans/unknown', expected=404)}


def verify(tour):
    record = tour.scan()
    sealed = tour.call(f"/scans/{record['id']}/seals", tour.selection(record), 201)
    uri = f"/seals/{sealed['id']}"
    verified = tour.call(uri + '/verify')
    package = tour.call(uri + '/bundle')
    assert verified['status'] == 'verified' and package.startswith(b'PK')
    path = tour.directory / 'vault' / 'objects' / sealed['sha256']
    with path.open('ab') as file:
        file.write(b'corrupted copy')
    return {'verified': verified, 'tampered': tour.call(uri + '/verify', expected=409)}


WORKFLOWS = {'intake': intake, 'policy': policy, 'scan': scan, 'intent': intent, 'review': review,
             'compare': compare, 'reinject': reinject, 'evidence': evidence, 'publish': seal,
             'catalog': catalog, 'verify': verify}


def main():
    parser = argparse.ArgumentParser(description='Observe a Slatewatch workflow over a loopback connection')
    parser.add_argument('workflow', choices=(*WORKFLOWS, 'all'))
    selection = parser.parse_args().workflow
    with TemporaryDirectory(prefix='slatewatch-demo-') as directory:
        root = Path(directory)
        with Server(('127.0.0.1', 0), root / 'vault') as server:
            runner = Thread(target=server.serve_forever)
            runner.start()
            try:
                tour = Tour(server.server_address, root)
                for key in WORKFLOWS if selection == 'all' else (selection,):
                    print(json.dumps({'workflow': key, 'observed': WORKFLOWS[key](tour)}, ensure_ascii=False))
            finally:
                server.shutdown()
                runner.join()


if __name__ == '__main__':
    main()
