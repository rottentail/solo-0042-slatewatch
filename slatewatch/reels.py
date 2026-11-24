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
REELS = ('basalt', 'revision', 'picture-hole', 'no-sound', 'fractional', 'slate-a', 'slate-b')
FPS = Fraction(25)
# Shared splice-reel script: every frame pattern is deterministic so the two
# cuts stay reproducible and the two title cards are pixel-identical while the
# neighbouring programme content differs.
SPLICE_FPS = FPS
# Shared splice-reel script: every frame pattern is deterministic so the two
# cuts stay reproducible.  Two pixel-identical title cards (TITLE_A and
# TITLE_D) sit in different neighbourhoods, so matching frame content or
# candidate names can never tell which card pairs with which.
TITLE_FRAMES, PROGRAM_FRAMES, FREEZE_FRAMES, LINE_FRAMES, TAIL_FRAMES = 8, 8, 16, 29, 31
TITLE_A_END = TITLE_FRAMES
PROGRAM_END = TITLE_A_END + PROGRAM_FRAMES
FREEZE_END = PROGRAM_END + FREEZE_FRAMES
TITLE_D_END = FREEZE_END + TITLE_FRAMES
TOTAL_FRAMES = TITLE_D_END + LINE_FRAMES + TAIL_FRAMES
SPLICE_DROP = 16  # whole "sentence" removed from the middle of slate-b
CUT_INDEX = TITLE_D_END  # the sentence starts right after the second title card


def pattern(index):
    """Distinct, deterministic gray pattern; both title cards are solid dark."""
    width, height = SIZE
    if index < TITLE_A_END or FREEZE_END <= index < TITLE_D_END:
        return bytes([18]) * (width * height)
    if PROGRAM_END <= index < FREEZE_END:
        phase = PROGRAM_END
        return bytes(40 + (x + y * 2 + phase * 5) % 181 for y in range(height) for x in range(width))
    return bytes(40 + (x + y * 2 + index * 5) % 181 for y in range(height) for x in range(width))


def splice_frame(index):
    frame = av.VideoFrame(SIZE[0], SIZE[1], 'gray')
    plane = frame.planes[0]
    pixels = pattern(index)
    raw = bytearray(plane.buffer_size)
    for y in range(SIZE[1]):
        raw[y * plane.line_size:y * plane.line_size + SIZE[0]] = pixels[y * SIZE[0]:(y + 1) * SIZE[0]]
    plane.update(raw)
    return frame


def splice_audio(first, count, drop_sample_range, clip_sample_range):
    values = array('h')
    for sample in range(first, first + count):
        tone = round(8000 * sin(2 * pi * 500 * sample / SAMPLE_RATE))
        left = 0 if drop_sample_range[0] <= sample < drop_sample_range[1] else tone
        right = 32767 if clip_sample_range[0] <= sample < clip_sample_range[1] else -tone
        values.extend((left, right))
    frame = av.AudioFrame(format='s16', layout='stereo', samples=count)
    if sys.byteorder != 'little':
        values.byteswap()
    frame.planes[0].update(values.tobytes())
    frame.sample_rate = SAMPLE_RATE
    frame.time_base = Fraction(1, SAMPLE_RATE)
    frame.pts = first
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


def make_splice(path, name):
    """Two cuts of one programme with a removed sentence and a fresh later fault.

    Audio windows are declared on the shared source timeline.  slate-b removes
    ``SPLICE_DROP`` frames at the cut, so every retained source sample after the
    cut is written at its cut-shifted output time.
    """
    drop = name == 'slate-b'
    total = TOTAL_FRAMES
    cut_index = CUT_INDEX
    cut_sample = cut_index * SAMPLE_RATE // SPLICE_FPS
    shift_samples = SPLICE_DROP * SAMPLE_RATE // SPLICE_FPS
    total_samples = total * SAMPLE_RATE // SPLICE_FPS
    # Old-only clip straddles the cut: 160 ms removed, only 40 ms survives,
    # below the 100 ms clip floor of the later scan.
    old_clip = (cut_sample - 1280, cut_sample + 320)
    # A real new problem lives in the retained tail.  Its source window is
    # 2.40-2.56 s; in slate-b the retained tail is written shift_samples earlier.
    new_clip = (round(2.40 * SAMPLE_RATE), round(2.56 * SAMPLE_RATE))
    if drop:
        clip_range = (new_clip[0] - shift_samples, new_clip[1] - shift_samples)
    else:
        clip_range = old_clip
    # 280 ms silence gap ending at the cut in slate-a.  slate-b removes the
    # sentence (and the gap's first 160 ms), leaving a 120 ms gap that still
    # clears the 100 ms sound-gap floor at the shifted cut.
    if drop:
        cut_output = cut_sample - shift_samples
        silence_range = (cut_output - 960, cut_output)
    else:
        silence_range = (cut_sample - 2240, cut_sample)
    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=SPLICE_FPS)
        video.width, video.height = SIZE
        video.pix_fmt = 'gray'
        audio = container.add_stream('pcm_s16le', rate=SAMPLE_RATE)
        audio.layout = 'stereo'
        output_index = 0
        for source_index in range(total):
            if not (drop and cut_index <= source_index < cut_index + SPLICE_DROP):
                frame = splice_frame(source_index)
                frame.pts = output_index
                frame.time_base = 1 / SPLICE_FPS
                container.mux(video.encode(frame))
                output_index += 1
        output_total = (total - SPLICE_DROP) * SAMPLE_RATE // SPLICE_FPS if drop else total_samples
        for first in range(0, output_total, 160):
            if silence_range[0] <= first < silence_range[1]:
                continue
            container.mux(audio.encode(splice_audio(first, 160, silence_range, clip_range)))
        container.mux(video.encode())
        container.mux(audio.encode())
    return Path(path)


def make(path, name):
    if name in ('slate-a', 'slate-b'):
        return make_splice(path, name)
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
