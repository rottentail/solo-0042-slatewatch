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
REELS = ('basalt', 'revision', 'picture-hole', 'no-sound', 'fractional', 'montage')


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


def montage_raster(index):
    """Mixed-format programme: full frame, letterbox, shifted composition,
    asymmetric pillarbox, fade-in over new bars and an inherently dark side."""
    width, height = SIZE
    frame = av.VideoFrame(width, height, 'gray')
    plane = frame.planes[0]
    phase = index * 5
    seconds = Fraction(index, 25)
    top = bottom = left = right = 0
    shift = 0
    gain = 1.0
    dark_side = False
    if seconds < Fraction(6, 5):
        pass
    elif seconds < Fraction(12, 5):
        top = bottom = 12
    elif seconds < Fraction(18, 5):
        top = bottom = 12
        shift = 30
    elif seconds < Fraction(24, 5):
        left, right = 6, 18
    elif seconds < 6:
        top, bottom = 8, 20
        gain = min(1.0, float((seconds - Fraction(24, 5)) / Fraction(3, 5)))
    else:
        dark_side = True
    mover_x = (index * 5) % 32
    mover_y = 20 + (index * 3) % 50
    raw = bytearray(plane.buffer_size)
    for y in range(height):
        start = y * plane.line_size
        row = bytearray(width)
        if top <= y < height - bottom:
            for x in range(left, width - right):
                value = 40 + (x - shift + y * 2 + phase) % 181
                if dark_side and x < 40:
                    value = 3 + (x + y + phase) % 6
                    if mover_x <= x < mover_x + 8 and mover_y <= y < mover_y + 8:
                        value = 40
                row[x] = round(value * gain) if gain < 1.0 else value
        raw[start:start + width] = row
    plane.update(raw)
    return frame


def audio_frame(first, count, revision, plain=False):
    values = array('h')
    for sample in range(first, first + count):
        tone = round(8000 * sin(2 * pi * 500 * sample / SAMPLE_RATE))
        left = tone if plain or not 9600 <= sample < 14400 else 0
        right = -tone if plain or revision or not 20000 <= sample < 22000 else 32767
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
    audio_end = 28000 if name == 'revision' else 57600 if name == 'montage' else 25600
    total = 180 if name == 'montage' else round(4 * fps)
    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=fps)
        video.width, video.height = SIZE
        video.pix_fmt = 'gray'
        audio = None if name == 'no-sound' else container.add_stream('pcm_s16le', rate=SAMPLE_RATE)
        if audio:
            audio.layout = 'stereo'
        next_sample = 0
        for i in range(total):
            seconds = Fraction(i, fps)
            if name == 'montage':
                frame = montage_raster(i)
                frame.pts = i
                frame.time_base = 1 / fps
                container.mux(video.encode(frame))
            elif name != 'picture-hole' or not Fraction(2) <= seconds < Fraction(5, 2):
                dark = Fraction(0) <= seconds < (Fraction(8, 25) if name == 'revision' else Fraction(12, 25))
                still = Fraction(3, 5) <= seconds < Fraction(7, 5)
                frame = raster(i, dark, still)
                frame.pts = i
                frame.time_base = 1 / fps
                container.mux(video.encode(frame))
            while audio and next_sample < audio_end and Fraction(next_sample, SAMPLE_RATE) <= seconds:
                if name == 'montage' or not 16000 <= next_sample < 17600:
                    container.mux(audio.encode(audio_frame(next_sample, 160, name == 'revision', name == 'montage')))
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
