"""Keep source-clock attribution for sampled frames and native-rate sound excerpts."""
from array import array
from bisect import bisect_right
from io import BytesIO
import sys
import wave

from .clock import SECOND, Span, coalesce, sample_boundary, uncovered
from .media import HEIGHT, WIDTH, Picture, decode


def write_wav(values, rate, channels):
    if sys.byteorder != 'little':
        values.byteswap()
    buf = BytesIO()
    with wave.open(buf, 'wb') as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(values.tobytes())
    return buf.getvalue()


def excerpt_span(finding, duration, padding_ms):
    """Programme span covered by an excerpt; recovery checks rely on the same formula."""
    hit_span = Span.parse(finding)
    padding = padding_ms * 1000
    return Span(max(0, hit_span.start - padding), min(duration, hit_span.end + padding))


class Excerpt:
    def __init__(self, finding, duration, sound, padding_ms, samples):
        self.hit = finding
        self.span = excerpt_span(finding, duration, padding_ms)
        self.targets = [self.span.start + self.span.duration * (i * 2 + 1) // (2 * samples)
                        for i in range(samples)]
        self.pictures = [None] * samples
        self.track = sound
        self.pcm = None
        self.coverage = []
        if sound:
            self.rate = sound['rate']
            self.channels = sound['channels']
            self.first = sample_boundary(self.span.start, self.rate)
            self.end = sample_boundary(self.span.end, self.rate)
            self.pcm = array('h', [0]) * ((self.end - self.first) * self.channels)

    def frame(self, frame, coverage, next_time):
        for index, point in enumerate(self.targets):
            if frame.timestamp_us <= point < next_time and any(s.start <= point < s.end for s in coverage):
                self.pictures[index] = frame

    def sound(self, block):
        if self.pcm is None:
            return
        begin, end = max(block.first_sample, self.first), min(block.end_sample, self.end)
        if begin >= end:
            return
        offset = (begin - self.first) * self.channels
        pos = (begin - block.first_sample) * self.channels
        count = (end - begin) * self.channels
        self.pcm[offset:offset + count] = block.samples[pos:pos + count]
        self.coverage.append(Span(begin, end))

    def finish(self):
        files = {}
        picture_map = []
        for index, (point, frame) in enumerate(zip(self.targets, self.pictures)):
            name = None
            if frame is not None:
                name = f'frame-{index + 1:02d}.pgm'
                files[name] = f'P5\n{WIDTH} {HEIGHT}\n255\n'.encode() + frame.pixels
            picture_map.append({'target_us': point, 'source_us': frame.timestamp_us if frame else None,
                                'file': name})
        sound_map = None
        if self.pcm is not None:
            files['sound.wav'] = write_wav(self.pcm, self.rate, self.channels)
            sound_map = {'file': 'sound.wav', 'sample_rate': self.rate, 'channels': self.channels,
                         'first_source_sample': self.first, 'end_source_sample': self.end,
                         'clock_offset_us': round(self.first * SECOND / self.rate),
                         'filled_sample_ranges': [{'first': s.start, 'end': s.end}
                                                  for s in uncovered(self.coverage, Span(self.first, self.end))]}
        return files, {'hit_id': self.hit['id'], **self.span.as_dict(),
                       'picture': picture_map, 'sound': sound_map}


def extract(path, scan, hits, padding_ms, samples):
    """Decode once for all excerpts requested in a package."""
    picture_coverage = [Span.parse(value) for value in scan['coverage']['picture']]
    stops = [span.end for span in picture_coverage]
    duration = scan['extent']['end_us']
    excerpts = [Excerpt(h, duration, scan['tracks']['sound'], padding_ms, samples) for h in hits]
    prior = None
    for block in decode(path, scan['tracks']):
        if isinstance(block, Picture):
            if prior is not None:
                _capture(excerpts, prior, block.timestamp_us, picture_coverage, stops)
            prior = block
        else:
            for excerpt in excerpts:
                excerpt.sound(block)
    if prior is not None:
        _capture(excerpts, prior, duration, picture_coverage, stops)
    return [excerpt.finish() for excerpt in excerpts]


def _capture(excerpts, frame, next_time, coverage, stops):
    index = bisect_right(stops, frame.timestamp_us)
    if index < len(coverage):
        for excerpt in excerpts:
            excerpt.frame(frame, coverage[index:index + 1], next_time)
