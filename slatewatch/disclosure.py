"""Recipient-scoped disclosure packages.

A disclosure freezes only the sound lanes and programme windows a producer
authorized for a recipient.  Unlike :mod:`slatewatch.evidence`, excerpts never
carry padding beyond the granted window and the WAV contains only the granted
source channels.  Every cut is recorded as an omission boundary so a hole
created by the disclosure can never be mistaken for a coverage hole present in
the source master.
"""
from array import array
from bisect import bisect_right
from hashlib import sha256
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .clock import InputError, Missing, SECOND, Span, coalesce, integer, sample_boundary, shape, text, uncovered
from .evidence import write_wav
from .media import HEIGHT, WIDTH, Picture, decode
from .rules import canonical, identifier
from .seal import check_bundle, index_for

DISCLOSURE_FORMAT = 1
MAX_GRANTS = 6

# Why a picture target has no frame inside the disclosure.
PICTURE_SOURCE_GAP = 'source_picture_gap'
PICTURE_WINDOW_EDGE = 'outside_authorized_window'
# Why a sound sample position has no PCM inside the disclosure.
SOUND_SOURCE_GAP = 'source_sound_gap'
SOUND_WINDOW_EDGE = 'outside_authorized_window'


def parse_grants(scan, review, data):
    """Validate one recipient disclosure request against the immutable scan."""
    shape(data, ('recipient', 'grants'), ('note',))
    recipient = text(data['recipient'], 'recipient', 120)
    note = text(data['note'], 'note', 350) if data.get('note') is not None else None
    raw_grants = data['grants']
    if not isinstance(raw_grants, list) or not 1 <= len(raw_grants) <= MAX_GRANTS:
        raise InputError('provide one to six authorization grants')
    sound = scan['tracks']['sound']
    channel_count = sound['channels'] if sound else 0
    grants = []
    seen = set()
    for entry in raw_grants:
        shape(entry, ('hit_id',), ('channels', 'padding_ms', 'frames',
                                   'window_start_us', 'window_end_us'))
        hit_id = text(entry['hit_id'], 'hit_id')
        if hit_id in seen:
            raise InputError('each finding can appear in a disclosure only once')
        seen.add(hit_id)
        hit = next((candidate for candidate in scan['hits'] if candidate['id'] == hit_id), None)
        if hit is None:
            raise Missing('finding does not belong to the selected scan')
        channels = _parse_channels(entry, hit, channel_count)
        padding = integer(entry.get('padding_ms', 0), 'padding_ms', 0, 2000)
        frames = integer(entry.get('frames', 3), 'frames', 1, 8)
        hit_span = Span.parse(hit, scan['extent']['end_us'])
        if 'window_start_us' in entry or 'window_end_us' in entry:
            start = integer(entry.get('window_start_us', hit_span.start),
                            'window_start_us', 0, scan['extent']['end_us'])
            end = integer(entry.get('window_end_us', hit_span.end),
                          'window_end_us', 1, scan['extent']['end_us'])
            window = Span(start, end)
            if not window.contains(hit_span):
                raise InputError('the authorized window must fully contain the finding interval')
        else:
            padding_us = padding * 1000
            window = Span(max(0, hit_span.start - padding_us),
                          min(scan['extent']['end_us'], hit_span.end + padding_us))
        grants.append({'hit': hit, 'review': _review_for(review, hit_id), 'window': window,
                       'channels': channels, 'frames': frames, 'recipient': recipient})
    return {'recipient': recipient, 'note': note, 'grants': grants}


def _parse_channels(entry, hit, channel_count):
    requested = entry.get('channels')
    if requested is None:
        if hit['channel'] is not None:
            if not channel_count:
                raise InputError('the selected scan has no sound lane')
            return [hit['channel']]
        return list(range(1, channel_count + 1)) if channel_count else []
    if not isinstance(requested, list) or not requested or len(requested) > 2:
        raise InputError('channels must list one or two source channel numbers')
    channels = [integer(value, 'channel', 1, max(1, channel_count)) for value in requested]
    if len(set(channels)) != len(channels):
        raise InputError('channels must not repeat')
    if not channel_count:
        raise InputError('the selected scan has no sound lane to disclose')
    if hit['channel'] is not None and channels != [hit['channel']]:
        raise InputError('a sound finding can only disclose its own measured channel')
    return channels


def _review_for(review, hit_id):
    return next(item for item in review['hits'] if item['id'] == hit_id)


