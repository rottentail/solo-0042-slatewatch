"""Generate small repeatable synthetic masters with known signal episodes."""
from array import array
from fractions import Fraction
from math import pi, sin
from pathlib import Path
import argparse
import sys

import av

SAMPLE_RATE = 8000
SIZE = (160, 96)
REELS = ('basalt', 'revision', 'picture-hole', 'no-sound', 'fractional', 'repeat-trailer', 'dubbed-repeat')
IDENT_FRAMES = 20
TRAILER_FRAMES = 50


def raster(index, dark, still):
    width, height = SIZE
    frame = av.VideoFrame(width, height, 'gray')
    plane = frame.planes[0]
    phase = 3 if still else index * 5
    raw = bytearray(plane.buffer_size)
    for y in range(height):
        start = y * plane.line_size
        row = bytes(width) if dark else bytes(40 + (x + y * 2 + phase) % 181 for x in range(width))
        raw[start:start + width] = row
    plane.update(raw)
    return frame


def audio_frame(first, count, revision):
    values = array('h')
    for sample in range(first, first + count):
        tone = round(8000 * sin(2 * pi * 500 * sample / SAMPLE_RATE))
        left = 0 if 9600 <= sample < 14400 else tone
        right = 32767 if 20000 <= sample < 22000 and not revision else -tone
        values.extend((left, right))
    frame = av.AudioFrame(format='s16', layout='stereo', samples=count)
    if sys.byteorder != 'little':
        values.byteswap()
    frame.planes[0].update(values.tobytes())
    frame.sample_rate = SAMPLE_RATE
    frame.time_base = Fraction(1, SAMPLE_RATE)
    frame.pts = first
    return frame


def _make_signal(path, name):
    fps = Fraction(30000, 1001) if name == 'fractional' else Fraction(25)
    audio_end = 28000 if name == 'revision' else 25600
    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=fps)
        video.width, video.height = SIZE
        video.pix_fmt = 'gray'
        audio = None if name == 'no-sound' else container.add_stream('pcm_s16le', rate=SAMPLE_RATE)
        if audio:
            audio.layout = 'stereo'
        next_sample = 0
        for i in range(round(4 * fps)):
            seconds = Fraction(i, fps)
            if name != 'picture-hole' or not Fraction(2) <= seconds < Fraction(5, 2):
                dark = Fraction(0) <= seconds < (Fraction(8, 25) if name == 'revision' else Fraction(12, 25))
                still = Fraction(3, 5) <= seconds < Fraction(7, 5)
                frame = raster(i, dark, still)
                frame.pts = i
                frame.time_base = 1 / fps
                container.mux(video.encode(frame))
            while audio and next_sample < audio_end and Fraction(next_sample, SAMPLE_RATE) <= seconds:
                if not 16000 <= next_sample < 17600:
                    container.mux(audio.encode(audio_frame(next_sample, 160, name == 'revision')))
                next_sample += 160
        container.mux(video.encode())
        if audio:
            container.mux(audio.encode())
    return Path(path)


def cell_value(seed, index, cell):
    value = (seed * 73856093) ^ (index * 19349663) ^ (cell * 83492791)
    value = (value ^ (value >> 13)) * 1274126177
    return (value ^ (value >> 16)) % 256


def grey_frame(pixels, index, fps):
    width, height = SIZE
    frame = av.VideoFrame(width, height, 'gray')
    plane = frame.planes[0]
    raw = bytearray(plane.buffer_size)
    for y in range(height):
        start = y * plane.line_size
        raw[start:start + width] = bytes(pixels(x, y) & 0xff for x in range(width))
    plane.update(raw)
    frame.pts = index
    frame.time_base = 1 / fps
    return frame


def ident_pixels():
    def pixels(x, y):
        if 12 <= x < 60 and 12 <= y < 36:
            return 240
        if 100 <= x < 148 and 60 <= y < 84:
            return 215
        if 66 <= x < 94 and 40 <= y < 58:
            return 250
        return 18 + (x * 3 + y * 5) % 11
    return pixels


