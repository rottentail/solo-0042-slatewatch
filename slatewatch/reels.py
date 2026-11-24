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
REELS = ('basalt', 'revision', 'picture-hole', 'no-sound', 'fractional', 'silence-lab')


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


def lab_left(sample):
    """Silence-lab left lane: silence beds, a short blip, low hum and one dropout."""
    if 8000 <= sample < 16000:       # silence A, 1.00–2.00 s
        return 0
    if 16000 <= sample < 16080:      # 10 ms half-level blip between A and B
        return round(4000 * sin(2 * pi * 500 * sample / SAMPLE_RATE))
    if 16080 <= sample < 24000:      # silence B, 2.01–3.00 s
        return 0
    if 25600 <= sample < 28000:      # silence C, 3.20–3.50 s after the dropout
        return 0
    if 36000 <= sample < 44000:      # silence D1, 4.50–5.50 s
        return 0
    if 44000 <= sample < 48000:      # low hum RMS ~55: passes 80, fails 40
        return round(78 * sin(2 * pi * 100 * sample / SAMPLE_RATE))
    if 48000 <= sample < 56000:      # silence D3, 6.00–7.00 s
        return 0
    if 60000 <= sample < 62240:      # short 280 ms bed E, under the 300 ms default
        return 0
    return round(8000 * sin(2 * pi * 500 * sample / SAMPLE_RATE))


def lab_frame(first, count):
    values = array('h')
    for sample in range(first, first + count):
        right = -round(8000 * sin(2 * pi * 500 * sample / SAMPLE_RATE))
        values.extend((lab_left(sample), right))
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
    lab = name == 'silence-lab'
    audio_end = 64000 if lab else (28000 if name == 'revision' else 25600)
    hole = (24000, 25600) if lab else (16000, 17600)
    length = 8 if lab else 4
    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=fps)
        video.width, video.height = SIZE
        video.pix_fmt = 'gray'
        audio = None if name == 'no-sound' else container.add_stream('pcm_s16le', rate=SAMPLE_RATE)
        if audio:
            audio.layout = 'stereo'
        next_sample = 0
        for i in range(round(length * fps)):
            seconds = Fraction(i, fps)
            if name != 'picture-hole' or not Fraction(2) <= seconds < Fraction(5, 2):
                dark = (not lab and Fraction(0) <= seconds
                        < (Fraction(8, 25) if name == 'revision' else Fraction(12, 25)))
                still = not lab and Fraction(3, 5) <= seconds < Fraction(7, 5)
                frame = raster(i, dark, still)
                frame.pts = i
                frame.time_base = 1 / fps
                container.mux(video.encode(frame))
            while audio and next_sample < audio_end and Fraction(next_sample, SAMPLE_RATE) <= seconds:
                if not hole[0] <= next_sample < hole[1]:
                    frame = (lab_frame(next_sample, 160) if lab
                             else audio_frame(next_sample, 160, name == 'revision'))
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
