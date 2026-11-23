"""Generate small repeatable synthetic masters with known signal episodes."""
from array import array
from fractions import Fraction
from math import cos, pi, sin
from pathlib import Path
import argparse
import sys

import av

SAMPLE_RATE = 8000
SIZE = (160, 96)
REELS = ('basalt', 'revision', 'picture-hole', 'no-sound', 'fractional', 'stripes')

# Faint banding at 28 +/- 6 luma: visible only against the dark cloth.  Periods
# are given in source pixels and halve onto the 80x48 measurement raster.  A
# zero rate is real repeating texture; a steady rate is a scrolling band.  The
# 1.2 s hard cut restarts an identical horizontal scroll half a cycle out of
# phase, so a naive stitcher would join two shots into one technical finding.
STRIPES = (
    {'start': Fraction(0), 'end': Fraction(6, 5), 'direction': 'horizontal',
     'period': 16, 'rate': 1.0 / 8, 'origin': 0.0},
    {'start': Fraction(6, 5), 'end': Fraction(12, 5), 'direction': 'horizontal',
     'period': 16, 'rate': 1.0 / 8, 'origin': 0.5},
    {'start': Fraction(12, 5), 'end': Fraction(18, 5), 'direction': 'horizontal',
     'period': 24, 'rate': 0.0, 'origin': 0.0},
    {'start': Fraction(18, 5), 'end': Fraction(24, 5), 'direction': 'vertical',
     'period': 20, 'rate': 0.0, 'origin': 0.0},
    {'start': Fraction(24, 5), 'end': Fraction(6), 'direction': 'vertical',
     'period': 12, 'rate': 1.0 / 5, 'origin': 0.25},
)
STRIPE_LENGTH = Fraction(37, 5)


def stripe_frame(index, seconds):
    width, height = SIZE
    frame = av.VideoFrame(width, height, 'gray')
    plane = frame.planes[0]
    episode = next((item for item in STRIPES if item['start'] <= seconds < item['end']), None)
    if episode is None:
        return raster(index, False, False)
    phase = episode['origin'] + episode['rate'] * index
    raw = bytearray(plane.buffer_size)
    if episode['direction'] == 'horizontal':
        levels = [min(255, max(0, 28 + round(6 * cos(2 * pi * (y / episode['period'] + phase)))))
                  for y in range(height)]
        for y in range(height):
            start = y * plane.line_size
            raw[start:start + width] = bytes([levels[y]]) * width
        band = (index * 6) % (width - 10)
        for y in range(height):
            start = y * plane.line_size + band
            raw[start:start + 10] = bytes([min(255, levels[y] + 40)]) * 10
    else:
        row = bytes(min(255, max(0, 28 + round(6 * cos(2 * pi * (x / episode['period'] + phase)))))
                    for x in range(width))
        for y in range(height):
            start = y * plane.line_size
            raw[start:start + width] = row
        band = (index * 4) % (height - 4)
        for y in range(band, band + 4):
            start = y * plane.line_size
            raw[start:start + width] = bytes(min(255, value + 40) for value in row)
    plane.update(raw)
    return frame


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


def audio_frame(first, count, revision, clean=False):
    values = array('h')
    for sample in range(first, first + count):
        tone = round(8000 * sin(2 * pi * 500 * sample / SAMPLE_RATE))
        left = tone if clean else 0 if 9600 <= sample < 14400 else tone
        right = -tone if clean or revision else 32767 if 20000 <= sample < 22000 else -tone
        values.extend((left, right))
    frame = av.AudioFrame(format='s16', layout='stereo', samples=count)
    if sys.byteorder != 'little':
        values.byteswap()
    frame.planes[0].update(values.tobytes())
    frame.sample_rate = SAMPLE_RATE
    frame.time_base = Fraction(1, SAMPLE_RATE)
    frame.pts = first
    return frame


def make(path, name):
    fps = Fraction(30000, 1001) if name == 'fractional' else Fraction(25)
    audio_end = 28000 if name == 'revision' else 25600
    if name == 'stripes':
        audio_end = round(STRIPE_LENGTH * SAMPLE_RATE)
    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=fps)
        video.width, video.height = SIZE
        video.pix_fmt = 'gray'
        audio = None if name == 'no-sound' else container.add_stream('pcm_s16le', rate=SAMPLE_RATE)
        if audio:
            audio.layout = 'stereo'
        next_sample = 0
        length = STRIPE_LENGTH if name == 'stripes' else Fraction(4)
        for i in range(round(length * fps)):
            seconds = Fraction(i, fps)
            if name != 'picture-hole' or not Fraction(2) <= seconds < Fraction(5, 2):
                dark = Fraction(0) <= seconds < (Fraction(8, 25) if name == 'revision' else Fraction(12, 25))
                still = Fraction(3, 5) <= seconds < Fraction(7, 5)
                frame = stripe_frame(i, seconds) if name == 'stripes' else raster(i, dark, still)
                frame.pts = i
                frame.time_base = 1 / fps
                container.mux(video.encode(frame))
            while audio and next_sample < audio_end and Fraction(next_sample, SAMPLE_RATE) <= seconds:
                if name == 'stripes' or not 16000 <= next_sample < 17600:
                    container.mux(audio.encode(audio_frame(next_sample, 160, name == 'revision', name == 'stripes')))
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