def trailer_pixels(index):
    def pixels(x, y):
        left = (index * 9) % 120
        top = (index * 5) % 66
        if left <= x < left + 40 and top <= y < top + 30:
            return 245
        if (x * 2 + y * 3 + index * 7) % 47 < 5:
            return 220
        return 30 + cell_value(11, index, (y // 12) * 8 + x // 20) % 90
    return pixels


def content_pixels(seed, index):
    def pixels(x, y):
        return 20 + cell_value(seed, index, (y // 16) * 10 + x // 16)
    return pixels


def trailer_tone(n, variant):
    if variant == 'missing':
        return None
    if variant == 'dub':
        value = round(5200 * sin(2 * pi * 430 * n / SAMPLE_RATE))
        if 4000 <= n < 5200:
            return 32767, value
        return value, value
    value = round(5200 * sin(2 * pi * 900 * n / SAMPLE_RATE))
    if variant == 'local-dip' and 3200 <= n < 6400:
        # A local defect confined to the first play, unrelated to the later missing audio.
        return value, 0
    return value, value


def content_tone(seed, n):
    frequency = 280 + seed * 260 + (n // 2000) * 25
    return round(3000 * sin(2 * pi * frequency * n / SAMPLE_RATE))


def repeat_segments(name):
    """Ident, content, trailer, ident, content, trailer, ... with no trailer between them."""
    if name == 'repeat-trailer':
        plan = [('content', 1), ('trailer', 'local-dip'), ('content', 2), ('trailer', 'missing'),
                ('content', 3), ('trailer', 'normal'), ('content', 4)]
    else:
        plan = [('content', 5), ('trailer', 'normal'), ('content', 6), ('trailer', 'dub'),
                ('content', 7)]
    segments = [{'kind': 'ident', 'start': 0}]
    cursor = IDENT_FRAMES
    for kind, key in plan:
        if kind == 'trailer':
            segments.append({'kind': 'trailer', 'start': cursor, 'key': key,
                             'frames': TRAILER_FRAMES})
            cursor += TRAILER_FRAMES
        else:
            segments.append({'kind': 'content', 'start': cursor, 'key': key, 'frames': 40})
            cursor += 40
        segments.append({'kind': 'ident', 'start': cursor})
        cursor += IDENT_FRAMES
    return segments[:-1], cursor - IDENT_FRAMES


def make_repeat(path, name):
    """A reel whose trailer recurs; the station ident recurs too but is not the trailer."""
    fps = Fraction(25)
    segments, total = repeat_segments(name)
    flash = {'repeat-trailer': 362}
    schedule = [None] * total
    for segment in segments:
        for offset in range(segment['frames'] if segment['kind'] == 'content' else
                            (TRAILER_FRAMES if segment['kind'] == 'trailer' else IDENT_FRAMES)):
            schedule[segment['start'] + offset] = (segment, offset)
    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=fps)
        video.width, video.height = SIZE
        video.pix_fmt = 'gray'
        audio = container.add_stream('pcm_s16le', rate=SAMPLE_RATE)
        audio.layout = 'stereo'
        next_sample = 0
        flash_index = flash.get(name, -1)
        for index in range(total):
            segment, offset = schedule[index]
            if segment['kind'] == 'ident':
                pixels = ident_pixels()
            elif segment['kind'] == 'trailer':
                pixels = (lambda x, y: 255) if index == flash_index else trailer_pixels(offset)
            else:
                pixels = content_pixels(segment['key'], offset)
            container.mux(video.encode(grey_frame(pixels, index, fps)))
            seconds = Fraction(index, fps)
            while next_sample < total * SAMPLE_RATE // 25 and Fraction(next_sample, SAMPLE_RATE) <= seconds:
                values = []
                segment, offset = schedule[index]
                start = segment['start'] * SAMPLE_RATE // 25
                for sample in range(next_sample, next_sample + 160):
                    local = sample - start
                    if segment['kind'] == 'trailer':
                        tones = trailer_tone(local, segment['key'])
                        if tones is None:
                            left = right = 0
                        else:
                            left, right = tones
                    elif segment['kind'] == 'ident':
                        left = right = 1600 if (sample // 1600) % 2 else 700
                    else:
                        value = content_tone(segment['key'], sample)
                        left, right = value, -value
                    values.extend((left, right))
                if not (segment['kind'] == 'trailer' and segment['key'] == 'missing'):
                    container.mux(audio.encode(_pcm_frame(values, next_sample)))
                next_sample += 160
        container.mux(video.encode())
        container.mux(audio.encode())
    return Path(path)


def _pcm_frame(values, first):
    frame = av.AudioFrame(format='s16', layout='stereo', samples=len(values) // 2)
    frame.planes[0].update(array('h', values).tobytes())
    frame.sample_rate = SAMPLE_RATE
    frame.time_base = Fraction(1, SAMPLE_RATE)
    frame.pts = first
    return frame


def make(path, name):
    """Render one named reel, choosing the repeat generator for repeat reels."""
    if name in ('repeat-trailer', 'dubbed-repeat'):
        return make_repeat(path, name)
    return _make_signal(path, name)


def main():
    parser = argparse.ArgumentParser(description='Generate sample source media for Slatewatch')
    parser.add_argument('name', choices=REELS)
    parser.add_argument('path', type=Path)
    options = parser.parse_args()
    print(make(options.path, options.name))


if __name__ == '__main__':
    main()
