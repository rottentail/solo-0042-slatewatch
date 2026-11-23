"""Locate active-picture (aperture) changes across a mixed-format programme.

Frames are measured on a fixed gray grid. A column or row belongs to the
blanking when its peak luma over a whole segment stays below the black level,
so asymmetric bars, fade-ins and inherently dark sides are treated as
distinct situations rather than per-frame threshold accidents.
"""
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import argparse
import json

from .clock import InputError, Span
from .media import Picture, decode, inspect
from .rules import canonical

APERTURE_VERSION = 1
GRID = (192, 108)
BLACK_LUMA = 24
DIM_LUMA = 40
DARK_EDGE_LUMA = 48
VOTE_WINDOW = 9
MIN_SEGMENT_MS = 240
CELL_TOLERANCE = 1
SIDES = ('left', 'right', 'top', 'bottom')


@dataclass(frozen=True)
class Profile:
    timestamp_us: int
    duration_us: int
    columns: bytes
    rows: bytes
    peak: int


def profiles(path, manifest, grid=GRID):
    """One pass over the picture track keeping only luma profiles."""
    result = []
    silent = {**manifest, 'sound': None}
    for entry in decode(path, silent, grid):
        if isinstance(entry, Picture):
            rows = [entry.pixels[y * entry.width:(y + 1) * entry.width] for y in range(entry.height)]
            row_peaks = bytes(max(row) for row in rows)
            result.append(Profile(entry.timestamp_us, entry.duration_us,
                                  bytes(max(column) for column in zip(*rows)), row_peaks, max(row_peaks)))
    if not result:
        raise InputError('selected video has no pictures in the programme clock')
    return result


def bounds_of(columns, rows, black):
    """Active rectangle as inclusive (left, right, top, bottom) cells."""
    wide = [x for x, value in enumerate(columns) if value > black]
    high = [y for y, value in enumerate(rows) if value > black]
    if not wide or not high:
        return None
    return (wide[0], wide[-1], high[0], high[-1])


def stabilize(bounds, window=VOTE_WINDOW):
    """Majority vote over a sliding window so brief fades cannot win."""
    half = window // 2
    voted = []
    for index, current in enumerate(bounds):
        choices = {}
        for other in bounds[max(0, index - half):index + half + 1]:
            if other is not None:
                key = tuple(round(value / 2) for value in other)
                choices[key] = choices.get(key, 0) + 1
        if not choices:
            voted.append(None)
            continue
        centre = tuple(round(value / 2) for value in current) if current else None
        voted.append(max(choices, key=lambda key: (choices[key], key == centre)))
    return voted


def fill(bounds):
    """Dim frames inherit the aperture of the nearest measured frame."""
    last = None
    for index, value in enumerate(bounds):
        if value is None:
            bounds[index] = last
        else:
            last = value
    following = None
    for index in range(len(bounds) - 1, -1, -1):
        if bounds[index] is None:
            bounds[index] = following
        else:
            following = bounds[index]
    return bounds


def distance(first, second):
    return sum(abs(a - b) for a, b in zip(first, second))


def merge_short(runs, min_frames):
    """Absorb too-brief runs into the nearer neighbour in aperture space."""
    runs = [list(run) for run in runs]
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for index, run in enumerate(runs):
            if run[2] - run[1] >= min_frames:
                continue
            before = runs[index - 1] if index > 0 else None
            after = runs[index + 1] if index + 1 < len(runs) else None
            if before is None:
                target = after
            elif after is None:
                target = before
            elif distance(run[0], before[0]) < distance(run[0], after[0]):
                target = before
            else:
                target = after
            target[1] = min(target[1], run[1])
            target[2] = max(target[2], run[2])
            del runs[index]
            changed = True
            break
    return runs


