"""Recheck an earlier clipping finding window by window on a later version.

A later scan not reporting a candidate is not proof that the earlier problem is
gone.  The two versions may use different selected tracks and sample rates, and
a short run of clipped samples can fall below the later scan's minimum event
length.  A recheck therefore keeps the earlier finding's own measurement windows
and thresholds, maps them explicitly onto one reviewer-confirmed sound lane of
the later version, and measures the later source samples only.

A recheck is evidence, not a ruling: it never changes the earlier candidate or
the comparison result.
"""
from collections import OrderedDict
from hashlib import sha256
from threading import Lock
from urllib.parse import urlencode

from .clock import InputError, Missing, Span, sample_boundary
from .media import decode
from .rules import Thresholds, canonical, identifier
from .sound import SoundMeter

CLEARABLE = 'clearable'
STILL_CLIPPED = 'still_clipped'
INSUFFICIENT = 'insufficient_evidence'
STATUS_ORDER = (STILL_CLIPPED, INSUFFICIENT, CLEARABLE)
LABELS = {CLEARABLE: '可销', STILL_CLIPPED: '仍削顶', INSUFFICIENT: '证据不足'}
EQUAL_LENGTH_TOLERANCE_US = 1000


def listen_path(after_scan_id, start_us, end_us, channel):
    query = urlencode({'start_us': start_us, 'end_us': end_us, 'channel': channel})
    return f'/desk/scans/{after_scan_id}/listen?{query}'


def window_spans(scan, hit):
    """Return the earlier finding's own measurement windows, in order."""
    span = Span.parse(hit, scan['extent']['end_us'])
    step_us = scan['thresholds']['audio_window_ms'] * 1000
    if span.start % step_us:
        raise InputError('finding start does not align with its measurement windows')
    if span.duration % step_us or span.duration < step_us:
        raise InputError('finding does not span whole measurement windows')
    count = span.duration // step_us
    if hit.get('bins') not in (None, count):
        raise InputError('finding window count differs from its stored bins')
    return [(span.start + index * step_us, span.start + (index + 1) * step_us)
            for index in range(count)]


