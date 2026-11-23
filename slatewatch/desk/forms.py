"""Parse bounded URL-encoded browser actions without changing JSON API inputs."""

from urllib.parse import parse_qs

from ..clock import InputError


def decode(raw):
    result = parse_qs(raw, encoding='utf-8', errors='strict', keep_blank_values=True, max_num_fields=24)
    if any(len(values) != 1 for values in result.values()):
        raise InputError('表单字段不能重复。')
    return {name: values[0] for name, values in result.items()}


def number(data, key, default=None):
    value = data.get(key, default)
    if value is None or not str(value).isascii() or not str(value).isdecimal() or len(str(value)) > 12:
        raise InputError(f'表单字段 {key} 需要正整数字面值或 0。')
    return int(value)


def policy(app, data):
    return app.policy({'label': data.get('label', ''), 'thresholds': {
        key: number(data, key) for key in ('black_min_ms', 'freeze_min_ms', 'sound_gap_ms') if data.get(key)
    }})
