"""Decode selected streams while retaining source display and sample times."""
from array import array
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import av

from .clock import InputError, LIMIT, SECOND, Span, timestamp

MAX_ASSET = 48 * 1024 * 1024
MAX_PIXELS = 3840 * 2160
MAX_FRAMES = 12000
WIDTH, HEIGHT = 80, 48
FIELD_WIDTH = 160
FORMATS = {'matroska', 'mov'}
CODECS = {'h264', 'hevc', 'mpeg4', 'ffv1', 'aac', 'pcm_s16le', 'flac', 'opus', 'mp3'}


@dataclass(frozen=True)
class Picture:
    timestamp_us: int
    duration_us: int
    pixels: bytes


@dataclass(frozen=True)
class FieldFrame:
    """Full-height, width-capped grayscale raster used for field inspection."""
    timestamp_us: int
    duration_us: int
    width: int
    height: int
    line_size: int
    pixels: bytes


@dataclass(frozen=True)
class PCM:
    first_sample: int
    rate: int
    channels: int
    samples: array

    @property
    def count(self):
        return len(self.samples) // self.channels

    @property
    def end_sample(self):
        return self.first_sample + self.count

    @property
    def span(self):
        return Span(round(Fraction(self.first_sample * SECOND, self.rate)),
                    round(Fraction(self.end_sample * SECOND, self.rate)))


def open_media(path: Path):
    try:
        return av.open(str(path), options={'protocol_whitelist': 'file',
                                           'format_whitelist': 'matroska,mov',
                                           'enable_drefs': '0'})
    except av.error.FFmpegError as exc:
        raise InputError('could not open a supported media container') from exc


def track_info(stream):
    codec = stream.codec_context
    info = {'index': stream.index, 'type': stream.type, 'codec': codec.name,
            'time_base': str(stream.time_base), 'start_pts': stream.start_time}
    if stream.type == 'video':
        info.update(width=codec.width, height=codec.height,
                    average_rate=str(stream.average_rate) if stream.average_rate else None)
    else:
        info.update(rate=codec.sample_rate, channels=codec.channels)
    return info


def pick_track(tracks, wanted, kind, required=False):
    if wanted is None and not required:
        return None
    if wanted is None and tracks:
        return tracks[0]
    for stream in tracks:
        if stream.index == wanted:
            return stream
    raise InputError(f'selected {kind} stream does not exist')


def validate_track(stream):
    codec = stream.codec_context
    if codec.name not in CODECS:
        raise InputError(f'unsupported codec {codec.name!r}')
    if stream.type == 'video':
        if not 16 <= codec.width <= 3840 or not 16 <= codec.height <= 2160:
            raise InputError('video dimensions outside supported bounds')
    elif codec.channels not in (1, 2) or not 8000 <= codec.sample_rate <= 48000:
        raise InputError('audio requires one or two channels at 8–48 kHz')


def inspect(path, picture_index, sound_index):
    with open_media(path) as container:
        if not FORMATS.intersection(container.format.name.split(',')):
            raise InputError('only Matroska and MOV/MP4 containers are supported')
        if container.duration is not None and container.duration > LIMIT:
            raise InputError('source exceeds 120 seconds')
        videos = [stream for stream in container.streams.video if not stream.disposition.attached_pic & stream.disposition]
        picture = pick_track(videos, picture_index, 'picture', required=True)
        audio = list(container.streams.audio)
        if sound_index == 'first':
            sound = audio[0] if audio else None
        else:
            sound = pick_track(audio, sound_index, 'sound')
        validate_track(picture)
        if sound is not None:
            validate_track(sound)
        return {'format': container.format.name, 'origin_us': container.start_time or 0,
                'container_duration_us': container.duration,
                'tracks': [track_info(stream) for stream in [*videos, *audio]],
                'picture': track_info(picture), 'sound': track_info(sound) if sound is not None else None}


def thumbnail(frame):
    if frame.width * frame.height > MAX_PIXELS:
        raise InputError('decoded picture exceeds geometry limit')
    gray = frame.reformat(width=WIDTH, height=HEIGHT, format='gray')
    plane = gray.planes[0]
    raw = memoryview(plane)
    return b''.join(raw[y * plane.line_size:y * plane.line_size + WIDTH] for y in range(HEIGHT))