def measure(manifest, path, step_us):
    """Measure the later selected sound lane on the earlier window grid.

    ``manifest`` is the later scan's stream description, which keeps the shared
    container origin together with the selected sound track.  All channels of
    that track are measured together so one pass serves findings on any lane.
    """
    track = manifest['sound']
    meter = SoundMeter(track, Thresholds(audio_window_ms=step_us // 1000), window_us=step_us)
    for block in decode(path, {'picture': None, 'sound': track, 'origin_us': manifest['origin_us']},
                        pictures=False):
        meter.push(block)
    rows = {start_us: {'covered': window.covered,
                       'total': window.end_sample - window.start_sample,
                       'clipped': list(window.clipped)}
            for start_us, window in ((key * step_us, window)
                                     for key, window in meter.windows.items())}
    stats = {'sample_rate': track['rate'], 'channels': track['channels'],
             'sound_index': track['index'], 'windows': rows}
    stats['signature'] = sha256(canonical({'rate': stats['sample_rate'],
                                           'index': stats['sound_index'],
                                           'windows': rows})).hexdigest()[:24]
    return stats


class WindowCache:
    """Share one later-version sound measurement across earlier findings.

    Entries are keyed by the later scan, its selected sound stream and the
    window width.  The confirmed lane is applied when a window is read, so
    different findings reuse the same decode while a different lane or window
    grid can never read another entry's numbers.
    """

    def __init__(self, capacity=8):
        self.capacity = capacity
        self.entries = OrderedDict()
        self.lock = Lock()

    @staticmethod
    def key(after_scan, step_us):
        return (after_scan['id'], after_scan['tracks']['sound']['index'], step_us)

    def lookup(self, key):
        with self.lock:
            if key not in self.entries:
                return None
            self.entries.move_to_end(key)
            return self.entries[key]

    def store(self, key, value):
        with self.lock:
            self.entries[key] = value
            self.entries.move_to_end(key)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)
        return value


def lane_bounds(sound, channel):
    if type(channel) is not int or not 1 <= channel <= sound['channels']:
        raise InputError('after_channel must name a lane of the later selected sound track')
    return channel


def window_status(measured, total, threshold_permille, channel):
    if measured is None or measured['covered'] < total:
        return INSUFFICIENT, None
    clipped = measured['clipped'][channel - 1]
    permille = clipped * 1000 // total
    return (STILL_CLIPPED if permille >= threshold_permille else CLEARABLE), permille


def windows_for(scan, hit, stats, after_scan, after_channel):
    step_us = scan['thresholds']['audio_window_ms'] * 1000
    threshold = scan['thresholds']['clip_permille']
    rate = after_scan['tracks']['sound']['rate']
    windows = []
    for index, (start_us, end_us) in enumerate(window_spans(scan, hit)):
        first = sample_boundary(start_us, rate)
        end = sample_boundary(end_us, rate)
        total = end - first
        measured = stats['windows'].get(start_us)
        status, permille = window_status(measured, total, threshold, after_channel)
        windows.append({
            'index': index, 'start_us': start_us, 'end_us': end_us,
            'first_sample': first, 'end_sample': end,
            'window_samples': total,
            'covered_samples': measured['covered'] if measured else 0,
            'clipped_samples': measured['clipped'][after_channel - 1] if measured else 0,
            'clip_permille': permille, 'status': status,
            'replay': listen_path(after_scan['id'], start_us, end_us, after_channel),
        })
    return windows


def groups_for(windows, after_scan_id, after_channel):
    groups = []
    for row in windows:
        if groups and groups[-1]['status'] == row['status'] and groups[-1]['end_us'] == row['start_us']:
            groups[-1]['end_us'] = row['end_us']
            groups[-1]['windows'] += 1
        else:
            groups.append({'status': row['status'], 'start_us': row['start_us'],
                           'end_us': row['end_us'], 'windows': 1,
                           'replay': listen_path(after_scan_id, row['start_us'], row['end_us'],
                                                 after_channel)})
    return groups


def counts_for(windows):
    counts = dict.fromkeys(STATUS_ORDER, 0)
    for row in windows:
        counts[row['status']] += 1
    return counts


def overall_status(counts):
    for status in STATUS_ORDER:
        if counts[status]:
            return status
    return INSUFFICIENT


def find(scan, hit_id):
    for item in scan['hits']:
        if item['id'] == hit_id:
            return item
    raise Missing('finding does not belong to the selected scan')


def validate(scan, after_scan, hit_id):
    if scan['id'] == after_scan['id']:
        raise InputError('recheck needs two different scans')
    hit = find(scan, hit_id)
    if hit['kind'] != 'clip':
        raise InputError('recheck applies to a clipping finding only')
    if hit['channel'] is None:
        raise InputError('clipping finding has no sound lane')
    if not scan['tracks']['sound']:
        raise InputError('earlier scan has no selected sound track')
    if not after_scan['tracks']['sound']:
        raise InputError('later scan has no selected sound track')
    if not 1 <= hit['channel'] <= scan['tracks']['sound']['channels']:
        raise InputError('earlier finding lane is outside its selected sound track')
    windows = window_spans(scan, hit)
    delta = after_scan['extent']['end_us'] - scan['extent']['end_us']
    if abs(delta) > EQUAL_LENGTH_TOLERANCE_US:
        raise InputError('versions are not equal length; align the programme clock before rechecking')
    if delta < 0:
        raise InputError('later version ends before the earlier finding')
    return hit, windows, delta


def build(scan, after_scan, hit, after_channel, stats, reviewer, reason):
    windows = windows_for(scan, hit, stats, after_scan, after_channel)
    counts = counts_for(windows)
    step_us = scan['thresholds']['audio_window_ms'] * 1000
    core = {
        'before_scan': scan['id'], 'after_scan': after_scan['id'],
        'before_hit_id': hit['id'], 'before_kind': hit['kind'],
        'before_channel': hit['channel'], 'after_channel': after_channel,
        'window_us': step_us,
        'thresholds': {'clip_permille': scan['thresholds']['clip_permille'],
                       'clip_min_ms': scan['thresholds']['clip_min_ms'],
                       'audio_window_ms': scan['thresholds']['audio_window_ms']},
        'windows': windows, 'groups': groups_for(windows, after_scan['id'], after_channel),
        'counts': counts, 'status': overall_status(counts),
        'reviewer': reviewer, 'reason': reason,
    }
    before_end = scan['extent']['end_us']
    after_end = after_scan['extent']['end_us']
    return {
        'id': identifier('recheck', core), **core,
        'lane': {'before_channel': hit['channel'], 'after_channel': after_channel,
                 'before_sound': scan['tracks']['sound'], 'after_sound': after_scan['tracks']['sound']},
        'alignment': {'before_extent_end_us': before_end, 'after_extent_end_us': after_end,
                      'delta_us': after_end - before_end,
                      'equal_length': abs(after_end - before_end) <= EQUAL_LENGTH_TOLERANCE_US},
        'measurement': {'sample_rate': stats['sample_rate'], 'sound_index': stats['sound_index'],
                        'signature': stats['signature'], 'window_us': step_us},
    }


def evaluate(scan, after_scan, hit_id, after_channel, cache, loader, reviewer, reason):
    """Validate the request, obtain the later measurement and build the record.

    ``loader`` is called only when the shared cache has no entry for this later
    scan, sound stream and window width.  The cache never appears in the stored
    record, so reusing a measurement cannot change the recheck identity.
    """
    hit, _windows, _delta = validate(scan, after_scan, hit_id)
    lane_bounds(after_scan['tracks']['sound'], after_channel)
    step_us = scan['thresholds']['audio_window_ms'] * 1000
    key = cache.key(after_scan, step_us)
    stats = cache.lookup(key)
    if stats is None:
        stats = cache.store(key, loader())
    return build(scan, after_scan, hit, after_channel, stats, reviewer, reason)
