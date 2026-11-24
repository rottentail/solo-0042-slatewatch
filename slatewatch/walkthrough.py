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


def rehearse(tour):
    master = tour.master('silence-lab')
    policy = tour.policy()
    scan = tour.call('/scans', {'master_id': master['id'], 'policy_id': policy['id']}, 201)
    silences = [(hit['start_us'], hit['end_us']) for hit in scan['hits'] if hit['kind'] == 'silence']
    assert silences == [(1000000, 2000000), (2050000, 3000000),
                        (3200000, 3500000), (4500000, 7000000)]
    session = tour.call(f"/scans/{scan['id']}/rehearsals", {}, 201)
    token = session['token']
    assert session['master']['id'] == master['id']
    assert session['policy']['id'] == policy['id']
    assert session['tuning'] == {'audio_window_ms': 50, 'silence_rms': 80, 'silence_min_ms': 300}
    scan_ids_before = {item['id'] for item in tour.call('/scans')}

    first = tour.call(f'/rehearsals/{token}',
                      {'audio_window_ms': 70, 'silence_rms': 2048, 'silence_min_ms': 300})
    assert [(hit['start_us'], hit['end_us']) for hit in first['candidates']] == [
        (1050000, 2940000), (4550000, 7000000)]
    assert first['partial_windows'] == [
        {'start_us': 2940000, 'end_us': 3010000}, {'start_us': 3150000, 'end_us': 3220000}]
    first_relations = {event['relation'] for event in first['explanation']}
    assert first_relations == {'merged', 'dropped'}
    merged = next(event for event in first['explanation'] if event['relation'] == 'merged')
    assert len(merged['before']) == 2 and len(merged['after']) == 1
    dropped = next(event for event in first['explanation'] if event['relation'] == 'dropped')
    assert dropped['before'][0]['start_us'] == 3200000 and dropped['after'] == []
    assert any('不足最短事件' in reason for reason in dropped['reasons'])

    second = tour.call(f'/rehearsals/{token}',
                       {'audio_window_ms': 30, 'silence_rms': 40, 'silence_min_ms': 200})
    assert [(hit['start_us'], hit['end_us']) for hit in second['candidates']] == [
        (1020000, 1980000), (2010000, 3000000), (3210000, 3480000),
        (4500000, 5490000), (6000000, 6990000), (7500000, 7770000)]
    assert second['partial_windows'] == [{'start_us': 3180000, 'end_us': 3210000}]
    second_relations = {event['relation'] for event in second['explanation']}
    assert second_relations == {'split', 'introduced'}
    split = next(event for event in second['explanation'] if event['relation'] == 'split')
    assert len(split['after']) == 2 and any('超过静音门槛' in r for r in split['reasons'])

    # Unconfirmed rehearsals must not show up in the official scan list.
    assert {item['id'] for item in tour.call('/scans')} == scan_ids_before

    # Another workstation window registers a different rule meanwhile.
    tour.policy(silence_rms=1)
    confirmed = tour.call(f'/rehearsals/{token}/confirm',
                          {'rehearsal_id': second['id'], 'label': '静音配方·30ms'}, 201)
    thresholds = confirmed['policy']['thresholds']
    assert (thresholds['audio_window_ms'], thresholds['silence_rms'],
            thresholds['silence_min_ms']) == (30, 40, 200)
    assert thresholds['clip_min_ms'] == 100 and thresholds['black_min_ms'] == 300
    official = confirmed['scan']
    assert confirmed['confirmed']['baseline_policy_id'] == policy['id']
    assert confirmed['confirmed']['master_id'] == master['id']
    assert confirmed['confirmed']['rehearsal_id'] == second['id']
    official_silence = [{k: v for k, v in hit.items() if k != 'id'}
                        for hit in official['hits'] if hit['kind'] == 'silence']
    assert official_silence == second['candidates']
    # The same master + rule combination resolves to the same immutable scan.
    again = tour.call('/scans', {'master_id': master['id'], 'policy_id': confirmed['policy']['id']}, 201)
    assert again['id'] == official['id']
    listed_after = tour.call('/scans')
    assert {item['id'] for item in listed_after} == scan_ids_before | {official['id']}
    described = tour.call(f'/rehearsals/{token}')
    assert len(described['rehearsals']) == 2

    errors = {
        'bad_window': tour.call(f'/rehearsals/{token}', {'audio_window_ms': 7}, 400),
        'window_over_clip_min': tour.call(f'/rehearsals/{token}', {'audio_window_ms': 120}, 400),
        'unknown_session': tour.call('/rehearsals/does-not-exist', {'silence_rms': 40}, 404),
        'unknown_rehearsal': tour.call(f'/rehearsals/{token}/confirm',
                                       {'rehearsal_id': 'rehearsal-99', 'label': 'x'}, 404),
    }
    no_sound = tour.scan('picture-hole', sound='none')
    errors['no_sound'] = tour.call(f"/scans/{no_sound['id']}/rehearsals", {}, 400)
    return {'first': sorted(first_relations), 'second': sorted(second_relations),
            'confirmed_scan': official['id'], 'errors': errors}


WORKFLOWS = {'intake': intake, 'policy': policy, 'scan': scan, 'rehearse': rehearse,
             'intent': intent, 'review': review,
             'compare': compare, 'evidence': evidence, 'publish': seal, 'catalog': catalog, 'verify': verify}
WORKFLOW_ALIASES = {'rehearsal': rehearse, 'silence': rehearse}


def main():
    parser = argparse.ArgumentParser(description='Observe a Slatewatch workflow over a loopback connection')
    parser.add_argument('workflow', choices=(*WORKFLOWS, *WORKFLOW_ALIASES, 'all'))
    selection = parser.parse_args().workflow
    with TemporaryDirectory(prefix='slatewatch-demo-') as directory:
        root = Path(directory)
        with Server(('127.0.0.1', 0), root / 'vault') as server:
            runner = Thread(target=server.serve_forever)
            runner.start()
            try:
                tour = Tour(server.server_address, root)
                if selection == 'all':
                    steps = WORKFLOWS
                else:
                    steps = {selection: WORKFLOWS.get(selection) or WORKFLOW_ALIASES[selection]}
                for key, step in steps.items():
                    print(json.dumps({'workflow': key, 'observed': step(tour)}, ensure_ascii=False))
            finally:
                server.shutdown()
                runner.join()


if __name__ == '__main__':
    main()
