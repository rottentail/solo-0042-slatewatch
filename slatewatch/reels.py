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
REELS = ('basalt', 'revision', 'picture-hole', 'no-sound', 'fractional',
         'field-interlaced', 'field-progressive', 'field-deinterlaced', 'field-mixed')
FIELD_REELS = frozenset(REELS[5:])
MIXED_BOUNDARY = (60, 105)


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


def _stationary_lines(width, height):
    """One-pixel vertical stripes: stationary fine detail that must not alarm."""
    stripe = bytes(30 + ((x // 2) % 3) * 70 for x in range(width))
    return [bytearray(stripe) for _ in range(height)]


def _paint(rows, x0, y0, y1, width, level=220, box=30):
    for y in range(y0, y1):
        for x in range(max(0, x0), min(width, x0 + box)):
            rows[y][x] = level


def _progressive_pixels(index, width, height):
    rows = _stationary_lines(width, height)
    _paint(rows, 8 + (index * 6) % (width - 46), 30, 66, width)
    return b''.join(bytes(row) for row in rows)


def _interlaced_pixels(index, width, height, top_first=True):
    """Weave two field instants; the moving box shifts 3 px between fields."""
    top, bottom = _stationary_lines(width, height), _stationary_lines(width, height)
    base = 8 + (index * 6) % (width - 49)
    early, late = base, base + 3
    if top_first:
        _paint(top, early, 30, 66, width)
        _paint(bottom, late, 30, 66, width)
    else:
        _paint(bottom, early, 30, 66, width)
        _paint(top, late, 30, 66, width)
    woven = bytearray(width * height)
    for y in range(height):
        woven[y * width:(y + 1) * width] = (top if y % 2 == 0 else bottom)[y]
    return bytes(woven)


def _bob_pixels(woven, width, height):
    """Reference line-doubling deinterlace that removes the weave."""
    output = bytearray(width * height)
    for y in range(height):
        source_y = y - 1 if y % 2 == 1 else y
        output[y * width:(y + 1) * width] = woven[source_y * width:source_y * width + width]
    return bytes(output)


def field_pixels(name, index, width, height):
    if name == 'field-progressive':
        return _progressive_pixels(index, width, height)
    woven = _interlaced_pixels(index, width, height)
    if name == 'field-interlaced':
        return woven
    if name == 'field-deinterlaced':
        return _bob_pixels(woven, width, height)
    # field-mixed: progressive head and tail with a woven TFF middle episode
    if MIXED_BOUNDARY[0] <= index < MIXED_BOUNDARY[1]:
        return woven
    return _progressive_pixels(index, width, height)


def make(path, name):
    fps = Fraction(30000, 1001) if name == 'fractional' else Fraction(25)
    audio_end = 28000 if name == 'revision' else 25600
    frame_count = 120 if name == 'field-mixed' else 100 if name in FIELD_REELS else round(4 * fps)
    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=fps)
        video.width, video.height = SIZE
        video.pix_fmt = 'gray'
        audio = None
        if name not in FIELD_REELS and name != 'no-sound':
            audio = container.add_stream('pcm_s16le', rate=SAMPLE_RATE)
            audio.layout = 'stereo'
        next_sample = 0
        for i in range(frame_count):
            seconds = Fraction(i, fps)
            if name in FIELD_REELS:
                frame = av.VideoFrame(SIZE[0], SIZE[1], 'gray')
                frame.planes[0].update(field_pixels(name, i, SIZE[0], SIZE[1]))
            else:
                if name == 'picture-hole' and Fraction(2) <= seconds < Fraction(5, 2):
                    frame = None
                else:
                    dark = Fraction(0) <= seconds < (Fraction(8, 25) if name == 'revision' else Fraction(12, 25))
                    still = Fraction(3, 5) <= seconds < Fraction(7, 5)
                    frame = raster(i, dark, still)
            if frame is not None:
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


def main():
    parser = argparse.ArgumentParser(description='Generate sample source media for Slatewatch')
    parser.add_argument('name', choices=REELS)
    parser.add_argument('path', type=Path)
    options = parser.parse_args()
    print(make(options.path, options.name))


if __name__ == '__main__':
    main()
