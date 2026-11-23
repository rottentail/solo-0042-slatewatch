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
         'clock-a', 'clock-b-offset', 'clock-b-rate', 'clock-b-cut')
CLOCK_FPS = Fraction(25)
CLOCK_FRAMES = 100
CLOCK_OFFSET_US = 40_000
# Shared scene anchors: frame indices on clock-a, spanning the whole programme.
# None falls inside the block removed by clock-b-cut, and none is the final frame.
CLOCK_ANCHOR_FRAMES = (0, 24, 48, 74, 90)


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


def clock_time_us(frame_index, rate):
    """Display timestamp in microseconds, matching media.timestamp's rounding."""
    return round(Fraction(frame_index, rate) * 1_000_000)


def clock_raster(index):
    """Distinct scene states so duty officers can locate the same scene twice."""
    frame = av.VideoFrame(SIZE[0], SIZE[1], 'gray')
    plane = frame.planes[0]
    level = 40 + (index * 37) % 181
    raw = bytearray(plane.buffer_size)
    for y in range(SIZE[1]):
        start = y * plane.line_size
        row = bytes((level + x // 8 + y // 8) % 256 for x in range(SIZE[0]))
        raw[start:start + SIZE[0]] = row
    plane.update(raw)
    return frame


def mux_clock(path, name):
    """Two-capture-machine family: same scenes, offset clocks, drift, or a cut.

    MP4 keeps a delayed first presentation timestamp on the programme clock,
    while the Matroska muxer folds it into the container origin.  Companions
    therefore start their clock at CLOCK_OFFSET_US so the offset stays visible
    as frame timestamps that a duty officer can anchor against clock-a.
    """
    rate = CLOCK_FPS
    with av.open(str(path), mode='w', format='mp4') as container:
        video = container.add_stream('h264', rate=rate)
        video.width, video.height = SIZE
        video.pix_fmt = 'yuv420p'
        video.time_base = Fraction(1, 1_000_000)
        if name == 'clock-a':
            for index in range(CLOCK_FRAMES):
                frame = clock_raster(index).reformat(format='yuv420p')
                frame.pts = clock_time_us(index, rate)
                frame.time_base = Fraction(1, 1_000_000)
                container.mux(video.encode(frame))
        elif name == 'clock-b-offset':
            for index in range(CLOCK_FRAMES):
                frame = clock_raster(index).reformat(format='yuv420p')
                frame.pts = clock_time_us(index, rate) + CLOCK_OFFSET_US
                frame.time_base = Fraction(1, 1_000_000)
                container.mux(video.encode(frame))
        elif name == 'clock-b-rate':
            drift_rate = CLOCK_FPS * 24 // 25
            for index in range(CLOCK_FRAMES):
                frame = clock_raster(index).reformat(format='yuv420p')
                frame.pts = clock_time_us(index, drift_rate) + CLOCK_OFFSET_US
                frame.time_base = Fraction(1, 1_000_000)
                container.mux(video.encode(frame))
        else:  # clock-b-cut: frames 40..59 of clock-a are removed, later clocks shift.
            kept = [index for index in range(CLOCK_FRAMES) if not 40 <= index < 60]
            cut_us = clock_time_us(20, rate)
            for index in kept:
                frame = clock_raster(index).reformat(format='yuv420p')
                source_us = clock_time_us(index, rate)
                shown_us = source_us + CLOCK_OFFSET_US if index < 40 else source_us - cut_us + CLOCK_OFFSET_US
                frame.pts, frame.time_base = shown_us, Fraction(1, 1_000_000)
                container.mux(video.encode(frame))
        container.mux(video.encode())
    return Path(path)


def clock_anchors(name):
    """Re-runnable scene anchor pairs between clock-a and one of its companions.

    Timestamps are programme-clock values after the shared container origin is
    removed, matching the scan's half-open ``[start_us, end_us)`` coordinates.
    """
    base = [clock_time_us(index, CLOCK_FPS) for index in CLOCK_ANCHOR_FRAMES]
    if name == 'clock-a':
        after = base
    elif name == 'clock-b-offset':
        # Identical frame clocks: only the container origin (the start offset)
        # separates the two capture machines.
        after = base
    elif name == 'clock-b-rate':
        drift_rate = CLOCK_FPS * 24 // 25
        after = [clock_time_us(index, drift_rate) for index in CLOCK_ANCHOR_FRAMES]
    else:
        cut_us = clock_time_us(20, CLOCK_FPS)
        after = [value if index < 40 else value - cut_us for index, value in
                 zip(CLOCK_ANCHOR_FRAMES, base)]
    return [{'label': f'anchor-{position}', 'before_us': before, 'after_us': later}
            for position, (before, later) in enumerate(zip(base, after))]


def make(path, name):
    if name.startswith('clock-'):
        return mux_clock(path.with_suffix('.mp4'), name)
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