def refine(first, last, frames, dim, black, fallback):
    """Segment aperture from the temporal peak of its measured frames.

    Frames within half a vote window of the boundaries are skipped: the vote
    may still assign them to the wrong side of a cut, and a single leaked
    frame would otherwise stretch the temporal peak to the neighbour's
    aperture.
    """
    margin = min(VOTE_WINDOW // 2, max(0, (last - first - 2) // 2))
    measured = [index for index in range(first + margin, last - margin) if not dim[index]]
    if not measured:
        return fallback, None, None
    columns = bytes(max(frames[index].columns[x] for index in measured) for x in range(len(frames[0].columns)))
    rows = bytes(max(frames[index].rows[y] for index in measured) for y in range(len(frames[0].rows)))
    return bounds_of(columns, rows, black) or fallback, columns, rows


def same_aperture(first, second):
    return all(abs(a - b) <= CELL_TOLERANCE for a, b in zip(first, second))


def nominal_step(frames):
    steps = sorted(b.timestamp_us - a.timestamp_us for a, b in zip(frames, frames[1:]))
    return steps[len(steps) // 2] if steps else (frames[0].duration_us or 40000)


def dim_spans(frames, dim, nominal):
    spans = []
    index = 0
    while index < len(frames):
        if not dim[index]:
            index += 1
            continue
        end = index
        while end + 1 < len(frames) and dim[end + 1]:
            end += 1
        spans.append(Span(frames[index].timestamp_us,
                          frames[end].timestamp_us + (frames[end].duration_us or nominal)))
        index = end + 1
    return spans


def to_pixels(bounds, raster, grid):
    left, right, top, bottom = bounds
    return {'left': round(left * raster[0] / grid[0]), 'right': round((right + 1) * raster[0] / grid[0]),
            'top': round(top * raster[1] / grid[1]), 'bottom': round((bottom + 1) * raster[1] / grid[1])}


def to_grid(bounds):
    left, right, top, bottom = bounds
    return {'left': left, 'right': right + 1, 'top': top, 'bottom': bottom + 1}


def segment_flags(segment):
    flags = []
    if segment['dim_count'] * 2 > segment['last'] - segment['first']:
        flags.append('mostly_dim')
    columns, rows = segment['columns'], segment['rows']
    if columns is None or rows is None:
        return flags
    left, right, top, bottom = segment['bounds']
    edges = {'left': columns[left:left + 2], 'right': columns[max(0, right - 1):right + 1],
             'top': rows[top:top + 2], 'bottom': rows[max(0, bottom - 1):bottom + 1]}
    for side, cells in edges.items():
        if cells and max(cells) <= DARK_EDGE_LUMA:
            flags.append(f'dark_edge:{side}')
    return flags


def pick_frames(frames, dim, first, last, count, side):
    ceiling = max(frame.peak for frame in frames[first:last])
    good = [index for index in range(first, last)
            if not dim[index] and frames[index].peak * 5 >= ceiling * 3]
    pool = good or [index for index in range(first, last) if not dim[index]] or list(range(first, last))
    return pool[-count:] if side == 'before' else pool[:count]


def capture(path, manifest, grid, wanted):
    """Second decode pass collecting the requested source frames."""
    found = {}
    for entry in decode(path, {**manifest, 'sound': None}, grid):
        if isinstance(entry, Picture) and entry.timestamp_us in wanted:
            found[entry.timestamp_us] = entry
    return found


def analyze(path, manifest, frames_per_side=2, black_luma=BLACK_LUMA, min_segment_ms=MIN_SEGMENT_MS):
    frames = profiles(path, manifest)
    nominal = nominal_step(frames)
    dim = [frame.peak <= DIM_LUMA for frame in frames]
    measured = [None if dim[index] else bounds_of(frame.columns, frame.rows, black_luma)
                for index, frame in enumerate(frames)]
    raster = (manifest['picture']['width'], manifest['picture']['height'])
    notes = []
    base = {'analysis_version': APERTURE_VERSION,
            'raster': dict(zip(('width', 'height'), raster)), 'grid': dict(zip(('width', 'height'), GRID)),
            'thresholds': {'black_luma_max': black_luma, 'dim_luma_max': DIM_LUMA,
                           'dark_edge_luma_max': DARK_EDGE_LUMA, 'min_segment_ms': min_segment_ms,
                           'vote_window_frames': VOTE_WINDOW},
            'extent': {'start_us': frames[0].timestamp_us,
                       'end_us': frames[-1].timestamp_us + (frames[-1].duration_us or nominal)}}
    if all(value is None for value in measured):
        notes.append('programme is uniformly dark; no active area could be measured')
        return {**base, 'segments': [], 'changes': [],
                'dim_spans': [span.as_dict() for span in dim_spans(frames, dim, nominal)], 'notes': notes}, {}

    bounds = fill(stabilize(measured))
    runs = []
    for index, value in enumerate(bounds):
        if runs and value == runs[-1][0]:
            runs[-1][2] = index + 1
        else:
            runs.append([value, index, index + 1])
    min_frames = max(2, round(min_segment_ms * 1000 / nominal))
    runs = merge_short(runs, min_frames)

    segments = []
    for voted, first, last in runs:
        refined, columns, rows = refine(first, last, frames, dim, black_luma, voted)
        segments.append({'bounds': refined, 'first': first, 'last': last, 'columns': columns, 'rows': rows,
                         'dim_count': sum(dim[first:last])})
    merged = True
    while merged:
        merged = False
        for index in range(len(segments) - 1):
            if same_aperture(segments[index]['bounds'], segments[index + 1]['bounds']):
                first, second = segments[index], segments.pop(index + 1)
                first['last'] = second['last']
                first['dim_count'] += second['dim_count']
                first['bounds'], first['columns'], first['rows'] = refine(
                    first['first'], first['last'], frames, dim, black_luma, first['bounds'])
                merged = True
                break

    spans = dim_spans(frames, dim, nominal)
    report_segments = []
    for index, segment in enumerate(segments):
        pixels = to_pixels(segment['bounds'], raster, GRID)
        report_segments.append({
            'index': index, 'start_us': frames[segment['first']].timestamp_us,
            'end_us': frames[segment['last'] - 1].timestamp_us + (frames[segment['last'] - 1].duration_us or nominal),
            'bounds': pixels, 'bounds_grid': to_grid(segment['bounds']),
            'bars': {'left': pixels['left'], 'right': raster[0] - pixels['right'],
                     'top': pixels['top'], 'bottom': raster[1] - pixels['bottom']},
            'flags': segment_flags(segment)})

    wanted = {}
    changes = []
    for index in range(1, len(segments)):
        before, after = segments[index - 1], segments[index]
        at_us = frames[after['first']].timestamp_us
        dim_near = next((span for span in spans
                         if span.start - 4 * nominal <= at_us <= span.end + 4 * nominal), None)
        chosen = []
        for side, segment in (('before', before), ('after', after)):
            for slot, frame_index in enumerate(pick_frames(frames, dim, segment['first'], segment['last'],
                                                           frames_per_side, side), start=1):
                stamp = frames[frame_index].timestamp_us
                name = f'frames/change-{index:02d}-{side}-{slot}.pgm'
                wanted[stamp] = None
                chosen.append({'file': name, 'side': side, 'source_us': stamp})
        changes.append({'index': index - 1, 'at_us': at_us, 'kind': 'fade' if dim_near else 'cut',
                        'dim_us': dim_near.as_dict() if dim_near else None,
                        'before': to_pixels(before['bounds'], raster, GRID),
                        'after': to_pixels(after['bounds'], raster, GRID),
                        'before_grid': to_grid(before['bounds']), 'after_grid': to_grid(after['bounds']),
                        'changed_sides': [side for side, old, new in zip(SIDES, before['bounds'], after['bounds'])
                                          if abs(old - new) > CELL_TOLERANCE],
                        'frames': chosen})
    if not changes:
        notes.append('aperture is uniform across the programme')

    pictures = capture(path, manifest, GRID, wanted)
    images = {}
    for change in changes:
        for frame in change['frames']:
            picture = pictures.get(frame['source_us'])
            if picture is not None:
                images[frame['file']] = f'P5\n{picture.width} {picture.height}\n255\n'.encode() + picture.pixels

    report = {**base, 'segments': report_segments, 'changes': changes,
              'dim_spans': [span.as_dict() for span in spans], 'notes': notes}
    return report, images


def bundle(report, images):
    buf = BytesIO()
    with ZipFile(buf, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr('aperture.json', canonical(report))
        for name, data in images.items():
            archive.writestr(name, data)
    return buf.getvalue()


def main():
    parser = argparse.ArgumentParser(description='Measure active-picture changes in a mixed-format master')
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--frames', type=int, default=2, help='source frames exported on each side of a change')
    parser.add_argument('--black-luma', type=int, default=BLACK_LUMA)
    parser.add_argument('--min-segment-ms', type=int, default=MIN_SEGMENT_MS)
    options = parser.parse_args()
    manifest = inspect(options.source, None, None)
    report, images = analyze(options.source, manifest, options.frames, options.black_luma, options.min_segment_ms)
    options.output.write_bytes(bundle(report, images))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