def field_raster(frame):
    """Grayscale raster that keeps every source row so field parity survives.

    Only the width is capped; the full vertical resolution is retained, which is
    what makes cross-field combing measurable.  Container interlace flags are
    deliberately never consulted here or downstream.  The returned buffer is
    compact, so its stride equals the capped width exactly.
    """
    if frame.width * frame.height > MAX_PIXELS:
        raise InputError('decoded picture exceeds geometry limit')
    if frame.height < 16:
        raise InputError('picture is too short to separate fields')
    width = min(frame.width, FIELD_WIDTH)
    gray = frame.reformat(width=width, height=frame.height, format='gray')
    plane = gray.planes[0]
    raw = memoryview(plane)
    pixels = b''.join(bytes(raw[y * plane.line_size:y * plane.line_size + width])
                      for y in range(frame.height))
    return width, frame.height, width, pixels


def decode(path, manifest):
    """Yield lightweight pictures and native-rate PCM in decode order."""
    video = manifest['picture']
    audio = manifest['sound']
    with open_media(path) as container:
        selected = [container.streams[video['index']]]
        if audio:
            selected.append(container.streams[audio['index']])
        resampler = None
        next_sample = None
        prior_picture = None
        count = 0
        try:
            for packet in container.demux(selected):
                for frame in packet.decode():
                    point = timestamp(frame, manifest['origin_us'])
                    if packet.stream.type == 'video':
                        when = round(point)
                        if when < 0:
                            continue
                        if prior_picture is not None and when <= prior_picture:
                            raise InputError('picture display timestamps must be strictly increasing')
                        prior_picture = when
                        count += 1
                        if count > MAX_FRAMES:
                            raise InputError('too many decoded pictures')
                        duration = round((frame.duration or 0) * frame.time_base * SECOND)
                        yield Picture(when, duration, thumbnail(frame))
                    else:
                        if frame.sample_rate != audio['rate'] or len(frame.layout.channels) != audio['channels']:
                            raise InputError('source audio format changes mid-stream')
                        proposed = round(point * frame.sample_rate / SECOND)
                        tolerance = max(1, round(frame.time_base * frame.sample_rate / 2) + 1)
                        first = next_sample if next_sample is not None and abs(proposed - next_sample) <= tolerance else proposed
                        if next_sample is not None and first < next_sample:
                            raise InputError('overlapping decoded audio timestamps')
                        if resampler is None:
                            resampler = av.AudioResampler(format='s16', layout=frame.layout.name,
                                                          rate=frame.sample_rate)
                        frame.pts = None
                        blocks = resampler.resample(frame)
                        if sum(block.samples for block in blocks) != frame.samples:
                            raise InputError('could not convert stream to native-rate PCM')
                        for block in blocks:
                            pcm = array('h')
                            pcm.frombytes(memoryview(block.planes[0])[:block.samples * audio['channels'] * 2])
                            if first < 0:
                                pcm = pcm[-first * audio['channels']:]
                                yield PCM(0, frame.sample_rate, audio['channels'], pcm)
                            else:
                                yield PCM(first, frame.sample_rate, audio['channels'], pcm)
                            first += block.samples
                        next_sample = first
        except (av.error.FFmpegError, ValueError) as exc:
            if isinstance(exc, InputError):
                raise
            raise InputError('the selected media stream could not be decoded') from exc


def field_decode(path, manifest):
    """Yield full-height grayscale field rasters in display order, video only."""
    video = manifest['picture']
    with open_media(path) as container:
        stream = container.streams[video['index']]
        prior = None
        count = 0
        try:
            for packet in container.demux(stream):
                for frame in packet.decode():
                    point = timestamp(frame, manifest['origin_us'])
                    when = round(point)
                    if when < 0:
                        continue
                    if prior is not None and when <= prior:
                        raise InputError('picture display timestamps must be strictly increasing')
                    prior = when
                    count += 1
                    if count > MAX_FRAMES:
                        raise InputError('too many decoded pictures')
                    width, height, line_size, pixels = field_raster(frame)
                    duration = round((frame.duration or 0) * frame.time_base * SECOND)
                    yield FieldFrame(when, duration, width, height, line_size, pixels)
        except (av.error.FFmpegError, ValueError) as exc:
            if isinstance(exc, InputError):
                raise
            raise InputError('the selected media stream could not be decoded') from exc