class DisclosureItem:
    def __init__(self, grant, picture_coverage, sound_coverage, sound):
        self.grant = grant
        self.window = grant['window']
        self.frames = grant['frames']
        self.picture_coverage = picture_coverage
        self.sound_coverage = sound_coverage
        self.targets = [self.window.start + self.window.duration * (i * 2 + 1) // (2 * self.frames)
                        for i in range(self.frames)]
        self.pictures = [None] * self.frames
        self.track = sound
        self.channels = grant['channels']
        self.pcm = None
        self.coverage = []
        if sound and self.channels:
            self.rate = sound['rate']
            self.first = sample_boundary(self.window.start, self.rate)
            self.end = sample_boundary(self.window.end, self.rate)
            self.pcm = array('h', [0]) * ((self.end - self.first) * len(self.channels))

    def frame(self, frame, coverage, next_time):
        for index, point in enumerate(self.targets):
            if frame.timestamp_us <= point < next_time and any(s.start <= point < s.end for s in coverage):
                self.pictures[index] = frame

    def sound(self, block):
        if self.pcm is None:
            return
        begin, end = max(block.first_sample, self.first), min(block.end_sample, self.end)
        if begin >= end:
            return
        source_layout = self.track['channels']
        for out_index, channel in enumerate(self.channels):
            source_offset = (begin - block.first_sample) * source_layout + channel - 1
            target_offset = (begin - self.first) * len(self.channels) + out_index
            step = len(self.channels)
            count = end - begin
            self.pcm[target_offset:target_offset + count * step:step] = \
                block.samples[source_offset:source_offset + count * source_layout:source_layout]
        self.coverage.append(Span(begin, end))

    def finish(self):
        hit = self.grant['hit']
        files = {}
        picture_map = []
        for index, (point, frame) in enumerate(zip(self.targets, self.pictures)):
            name = None
            if frame is not None:
                name = f'frame-{index + 1:02d}.pgm'
                files[name] = f'P5\n{WIDTH} {HEIGHT}\n255\n'.encode() + frame.pixels
                reason = None
            else:
                reason = self._missing_picture_reason(point)
            picture_map.append({'target_us': point,
                                'source_us': frame.timestamp_us if frame else None,
                                'file': name, 'missing_reason': reason})
        sound_map = None
        if self.pcm is not None:
            files['sound.wav'] = write_wav(self.pcm, self.rate, len(self.channels))
            filled = list(uncovered(coalesce(self.coverage), Span(self.first, self.end)))
            sound_map = {
                'file': 'sound.wav', 'sample_rate': self.rate,
                'channels': list(self.channels),
                'source_track_channels': self.track['channels'],
                'first_source_sample': self.first, 'end_source_sample': self.end,
                'clock_offset_us': round(self.first * SECOND / self.rate),
                'filled_sample_ranges': [{'first': span.start, 'end': span.end,
                                          'reason': self._fill_reason(span)} for span in filled],
                'source_sound_coverage': [span.as_dict() for span in self._window_sound_coverage()],
            }
        mapping = {
            'hit_id': hit['id'], 'recipient': self.grant['recipient'],
            'authorized_window': self.window.as_dict(),
            'finding_span': {'start_us': hit['start_us'], 'end_us': hit['end_us']},
            'finding_kind': hit['kind'], 'finding_channel': hit['channel'],
            'review_status': self.grant['review']['status'],
            'picture': picture_map, 'sound': sound_map,
            'picture_source_coverage': [span.as_dict() for span in self._window_picture_coverage()],
        }
        return files, mapping

    def _missing_picture_reason(self, point):
        if not self.window.start <= point < self.window.end:
            return PICTURE_WINDOW_EDGE
        if any(span.start <= point < span.end for span in self.picture_coverage):
            return None
        return PICTURE_SOURCE_GAP

    def _fill_reason(self, span):
        # Only a fill whose whole extent sits inside the authorized window is a
        # hole that existed in the source master: measured PCM must bracket it on
        # both sides.  A fill touching a window edge exists because the material
        # beyond that edge was withheld; re-labeling it as a source hole would
        # fabricate evidence the recipient's cut cannot actually support.
        coverage = [Span(sample_boundary(cov.start, self.rate),
                         sample_boundary(cov.end, self.rate))
                    for cov in self._window_sound_coverage()]
        inside_window = span.start >= self.first and span.end <= self.end
        has_pcm_before = any(cov.end <= span.start for cov in coverage)
        has_pcm_after = any(cov.start >= span.end for cov in coverage)
        if inside_window and has_pcm_before and has_pcm_after:
            return SOUND_SOURCE_GAP
        return SOUND_WINDOW_EDGE

    def _window_sound_coverage(self):
        return [overlap for span in self.sound_coverage
                if (overlap := span.intersect(self.window)) is not None]

    def _window_picture_coverage(self):
        return [overlap for span in self.picture_coverage
                if (overlap := span.intersect(self.window)) is not None]


