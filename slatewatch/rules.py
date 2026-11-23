"""Technical tolerances are immutable and stored with each scan."""
from dataclasses import asdict, dataclass, fields
from hashlib import sha256
import json

from .clock import InputError, integer, shape, text

PCM_FULL_SCALE = 1 << 15

KINDS = ('black', 'freeze', 'silence', 'clip', 'picture_gap', 'sound_gap', 'duplicate')
INTENT_KINDS = frozenset(('black', 'freeze', 'silence'))
CHANNEL_KINDS = frozenset(('silence', 'clip'))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def identifier(prefix, value):
    return f'{prefix}-{sha256(canonical(value)).hexdigest()[:24]}'


@dataclass(frozen=True)
class Thresholds:
    black_luma_max: int = 24
    black_permille: int = 940
    black_min_ms: int = 300
    freeze_delta_milli: int = 300
    freeze_edge_milli: int = 400
    freeze_min_ms: int = 500
    audio_window_ms: int = 50
    silence_rms: int = 80
    silence_min_ms: int = 300
    clip_permille: int = 100
    clip_min_ms: int = 100
    duplicate_corr_permille: int = 980
    duplicate_lag_ms: int = 12
    duplicate_min_ms: int = 400
    picture_gap_ms: int = 250
    sound_gap_ms: int = 100

    @classmethod
    def from_request(cls, data):
        shape(data, (), [field.name for field in fields(cls)])
        limits = {
            'black_luma_max': (0, 127), 'black_permille': (500, 1000),
            'black_min_ms': (20, 10000), 'freeze_delta_milli': (0, 25500),
            'freeze_edge_milli': (0, 25500), 'freeze_min_ms': (30, 10000),
            'audio_window_ms': (10, 500), 'silence_rms': (0, PCM_FULL_SCALE // 16),
            'silence_min_ms': (10, 10000), 'clip_permille': (1, 1000),
            'clip_min_ms': (10, 10000), 'duplicate_corr_permille': (500, 1000),
            'duplicate_lag_ms': (0, 40), 'duplicate_min_ms': (10, 10000),
            'picture_gap_ms': (50, 5000),
            'sound_gap_ms': (10, 5000),
        }
        values = {key: integer(value, key, *limits[key]) for key, value in data.items()}
        result = cls(**values)
        if result.silence_min_ms < result.audio_window_ms or result.clip_min_ms < result.audio_window_ms \
                or result.duplicate_min_ms < result.audio_window_ms:
            raise InputError('minimum sound event length must cover a full sound window')
        if result.duplicate_lag_ms * 2 >= result.audio_window_ms:
            raise InputError('channel copy lag search must keep half a sound window of overlap')
        return result


def new_policy(payload):
    shape(payload, ('label',), ('thresholds',))
    record = {'label': text(payload['label'], 'label'),
              'thresholds': asdict(Thresholds.from_request(payload.get('thresholds', {})))}
    return {'id': identifier('policy', record), **record}
