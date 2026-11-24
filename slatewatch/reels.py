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
         'rework-before', 'rework-after')
REWORK_SECONDS = 4
REWORK_WINDOW = 160  # 20 ms of native PCM per generated audio packet

# Clipping layout of the rework pair, in programme microseconds.
REWORK_BEFORE_CLIPS = {
    2: ((500_000, 700_000), (1_200_000, 1_400_000)),
    1: ((1_600_000, 1_800_000),),
}
REWORK_AFTER_RATE = 16_000
REWORK_AFTER_GAP = (10_384, 10_400)  # 1 ms inside the third old window


def rework_after_clips(channel, sample):
    """Full-scale samples of the later measured track at its own sample rate."""
    if channel == 2:
        if 8_000 <= sample < 8_800:
            return True
        if 10_400 <= sample < 11_190 and (sample - 10_400) % 10 == 0:
            return True
        if sample == 11_200:  # adjacent to, not inside, the old candidate
            return True
    if channel == 1 and 25_600 <= sample < 26_400:
        return True
    return False


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


def rework_raster(index):
    return raster(index, False, False)


def rework_audio(first, count, rate, channel_clip):
    values = array('h')
    for sample in range(first, first + count):
        tone = round(8000 * sin(2 * pi * 500 * sample / rate))
        values.extend(32767 if channel_clip(channel, sample) else tone for channel in (1, 2))
    frame = av.AudioFrame(format='s16', layout='stereo', samples=count)
    if sys.byteorder != 'little':
        values.byteswap()
    frame.planes[0].update(values.tobytes())
    frame.sample_rate = rate
    frame.time_base = Fraction(1, rate)
    frame.pts = first
    return frame


def emit_audio(container, stream, first, stop, rate, channel_clip, holes=()):
    """Emit native PCM while leaving real uncovered sample ranges."""
    while first < stop:
        boundary = min(first + REWORK_WINDOW, stop)
        pieces = [(first, boundary)]
        for hole_start, hole_end in holes:
            cut = []
            for begin, end in pieces:
                if end <= hole_start or begin >= hole_end:
                    cut.append((begin, end))
                else:
                    if begin < hole_start:
                        cut.append((begin, hole_start))
                    if end > hole_end:
                        cut.append((hole_end, end))
            pieces = cut
        for begin, end in pieces:
            if begin < end:
                container.mux(stream.encode(rework_audio(begin, end - begin, rate, channel_clip)))
        first = boundary


def write_rework_before(path):
    fps = Fraction(25)
    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=fps)
        video.width, video.height = SIZE
        video.pix_fmt = 'gray'
        sound = container.add_stream('pcm_s16le', rate=SAMPLE_RATE)
        sound.layout = 'stereo'
        for index in range(REWORK_SECONDS * 25):
            frame = rework_raster(index)
            frame.pts = index
            frame.time_base = 1 / fps
            container.mux(video.encode(frame))

        def clipped(channel, sample):
            return any(start <= sample * 1_000_000 // SAMPLE_RATE < end
                       for start, end in REWORK_BEFORE_CLIPS[channel])

        emit_audio(container, sound, 0, SAMPLE_RATE * REWORK_SECONDS, SAMPLE_RATE, clipped)
        container.mux(video.encode())
        container.mux(sound.encode())
    return Path(path)


def write_rework_after(path):
    """Equal-length rework with a decoy first sound stream and 16 kHz measurement."""
    fps = Fraction(25)
    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=fps)
        video.width, video.height = SIZE
        video.pix_fmt = 'gray'
        decoy = container.add_stream('pcm_s16le', rate=SAMPLE_RATE)
        decoy.layout = 'stereo'
        sound = container.add_stream('pcm_s16le', rate=REWORK_AFTER_RATE)
        sound.layout = 'stereo'
        for index in range(REWORK_SECONDS * 25):
            frame = rework_raster(index)
            frame.pts = index
            frame.time_base = 1 / fps
            container.mux(video.encode(frame))
        emit_audio(container, decoy, 0, SAMPLE_RATE * REWORK_SECONDS, SAMPLE_RATE,
                   lambda channel, sample: False)
        emit_audio(container, sound, 0, REWORK_AFTER_RATE * REWORK_SECONDS, REWORK_AFTER_RATE,
                   rework_after_clips, holes=(REWORK_AFTER_GAP,))
        container.mux(video.encode())
        container.mux(decoy.encode())
        container.mux(sound.encode())
    return Path(path)


def make(path, name):
    if name == 'rework-before':
        return write_rework_before(path)
    if name == 'rework-after':
        return write_rework_after(path)
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
