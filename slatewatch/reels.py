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
REELS = ('basalt', 'revision', 'picture-hole', 'no-sound', 'fractional', 'patched')


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


def frame_of(values, first, count):
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
        other = round(6500 * sin(2 * pi * 700 * sample / SAMPLE_RATE))
        left = 0 if 9600 <= sample < 14400 else tone
        right = 32767 if 20000 <= sample < 22000 and not revision else other
        values.extend((left, right))
    return frame_of(values, first, count)


def patched_voice(sample, seed):
    level = 10000 if sample % 2000 < 1000 else 3000
    mixed = (sample * 0x9E3779B1 + seed * 0x85EBCA77) & 0xFFFFFFFF
    mixed = (mixed ^ (mixed >> 16)) * 0x45D9F3B & 0xFFFFFFFF
    mixed = (mixed ^ (mixed >> 16)) * 0x45D9F3B & 0xFFFFFFFF
    mixed = mixed ^ (mixed >> 16)
    return level * ((mixed & 0x7FFF) - 16384) // 16384


def patched_left(sample):
    return 0 if 9600 <= sample < 12800 else patched_voice(sample, 12345)


def patched_frame(first, count):
    values = array('h')
    for sample in range(first, first + count):
        left = patched_left(sample)
        if 12800 <= sample < 25600:
            right = patched_left(sample - 40) // 2
        elif 9600 <= sample < 12800:
            right = 0
        else:
            right = patched_voice(sample, 54321)
        values.extend((left, right))
    return frame_of(values, first, count)


def make(path, name):
    fps = Fraction(30000, 1001) if name == 'fractional' else Fraction(25)
    audio_end = 28000 if name == 'revision' else 32000 if name == 'patched' else 25600
    hole = (19200, 20800) if name == 'patched' else (16000, 17600)
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
                dark = name != 'patched' and Fraction(0) <= seconds < (
                    Fraction(8, 25) if name == 'revision' else Fraction(12, 25))
                still = name != 'patched' and Fraction(3, 5) <= seconds < Fraction(7, 5)
                frame = raster(i, dark, still)
                frame.pts = i
                frame.time_base = 1 / fps
                container.mux(video.encode(frame))
            while audio and next_sample < audio_end and Fraction(next_sample, SAMPLE_RATE) <= seconds:
                if not hole[0] <= next_sample < hole[1]:
                    if name == 'patched':
                        container.mux(audio.encode(patched_frame(next_sample, 160)))
                    else:
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
