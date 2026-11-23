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
from .exchange import build
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


def markers(tour):
    picture = tour.scan('fractional')
    holey = tour.scan('picture-hole')
    ids = [hit['id'] for hit in picture['hits']]
    uri = f"/scans/{picture['id']}/markers"
    output = tour.call(uri, {'hit_ids': ids})
    with ZipFile(BytesIO(output)) as archive:
        assert sorted(archive.namelist()) == ['README.txt', 'exchange.json', 'markers.csv']
        manifest = json.loads(archive.read('exchange.json'))
        table = archive.read('markers.csv').decode('utf-8').splitlines()
    assert manifest['target']['rate'] == '30000/1001'
    assert manifest['source']['rate'] == '30000/1001'
    assert [marker['kind'] for marker in manifest['markers']] == [hit['kind'] for hit in picture['hits']]
    assert all(marker['source']['start_us'] == hit['start_us'] and marker['source']['end_us'] == hit['end_us']
               for marker, hit in zip(manifest['markers'], picture['hits']))
    assert all(marker['sequence']['start_timecode'] == marker['source']['start_timecode']
               for marker in manifest['markers']), 'same-rate source should map frame for frame'
    assert len(table) == len(ids) + 1
    source = tour.scan('basalt')
    with ZipFile(BytesIO(tour.call(f"/scans/{source['id']}/markers", {'hit_ids': [source['hits'][0]['id']]}))) as archive:
        converted = json.loads(archive.read('exchange.json'))
    assert converted['source']['rate'] == '25'
    assert converted['markers'][0]['sequence']['start_frame'] == 0
    holed = tour.call(f"/scans/{holey['id']}/markers",
                      {'hit_ids': [tour.hit(holey, 'picture_gap')['id']]})
    with ZipFile(BytesIO(holed)) as archive:
        gap = json.loads(archive.read('exchange.json'))['markers'][0]
    partial = [item['frame'] for item in gap['material']['picture']['partial_frames']]
    assert partial and gap['material']['picture']['empty_ranges']
    assert gap['material']['picture']['no_material'] is False
    assert gap['sequence']['frames'] > gap['source']['frames']
    assert gap['material']['picture']['available_us'] + gap['material']['picture']['hole_us'] \
        == gap['sequence']['end_us'] - gap['sequence']['start_us']
    assert tour.call(uri, {'hit_ids': []}, 400)['error'].startswith('provide one to 64')
    synthetic = mapping()
    return {'target': manifest['target']['rate'], 'markers': len(manifest['markers']),
            'first': table[1], 'source_rate': converted['source']['rate'],
            'gap_partial_frames': partial,
            'gap_empty_ranges': gap['material']['picture']['empty_ranges'],
            'gap_frames': [gap['source']['frames'], gap['sequence']['frames']],
            'sub_frame': synthetic['sub_frame'], 'shared_frame': synthetic['shared_frame']}


def mapping():
    """Exercise the two mapping cases real sample signals cannot produce."""
    scan = {'id': 'scan-synthetic', 'analysis_version': 1,
            'tracks': {'picture': {'average_rate': '25'}, 'sound': None},
            'coverage': {'picture': [{'start_us': 0, 'end_us': 2000000},
                                     {'start_us': 2400000, 'end_us': 4000000}], 'sound': None},
            'extent': {'start_us': 0, 'end_us': 4000000}}
    hits = [{'id': 'hit-under-frame', 'kind': 'clip', 'channel': 2,
             'start_us': 1000010, 'end_us': 1000020},
            {'id': 'hit-across-hole', 'kind': 'black', 'channel': None,
             'start_us': 2300000, 'end_us': 2500000}]
    manifest = build(scan, {'id': 'master-synthetic'}, hits)
    under, across = manifest['markers']
    assert under['single_frame'] and under['source']['frames'] == 1
    assert under['sequence']['frames'] == 1 and under['extension']['head_us'] > 0
    assert under['kind'] == 'clip' and across['kind'] == 'black'
    assert across['material']['picture']['empty_ranges']
    assert across['material']['picture']['available_us'] > 0
    return {'sub_frame': {'source_frames': under['source']['frames'],
                          'sequence': [under['sequence']['start_frame'], under['sequence']['end_frame']],
                          'extension_us': [under['extension']['head_us'], under['extension']['tail_us']]},
            'shared_frame': {'sequence': [across['sequence']['start_frame'], across['sequence']['end_frame']],
                             'empty_ranges': across['material']['picture']['empty_ranges']}}


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
             'compare': compare, 'evidence': evidence, 'publish': seal, 'catalog': catalog,
             'verify': verify, 'markers': markers}


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
