"""Re-runnable verification of browser evidence against independent ground truth.

The check deliberately avoids reusing the pipeline's own readers and writers:

* PGM files are parsed byte by byte from the frozen spec instead of going through
  the evidence builder that produced them.
* PNG previews are inflated with the standard library only, so a pixel-changing
  conversion cannot be hidden behind the browser renderer's own image stack.
* Ground-truth rasters are decoded straight from the source master with PyAV and
  reduced with a self-contained thumbnail routine, independent of
  :mod:`slatewatch.media` and :mod:`slatewatch.evidence`.

Every verdict names the clock mapping entry, the cell and the expected source
time, so a failure pinpoints whether the browser used the wrong source cell,
the PNG conversion altered pixels, or a missing cell was filled with the
previous frame instead of being reported as uncovered.
"""
from dataclasses import dataclass
import struct
from zlib import compress, crc32, decompress

from .clock import SECOND
from .media import HEIGHT, WIDTH
from .reels import (CELL_H, CELL_W, DUP_CELLS, DUP_LEVEL, GRID_COLS,
                    GRID_ROWS, TWIN_CELLS, TWIN_LEVEL)

RASTER_SIZE = WIDTH * HEIGHT
PGM_MAGIC = b'P5'
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


class CheckFailure(AssertionError):
    """A reproducible evidence expectation was not met."""


# ---------------------------------------------------------------------------
# Independent byte-level readers (no reuse of the pipeline's writers)
# ---------------------------------------------------------------------------

def read_pgm(data):
    """Parse a binary P5 PGM without PIL or the package's own PGM writer."""
    if not data.startswith(PGM_MAGIC):
        raise CheckFailure('PGM: missing P5 magic number')
    tokens = []
    cursor = 2
    while len(tokens) < 3:
        if cursor >= len(data):
            raise CheckFailure('PGM: header truncated')
        if data[cursor:cursor + 1] == b'#':
            newline = data.find(b'\n', cursor)
            if newline < 0:
                raise CheckFailure('PGM: unterminated comment')
            cursor = newline + 1
            continue
        if data[cursor] in b' \t\r\n':
            cursor += 1
            continue
        start = cursor
        while cursor < len(data) and data[cursor] not in b' \t\r\n':
            cursor += 1
        tokens.append(data[start:cursor])
    width, height, max_value = (int(token) for token in tokens)
    if (width, height, max_value) != (WIDTH, HEIGHT, 255):
        raise CheckFailure(f'PGM: unsupported raster {width}x{height} max {max_value}')
    # Exactly one whitespace byte separates the final header token from pixels.
    if cursor >= len(data) or data[cursor] not in b' \t\r\n':
        raise CheckFailure('PGM: missing raster separator')
    pixels = data[cursor + 1:]
    if len(pixels) != RASTER_SIZE:
        raise CheckFailure(f'PGM: raster has {len(pixels)} bytes, expected {RASTER_SIZE}')
    return bytes(pixels)


def _png_chunks(data):
    if not data.startswith(PNG_MAGIC):
        raise CheckFailure('PNG: missing signature')
    cursor = len(PNG_MAGIC)
    chunks = []
    while cursor < len(data):
        if cursor + 8 > len(data):
            raise CheckFailure('PNG: truncated chunk header')
        length = struct.unpack('!I', data[cursor:cursor + 4])[0]
        name = data[cursor + 4:cursor + 8]
        start = cursor + 8
        end = start + length
        if end + 4 > len(data):
            raise CheckFailure('PNG: truncated chunk body')
        chunks.append((name, data[start:end]))
        cursor = end + 4
    return chunks


