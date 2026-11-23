"""Assemble same-position frame pairs around a picture finding for shot-continuity review."""
from collections import deque

from .clock import Span
from .media import HEIGHT, WIDTH, Picture, decode
from .picture import differences, nominal_duration

EVENT_KINDS = ('black', 'freeze', 'picture_gap')


def pgm(pixels):
    return f'P5\n{WIDTH} {HEIGHT}\n255\n'.encode() + pixels


def delta_image(older, newer):
    return bytes(abs(old - new) for old, new in zip(older, newer))


class Window:
    """Keep the decoded frames that surround one finding on the programme clock."""

    def __init__(self, span, context):
        self.span = span
        self.context = context
        self.before = deque(maxlen=context)
        self.inside = []
        self.inside_count = 0
        self.after = []

    @property
    def full(self):
        return len(self.after) >= self.context

    def push(self, frame):
        point = frame.timestamp_us
        if point < self.span.start:
            self.before.append(frame)
        elif point < self.span.end:
            self.inside_count += 1
            if len(self.inside) < 2:
                self.inside.append(frame)
            else:
                self.inside[-1] = frame
        elif not self.full:
            self.after.append(frame)


def continuous(coverage, older, newer):
    return any(span.start <= older and newer <= span.end for span in coverage)


def build(path, scan, hit, context):
    """Decode once and pair neighbouring frames; only same-segment, on-grid pairs are differenced."""
    span = Span.parse(hit)
    coverage = [Span.parse(value) for value in scan['coverage']['picture']]
    grid = nominal_duration(scan['tracks']['picture'])
    long_step = grid + grid // 2
    window = Window(span, context)
    stream = decode(path, scan['tracks'])
    try:
        for block in stream:
            if isinstance(block, Picture):
                window.push(block)
                if window.full:
                    break
    finally:
        stream.close()
    frames = [(None, 'before', frame) for frame in window.before]
    if window.inside_count:
        boundary = 'single' if window.inside_count == 1 else 'first'
        frames.append((boundary, 'event', window.inside[0]))
        if window.inside_count > 1:
            frames.append(('last', 'event', window.inside[-1]))
    frames += [(None, 'after', frame) for frame in window.after]
    files, entries = {}, []
    for index, (boundary, role, frame) in enumerate(frames):
        name = f'frame-{index + 1:02d}.pgm'
        files[name] = pgm(frame.pixels)
        entries.append({'key': f'f{index + 1:02d}', 'file': name, 'role': role, 'boundary': boundary,
                        'source_us': frame.timestamp_us, 'duration_us': frame.duration_us})
    pairs = []
    for index in range(len(frames) - 1):
        first, second = entries[index], entries[index + 1]
        pair = {'before': first['key'], 'after': second['key'],
                'before_us': first['source_us'], 'after_us': second['source_us']}
        if frames[index][0] == 'first' and frames[index + 1][0] == 'last' and window.inside_count > 2:
            pairs.append({**pair, 'verdict': 'elided', 'omitted_frames': window.inside_count - 2})
            continue
        step = second['source_us'] - first['source_us']
        pair['step_us'] = step
        if not continuous(coverage, first['source_us'], second['source_us']):
            pair['verdict'] = 'hole'
        elif step > long_step:
            pair['verdict'] = 'long'
        else:
            older, newer = frames[index][2].pixels, frames[index + 1][2].pixels
            delta, edge = differences(older, newer)
            name = f'diff-{index + 1:02d}-{index + 2:02d}.pgm'
            files[name] = pgm(delta_image(older, newer))
            pair.update(verdict='diffable', delta_milli=delta, edge_milli=edge, diff=name)
        pairs.append(pair)
    mapping = {'hit_id': hit['id'], 'scan_id': scan['id'], 'kind': hit['kind'], **span.as_dict(),
               'context': context, 'grid_us': grid, 'long_step_us': long_step,
               'frames': entries, 'pairs': pairs}
    return files, mapping
