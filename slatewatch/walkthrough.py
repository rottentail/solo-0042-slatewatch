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
from .media import HEIGHT, WIDTH
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


def excerpt_frame_us(tour, scan, hit, frames=3):
    package = tour.call(f"/scans/{scan['id']}/evidence", {'hit_ids': [hit['id']], 'padding_ms': 80, 'frames': frames})
    with ZipFile(BytesIO(package)) as archive:
        clock = json.loads(archive.read(f"{hit['id']}/clock.json"))
    return next(frame['source_us'] for frame in clock['picture'] if frame['source_us'] is not None)


def check_region(tour, scan, hit, source_us, rect):
    package = tour.call(f"/scans/{scan['id']}/regions",
                        {'hit_id': hit['id'], 'source_us': source_us, 'rect': rect})
    track = scan['tracks']['picture']
    with ZipFile(BytesIO(package)) as archive:
        assert sorted(archive.namelist()) == ['region.json', 'region.pgm']
        mapping = json.loads(archive.read('region.json'))
        pgm = archive.read('region.pgm')
    width, height = track['width'], track['height']
    expected = {'x': rect['x'] * width // WIDTH, 'y': rect['y'] * height // HEIGHT}
    expected['width'] = -(-(rect['x'] + rect['width']) * width // WIDTH) - expected['x']
    expected['height'] = -(-(rect['y'] + rect['height']) * height // HEIGHT) - expected['y']
    assert mapping['scan_id'] == scan['id'] and mapping['hit_id'] == hit['id']
    assert mapping['source_us'] == source_us and mapping['thumbnail']['rect'] == rect
    assert mapping['source'] == {'width': width, 'height': height, 'rect': expected}
    header = f"P5\n{expected['width']} {expected['height']}\n255\n".encode()
    assert pgm.startswith(header) and len(pgm) == len(header) + expected['width'] * expected['height']
    return {'source_us': source_us, 'source_rect': mapping['source']['rect']}


def region(tour):
    record = tour.scan()
    hit = tour.hit(record, 'freeze')
    source_us = excerpt_frame_us(tour, record, hit)
    observed = check_region(tour, record, hit, source_us, {'x': 10, 'y': 6, 'width': 20, 'height': 12})
    odd_path = make(tour.directory / 'odd.mkv', 'odd', size=(171, 89))
    odd_master = tour.call(f"/masters?{urlencode({'label': 'odd'})}", odd_path.read_bytes(), 201)
    odd_scan = tour.call('/scans', {'master_id': odd_master['id'], 'policy_id': tour.policy()['id']}, 201)
    odd_hit = tour.hit(odd_scan, 'freeze')
    odd = check_region(tour, odd_scan, odd_hit, excerpt_frame_us(tour, odd_scan, odd_hit, frames=2),
                       {'x': 3, 'y': 5, 'width': 7, 'height': 9})
    hole = tour.scan('picture-hole', sound='none')
    gap = tour.hit(hole, 'picture_gap')
    missing = tour.call(f"/scans/{hole['id']}/regions",
                        {'hit_id': gap['id'], 'source_us': 2040000, 'rect': {'x': 0, 'y': 0, 'width': 4, 'height': 4}}, 404)
    overflow = tour.call(f"/scans/{record['id']}/regions",
                         {'hit_id': hit['id'], 'source_us': source_us, 'rect': {'x': 70, 'y': 0, 'width': 20, 'height': 4}}, 400)
    foreign = tour.call(f"/scans/{record['id']}/regions",
                        {'hit_id': gap['id'], 'source_us': source_us, 'rect': {'x': 1, 'y': 1, 'width': 2, 'height': 2}}, 404)
    return {'source': observed, 'odd_geometry': odd, 'no_coverage': missing,
            'overflow_rect': overflow, 'foreign_hit': foreign}


WORKFLOWS = {'intake': intake, 'policy': policy, 'scan': scan, 'intent': intent, 'review': review,
             'compare': compare, 'evidence': evidence, 'region': region, 'publish': seal,
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
