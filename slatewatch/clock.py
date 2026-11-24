"""Half-open spans on the container's shared presentation clock."""
from dataclasses import dataclass
from fractions import Fraction

SECOND = 1_000_000
LIMIT = 120 * SECOND


class InputError(ValueError):
    pass


class Missing(LookupError):
    pass


class Conflict(ValueError):
    pass


def integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise InputError(f'{name} must be an integer in [{minimum}, {maximum}]')
    return value


def shape(data, required, optional=()):
    if not isinstance(data, dict):
        raise InputError('expected a JSON object')
    missing = sorted(set(required) - data.keys())
    extra = sorted(data.keys() - set(required) - set(optional))
    if missing or extra:
        raise InputError(f'missing fields: {missing}; unknown fields: {extra}')
    return data


def text(value, name, limit=120):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise InputError(f'{name} must contain 1 to {limit} characters')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise InputError(f'{name} must contain valid Unicode') from exc
    return value.strip()


@dataclass(frozen=True, order=True)
class Span:
    start: int
    end: int

    @classmethod
    def parse(cls, data, limit=LIMIT):
        start = integer(data['start_us'], 'start_us', 0, limit)
        end = integer(data['end_us'], 'end_us', 0, limit)
        if start >= end:
            raise InputError('end_us must be greater than start_us')
        return cls(start, end)

    @property
    def duration(self):
        return self.end - self.start

    def as_dict(self):
        return {'start_us': self.start, 'end_us': self.end}

    def intersect(self, other):
        start, end = max(self.start, other.start), min(self.end, other.end)
        return Span(start, end) if start < end else None

    def contains(self, other):
        return self.start <= other.start and other.end <= self.end


def coalesce(spans):
    result = []
    for span in sorted(spans):
        if result and span.start <= result[-1].end:
            result[-1] = Span(result[-1].start, max(span.end, result[-1].end))
        else:
            result.append(span)
    return result


def uncovered(coverage, extent):
    cursor = extent.start
    for span in coalesce(coverage):
        overlap = span.intersect(extent)
        if overlap is None:
            continue
        if cursor < overlap.start:
            yield Span(cursor, overlap.start)
        cursor = overlap.end
    if cursor < extent.end:
        yield Span(cursor, extent.end)


def partition(boundaries, extent):
    """Cut one half-open span at every boundary strictly inside it."""
    cuts = sorted({point for point in boundaries if extent.start < point < extent.end})
    points = [extent.start, *cuts, extent.end]
    return [Span(left, right) for left, right in zip(points, points[1:])]


def timestamp(frame, origin):
    if frame.pts is None or frame.time_base is None:
        raise InputError('decoded frame has no presentation timestamp')
    us = Fraction(frame.pts) * frame.time_base * SECOND - origin
    if us > LIMIT or us < -SECOND:
        raise InputError('presentation timestamp is outside the supported range')
    return us


def sample_boundary(us, rate):
    return -((-us * rate) // SECOND)
