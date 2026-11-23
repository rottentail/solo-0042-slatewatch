"""Show which frame pairs around a picture finding support a same-shot difference call."""

from io import BytesIO
import json
from zipfile import ZipFile

from ..clock import InputError
from .cache import PREVIEW_LIMIT
from .preview import png
from .render import page
from .text import KINDS, interval, safe, timestamp

VERDICTS = {
    'diffable': ('适合取差', 'expected'),
    'long': ('长格间隔 · 无法支持同一连续镜头的判断', 'pending'),
    'hole': ('覆盖缺口 · 无法支持同一连续镜头的判断', 'held'),
    'elided': ('事件内部省略 · 未取差', 'pending'),
}
ROLES = {'before': '事件前', 'event': '事件内', 'after': '事件后'}
BOUNDARIES = {'first': '首帧', 'last': '末帧', 'single': '唯一帧', None: ''}


def create(app, cache, scan_id, hit_id, context):
    raw = app.board(scan_id, {'hit_id': hit_id, 'context': context})
    with ZipFile(BytesIO(raw)) as archive:
        if sum(item.file_size for item in archive.infolist()) > PREVIEW_LIMIT:
            raise InputError('证据板超过浏览器预览容量，请调整前后帧数。')
        mapping = json.loads(archive.read(f'{hit_id}/board.json'))
        assets = {}
        for name in archive.namelist():
            if name.endswith('.pgm'):
                view = name.rsplit('/', 1)[-1].removesuffix('.pgm') + '.png'
                assets[view] = (png(archive.read(name)), 'image/png')
    return cache.put(mapping, assets)


def frame_figure(base, frame):
    name = frame['file'].removesuffix('.pgm') + '.png'
    label = ROLES[frame['role']]
    boundary = BOUNDARIES[frame['boundary']]
    if boundary:
        label = f'{label} · {boundary}'
    return (f'<figure><img src="{base}/{name}" alt="画格 {frame["key"]}">'
            f'<figcaption><strong>{frame["key"]} · {label}</strong>'
            f'<small>源时标 {timestamp(frame["source_us"])} · 持续 {frame["duration_us"]} µs</small>'
            f'</figcaption></figure>')


def pair_item(base, pair):
    verdict, state = VERDICTS[pair['verdict']]
    head = (f'<header><strong>{pair["before"]} → {pair["after"]}</strong>'
            f'<span class="state {state}">{verdict}</span></header>')
    if pair['verdict'] == 'elided':
        detail = f'<p class="muted">两帧之间的事件内部另有 {pair["omitted_frames"]} 帧未列入，未形成差分。</p>'
    else:
        when = f'源时标 {timestamp(pair["before_us"])} → {timestamp(pair["after_us"])} · 间隔 {pair["step_us"]} µs'
        if pair['verdict'] == 'diffable':
            name = pair['diff'].removesuffix('.pgm') + '.png'
            detail = (f'<p>{when}</p><figure class="diff"><img src="{base}/{name}" alt="同位置差分图">'
                      f'<figcaption><small>同位置差分 · 画面差 {pair["delta_milli"]}‰ · 边缘差 {pair["edge_milli"]}‰'
                      f'</small></figcaption></figure>')
        else:
            detail = f'<p>{when}</p>'
    return f'<li class="pair {pair["verdict"]}">{head}{detail}</li>'


def show(key, mapping):
    base = f'/desk/boards/{key}'
    frames = ''.join(frame_figure(base, frame) for frame in mapping['frames'])
    pairs = ''.join(pair_item(base, pair) for pair in mapping['pairs'])
    return page('技术证据板', 'scan', 'board', scan=safe(mapping['scan_id']), hit=safe(mapping['hit_id']),
                kind=KINDS[mapping['kind']], span=interval(mapping),
                grid=mapping['grid_us'], long_step=mapping['long_step_us'],
                frames=frames, pairs=pairs)
