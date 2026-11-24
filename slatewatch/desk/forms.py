"""Parse bounded URL-encoded browser actions without changing JSON API inputs."""

from urllib.parse import parse_qs

from ..clock import InputError

SELECTION_LIMIT = 6


def decode(raw, repeat=()):
    result = parse_qs(raw, encoding='utf-8', errors='strict', keep_blank_values=True, max_num_fields=24)
    if any(len(values) != 1 for name, values in result.items() if name not in repeat):
        raise InputError('表单字段不能重复。')
    return {name: values if name in repeat else values[0] for name, values in result.items()}


def selection(data):
    ids = data.get('hit', [])
    if isinstance(ids, str):
        ids = [ids]
    ids = [key for key in ids if key.strip()]
    if not 1 <= len(ids) <= SELECTION_LIMIT:
        raise InputError(f'请选择 1 至 {SELECTION_LIMIT} 条候选。')
    if len(set(ids)) != len(ids):
        raise InputError('候选选择不能重复。')
    return ids


def number(data, key, default=None):
    value = data.get(key, default)
    if value is None or not str(value).isascii() or not str(value).isdecimal() or len(str(value)) > 12:
        raise InputError(f'表单字段 {key} 需要正整数字面值或 0。')
    return int(value)


def policy(app, data):
    return app.policy({'label': data.get('label', ''), 'thresholds': {
        key: number(data, key) for key in ('black_min_ms', 'freeze_min_ms', 'sound_gap_ms') if data.get(key)
    }})
