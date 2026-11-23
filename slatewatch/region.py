"""Export native source pixels behind a rectangle drawn on an evidence thumbnail."""
from .clock import InputError, integer, shape
from .media import HEIGHT, WIDTH, native_picture


def thumbnail_rect(data):
    shape(data, ('x', 'y', 'width', 'height'))
    rect = {'x': integer(data['x'], 'x', 0, WIDTH - 1),
            'y': integer(data['y'], 'y', 0, HEIGHT - 1)}
    rect['width'] = integer(data['width'], 'width', 1, WIDTH - rect['x'])
    rect['height'] = integer(data['height'], 'height', 1, HEIGHT - rect['y'])
    return rect


def source_rect(rect, width, height):
    """Whole source pixels under a thumbnail rectangle; each axis stretches on its own."""
    x0 = rect['x'] * width // WIDTH
    x1 = -(-(rect['x'] + rect['width']) * width // WIDTH)
    y0 = rect['y'] * height // HEIGHT
    y1 = -(-(rect['y'] + rect['height']) * height // HEIGHT)
    return {'x': x0, 'y': y0, 'width': x1 - x0, 'height': y1 - y0}


def extract(path, scan, hit, source_us, rect):
    """Crop the native frame shown at source_us; never reuse another frame."""
    rect = thumbnail_rect(rect)
    frame = native_picture(path, scan['tracks'], source_us)
    track = scan['tracks']['picture']
    if frame['width'] != track['width'] or frame['height'] != track['height']:
        raise InputError('decoded geometry differs from the selected stream description')
    source = source_rect(rect, frame['width'], frame['height'])
    stride = frame['width']
    pixels = b''.join(
        frame['pixels'][(source['y'] + row) * stride + source['x']:
                        (source['y'] + row) * stride + source['x'] + source['width']]
        for row in range(source['height']))
    mapping = {'scan_id': scan['id'], 'hit_id': hit['id'], 'source_us': frame['timestamp_us'],
               'duration_us': frame['duration_us'],
               'thumbnail': {'width': WIDTH, 'height': HEIGHT, 'rect': rect},
               'source': {'width': frame['width'], 'height': frame['height'], 'rect': source},
               'file': 'region.pgm'}
    header = f'P5\n{source["width"]} {source["height"]}\n255\n'.encode()
    return mapping, header + pixels
