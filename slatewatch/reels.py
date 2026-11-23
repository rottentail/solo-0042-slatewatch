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
REELS = ('basalt', 'revision', 'picture-hole', 'no-sound', 'fractional', 'ad-switch')
PICTURE_HOLES = {'picture-hole': (Fraction(2), Fraction(5, 2)), 'ad-switch': (Fraction(11, 5), Fraction(13, 5))}
SOUND_HOLES = {'ad-switch': ((16000, 18400), (24000, 24480))}
DEFAULT_SOUND_HOLES = ((16000, 17600),)


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


def pcm_frame(first, count, values):
    frame = av.AudioFrame(format='s16', layout='stereo', samples=count)
    if sys.byteorder != 'little':
        values.byteswap()
    frame.planes[0].update(values.tobytes())
    frame.sample_rate = SAMPLE_RATE
    frame.time_base = Fraction(1, SAMPLE_RATE)
    frame.pts = first
    return frame


def audio_frame(first, count, revision):
    values = array('h')
    for sample in range(first, first + count):
        tone = round(8000 * sin(2 * pi * 500 * sample / SAMPLE_RATE))
        left = 0 if 9600 <= sample < 14400 else tone
        right = 32767 if 20000 <= sample < 22000 and not revision else -tone
        values.extend((left, right))
    return pcm_frame(first, count, values)


def switch_audio_frame(first, count):
    values = array('h')
    for sample in range(first, first + count):
        tone = round(8000 * sin(2 * pi * 500 * sample / SAMPLE_RATE))
        level = 0 if 8000 <= sample < 11200 else tone
        values.extend((level, -level))
    return pcm_frame(first, count, values)


def make(path, name):
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
        picture_hole = PICTURE_HOLES.get(name)
        sound_holes = SOUND_HOLES.get(name, DEFAULT_SOUND_HOLES)
        for i in range(round(4 * fps)):
            seconds = Fraction(i, fps)
            if picture_hole is None or not picture_hole[0] <= seconds < picture_hole[1]:
                dark = name != 'ad-switch' and Fraction(0) <= seconds < (
                    Fraction(8, 25) if name == 'revision' else Fraction(12, 25))
                still = name != 'ad-switch' and Fraction(3, 5) <= seconds < Fraction(7, 5)
                frame = raster(i, dark, still)
                frame.pts = i
                frame.time_base = 1 / fps
                container.mux(video.encode(frame))
            while audio and next_sample < audio_end and Fraction(next_sample, SAMPLE_RATE) <= seconds:
                if not any(start <= next_sample < end for start, end in sound_holes):
                    frame = switch_audio_frame(next_sample, 160) if name == 'ad-switch' \
                        else audio_frame(next_sample, 160, name == 'revision')
                    container.mux(audio.encode(frame))
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
