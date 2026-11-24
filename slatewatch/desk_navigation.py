"""Standalone end-to-end navigation check for the browser review desk.

One programme file carries two selectable sound tracks with different PCM but the
same signal layout.  It is registered twice (identical bytes, different selected
track) and scanned; the same short silence pair therefore exists on both scans
at the same two timestamps, and stacks on one narrow timeline strip.

The check then drives the rendered pages: every mark on a timeline lane and every
finding row is followed, its evidence form is submitted, and the served excerpt
is compared with expectations built from the HTTP API and from the fixture
generator.  Links, titles, candidate ids and timestamps read from the page are
only compared *to* an expectation, never used to build one.  Two internal
consistency checks alone would not be enough — a page, its links and its evidence
could all lie together — so each preview's PCM bytes and PNG pixels are matched
against samples and frames recomputed from this module's own generator, and the
mark's drawn lane and span against percentages recomputed from the scan
descriptor.  ``--control`` patches the renderer so that every surface consistently
names the other scan, shows a page-derived probe passing under that patch, and
shows this check rejecting it.

The overlapping pair is opened through its two distinct ``href`` targets, the way
a keyboard user reaches list items; no assumption is made that two clicks at one
pixel can select both.  Geometry is only inspected to prove the strip really is
narrow enough for the two boxes to overlap.

Nothing depends on a scan id, on how timestamps are printed, or on a window
width: the check runs against a rebuilt container with fresh ids, and the strip
geometry is evaluated at several narrow widths.
"""

import argparse
from array import array
from fractions import Fraction
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import re
import struct
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
import wave
from zipfile import ZipFile
import zlib

from .http import PREFIX, Server
from .media import thumbnail

SAMPLE_RATE = 8000
PICTURE_SIZE = (160, 96)
FPS = Fraction(25)
DURATION_US = 4_000_000
SILENCE = ((1_000_000, 1_020_000), (1_030_000, 1_050_000))
CLIP = (2_000_000, 2_200_000)
TRACK_SEEDS = {
    1: (0x9E3779B1, 0x85EBCA6B, 0xC2B2AE35),
    2: (0x27D4EB2F, 0x165667B1, 0x9E3779B9),
}
TRACKS = (1, 2)
PADDING_MS = 80
FRAMES = 3
STRIP_MIN_PX = 2.0
GEOMETRY_PERCENT = 0.001
NARROW_WIDTHS = (120, 160, 200, 240)
WIDE_WIDTH = 900
THRESHOLDS = {'audio_window_ms': 10, 'silence_min_ms': 20, 'clip_min_ms': 10}


class NavigationFailure(AssertionError):
    """The desk did not lead to the scan, track or sample the API names."""


class SetupFailure(AssertionError):
    """The fixture or the API no longer matches what this check relies on."""


# ---------------------------------------------------------------------------
# Fixture: one programme, two tracks, PCM and pictures reproducible from bytes.
# ---------------------------------------------------------------------------

def _mix(value):
    value &= 0xFFFFFFFF
    value ^= value >> 16
    value = (value * 0x7FEB352D) & 0xFFFFFFFF
    value ^= value >> 15
    value = (value * 0x846CA68B) & 0xFFFFFFFF
    value ^= value >> 16
    return value


def _sample(track, sample, channel):
    us = sample * 1_000_000 // SAMPLE_RATE
    if channel == 0 and any(start <= us < end for start, end in SILENCE):
        return 0
    if channel == 1 and CLIP[0] <= us < CLIP[1]:
        return 32767 if sample % 2 == 0 else -32768
    a, b, c = TRACK_SEEDS[track]
    return (_mix((sample + 1) * a + (channel + 1) * b + c) % 12001) - 6000


def _raster(index):
    import av

    width, height = PICTURE_SIZE
    frame = av.VideoFrame(width, height, 'gray')
    plane = frame.planes[0]
    raw = bytearray(plane.buffer_size)
    phase = index * 9
    for y in range(height):
        start = y * plane.line_size
        raw[start:start + width] = bytes(40 + (x * 3 + y * 2 + phase) % 181 for x in range(width))
        bar = (index * 7) % width
        for x in range(bar, min(width, bar + 5)):
            raw[start + x] = 250
    plane.update(raw)
    return frame


