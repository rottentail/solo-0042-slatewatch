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
REELS = ('basalt', 'revision', 'picture-hole', 'no-sound', 'fractional', 'mix-main', 'mix-ref', 'mix-long')

# 对听素材：主混 44.1 kHz，参考轨 48 kHz 且容器起点靠后 0.8 秒。
MIX_RATES = {'mix-main': 44100, 'mix-ref': 48000, 'mix-long': 44100}
MIX_ORIGIN_US = {'mix-main': 0, 'mix-ref': 800000, 'mix-long': 0}
MIX_SECONDS = {'mix-main': 4, 'mix-ref': 4, 'mix-long': 70}
MIX_CLIP = (Fraction(5, 2), Fraction(7, 2))       # mix-main 右声道满幅
MIX_HOLE = (Fraction(13, 5), Fraction(14, 5))     # mix-ref 中间缺几包
MIX_ZERO = (Fraction(3), Fraction(33, 10))        # mix-ref 实录为零


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


def mix_frame(name, first, count, rate, origin_samples):
    values = array('h')
    for sample in range(first, first + count):
        tone = round(8000 * sin(2 * pi * 500 * sample / rate))
        left, right = tone, -tone
        instant = Fraction(sample, rate)
        if name == 'mix-long' or (name == 'mix-main' and MIX_CLIP[0] <= instant < MIX_CLIP[1]):
            right = 32767
        if name == 'mix-ref' and MIX_ZERO[0] <= instant < MIX_ZERO[1]:
            left = right = 0
        values.extend((left, right))
    frame = av.AudioFrame(format='s16', layout='stereo', samples=count)
    if sys.byteorder != 'little':
        values.byteswap()
    frame.planes[0].update(values.tobytes())
    frame.sample_rate = rate
    frame.time_base = Fraction(1, rate)
    frame.pts = origin_samples + first
    return frame


def make_mix(path, name):
    rate = MIX_RATES[name]
    origin_us = MIX_ORIGIN_US[name]
    total = MIX_SECONDS[name] * rate
    packet = rate // 25
    fps = 1 if name == 'mix-long' else 25
    origin_frames = origin_us * fps // 1_000_000
    origin_samples = origin_us * rate // 1_000_000
    hole = tuple(int(point * rate) for point in MIX_HOLE)

    def keep(first):
        return name != 'mix-ref' or not hole[0] <= first < hole[1]

    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=fps)
        video.width, video.height = SIZE
        video.pix_fmt = 'gray'
        audio = container.add_stream('pcm_s16le', rate=rate)
        audio.layout = 'stereo'
        next_sample = 0
        for i in range(round(MIX_SECONDS[name] * fps)):
            frame = raster(i, False, False)
            frame.pts = i + origin_frames
            frame.time_base = Fraction(1, fps)
            container.mux(video.encode(frame))
            while next_sample < total and Fraction(next_sample, rate) <= Fraction(i, fps):
                if keep(next_sample):
                    container.mux(audio.encode(mix_frame(name, next_sample, packet, rate, origin_samples)))
                next_sample += packet
        while next_sample < total:
            count = min(packet, total - next_sample)
            if keep(next_sample):
                container.mux(audio.encode(mix_frame(name, next_sample, count, rate, origin_samples)))
            next_sample += packet
        container.mux(video.encode())
        container.mux(audio.encode())
    return Path(path)


def make(path, name):
    if name in MIX_RATES:
        return make_mix(path, name)
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


def main():
    parser = argparse.ArgumentParser(description='Generate sample source media for Slatewatch')
    parser.add_argument('name', choices=REELS)
    parser.add_argument('path', type=Path)
    options = parser.parse_args()
    print(make(options.path, options.name))


if __name__ == '__main__':
    main()
