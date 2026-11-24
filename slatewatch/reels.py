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
REELS = ('basalt', 'revision', 'picture-hole', 'no-sound', 'fractional', 'grid-cells')

# Geometry of the grid-cells verification reel: 10x6 cells of 8x8 pixels on an
# 80x48 gray raster, matching the evidence thumbnail raster exactly.
GRID_WIDTH, GRID_HEIGHT = 80, 48
GRID_COLS, GRID_ROWS = 10, 6
CELL_W, CELL_H = 8, 8
GRID_FPS = Fraction(25)
# The duplicated gray cell pair and the one-pixel-different twin pair.
DUP_CELLS = (5, 42)
DUP_LEVEL = 110
TWIN_CELLS = (12, 53)
TWIN_LEVEL = 173
GRID_HOLE = (Fraction(2), Fraction(5, 2))
GRID_FREEZE = (Fraction(5, 2), Fraction(7, 2))
GRID_FREEZE_SEED = 40
# 100 presentation slots at 25 fps over 4 seconds; the hole muxes no frames.
GRID_TOTAL_SLOTS = round(4 * GRID_FPS)
# Slots i satisfying hole_start <= i/fps < hole_end (half-open, like make_grid).
GRID_HOLE_FRAMES = sum(
    1 for i in range(GRID_TOTAL_SLOTS)
    if GRID_HOLE[0] <= Fraction(i, GRID_FPS) < GRID_HOLE[1])
EXPECTED_GRID_FRAMES = GRID_TOTAL_SLOTS - GRID_HOLE_FRAMES


def raster(index, dark, still):
    width, height = SIZE
    frame = av.VideoFrame(width, height, 'gray')
    plane = frame.planes[0]
    phase = 3 if still else index * 5
    raw = bytearray(plane.buffer_size)
    stride = getattr(plane, 'line_size', width)
    for y in range(height):
        start = y * stride
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


def grid_raster(index, seconds):
    """Deterministic 10x6 gray-cell raster with a duplicate cell and twin cells.

    Cells carry independent gray levels so cell identity survives pixel-level
    comparison.  Two cell indices share one level; two more share a level but
    the second twin differs by exactly one pixel outside the freeze interval.
    """
    frozen = GRID_FREEZE[0] <= seconds < GRID_FREEZE[1]
    seed = GRID_FREEZE_SEED if frozen else index
    levels = bytearray(GRID_COLS * GRID_ROWS)
    for cell in range(len(levels)):
        levels[cell] = 30 + ((seed * 7 + cell * 13) % 200)
    levels[DUP_CELLS[0]] = DUP_LEVEL
    levels[DUP_CELLS[1]] = DUP_LEVEL
    levels[TWIN_CELLS[0]] = TWIN_LEVEL
    levels[TWIN_CELLS[1]] = TWIN_LEVEL
    pixels = bytearray(GRID_WIDTH * GRID_HEIGHT)
    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            level = levels[row * GRID_COLS + col]
            for y in range(CELL_H):
                start = (row * CELL_H + y) * GRID_WIDTH + col * CELL_W
                pixels[start:start + CELL_W] = bytes([level]) * CELL_W
    if not frozen:
        twin_row, twin_col = divmod(TWIN_CELLS[1], GRID_COLS)
        mark = (twin_row * CELL_H + 4) * GRID_WIDTH + twin_col * CELL_W + 3
        pixels[mark] = (pixels[mark] + 1) % 256
    frame = av.VideoFrame(GRID_WIDTH, GRID_HEIGHT, 'gray')
    frame.planes[0].update(bytes(pixels))
    return frame


def make(path, name):
    if name == 'grid-cells':
        return make_grid(path)
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


def make_grid(path):
    """Picture-only master: head, a half-second hole, a freeze episode, a tail."""
    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=GRID_FPS)
        video.width, video.height = GRID_WIDTH, GRID_HEIGHT
        video.pix_fmt = 'gray'
        for i in range(GRID_TOTAL_SLOTS):
            seconds = Fraction(i, GRID_FPS)
            if GRID_HOLE[0] <= seconds < GRID_HOLE[1]:
                continue
            frame = grid_raster(i, seconds)
            frame.pts = i
            frame.time_base = 1 / GRID_FPS
            container.mux(video.encode(frame))
        container.mux(video.encode())
    return Path(path)


def main():
    parser = argparse.ArgumentParser(description='Generate sample source media for Slatewatch')
    parser.add_argument('name', choices=REELS)
    parser.add_argument('path', type=Path)
    options = parser.parse_args()
    print(make(options.path, options.name))


if __name__ == '__main__':
    main()