def _audio(track, first, count):
    import av

    values = array('h')
    for sample in range(first, first + count):
        values.append(_sample(track, sample, 0))
        values.append(_sample(track, sample, 1))
    if sys.byteorder != 'little':
        values.byteswap()
    frame = av.AudioFrame(format='s16', layout='stereo', samples=count)
    frame.planes[0].update(values.tobytes())
    frame.sample_rate = SAMPLE_RATE
    frame.time_base = Fraction(1, SAMPLE_RATE)
    frame.pts = first
    return frame


def build_programme(path, nonce):
    """Write the same programme signal; ``nonce`` only perturbs container bytes."""
    import av

    with av.open(str(path), mode='w', format='matroska') as container:
        if nonce:
            container.metadata['title'] = nonce
        video = container.add_stream('ffv1', rate=FPS)
        video.width, video.height = PICTURE_SIZE
        video.pix_fmt = 'gray'
        audio = []
        for _track in TRACKS:
            stream = container.add_stream('pcm_s16le', rate=SAMPLE_RATE)
            stream.layout = 'stereo'
            audio.append(stream)
        for index in range(round(DURATION_US * FPS // 1_000_000)):
            frame = _raster(index)
            frame.pts = index
            frame.time_base = 1 / FPS
            container.mux(video.encode(frame))
        total = DURATION_US * SAMPLE_RATE // 1_000_000
        for stream, track in zip(audio, TRACKS):
            first = 0
            while first < total:
                count = min(800, total - first)
                container.mux(stream.encode(_audio(track, first, count)))
                first += count
        container.mux(video.encode())
        for stream in audio:
            container.mux(stream.encode())
    return path


def sample_at(us):
    """Frame boundary the evidence layer uses when mapping microseconds to samples."""
    return -((-us * SAMPLE_RATE) // 1_000_000)


def fixture_pcm(track, first, end):
    values = array('h')
    for sample in range(first, end):
        values.append(_sample(track, sample, 0))
        values.append(_sample(track, sample, 1))
    if sys.byteorder != 'little':
        values.byteswap()
    return values.tobytes()


def fixture_picture(us):
    """Grayscale pixels of the fixture frame that presentation time ``us`` falls on."""
    step = 1_000_000 * FPS.denominator // FPS.numerator
    return thumbnail(_raster(us // step))


def parse_png(data):
    """Decode a non-interlaced 8-bit grayscale PNG to its pixel bytes."""
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise NavigationFailure('preview picture is not a PNG')
    position, header, payload = 8, None, b''
    while position < len(data):
        length = struct.unpack('!I', data[position:position + 4])[0]
        name = data[position + 4:position + 8]
        body = data[position + 8:position + 8 + length]
        if name == b'IHDR':
            header = struct.unpack('!2I5B', body)
        elif name == b'IDAT':
            payload += body
        elif name == b'IEND':
            break
        position += 12 + length
    if header is None or header[2:] != (8, 0, 0, 0, 0):
        raise NavigationFailure(f'unsupported PNG header {header!r}')
    width, height = header[:2]
    raw = zlib.decompress(payload)
    stride, prior, rows = width + 1, bytearray(width), []
    if len(raw) != stride * height:
        raise NavigationFailure('PNG payload length does not match its header')
    for row in range(height):
        kind = raw[row * stride]
        line = bytearray(raw[row * stride + 1:(row + 1) * stride])
        for x in range(width):
            left = line[x - 1] if x else 0
            up = prior[x]
            corner = prior[x - 1] if x else 0
            if kind == 1:
                line[x] = (line[x] + left) & 0xFF
            elif kind == 2:
                line[x] = (line[x] + up) & 0xFF
            elif kind == 3:
                line[x] = (line[x] + (left + up) // 2) & 0xFF
            elif kind == 4:
                p = left + up - corner
                a, b, c = abs(p - left), abs(p - up), abs(p - corner)
                line[x] = (line[x] + (left if a <= b and a <= c else up if b <= c else corner)) & 0xFF
            elif kind != 0:
                raise NavigationFailure(f'unknown PNG filter {kind}')
        rows.append(bytes(line))
        prior = line
    return b''.join(rows)


def parse_wav(data):
    with wave.open(BytesIO(data), 'rb') as audio:
        if audio.getsampwidth() != 2:
            raise NavigationFailure('preview sound is not 16-bit')
        return {'channels': audio.getnchannels(), 'rate': audio.getframerate(),
                'frames': audio.readframes(audio.getnframes())}


# ---------------------------------------------------------------------------
# A tiny DOM, enough to read the semantic landmarks the desk already exposes.
# ---------------------------------------------------------------------------

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
        'meta', 'source', 'track', 'wbr'}


class Node:
    __slots__ = ('tag', 'attrs', 'children')

    def __init__(self, tag, attrs=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('#document')
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse(body):
    tree = _Tree()
    tree.feed(body.decode('utf-8'))
    tree.close()
    return tree.root


def walk(node):
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from walk(child)


def classes(node):
    return set(node.attrs.get('class', '').split())


def find_all(node, tag=None, accept=None):
    return [item for item in walk(node)
            if (tag is None or item.tag == tag) and (accept is None or accept(item))]


def section(doc, label):
    found = find_all(doc, 'section', lambda item: item.attrs.get('aria-label') == label
                     or item.attrs.get('aria-labelledby') == label)
    if len(found) != 1:
        raise NavigationFailure(f'expected one section labelled {label!r}, found {len(found)}')
    return found[0]


def scan_target(href):
    split = urlsplit(href)
    parts = split.path.strip('/').split('/')
    if len(parts) != 3 or parts[:2] != ['desk', 'scans'] or not parts[2]:
        raise NavigationFailure(f'not a desk finding link: {href!r}')
    return parts[2], parse_qs(split.query).get('hit', [''])[0]


def hidden_hit(form):
    for item in find_all(form, 'input'):
        if item.attrs.get('name') == 'hit':
            return item.attrs.get('value', '')
    raise NavigationFailure('finding form has no hit field')


def preview_form(doc):
    forms = find_all(doc, 'form', lambda item: urlsplit(item.attrs.get('action', '')).path.endswith('/preview'))
    if len(forms) != 1:
        raise NavigationFailure(f'expected one preview form, found {len(forms)}')
    return forms[0]


def lane_marks(doc):
    """(lane index, anchor) for every mark the timeline actually draws."""
    timeline = section(doc, '节目时间轴')
    result = []
    for index, lane in enumerate(find_all(timeline, 'div', lambda item: 'lane' in classes(item))):
        for anchor in find_all(lane, 'a', lambda item: 'mark' in classes(item)):
            result.append((index, anchor))
    return result


def lane_layout(scan):
    """Lane order drawn by the desk, derived only from the scan descriptor."""
    channels = scan['tracks']['sound']['channels'] if scan['tracks']['sound'] else 0
    names = ['画面', *(f'伴音 {index}' for index in range(1, channels + 1))]
    if channels and any(hit['kind'] == 'sound_gap' for hit in scan['hits']):
        names.append('伴音')
    return names


def lane_of(scan, hit):
    if hit['kind'] in ('silence', 'clip') and hit['channel'] is not None:
        return f"伴音 {hit['channel']}"
    if hit['kind'] == 'sound_gap':
        return '伴音'
    return '画面'


def percent_box(span, duration):
    left = 100 * span[0] / duration
    return left, 100 * (span[1] - span[0]) / duration


def table_marks(doc):
    results = section(doc, 'results')
    marks = []
    for row in find_all(results, 'tr'):
        anchors = [item for item in find_all(row, 'a')
                   if urlsplit(item.attrs.get('href', '')).path.startswith('/desk/scans/')]
        if anchors:
            marks.append(anchors[0])
    return marks


def preview_assets(doc):
    frames = section(doc, '证据画格')
    pictures = [item.attrs['src'] for item in find_all(frames, 'img') if item.attrs.get('src')]
    audio = find_all(doc, 'audio', lambda item: bool(item.attrs.get('src')))
    if len(audio) != 1:
        raise NavigationFailure(f'expected one sound player, found {len(audio)}')
    return pictures, audio[0].attrs['src']


def back_link(doc):
    links = [item for item in find_all(doc, 'a')
             if urlsplit(item.attrs.get('href', '')).path.startswith('/desk/scans/')]
    if len(links) != 1:
        raise NavigationFailure(f'expected one back link on the preview, found {len(links)}')
    return links[0].attrs['href']


def mark_box(style):
    """(left percent, width percent) exactly as the mark declares it."""
    left = re.search(r'left:([0-9.]+)%', style)
    width = re.search(r'width:([0-9.]+)%', style)
    if not left or not width:
        raise NavigationFailure(f'mark has no percentage geometry: {style!r}')
    return float(left.group(1)), float(width.group(1))


def pixels(box, strip_px, minimum):
    start = box[0] * strip_px / 100
    return start, start + max(box[1] * strip_px / 100, minimum)


def strip_metrics(origin):
    """Read the lane label width, gap and mark minimum width from served styles."""
    text = ''
    for name in ('shell.css', 'review.css', 'media.css'):
        text += fetch(origin, f'/desk/static/{name}')[2].decode('utf-8')
    rules = re.findall(r'([^{}]+)\{([^}]*)\}', text)

    def declared(selector_fragment, property_name):
        for selector, body in rules:
            if selector_fragment in selector and property_name in body:
                match = re.search(rf'{property_name}\s*:\s*([0-9.]+)px', body)
                if match:
                    return float(match.group(1))
        return None

    lane = next((body for selector, body in rules if re.search(r'\.lane\s*$', selector.strip())), '')
    columns = re.search(r'grid-template-columns\s*:\s*([0-9.]+)px', lane)
    gap = re.search(r'(?:column-)?gap\s*:\s*([0-9.]+)px', lane)
    # `.lane` lays out a fixed label column then the strip; `.mark` may declare min-width.
    label = float(columns.group(1)) if columns else 63.0
    spacing = float(gap.group(1)) if gap else 5.0
    minimum = declared(r'.mark', 'min-width') or STRIP_MIN_PX
    return label, spacing, minimum


# ---------------------------------------------------------------------------
# HTTP helpers.
# ---------------------------------------------------------------------------

def fetch(origin, path, data=None, mime=None, expected=200):
    headers = {'Origin': origin}
    body = None
    if data is not None:
        if isinstance(data, dict):
            body = urlencode(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        else:
            body = data
            headers['Content-Type'] = mime or 'application/octet-stream'
    try:
        response = urlopen(Request(origin + path, data=body, headers=headers), timeout=30)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        if response.status != expected:
            raise NavigationFailure(f'{path}: expected HTTP {expected}, got {response.status}: {raw[:200]!r}')
        return response.geturl().removeprefix(origin), response.headers.get_content_type(), raw


class Api:
    def __init__(self, origin):
        self.origin = origin

    def call(self, route, value=None, expected=200):
        body = None if value is None else json.dumps(value).encode()
        _url, _mime, raw = fetch(self.origin, PREFIX + route, body, 'application/json', expected)
        return json.loads(raw) if raw else None

    def upload(self, label, data, sound):
        query = urlencode({'label': label, 'sound': sound})
        _url, _mime, raw = fetch(self.origin, f'{PREFIX}/masters?{query}', data, expected=201)
        return json.loads(raw)

    def excerpt(self, scan_id, hit_id):
        selection = {'hit_ids': [hit_id], 'padding_ms': PADDING_MS, 'frames': FRAMES}
        _url, _mime, raw = fetch(self.origin, f'{PREFIX}/scans/{scan_id}/evidence',
                                 json.dumps(selection).encode(), 'application/json')
        with ZipFile(BytesIO(raw)) as archive:
            clock = json.loads(archive.read(f'{hit_id}/clock.json'))
            served = parse_wav(archive.read(f'{hit_id}/sound.wav'))
        return clock, served


# ---------------------------------------------------------------------------
# Scenario setup and expectations.
# ---------------------------------------------------------------------------

class Expectation:
    def __init__(self, scan_id, hit, track, clock, pcm, pictures):
        self.scan_id = scan_id
        self.hit = hit
        self.hit_id = hit['id']
        self.track = track
        self.clock = clock
        self.pcm = pcm
        self.pictures = pictures

    @property
    def span(self):
        return self.clock['start_us'], self.clock['end_us']


class Scenario:
    """A running server, one programme, and two scans that differ only by track."""

    def __init__(self, root, nonce=''):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.media = build_programme(self.root / 'programme.mkv', nonce)
        self.data = self.media.read_bytes()
        self.server = Server(('127.0.0.1', 0), self.root / 'vault')
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.origin = f'http://{host}:{port}'
        self.api = Api(self.origin)
        self.scans = []
        self.tracks = {}
        self.expectations = {}
        try:
            self.setup()
        except BaseException:
            self.close()
            raise

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def setup(self):
        policy = self.api.call('/policies', {'label': 'Navigation', 'thresholds': THRESHOLDS}, 201)
        masters = [self.api.upload(f'Navigation track {track}', self.data, str(track)) for track in TRACKS]
        if masters[0]['sha256'] != masters[1]['sha256']:
            raise SetupFailure('the two registrations must share one programme')
        selected = [master['media']['sound']['index'] for master in masters]
        if selected != list(TRACKS):
            raise SetupFailure(f'expected sound streams {TRACKS}, got {selected}')
        for master, track in zip(masters, TRACKS):
            created = self.api.call('/scans', {'master_id': master['id'], 'policy_id': policy['id']}, 201)
            stored = self.api.call(f"/scans/{created['id']}")
            if stored != created:
                raise SetupFailure('stored scan differs from the creation response')
            self.scans.append(stored)
            self.tracks[stored['id']] = track
            self.expectations[stored['id']] = self._expect(stored, track)
        first = self.scans[0]
        kinds = sorted((hit['kind'], hit['channel']) for hit in first['hits'])
        if kinds != [('clip', 2), ('silence', 1), ('silence', 1)]:
            raise SetupFailure(f'unexpected fixture findings: {kinds}')
        if self.scans[0]['id'] == self.scans[1]['id']:
            raise SetupFailure('the two track selections produced one scan')
        self._guard()

    def _expect(self, scan, track):
        result = {}
        for hit in scan['hits']:
            clock, served = self.api.excerpt(scan['id'], hit['id'])
            first, end = clock['sound']['first_source_sample'], clock['sound']['end_source_sample']
            if (first, end) != (sample_at(clock['start_us']), sample_at(clock['end_us'])):
                raise SetupFailure('evidence sample range does not follow the programme span')
            pcm = fixture_pcm(track, first, end)
            if served != {'channels': 2, 'rate': SAMPLE_RATE, 'frames': pcm}:
                raise SetupFailure(f'fixture PCM model does not match track {track}')
            pictures = []
            step = 1_000_000 * FPS.denominator // FPS.numerator
            for frame in clock['picture']:
                if not frame['file']:
                    continue
                if frame['source_us'] % step or frame['source_us'] != fixture_index_us(frame['target_us']):
                    raise SetupFailure('fixture picture mapping is not the expected 25 fps grid')
                pictures.append(fixture_picture(frame['source_us']))
            result[hit['id']] = Expectation(scan['id'], hit, track, clock, pcm, pictures)
        fingerprints = {(item.pcm, tuple(item.pictures)) for item in result.values()}
        if len(fingerprints) != len(result):
            raise SetupFailure('fixture candidates are not distinguishable by their excerpts')
        return result

    def _guard(self):
        """Both the track and the short candidates must be genuinely different."""
        for scan in self.scans:
            scan_id = scan['id']
            other = 2 if self.tracks[scan_id] == 1 else 1
            for item in self.expectations[scan_id].values():
                first, end = item.clock['sound']['first_source_sample'], item.clock['sound']['end_source_sample']
                if fixture_pcm(other, first, end) == item.pcm:
                    raise SetupFailure('the fixture tracks are not distinguishable')
            short = [item for item in self.expectations[scan_id].values()
                     if item.hit['end_us'] - item.hit['start_us'] < 100_000]
            if len(short) != 2 or len({item.pcm for item in short}) != 2:
                raise SetupFailure('fixture does not hold two distinct short candidates')

    def match(self, scan_id, served, pictures):
        found = [item for item in self.expectations[scan_id].values()
                 if item.pcm == served['frames'] and item.pictures == pictures]
        if len(found) != 1:
            raise NavigationFailure(
                f'the preview reached from scan {scan_id} matched {len(found)} expected excerpts; '
                'the page led to another scan, track or sample')
        return found[0]


def fixture_index_us(target_us):
    step = 1_000_000 * FPS.denominator // FPS.numerator
    return target_us // step * step


# ---------------------------------------------------------------------------
# Acceptance: follow every drawing and every table row, then prove what it is.
# ---------------------------------------------------------------------------

def open_evidence(origin, scan_id, href):
    """Follow a desk link and its finding form the way a reviewer would."""
    target, linked_hit = scan_target(href)
    if target != scan_id:
        raise NavigationFailure(f'link points at scan {target}, not the requested scan {scan_id}')
    final, _mime, body = fetch(origin, urlsplit(href).path + '?' + urlsplit(href).query)
    landed, _ = scan_target(final)
    if landed != scan_id:
        raise NavigationFailure(f'following the link landed on scan {landed}')
    doc = parse(body)
    form = preview_form(doc)
    action = form.attrs.get('action', '')
    action_scan, _ = scan_target(action.rsplit('/', 1)[0])
    if action_scan != scan_id:
        raise NavigationFailure(f'evidence form targets scan {action_scan}, not {scan_id}')
    hit = hidden_hit(form)
    if hit != linked_hit:
        raise NavigationFailure(f'finding form carries hit {hit} for a link to {linked_hit}')
    fields = {'hit': hit, 'padding_ms': str(PADDING_MS), 'frames': str(FRAMES)}
    preview_url, _mime, preview_body = fetch(origin, action, fields)
    preview_doc = parse(preview_body)
    back_scan, back_hit = scan_target(back_link(preview_doc))
    if (back_scan, back_hit) != (scan_id, hit):
        raise NavigationFailure(f'preview points back at {back_scan}/{back_hit}, not {scan_id}/{hit}')
    pictures, sound = preview_assets(preview_doc)
    pixels = [parse_png(fetch(origin, path)[2]) for path in pictures]
    served = parse_wav(fetch(origin, sound)[2])
    if served['channels'] != 2 or served['rate'] != SAMPLE_RATE:
        raise NavigationFailure(f'served sound is {served["rate"]} Hz x {served["channels"]}')
    return preview_url, hit, served, pixels


def lane_names(doc):
    timeline = section(doc, '节目时间轴')
    names = []
    for lane in find_all(timeline, 'div', lambda item: 'lane' in classes(item)):
        labels = [item for item in lane.children if isinstance(item, Node) and item.tag == 'span']
        names.append(text_of(labels[0]) if labels else '')
    return names


def text_of(node):
    return ''.join(child for child in node.children if isinstance(child, str)).strip()


def check_geometry(scan, doc, opened):
    """Each mark must sit on the lane and span the API says its finding has."""
    expected_names = lane_layout(scan)
    found_names = lane_names(doc)
    if found_names != expected_names:
        raise NavigationFailure(f'lane order {found_names} does not match {expected_names}')
    duration = scan['extent']['end_us']
    if duration <= 0:
        raise NavigationFailure('scan has no programme extent to draw')
    expected = {}
    for hit in scan['hits']:
        expected[hit['id']] = (lane_of(scan, hit), percent_box((hit['start_us'], hit['end_us']), duration))
    targets = opened['timeline']
    drawn = [targets.get(anchor.attrs.get('href')) for _lane, anchor in lane_marks(doc)]
    if sorted(drawn) != sorted(expected):
        raise NavigationFailure(f'timeline draws {sorted(drawn)}, but the scan holds {sorted(expected)}')
    for index, anchor in lane_marks(doc):
        hit_id = targets.get(anchor.attrs.get('href'))
        if hit_id not in expected:
            raise NavigationFailure(f'mark {anchor.attrs.get("href")} was not verified to a finding')
        lane, (start, width) = expected[hit_id]
        if found_names[index] != lane:
            raise NavigationFailure(f'mark for {hit_id} is on lane {found_names[index]!r}, expected {lane!r}')
        got_left, got_width = mark_box(anchor.attrs.get('style', ''))
        if abs(got_left - start) > GEOMETRY_PERCENT or abs(got_width - width) > GEOMETRY_PERCENT:
            raise NavigationFailure(
                f'mark for {hit_id} is drawn at left {got_left:.4f}% width {got_width:.4f}%, '
                f'but its programme span is left {start:.4f}% width {width:.4f}%')


def accept(scenario):
    """Return what each surface opened; raise on any identity mismatch."""
    metrics = strip_metrics(scenario.origin)
    report = {}
    for scan in scenario.scans:
        scan_id = scan['id']
        _final, _mime, body = fetch(scenario.origin, f'/desk/scans/{scan_id}')
        doc = parse(body)
        expected = scenario.expectations[scan_id]
        surfaces = {'timeline': [anchor for _lane, anchor in lane_marks(doc)],
                    'table': table_marks(doc)}
        opened = {}
        for name, anchors in surfaces.items():
            if not anchors:
                raise NavigationFailure(f'{name} on {scan_id} exposed no finding links')
            reached = {}
            for anchor in anchors:
                href = anchor.attrs.get('href', '')
                _url, hit, served, pixels = open_evidence(scenario.origin, scan_id, href)
                expectation = scenario.match(scan_id, served, pixels)
                if expectation.hit_id != hit:
                    raise NavigationFailure(
                        f'{name} link for {hit} served the excerpt of {expectation.hit_id}')
                reached[href] = expectation.hit_id
            if set(reached.values()) != set(expected):
                raise NavigationFailure(
                    f'{name} on {scan_id} reached {sorted(reached.values())}, expected {sorted(expected)}')
            opened[name] = reached
        report[scan_id] = opened

        marks = lane_marks(doc)
        if len({anchor.attrs.get('href') for _lane, anchor in marks}) != len(marks):
            raise NavigationFailure('two timeline marks share one target')
        for _lane, anchor in marks:
            if not (anchor.attrs.get('aria-label') or anchor.attrs.get('title')):
                raise NavigationFailure(f'mark {anchor.attrs.get("href")} has no accessible name')
        check_geometry(scan, doc, opened)

        stacked = stacked_pair(doc, opened['timeline'], metrics)
        short = {hit_id for hit_id, item in expected.items()
                 if item.hit['kind'] == 'silence' and item.hit['end_us'] - item.hit['start_us'] < 100_000}
        if not stacked:
            raise NavigationFailure('narrow timeline did not stack two distinct short candidates')
        if set(stacked[:2]) != short:
            raise NavigationFailure(
                f'stacked marks on {scan_id} are {stacked[:2]}, not the short pair {sorted(short)}')
        spans = [expected[hit_id].span for hit_id in stacked[:2]]
        if spans[0] == spans[1]:
            raise NavigationFailure('stacked candidates do not carry distinct programme spans')
        if pair_overlaps(doc, stacked[:2], opened['timeline'], metrics, WIDE_WIDTH):
            raise NavigationFailure('the short pair overlaps even on a wide strip, so stacking is unproven')
    return report


def pair_overlaps(doc, hit_ids, targets, metrics, width):
    """Whether the two named marks would visually overlap on a strip of ``width``."""
    label, spacing, minimum = metrics
    strip_px = max(width - label - spacing, 8.0)
    boxes = {}
    for _lane, anchor in lane_marks(doc):
        hit_id = targets.get(anchor.attrs.get('href'))
        if hit_id in hit_ids:
            boxes[hit_id] = pixels(mark_box(anchor.attrs.get('style', '')), strip_px, minimum)
    if sorted(boxes) != sorted(hit_ids):
        raise NavigationFailure(f'both marks of {hit_ids} must be drawn')
    lefts = [boxes[hit_id][0] for hit_id in hit_ids]
    rights = [boxes[hit_id][1] for hit_id in hit_ids]
    return max(lefts) < min(rights)


def stacked_pair(doc, targets, metrics):
    """Two marks on one lane whose boxes overlap at a narrow strip width."""
    label, spacing, minimum = metrics
    lanes = {}
    for lane, anchor in lane_marks(doc):
        lanes.setdefault(lane, []).append(anchor)
    for width in NARROW_WIDTHS:
        strip_px = max(width - label - spacing, 8.0)
        for anchors in lanes.values():
            for first in anchors:
                for second in anchors:
                    if first is second or first.attrs.get('href') == second.attrs.get('href'):
                        continue
                    a = pixels(mark_box(first.attrs.get('style', '')), strip_px, minimum)
                    b = pixels(mark_box(second.attrs.get('style', '')), strip_px, minimum)
                    if max(a[0], b[0]) < min(a[1], b[1]):
                        one = targets.get(first.attrs.get('href'))
                        two = targets.get(second.attrs.get('href'))
                        if one and two and one != two:
                            return (one, two, width)
    return ()


# ---------------------------------------------------------------------------
# Control: a consistent misdirection must be caught, a page-derived test is not.
# ---------------------------------------------------------------------------

def swap_renderer(scans_module, first, second):
    """Resolve both scans to one, with findings mapped across by their content.

    Every desk page, both the timeline marks and the findings table, and the
    evidence form action then name the same single scan, so navigation, the
    detail panel and the preview agree with each other while disagreeing with
    the scan that was actually requested.
    """
    original = scans_module.show
    collapse = {first: second, second: second}

    def swapped(app, scan_id, selected):
        target = collapse.get(scan_id, scan_id)
        mapped = selected
        if selected and target != scan_id:
            source, destination = app.vault.get('scan', scan_id), app.vault.get('scan', target)
            chosen = next((item for item in source['hits'] if item['id'] == selected), None)
            mapped = ''
            if chosen:
                key = (chosen['kind'], chosen['channel'], chosen['start_us'], chosen['end_us'])
                twin = next((item for item in destination['hits']
                             if (item['kind'], item['channel'], item['start_us'], item['end_us']) == key), None)
                mapped = twin['id'] if twin else ''
        return original(app, target, mapped)

    scans_module.show = swapped
    return lambda: setattr(scans_module, 'show', original)


def page_derived_probe(scenario, scan):
    """The insufficient test: take expected identity from the page it is testing."""
    _final, _mime, body = fetch(scenario.origin, f"/desk/scans/{scan['id']}")
    href = lane_marks(parse(body))[0][1].attrs['href']
    linked_scan, linked_hit = scan_target(href)          # expectation manufactured from the page
    _url, hit, served, _pixels = open_evidence(scenario.origin, linked_scan, href)
    _clock, oracle = scenario.api.excerpt(linked_scan, linked_hit)   # likewise from the page's id
    return hit == linked_hit and served['frames'] == oracle['frames']


def run_control(root):
    from .desk import scans as scans_module

    with Scenario(root, nonce='control') as scenario:
        first, second = (scan['id'] for scan in scenario.scans)
        restore = swap_renderer(scans_module, first, second)
        try:
            _final, _mime, body = fetch(scenario.origin, f'/desk/scans/{first}')
            linked = {scan_target(anchor.attrs['href'])[0] for _lane, anchor in lane_marks(parse(body))}
            if linked != {second}:
                raise SetupFailure(f'misdirection did not take effect: {sorted(linked)}')
            if not page_derived_probe(scenario, scenario.scans[0]):
                raise SetupFailure('the page-derived probe was expected to pass under misdirection')
            try:
                accept(scenario)
            except NavigationFailure as exc:
                print(f'对照：一致错指已被抓出 — {exc}')
            else:
                raise SetupFailure('consistent misdirection escaped the check')
        finally:
            restore()


def run_acceptance(root, nonce, label):
    with Scenario(root, nonce=nonce) as scenario:
        accept(scenario)
        first, second = (scan['id'] for scan in scenario.scans)
        if first == second:
            raise SetupFailure('one scan for two track selections')
        print(f'{label}：{first} / {second} 的轨上标记与候选行分别打开，'
              f'证据与轨 {scenario.tracks[first]}/{scenario.tracks[second]} 的独立样本一致，'
              f'叠置短候选按各自 href 复核')


def main():
    parser = argparse.ArgumentParser(description='Exercise Slatewatch desk navigation end to end')
    parser.add_argument('--control', action='store_true', help='only run the misdirection control')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='slatewatch-navigation-') as directory:
        root = Path(directory)
        if not args.control:
            run_acceptance(root / 'first', '', '首次扫描')
            run_acceptance(root / 'rebuilt', 'rebuilt-container', '重建容器')
            print('浏览器导航检查：请求的扫描、选定轨、真实样本与叠置候选均独立核对通过')
        run_control(root / 'control')
        print('浏览器导航对照：按页面自身链接取预期的写法会在一致错指下误过并被本检查拒绝')


if __name__ == '__main__':
    main()