def _unfilter(raw, width, height, channels):
    stride = width * channels
    pixels = bytearray(stride * height)
    previous_row = bytearray(stride)
    offset = 0
    for row in range(height):
        kind = raw[offset]
        offset += 1
        scan = bytearray(raw[offset:offset + stride])
        offset += stride
        if kind == 0:
            pass
        elif kind == 1:  # Sub
            for i in range(stride):
                left = scan[i - channels] if i >= channels else 0
                scan[i] = (scan[i] + left) & 0xFF
        elif kind == 2:  # Up
            for i in range(stride):
                scan[i] = (scan[i] + previous_row[i]) & 0xFF
        elif kind == 3:  # Average
            for i in range(stride):
                left = scan[i - channels] if i >= channels else 0
                scan[i] = (scan[i] + ((left + previous_row[i]) // 2)) & 0xFF
        elif kind == 4:  # Paeth
            for i in range(stride):
                left = scan[i - channels] if i >= channels else 0
                up = previous_row[i]
                up_left = previous_row[i - channels] if i >= channels else 0
                predictor = left + up - up_left
                choices = ((abs(predictor - left), left),
                           (abs(predictor - up), up),
                           (abs(predictor - up_left), up_left))
                scan[i] = (scan[i] + min(choices)[1]) & 0xFF
        else:
            raise CheckFailure(f'PNG: unsupported filter type {kind}')
        row_start = row * stride
        pixels[row_start:row_start + stride] = scan
        previous_row = scan
    return bytes(pixels)


def read_png(data):
    """Decode an 8-bit grayscale PNG using only zlib and struct, returning pixels."""
    chunks = _png_chunks(data)
    names = [name for name, _body in chunks]
    if names[:1] != [b'IHDR'] or b'IEND' not in names or b'IDAT' not in names:
        raise CheckFailure('PNG: required chunks are missing')
    width, height, bit_depth, color_type, compression, filtering, interlace = \
        struct.unpack('!2I5B', chunks[0][1])
    if (width, height, bit_depth, color_type, compression, filtering, interlace) \
            != (WIDTH, HEIGHT, 8, 0, 0, 0, 0):
        raise CheckFailure('PNG: only non-interlaced 8-bit grayscale evidence is supported')
    payload = b''.join(body for name, body in chunks if name == b'IDAT')
    raw = decompress(payload)
    decoded = _unfilter(raw, width, height, 1)
    if len(decoded) != RASTER_SIZE:
        raise CheckFailure(f'PNG: decoded {len(decoded)} pixels, expected {RASTER_SIZE}')
    return decoded


def encode_png(pixels):
    """Encode grayscale pixels with filter-0 rows; used only to build tamper controls."""
    if len(pixels) != RASTER_SIZE:
        raise CheckFailure('PNG encode: raster has the wrong size')

    def chunk(name, payload):
        return struct.pack('!I', len(payload)) + name + payload + struct.pack('!I', crc32(name + payload))

    rows = b''.join(b'\0' + pixels[start:start + WIDTH] for start in range(0, RASTER_SIZE, WIDTH))
    return (PNG_MAGIC + chunk(b'IHDR', struct.pack('!2I5B', WIDTH, HEIGHT, 8, 0, 0, 0, 0))
            + chunk(b'IDAT', compress(rows)) + chunk(b'IEND', b''))


# ---------------------------------------------------------------------------
# Cell-level identity on the 10x6 cell grid
# ---------------------------------------------------------------------------

def cell_block(pixels, index):
    if not 0 <= index < GRID_COLS * GRID_ROWS:
        raise CheckFailure(f'cell {index} is outside the {GRID_COLS}x{GRID_ROWS} grid')
    row, col = divmod(index, GRID_COLS)
    parts = []
    for y in range(CELL_H):
        start = (row * CELL_H + y) * WIDTH + col * CELL_W
        parts.append(pixels[start:start + CELL_W])
    return b''.join(parts)


def cell_levels(pixels):
    return {index: cell_block(pixels, index)[0] for index in range(GRID_COLS * GRID_ROWS)}


def differing_pixels(left, right):
    return [(offset, old, new) for offset, (old, new) in enumerate(zip(left, right)) if old != new]


# ---------------------------------------------------------------------------
# Independent ground truth decoded straight from the master
# ---------------------------------------------------------------------------

def _independent_thumbnail(frame):
    """Reduce a decoded frame to 80x48 gray without calling media.thumbnail."""
    gray = frame.reformat(width=WIDTH, height=HEIGHT, format='gray')
    plane = gray.planes[0]
    raw = memoryview(plane)
    stride = getattr(plane, 'line_size', WIDTH)
    return b''.join(bytes(raw[y * stride:y * stride + WIDTH]) for y in range(HEIGHT))


def ground_truth(path, manifest):
    """Map every source presentation time (us) to its independently decoded raster."""
    import av

    origin = manifest.get('origin_us') or 0
    stream_index = manifest['picture']['index']
    truth = {}
    with av.open(str(path), options={'protocol_whitelist': 'file',
                                     'format_whitelist': 'matroska,mov',
                                     'enable_drefs': '0'}) as container:
        stream = container.streams[stream_index]
        for packet in container.demux(stream):
            for frame in packet.decode():
                if frame.pts is None or frame.time_base is None:
                    raise CheckFailure('ground truth frame lacks a presentation timestamp')
                when = round(frame.pts * frame.time_base * SECOND - origin)
                if when in truth:
                    raise CheckFailure(f'duplicate ground truth timestamp {when}us')
                truth[when] = _independent_thumbnail(frame)
    if not truth:
        raise CheckFailure('ground truth master contains no decoded pictures')
    return truth


def expected_source_for_target(target_us, truth, coverage):
    """Reproduce the evidence attribution rule independently.

    A target is attributed only when it lies inside a declared picture
    coverage span; the attributed frame is the decoded frame whose
    presentation time is the last one not later than the target.
    """
    if not any(span.start <= target_us < span.end for span in coverage):
        return None
    earlier = [when for when in truth if when <= target_us]
    if not earlier:
        return None
    return max(earlier)


# ---------------------------------------------------------------------------
# Verification records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FrameVerdict:
    index: int
    target_us: int
    source_us: int | None
    status: str
    detail: str

    def as_dict(self):
        return {'index': self.index, 'target_us': self.target_us,
                'source_us': self.source_us, 'status': self.status, 'detail': self.detail}


def _clock_entries(mapping):
    return mapping['picture'] if isinstance(mapping, dict) else mapping


def verify_mapping(mapping, truth, coverage, raster_loader, label):
    """Check one clock.json against ground truth via a caller-supplied raster reader.

    ``raster_loader(file_name)`` returns the packaged bytes for each present
    frame (PGM inside a ZIP/PNG inside the preview cache), keeping this routine
    independent of where the bytes live.
    """
    verdicts = []
    entries = _clock_entries(mapping)
    for index, entry in enumerate(entries, start=1):
        target = entry['target_us']
        expected = expected_source_for_target(target, truth, coverage)
        actual = entry['source_us']
        name = entry['file']
        if expected is None:
            if name is not None or actual is not None:
                raise CheckFailure(
                    f'{label} frame {index}: target {target}us falls in a picture hole but '
                    f'a frame was supplied ({name}, source {actual}); a missing cell must stay missing')
            verdicts.append(FrameVerdict(index, target, None, 'missing',
                                         'target is outside picture coverage; no frame may fill it'))
            continue
        if name is None or actual is None:
            raise CheckFailure(
                f'{label} frame {index}: target {target}us is covered at source {expected}us '
                'but the mapping reports no frame')
        if actual != expected:
            raise CheckFailure(
                f'{label} frame {index}: wrong source cell for target {target}us: '
                f'mapping says {actual}us, ground truth requires {expected}us')
        packaged = raster_loader(name)
        observed = read_pgm(packaged) if name.endswith('.pgm') else read_png(packaged)
        reference = truth[actual]
        if observed != reference:
            changes = differing_pixels(observed, reference)
            raise CheckFailure(
                f'{label} frame {index} ({name}): raster differs from source frame {actual}us '
                f'at {len(changes)} pixel(s), first {changes[:3]}')
        verdicts.append(FrameVerdict(index, target, actual, 'exact',
                                     f'{name} matches decoded source pixel-for-pixel'))
    return verdicts


def verify_cell_invariants(pixels, label):
    """The duplicated gray cell and one-pixel twin properties must be detectable."""
    levels = cell_levels(pixels)
    first, second = DUP_CELLS
    if levels[first] != DUP_LEVEL or levels[second] != DUP_LEVEL:
        raise CheckFailure(
            f'{label}: duplicate cells {DUP_CELLS} must both sit at gray level {DUP_LEVEL}, '
            f'got {levels[first]} and {levels[second]}')
    if cell_block(pixels, first) != cell_block(pixels, second):
        raise CheckFailure(f'{label}: duplicate gray cells {DUP_CELLS} are no longer byte-identical')
    twin_a, twin_b = TWIN_CELLS
    if levels[twin_a] != TWIN_LEVEL or levels[twin_b] not in (TWIN_LEVEL, TWIN_LEVEL + 1):
        raise CheckFailure(f'{label}: twin cells {TWIN_CELLS} lost their shared gray level')
    changes = differing_pixels(cell_block(pixels, twin_a), cell_block(pixels, twin_b))
    if len(changes) != 1:
        raise CheckFailure(
            f'{label}: twin cells {TWIN_CELLS} must differ by exactly one pixel, found {len(changes)}')
    return {'duplicate_cells': list(DUP_CELLS), 'duplicate_level': DUP_LEVEL,
            'twin_cells': list(TWIN_CELLS), 'twin_pixel_diff': changes[0]}


def verify_shared_cell(left_pixels, right_pixels):
    """Two excerpts must be able to select the same gray cell independently."""
    left_levels, right_levels = cell_levels(left_pixels), cell_levels(right_pixels)
    shared = [index for index in range(GRID_COLS * GRID_ROWS)
              if cell_block(left_pixels, index) == cell_block(right_pixels, index)]
    if not shared:
        raise CheckFailure('the two excerpts share no identical gray cell')
    return {'identical_cell_count': len(shared),
            'duplicate_pair_levels': [left_levels[DUP_CELLS[0]], right_levels[DUP_CELLS[0]]],
            'sample_cells': shared[:5]}


def verify_frozen_twin(pixels):
    """Inside the freeze episode twin cells are identical (no one-pixel mark)."""
    changes = differing_pixels(cell_block(pixels, TWIN_CELLS[0]), cell_block(pixels, TWIN_CELLS[1]))
    if changes:
        raise CheckFailure(f'frozen frame twin cells should match, but differ at {changes[:3]}')
    return {'twin_cells': list(TWIN_CELLS), 'pixel_differences': 0}


def assert_detects_wrong_source(mapping, truth, coverage, raster_loader, label):
    """Negative control: a swapped source frame must be rejected.

    Both the clock attribution (source_us) and the raster bytes are swapped,
    so the check fails on the wrong source-cell timestamp before it even
    reaches pixel comparison.
    """
    entries = _clock_entries(mapping)
    present_positions = [i for i, entry in enumerate(entries) if entry['file']]
    if len(present_positions) < 2:
        raise CheckFailure('negative control needs at least two present frames')
    broken = [dict(entry) for entry in entries]
    first, second = present_positions[0], present_positions[1]
    broken[first]['source_us'], broken[second]['source_us'] = (
        broken[second]['source_us'], broken[first]['source_us'])
    swap = {entries[first]['file']: entries[second]['file'],
            entries[second]['file']: entries[first]['file']}

    def swapped_loader(name):
        return raster_loader(swap[name])

    try:
        verify_mapping({'picture': broken}, truth, coverage, swapped_loader, label)
    except CheckFailure as exc:
        if 'wrong source' not in str(exc):
            raise
        return str(exc)
    raise CheckFailure('negative control failed: a swapped source frame was accepted')


def assert_detects_pixel_tamper(mapping, truth, coverage, raster_loader, label):
    """Negative control: one changed pixel in a packaged raster must be rejected."""
    entries = _clock_entries(mapping)
    target = next(entry for entry in entries if entry['file'])

    def tampered_loader(name):
        data = raster_loader(name)
        if name != target['file']:
            return data
        if name.endswith('.pgm'):
            broken = bytearray(data)
            broken[-1] ^= 0x01
            return bytes(broken)
        # Re-encode a valid PNG from a one-pixel-mutated raster so the failure
        # must come from pixel comparison, not from a malformed container.
        pixels = bytearray(read_png(data))
        pixels[-1] ^= 0x01
        return encode_png(bytes(pixels))

    try:
        verify_mapping({'picture': entries}, truth, coverage, tampered_loader, label)
    except CheckFailure as exc:
        if 'differs from source' not in str(exc):
            raise
        return str(exc)
    raise CheckFailure('negative control failed: a pixel-tampered raster was accepted')


def assert_detects_filled_hole(mapping, truth, coverage, raster_loader, label):
    """Negative control: filling a hole with the previous frame must be rejected."""
    entries = _clock_entries(mapping)
    missing_index = next((i for i, entry in enumerate(entries) if entry['file'] is None), -1)
    if missing_index < 0:
        raise CheckFailure('negative control needs an excerpt that contains a picture hole')
    previous = entries[missing_index - 1]
    broken = [dict(entry) for entry in entries]
    stolen_name = previous['file'].removesuffix('.pgm') + '.stolen.pgm' \
        if previous['file'].endswith('.pgm') else previous['file']
    broken[missing_index] = {'target_us': entries[missing_index]['target_us'],
                             'source_us': previous['source_us'], 'file': stolen_name}

    def filled_loader(name):
        if name == stolen_name:
            return raster_loader(previous['file'])
        return raster_loader(name)

    try:
        verify_mapping({'picture': broken}, truth, coverage, filled_loader, label)
    except CheckFailure as exc:
        if 'hole' not in str(exc):
            raise
        return str(exc)
    raise CheckFailure('negative control failed: a hole filled from the previous frame was accepted')
