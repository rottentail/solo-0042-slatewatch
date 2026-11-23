"""Build playable calibration tapes whose pulses trace back to source times."""
from array import array
from bisect import bisect_right
from fractions import Fraction
from io import BytesIO
from math import pi, sin
import sys

import av

from .clock import SECOND, InputError, Span, sample_boundary, uncovered
from .media import Picture, decode

TAPE_RATE = 48000
TAPE_SIZE = (160, 96)
SYNC_HZ = 1000
SYNC_MS = 40
SYNC_LEVEL = 12000
HOLE_HZ = 220
HOLE_LEVEL = 8000
FADE_MS = 2
BITS = 20
BAND_ROWS = 8

PLAIN_ROW = bytes([32]) * TAPE_SIZE[0]
SOLID_ROW = bytes([16]) * TAPE_SIZE[0]
FLASH_ROW = b'\x10\x10' + bytes([235]) * (TAPE_SIZE[0] - 4) + b'\x10\x10'


def tape_rate(track):
    """Output frame rate follows the registered picture track, not a rebuilt grid."""
    try:
        rate = Fraction(track['average_rate'] or '25')
    except (ValueError, ZeroDivisionError) as exc:
        raise InputError('picture track has no usable frame rate') from exc
    if rate <= 0:
        return Fraction(25)
    if rate > 120:
        raise InputError('picture track rate is outside the supported range')
    return rate


