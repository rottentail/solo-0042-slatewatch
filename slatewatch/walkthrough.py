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


def _chapter_sheet():
    # Programme clock zero is the player reel zero; the first picture starts at the
    # head offset (1.0s headline, 0.4s headtrim). A picture reading is already on the
    # programme clock, a reel reading only becomes playable once it clears the head.
    return {'label': '制作部章节表', 'chapters': [
        {'title': '片头标志', 'picture_time_us': 1600000, 'reel_time_us': 900000},
        {'title': '正片开场', 'picture_time_us': 1300000, 'reel_time_us': 2500000},
        {'title': '静帧段落', 'origin': 'picture', 'picture_time_us': 2600000},
        {'title': '片尾', 'origin': 'reel', 'reel_time_us': 4200000},
    ]}


def chapters(tour):
    full = tour.scan('headline')
    trimmed = tour.scan('headtrim')
    sheet = _chapter_sheet()
    full_uri = f"/scans/{full['id']}"
    freeze = next(hit for hit in full['hits'] if hit['kind'] == 'freeze')

    imported = tour.call(full_uri + '/chapters', {'sheet': sheet}, 201)
    track = tour.call(full_uri + '/chapters/track')
    by_seq = {chapter['seq']: chapter for chapter in track['chapters']}
    # Chapter 1: the reel reading (0.9s) is inside the 1.0s audio-only head, so only the
    # picture reading is playable and the chapter is determined.
    assert by_seq[1]['state'] == 'determined' and by_seq[1]['origin'] == 'picture'
    assert by_seq[1]['time_us'] == 1600000
    # Chapter 2: both readings are playable picture, so it can only be shown as a range.
    assert by_seq[2]['state'] == 'candidate'
    assert by_seq[2]['range'] == {'start_us': 1300000, 'end_us': 1500000}
    # Declared-origin rows are always determined.
    assert by_seq[3]['state'] == 'determined' and by_seq[3]['origin'] == 'picture'
    assert by_seq[4]['state'] == 'determined' and by_seq[4]['origin'] == 'reel'
    # A determined chapter can navigate back to the actual selectable freeze anomaly.
    assert any(item['id'] == freeze['id'] for item in by_seq[3]['available_hits'])

    tour.call(full_uri + '/chapter-link',
              {'chapter_seq': 3, 'hit_id': freeze['id']}, 201)
    linked = tour.call(full_uri + '/chapters/track')
    link = next(chapter for chapter in linked['chapters'] if chapter['seq'] == 3)['link']
    assert link['status'] == 'active' and link['hit_id'] == freeze['id']
    foreign_hit = next(hit for hit in trimmed['hits'] if hit['kind'] == 'freeze')['id']
    bad_link = tour.call(full_uri + '/chapter-link', {'chapter_seq': 3, 'hit_id': foreign_hit}, 404)

    # On the trimmed reel the head is only 0.4s, so chapter 1's reel reading is now playable
    # too; the previously determined chapter must be presented as a candidate range.
    tour.call(f"/scans/{trimmed['id']}/chapters", {'sheet': sheet}, 201)
    moved_view = tour.call(f"/scans/{trimmed['id']}/chapters/track")
    assert next(chapter for chapter in moved_view['chapters'] if chapter['seq'] == 1)['state'] == 'candidate'

    # Re-import the identical sheet into the trimmed programme, carrying the old association.
    carried = tour.call(f"/scans/{trimmed['id']}/chapters",
                        {'sheet': sheet, 'links': [
                            {'chapter_seq': 3, 'hit_id': freeze['id'], 'from_scan_id': full['id']}]}, 201)
    assert carried['links'] == [] and carried['unresolved_links'][0]['hit_id'] == freeze['id']
    moved = tour.call(f"/scans/{trimmed['id']}/chapters/track")
    moved_link = next(chapter for chapter in moved['chapters'] if chapter['seq'] == 3)['link']
    assert moved_link['status'] == 'unresolved' and moved_link['hit_id'] == freeze['id']

    # A row whose only reading sits in the head is rejected rather than placed on nothing.
    head_only = {'chapters': [{'title': '卷零点', 'origin': 'reel', 'reel_time_us': 100000}]}
    rejected = tour.call(full_uri + '/chapters', {'sheet': head_only}, 400)
    return {'import_event': imported['seq'], 'determined': [1, 3, 4], 'candidate': [2],
            'foreign_scan_link': bad_link, 'unresolved_on_trim': moved['unresolved_links'],
            'head_only_rejected': rejected['error'][:24]}





WORKFLOWS = {'intake': intake, 'policy': policy, 'scan': scan, 'intent': intent, 'review': review,
             'compare': compare, 'evidence': evidence, 'publish': seal, 'catalog': catalog,
             'verify': verify, 'chapters': chapters}


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