def build(path, scan, authorization):
    """Decode the master once and produce one independently reviewable item per grant."""
    picture_coverage = [Span.parse(value) for value in scan['coverage']['picture']]
    sound_coverage = ([Span.parse(value) for value in scan['coverage']['sound']]
                      if scan['coverage']['sound'] else [])
    stops = [span.end for span in picture_coverage]
    sound = scan['tracks']['sound']
    items = [DisclosureItem(grant, picture_coverage, sound_coverage, sound)
             for grant in authorization['grants']]
    prior = None
    for block in decode(path, scan['tracks']):
        if isinstance(block, Picture):
            if prior is not None:
                _capture(items, prior, block.timestamp_us, picture_coverage, stops)
            prior = block
        else:
            for item in items:
                item.sound(block)
    if prior is not None:
        _capture(items, prior, scan['extent']['end_us'], picture_coverage, stops)
    return [item.finish() for item in items]


def _capture(items, frame, next_time, coverage, stops):
    index = bisect_right(stops, frame.timestamp_us)
    if index < len(coverage):
        for item in items:
            item.frame(frame, coverage[index:index + 1], next_time)


def pack(scan, review, master_sha256, authorization, items):
    """Assemble the frozen disclosure ZIP and its content-addressed descriptor."""
    files = {}
    maps = []
    for members, mapping in items:
        hit_id = mapping['hit_id']
        for name, data in members.items():
            files[f'disclosure/{hit_id}/{name}'] = data
        maps.append(mapping)
        files[f'disclosure/{hit_id}/clock.json'] = canonical(mapping)
    files['disclosure.json'] = canonical({
        'recipient': authorization['recipient'], 'note': authorization['note'],
        'scan_id': scan['id'], 'master_sha256': master_sha256,
        'review_status': review['status'], 'items': maps,
    })
    files['scan-context.json'] = canonical({
        'scan_id': scan['id'], 'master_id': scan['master_id'],
        'policy_id': scan['policy_id'], 'analysis_version': scan['analysis_version'],
        'extent': scan['extent'], 'tracks': scan['tracks'], 'coverage': scan['coverage'],
    })
    index = {'format': DISCLOSURE_FORMAT, 'master_sha256': master_sha256,
             'scan_id': scan['id'], 'recipient': authorization['recipient'],
             'review_status': review['status'], 'members': index_for(files)}
    disclosure_id = identifier('disclosure', index)
    record = {'id': disclosure_id, **index}
    files['index.json'] = canonical(record)
    buf = BytesIO()
    with ZipFile(buf, 'w') as archive:
        for name, data in sorted(files.items()):
            entry = ZipInfo(name)
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return record, buf.getvalue()


def verify(path, record):
    check_bundle(path, record)
    return {'id': record['id'], 'status': 'verified', 'sha256': record['sha256'],
            'members': len(record['members']), 'scan_id': record['scan_id'],
            'recipient': record['recipient'], 'review_status': record['review_status'],
            'format': DISCLOSURE_FORMAT}


def verify_bundle_bytes(data):
    """Verify a disclosure ZIP using only the index sealed inside it.

    Recipients have no workstation vault, so the embedded ``index.json`` is
    treated as the expected manifest: every listed member is re-hashed and the
    membership set must match exactly.  A changed file therefore fails even
    though the original descriptor is unavailable to the recipient.
    """
    import json
    from zipfile import BadZipFile, ZipFile

    try:
        with ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or 'index.json' not in names:
                raise Missing('bundle index is missing or duplicated')
            record = json.loads(archive.read('index.json'))
            expected_members = record.get('members')
            if not isinstance(expected_members, dict) or record.get('format') != DISCLOSURE_FORMAT:
                raise Missing('bundle index is not a disclosure manifest')
            if set(names) != {'index.json', *expected_members}:
                raise Missing('bundle membership differs from the sealed index')
            for name, expected in expected_members.items():
                member = archive.read(name)
                if len(member) != expected['size'] or sha256(member).hexdigest() != expected['sha256']:
                    raise Missing(f'bundle member {name!r} failed verification')
    except Missing:
        raise
    except (BadZipFile, OSError, ValueError, EOFError, KeyError) as exc:
        raise Missing('disclosure bundle cannot be read') from exc
    return {'id': record['id'], 'status': 'verified', 'members': len(expected_members),
            'scan_id': record['scan_id'], 'recipient': record['recipient'],
            'review_status': record['review_status'], 'bundle_sha256': sha256(data).hexdigest(),
            'format': DISCLOSURE_FORMAT}