def _ceil(value):
    return -((-value.numerator) // value.denominator)


def _nearest(value):
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def plan(picture_times, coverage, holes, extent_end, rate, pulse_us, total_samples):
    """Classify tape frames and schedule source-anchored sync and hole events."""
    frame_us = Fraction(SECOND) / rate
    frames_total = _ceil(Fraction(extent_end) / frame_us)
    kinds = ['plain'] * frames_total
    events = []
    for gap in holes:
        first_frame = last_frame = None
        for n in range(frames_total):
            if gap.start <= (2 * n + 1) * frame_us / 2 < gap.end:
                kinds[n] = 'hole'
                first_frame = n if first_frame is None else first_frame
                last_frame = n
        event = {'kind': 'hole', 'source_start_us': gap.start, 'source_end_us': gap.end,
                 'tape_first_frame': first_frame,
                 'tape_frames': 0 if first_frame is None else last_frame - first_frame + 1}
        if first_frame is None:
            event.update(tone_first_sample=None, tone_samples=0)
        else:
            tone_first = min(_nearest(first_frame * frame_us * TAPE_RATE / SECOND), total_samples)
            tone_end = _nearest((last_frame + 1) * frame_us * TAPE_RATE / SECOND)
            event.update(tone_first_sample=tone_first,
                         tone_samples=max(0, min(tone_end, total_samples) - tone_first))
        events.append(event)
    beep = SYNC_MS * TAPE_RATE // 1000
    used = set()
    scheduled = 0
    while scheduled < extent_end:
        index = bisect_right(picture_times, scheduled) - 1
        if index >= 0 and any(span.start <= scheduled < span.end for span in coverage):
            source_us = picture_times[index]
            tape_frame = min(_nearest(Fraction(source_us) / frame_us), frames_total - 1)
            if tape_frame not in used:
                used.add(tape_frame)
                kinds[tape_frame] = 'sync'
                first = _nearest(Fraction(source_us * TAPE_RATE, SECOND))
                events.append({'kind': 'sync', 'scheduled_us': scheduled,
                               'source_frame_us': source_us, 'source_frame_index': index,
                               'tape_frame': tape_frame,
                               'tape_frame_us': _nearest(tape_frame * frame_us),
                               'beep_first_sample': first,
                               'beep_samples': max(0, min(beep, total_samples - first)),
                               'beep_first_us': _nearest(Fraction(first * SECOND, TAPE_RATE))})
        scheduled += pulse_us
    events.sort(key=lambda event: event['source_frame_us'] if event['kind'] == 'sync' else event['source_start_us'])
    return kinds, events


def synth(events, total_samples):
    """Render sync beeps and hole tones on the output sample grid."""
    pcm = array('h', bytes(2 * total_samples))
    waves = {}

    def wave(hz, level):
        key = (hz, level)
        if key not in waves:
            waves[key] = [int(level * sin(2 * pi * hz * t / TAPE_RATE)) for t in range(TAPE_RATE)]
        return waves[key]

    def burst(first, count, hz, level, fade):
        if first is None or count <= 0:
            return
        tone = wave(hz, level)
        stop = min(count, total_samples - first)
        head = min(fade, stop)
        tail = max(head, stop - fade)
        for j in range(head):
            pcm[first + j] = tone[(first + j) % TAPE_RATE] * j // fade
        for j in range(head, tail):
            pcm[first + j] = tone[(first + j) % TAPE_RATE]
        for j in range(tail, stop):
            pcm[first + j] = tone[(first + j) % TAPE_RATE] * (stop - j) // fade

    fade = FADE_MS * TAPE_RATE // 1000
    for event in events:
        if event['kind'] == 'sync':
            burst(event['beep_first_sample'], event['beep_samples'], SYNC_HZ, SYNC_LEVEL, fade)
        else:
            burst(event['tone_first_sample'], event['tone_samples'], HOLE_HZ, HOLE_LEVEL, 5 * fade)
    if sys.byteorder != 'little':
        pcm.byteswap()
    return pcm


def raster(kind, index):
    """One tape frame: locator bit row on top, sync flash, hole stripes or dim field."""
    width, height = TAPE_SIZE
    band = bytearray(SOLID_ROW)
    for bit in range(BITS):
        if (index >> (BITS - 1 - bit)) & 1:
            band[bit * 8:(bit + 1) * 8] = b'\xeb' * 8
    rows = []
    for y in range(height):
        if y == 0 or y == BAND_ROWS - 1:
            rows.append(SOLID_ROW)
        elif y < BAND_ROWS:
            rows.append(bytes(band))
        elif kind == 'hole':
            shift = (3 * index) % 16
            rows.append(bytes(210 if (x + shift) % 16 < 8 else 24 for x in range(width)))
        elif kind == 'sync':
            rows.append(SOLID_ROW if y < BAND_ROWS + 2 or y >= height - 2 else FLASH_ROW)
        else:
            rows.append(PLAIN_ROW)
    frame = av.VideoFrame(width, height, 'gray')
    plane = frame.planes[0]
    raw = bytearray(plane.buffer_size)
    for y, row in enumerate(rows):
        start = y * plane.line_size
        raw[start:start + width] = row
    plane.update(raw)
    return frame


def sound_frame(pcm, first, count):
    frame = av.AudioFrame(format='s16', layout='mono', samples=count)
    frame.planes[0].update(pcm[first:first + count].tobytes())
    frame.sample_rate = TAPE_RATE
    frame.time_base = Fraction(1, TAPE_RATE)
    frame.pts = first
    return frame


def encode(kinds, rate, pcm):
    """Mux the planned frames and pulse track into a playable Matroska tape."""
    buf = BytesIO()
    total_samples = len(pcm)
    with av.open(buf, mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=rate)
        video.width, video.height = TAPE_SIZE
        video.pix_fmt = 'gray'
        audio = container.add_stream('pcm_s16le', rate=TAPE_RATE)
        audio.layout = 'mono'
        next_sample = 0
        chunk = TAPE_RATE // 10
        for index, kind in enumerate(kinds):
            frame = raster(kind, index)
            frame.pts = index
            frame.time_base = 1 / rate
            container.mux(video.encode(frame))
            horizon = Fraction(index + 1, 1) / rate
            while next_sample < total_samples and Fraction(next_sample, TAPE_RATE) <= horizon:
                count = min(chunk, total_samples - next_sample)
                container.mux(audio.encode(sound_frame(pcm, next_sample, count)))
                next_sample += count
        while next_sample < total_samples:
            count = min(chunk, total_samples - next_sample)
            container.mux(audio.encode(sound_frame(pcm, next_sample, count)))
            next_sample += count
        container.mux(video.encode())
        container.mux(audio.encode())
    return buf.getvalue()


def build_tape(path, scan, asset, pulse_us):
    """Decode the master once, then return (tape bytes, source-time mapping)."""
    tracks = scan['tracks']
    rate = tape_rate(tracks['picture'])
    coverage = [Span.parse(span) for span in scan['coverage']['picture']]
    extent_end = scan['extent']['end_us']
    picture_times = [block.timestamp_us for block in decode(path, tracks) if isinstance(block, Picture)]
    holes = list(uncovered(coverage, Span(0, extent_end)))
    total_samples = sample_boundary(extent_end, TAPE_RATE)
    kinds, events = plan(picture_times, coverage, holes, extent_end, rate, pulse_us, total_samples)
    tape = encode(kinds, rate, synth(events, total_samples))
    width, height = TAPE_SIZE
    mapping = {
        'format': 1,
        'kind': 'slatewatch-calibration',
        'master_id': asset['id'],
        'master_sha256': asset['sha256'],
        'scan_id': scan['id'],
        'source': {
            'extent': scan['extent'],
            'picture_coverage': scan['coverage']['picture'],
            'picture_holes': [span.as_dict() for span in holes],
            'sound': 'selected' if tracks['sound'] else 'not-selected',
        },
        'tape': {
            'file': 'calibration.mkv',
            'container': 'matroska',
            'video': {'codec': 'ffv1', 'width': width, 'height': height,
                      'frame_rate': str(rate), 'frames': len(kinds)},
            'audio': {'codec': 'pcm_s16le', 'sample_rate': TAPE_RATE, 'channels': 1,
                      'samples': total_samples},
        },
        'pulse_us': pulse_us,
        'events': events,
    }
    return tape, mapping
