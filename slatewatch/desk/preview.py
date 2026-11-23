"""Show exact evidence mappings and browser-compatible views of packaged assets."""

from io import BytesIO
import json
from struct import pack
from zipfile import ZipFile
from zlib import compress, crc32

from ..clock import InputError
from ..media import HEIGHT, WIDTH
from .cache import PREVIEW_LIMIT
from .render import page
from .text import interval, safe, timestamp


def png(pgm):
    prefix = f'P5\n{WIDTH} {HEIGHT}\n255\n'.encode()
    if not pgm.startswith(prefix) or len(pgm) != len(prefix) + WIDTH * HEIGHT:
        raise InputError('无法显示此证据画格。')
    pixels = pgm[len(prefix):]

    def chunk(name, payload):
        return pack('!I', len(payload)) + name + payload + pack('!I', crc32(name + payload))

    rows = b''.join(b'\0' + pixels[i:i + WIDTH] for i in range(0, len(pixels), WIDTH))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', pack('!2I5B', WIDTH, HEIGHT, 8, 0, 0, 0, 0))
            + chunk(b'IDAT', compress(rows)) + chunk(b'IEND', b''))


def create(app, cache, scan_id, hit_id, padding, frames):
    selection = {'hit_ids': [hit_id], 'padding_ms': padding, 'frames': frames}
    raw = app.evidence(scan_id, selection)
    with ZipFile(BytesIO(raw)) as archive:
        if sum(item.file_size for item in archive.infolist()) > PREVIEW_LIMIT:
            raise InputError('摘片超过浏览器预览容量，请下载完整证据包。')
        mapping = json.loads(archive.read(f'{hit_id}/clock.json'))
        assets = {frame['file'].removesuffix('.pgm') + '.png': (png(archive.read(f'{hit_id}/{frame["file"]}')), 'image/png')
                  for frame in mapping['picture'] if frame['file']}
        if mapping['sound']:
            assets['sound.wav'] = (archive.read(f'{hit_id}/sound.wav'), 'audio/wav')
    mapping['scan_id'] = scan_id
    return cache.put(mapping, assets)


def show(key, mapping):
    base = f'/desk/previews/{key}'
    frames = []
    for item in mapping['picture']:
        label = timestamp(item['target_us'])
        if item['file']:
            name = item['file'].removesuffix('.pgm') + '.png'
            picture = f'<img src="{base}/{name}" alt="证据画格">'
            fact = f'源帧 {timestamp(item["source_us"])}'
        else:
            picture = '<div class="missing-frame">无画格覆盖</div>'
            fact = '此采样点无源帧'
        frames.append(f'<figure>{picture}<figcaption><strong>{label}</strong><small>{fact}</small></figcaption></figure>')
    track = mapping['sound']
    sound = '<p class="muted">未选伴音轨</p>'
    if track is not None:
        filled = ', '.join(f'[{item["first"]}, {item["end"]})' for item in track['filled_sample_ranges'])
        sound = (f'<audio controls preload="metadata" src="{base}/sound.wav"></audio>'
                 f'<p>{track["sample_rate"]} Hz · {track["channels"]} 声道 · 源采样 '
                 f'[{track["first_source_sample"]}, {track["end_source_sample"]})</p>'
                 f'<p>补零区间 <code>{filled or "无"}</code></p>')
    return page('证据预览', 'scan', 'preview', scan=safe(mapping['scan_id']),
                hit=safe(mapping['hit_id']), span=interval(mapping), frames=''.join(frames), sound=sound)
