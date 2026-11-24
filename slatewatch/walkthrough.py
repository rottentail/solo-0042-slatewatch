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


def handoff(tour):
    """Black-field correction, channel-qualified silence and a frozen pre-correction handoff."""
    record = tour.scan('picture-hole')
    uri = f"/scans/{record['id']}"
    black = tour.hit(record, 'black')
    silence = next(hit for hit in record['hits'] if hit['kind'] == 'silence' and hit['channel'] == 1)
    hole = tour.hit(record, 'picture_gap')

    def black_view():
        view = tour.call(uri + '/review')
        return view, next(hit for hit in view['hits'] if hit['id'] == black['id'])

    # Two overlapping black intents from different production records cover the candidate.
    first = tour.call(uri + '/intents', {
        'kind': 'black', 'start_us': black['start_us'], 'end_us': black['end_us'],
        'reason': '制作记录 A：片头黑场', 'record': 'PROD-A-17'}, 201)
    second = tour.call(uri + '/intents', {
        'kind': 'black', 'start_us': black['start_us'], 'end_us': black['end_us'] - 80000,
        'reason': '制作记录 B：片头黑场交接单', 'record': 'PROD-B-04'}, 201)
    _, covered = black_view()
    assert covered['status'] == 'expected'
    assert {segment['basis'] for segment in covered['segments']} == {'expected'}

    # Manual accept is frozen before the producer correction, then withdrawn.
    tour.call(uri + '/rulings', {'hit_id': black['id'], 'outcome': 'accept',
                                 'reason': '交接前人工放行', 'reviewer': '甲班'}, 201)
    accepted_seal = tour.call(uri + '/seals', {'hit_ids': [black['id']]}, 201)
    tour.call(uri + '/rulings', {'hit_id': black['id'], 'outcome': 'reset',
                                 'reason': '制作方更正，撤回放行重新核对', 'reviewer': '乙班'}, 201)
    view, withdrawn = black_view()
    assert withdrawn['status'] == 'expected' and withdrawn['ruling']['outcome'] == 'reset'
    history = [event['outcome'] for event in view['rulings'] if event['hit_id'] == black['id']]
    assert history == ['accept', 'reset']

    # Producer corrects record A: only the first 120 ms of the original span still stand.
    cut = black['start_us'] + 120000
    corrected = tour.call(uri + '/intent-corrections', {
        'intent_seq': first['seq'], 'start_us': black['start_us'], 'end_us': cut,
        'reason': '制作方更正：黑场提前收束', 'record': 'PROD-A-17-R1'}, 201)
    view, after = black_view()
    assert after['status'] == 'pending'
    assert [(segment['start_us'], segment['end_us'], segment['basis'], segment['intent_sequences'])
            for segment in after['segments']] == [
        (0, cut, 'expected', sorted((second['seq'], corrected['seq']))),
        (cut, second['end_us'], 'expected', [second['seq']]),
        (second['end_us'], black['end_us'], 'pending', []),
    ]
    ledger = {row['seq']: row for row in view['intents']}
    assert ledger[first['seq']]['state'] == 'superseded'
    assert ledger[first['seq']]['reason'] == '制作记录 A：片头黑场'
    assert ledger[corrected['seq']]['state'] == 'active'
    assert ledger[corrected['seq']]['corrected_from_seq'] == first['seq']
    assert ledger[second['seq']]['state'] == 'active'
    assert view['intent_events'][0]['record'] == 'PROD-A-17'
    double = tour.call(uri + '/intent-corrections', {
        'intent_seq': first['seq'], 'start_us': 0, 'end_us': cut, 'reason': '重复更正'}, expected=400)
    unknown = tour.call(uri + '/intent-corrections', {
        'intent_seq': 2 ** 31, 'start_us': 0, 'end_us': cut, 'reason': '找不到意图'}, expected=404)
    inverted = tour.call(uri + '/intent-corrections', {
        'intent_seq': second['seq'], 'start_us': cut, 'end_us': 0, 'reason': '反向区间'}, expected=400)
    no_channel = tour.call(uri + '/intents', {
        'kind': 'silence', 'start_us': silence['start_us'], 'end_us': silence['end_us'],
        'reason': '未指声道'}, expected=400)

    # Silence intent on the other channel cannot support a channel-1 finding.
    tour.call(uri + '/intents', {
        'kind': 'silence', 'channel': 2, 'start_us': silence['start_us'], 'end_us': silence['end_us'],
        'reason': '误指右声道的静音', 'record': 'PROD-C-02'}, 201)
    sound_view = tour.call(uri + '/review')
    wrong = next(hit for hit in sound_view['hits'] if hit['id'] == silence['id'])
    assert wrong['status'] == 'pending' and all(s['basis'] == 'pending' for s in wrong['segments'])
    matched = tour.call(uri + '/intents', {
        'kind': 'silence', 'channel': 1, 'start_us': silence['start_us'], 'end_us': silence['end_us'],
        'reason': '左声道确认为有意静音', 'record': 'PROD-C-03'}, 201)
    sound_view = tour.call(uri + '/review')
    right = next(hit for hit in sound_view['hits'] if hit['id'] == silence['id'])
    assert right['status'] == 'expected'
    assert right['segments'][0]['basis'] == 'expected' and right['segments'][0]['intent_sequences'] == [matched['seq']]

    # A ruling cannot turn a coverage hole into measured material.
    tour.call(uri + '/rulings', {'hit_id': hole['id'], 'outcome': 'accept',
                                 'reason': '交接放行画面缺口候选', 'reviewer': '甲班'}, 201)
    hole_view = tour.call(uri + '/review')
    accepted_hole = next(hit for hit in hole_view['hits'] if hit['id'] == hole['id'])
    assert accepted_hole['status'] == 'accepted'
    assert [(s['basis'], s['start_us'], s['end_us']) for s in accepted_hole['segments']] == [
        ('unmeasured', hole['start_us'], hole['end_us'])]
    assert hole_view['basis_us']['unmeasured'] >= hole['end_us'] - hole['start_us']

    # The current handoff seal differs; the earlier frozen bundle keeps the pre-correction release.
    current_seal = tour.call(uri + '/seals', {'hit_ids': [black['id']]}, 201)
    assert current_seal['id'] != accepted_seal['id']
    verified = tour.call(f"/seals/{accepted_seal['id']}/verify")
    with ZipFile(BytesIO(tour.call(f"/seals/{accepted_seal['id']}/bundle"))) as archive:
        frozen = json.loads(archive.read('review.json'))
    frozen_black = next(hit for hit in frozen['hits'] if hit['id'] == black['id'])
    assert frozen_black['status'] == 'accepted' and frozen_black['ruling']['reason'] == '交接前人工放行'
    assert all(row['state'] == 'active' for row in frozen['intents'])
    assert frozen['intent_corrections'] == []
    return {'segments_after_correction': after['segments'], 'frozen_accept_seal': accepted_seal['id'],
            'verified_frozen': verified['status'], 'current_seal': current_seal['id'],
            'duplicate_rejected': double, 'unknown_intent': unknown,
            'inverted_rejected': inverted, 'silence_without_channel': no_channel}


WORKFLOWS = {'intake': intake, 'policy': policy, 'scan': scan, 'intent': intent, 'review': review,
             'compare': compare, 'evidence': evidence, 'publish': seal, 'catalog': catalog,
             'verify': verify, 'handoff': handoff}


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
