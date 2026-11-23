"""Offer browser re-inspection of native source pixels behind a thumbnail region."""

from io import BytesIO
import json
from zipfile import ZipFile

from ..clock import InputError
from . import forms
from .preview import gray_png, pgm_geometry
from .render import page
from .text import safe, timestamp


def create(app, cache, scan_id, data):
    request = {'hit_id': data.get('hit', ''), 'source_us': forms.number(data, 'source_us'),
               'rect': {axis: forms.number(data, axis) for axis in ('x', 'y', 'width', 'height')}}
    raw = app.region(scan_id, request)
    with ZipFile(BytesIO(raw)) as archive:
        mapping = json.loads(archive.read('region.json'))
        width, height, pixels = pgm_geometry(archive.read('region.pgm'))
    rect = mapping['source']['rect']
    if (width, height) != (rect['width'], rect['height']):
        raise InputError('局部证据与来源映射不一致。')
    assets = {'region.png': (gray_png(pixels, width, height), 'image/png'),
              'region.zip': (raw, 'application/zip')}
    return cache.put(mapping, assets)


def describe(rect):
    return f'x {rect["x"]} · y {rect["y"]} · 宽 {rect["width"]} · 高 {rect["height"]}'


def show(key, mapping):
    base = f'/desk/regions/{key}'
    source = mapping['source']
    rect = source['rect']
    return page('局部源像素复查', 'scan', 'region', scan=safe(mapping['scan_id']), hit=safe(mapping['hit_id']),
                base=base, source=timestamp(mapping['source_us']),
                thumb=describe(mapping['thumbnail']['rect']),
                geometry=f'{source["width"]}×{source["height"]}', src=describe(rect),
                pixels=f'{rect["width"]}×{rect["height"]}')
