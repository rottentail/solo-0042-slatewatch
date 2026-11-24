"""Cut short same-span audition segments and convert them onto a shared audition grid.

试听只读源母带：按节目区间切取原速率 PCM，未覆盖采样保持为零并单列；
为顺畅切听允许把试听信号线性插值到统一采样率，映射始终保留原轨与原采样区间。
"""
from array import array
from dataclasses import dataclass

from .clock import Span, sample_boundary, uncovered
from .media import Picture, decode


@dataclass(frozen=True)
class Segment:
    pcm: array
    rate: int
    channels: int
    first: int
    end: int
    filled: list


def cut(path, media, span):
    """Collect native-rate PCM covering the programme span; uncovered samples stay zero."""
    track = media['sound']
    rate, channels = track['rate'], track['channels']
    first = sample_boundary(span.start, rate)
    end = sample_boundary(span.end, rate)
    pcm = array('h', [0]) * ((end - first) * channels)
    coverage = []
    for block in decode(path, media):
        if isinstance(block, Picture):
            continue
        if block.first_sample >= end:
            break
        begin, stop = max(block.first_sample, first), min(block.end_sample, end)
        if begin >= stop:
            continue
        offset = (begin - first) * channels
        pos = (begin - block.first_sample) * channels
        count = (stop - begin) * channels
        pcm[offset:offset + count] = block.samples[pos:pos + count]
        coverage.append(Span(begin, stop))
    return Segment(pcm, rate, channels, first, end, list(uncovered(coverage, Span(first, end))))


def convert(pcm, channels, first_source, source_rate, first_out, out_count, out_rate):
    """Resample native PCM onto the audition grid by deterministic linear interpolation.

    Output sample j sits at programme time (first_out + j) / out_rate; the matching
    source position is (first_out + j) * source_rate / out_rate - first_source.
    Positions before the first source sample clamp to it; interpolation rounds half to even.
    """
    out = array('h', [0]) * (out_count * channels)
    for channel in range(channels):
        source = pcm[channel::channels]
        last = len(source) - 1
        values = array('h')
        for j in range(out_count):
            base, rem = divmod((first_out + j) * source_rate - first_source * out_rate, out_rate)
            if base < 0:
                values.append(source[0])
                continue
            low = source[base]
            high = source[base + 1] if base < last else low
            values.append(round(low + (high - low) * rem / out_rate))
        out[channel::channels] = values
    return out
