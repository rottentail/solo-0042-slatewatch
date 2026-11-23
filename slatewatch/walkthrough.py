"""Short production walkthroughs using a private vault and real HTTP requests."""
import argparse
from array import array
from io import BytesIO
import json
from math import isqrt
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import wave
from zipfile import ZipFile

import av

from .calibrate import BITS, TAPE_SIZE
from .http import PREFIX, Server
from .media import Picture, decode
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


def tape_media(data, path):
    """Decode a calibration tape the way a player would: pictures plus raw PCM."""
    path.write_bytes(data)
    width, height = TAPE_SIZE
    pictures = []
    samples = array('h')
    with av.open(str(path)) as container:
        video = container.streams.video[0]
        audio = container.streams.audio[0]
        rate = video.average_rate
        for packet in container.demux((video, audio)):
            for frame in packet.decode():
                if isinstance(frame, av.VideoFrame):
                    gray = frame.reformat(format='gray')
                    plane = gray.planes[0]
                    raw = memoryview(plane)
                    pictures.append(b''.join(raw[y * plane.line_size:y * plane.line_size + width]
                                             for y in range(height)))
                else:
                    block = array('h')
                    block.frombytes(memoryview(frame.planes[0])[:frame.samples * 2])
                    samples.extend(block)
    return rate, pictures, samples


def rms(values):
    return isqrt(sum(x * x for x in values) // max(1, len(values)))


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


def calibration(tour):
    observed = {}
    mappings = {}
    reels = [('basalt', {}, 4, 0), ('no-sound', {}, 4, 0),
             ('picture-hole', {'sound': 'none'}, 3, 1), ('fractional', {}, 5, 0)]
    for name, streams, sync_count, hole_count in reels:
        record = tour.scan(name, **streams)
        master = tour.call(f"/masters/{record['master_id']}")
        payload = tour.call(f"/scans/{record['id']}/calibration", {'pulse_ms': 1000})
        with ZipFile(BytesIO(payload)) as archive:
            assert sorted(archive.namelist()) == ['calibration.mkv', 'clock.json']
            mapping = json.loads(archive.read('clock.json'))
            rate, pictures, samples = tape_media(archive.read('calibration.mkv'),
                                                 tour.directory / f'{name}-tape.mkv')
        mappings[name] = mapping
        assert mapping['master_sha256'] == master['sha256'] and mapping['scan_id'] == record['id']
        assert mapping['tape']['video']['frames'] == len(pictures)
        assert mapping['tape']['audio']['samples'] == len(samples)
        assert str(rate) == mapping['tape']['video']['frame_rate']
        width, height = TAPE_SIZE
        for n, rows in enumerate(pictures):
            decoded = 0
            for bit in range(BITS):
                block = rows[(3 * width + bit * 8):(3 * width + bit * 8 + 8)]
                decoded = decoded * 2 + (sum(block) > len(block) * 125)
            assert decoded == n, (name, n, decoded)
        syncs = [event for event in mapping['events'] if event['kind'] == 'sync']
        holes = [event for event in mapping['events'] if event['kind'] == 'hole']
        assert (len(syncs), len(holes)) == (sync_count, hole_count), name
        marked = set()
        for event in syncs:
            marked.add(event['tape_frame'])
            body = pictures[event['tape_frame']][8 * width:]
            assert sum(body) > len(body) * 180, (name, event)
            first, count = event['beep_first_sample'], event['beep_samples']
            assert rms(samples[first:first + count]) > 3000
            before = samples[max(0, first - 4800):first]
            assert not before or max(abs(x) for x in before) < 500
        for event in holes:
            for n in range(event['tape_first_frame'], event['tape_first_frame'] + event['tape_frames']):
                marked.add(n)
                body = pictures[n][8 * width:]
                bright = sum(v > 180 for v in body)
                dark = sum(v < 40 for v in body)
                assert bright * 3 > len(body) and dark * 3 > len(body), (name, n)
            first, count = event['tone_first_sample'], event['tone_samples']
            assert rms(samples[first:first + count]) > 1000
        for n, rows in enumerate(pictures):
            if n not in marked:
                assert sum(rows[8 * width:]) < len(rows[8 * width:]) * 100, (name, n)
        path = tour.directory / 'vault' / 'objects' / master['sha256']
        source_times = [block.timestamp_us for block in decode(path, record['tracks'])
                        if isinstance(block, Picture)]
        assert {event['source_frame_us'] for event in syncs} <= set(source_times)
        for event in syncs:
            assert event['beep_first_sample'] == round(event['source_frame_us'] * 48000 / 1_000_000)
        observed[name] = {'sync': len(syncs), 'hole': len(holes), 'frames': len(pictures),
                          'sound': mapping['source']['sound']}
    assert observed['no-sound']['sound'] == 'not-selected'
    assert observed['basalt']['sound'] == 'selected'
    hole = next(event for event in mappings['picture-hole']['events'] if event['kind'] == 'hole')
    assert (hole['source_start_us'], hole['source_end_us']) == (2000000, 2520000)
    punched = mappings['picture-hole']['events']
    assert not any(event['kind'] == 'sync' and event['scheduled_us'] == 2000000 for event in punched)
    fractional = mappings['fractional']
    assert fractional['tape']['video']['frame_rate'] == '30000/1001'
    first_sync = next(event for event in fractional['events'] if event['kind'] == 'sync')
    assert first_sync['tape_frame'] == first_sync['source_frame_index'] == 0
    fractional_scan = tour.scan('fractional')
    bad = tour.call(f"/scans/{fractional_scan['id']}/calibration", {'pulse_ms': 10}, 400)
    return {'reels': observed, 'unknown_scan': tour.call('/scans/unknown/calibration', {}, 404),
            'invalid_pulse': bad}


WORKFLOWS = {'intake': intake, 'policy': policy, 'scan': scan, 'intent': intent, 'review': review,
             'compare': compare, 'evidence': evidence, 'publish': seal, 'catalog': catalog,
             'calibration': calibration, 'verify': verify}


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
