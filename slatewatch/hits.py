"""Fold contiguous positive measurement bins into attributed findings."""
from dataclasses import dataclass

from .clock import Span
from .rules import identifier


@dataclass
class Run:
    span: Span
    bins: int
    minimum: int
    maximum: int

    def extend(self, span, value):
        self.span = Span(self.span.start, span.end)
        self.bins += 1
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)


def candidates(bins, kind, minimum_us, channel=None):
    run = None
    for span, value in bins:
        if run and (value is None or span.start != run.span.end):
            if run.span.duration >= minimum_us:
                yield hit(run, kind, channel)
            run = None
        if value is not None:
            if run:
                run.extend(span, value)
            else:
                run = Run(span, 1, value, value)
    if run and run.span.duration >= minimum_us:
        yield hit(run, kind, channel)


def hit(run, kind, channel):
    return {'kind': kind, 'channel': channel, **run.span.as_dict(),
            'bins': run.bins, 'minimum': run.minimum, 'maximum': run.maximum}


def bind_hits(scan_id, findings):
    ordered = sorted(findings, key=lambda f: (f['start_us'], f['kind'], f['channel'] or 0, f['end_us']))
    return [{'id': identifier('hit', [scan_id, f]), **f} for f in ordered]
