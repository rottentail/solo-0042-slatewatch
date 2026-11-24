"""Show exact evidence mappings and browser-compatible views of packaged assets."""

from io import BytesIO
import json
import re
from struct import pack
from zipfile import ZipFile
from zlib import compress, crc32

from ..clock import InputError, Missing
from ..media import HEIGHT, WIDTH
from .cache import PREVIEW_LIMIT
from .render import page
from .text import interval, safe, timestamp

# A preview is identified by its exact selection: scan, finding, padding and frame count.
# The token travels in the preview URL so an evicted entry can still be diagnosed.
TOKEN = re.compile(r'(scan-[0-9a-f]{24})\.(hit-[0-9a-f]{24})\.([0-9]{1,4})\.([0-9]{1,2})')


def token_for(scan_id, hit_id, padding_ms, frames):
    return f'{scan_id}.{hit_id}.{padding_ms}.{frames}'


def parse_token(token):
    match = TOKEN.fullmatch(token)
    if not match:
        if re.fullmatch(r'[0-9a-f]{32}', token):
            raise Missing('此预览链接来自旧版，未携带摘片参数，无法核对恢复条件，请从候选复核页重新摘片。')
        raise Missing('预览链接不完整，无法核对恢复条件。')
    scan_id, hit_id, padding, frames = match.groups()
    padding, frames = int(padding), int(frames)
    if not 0 <= padding <= 2000 or not 1 <= frames <= 8:
        raise Missing('预览链接中的摘片参数超出允许范围。')
    return scan_id, hit_id, padding, frames


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
    key = token_for(scan_id, hit_id, padding, frames)
    try:
        cache.get(key)
        return key
    except Missing:
        pass
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
    return cache.put(key, mapping, assets)


def show(key, mapping):
    _scan_id, _hit_id, padding, frames = parse_token(key)
    base = f'/desk/previews/{key}'
    shots = []
    for item in mapping['picture']:
        label = timestamp(item['target_us'])
        if item['file']:
            name = item['file'].removesuffix('.pgm') + '.png'
            picture = f'<img src="{base}/{name}" alt="证据画格">'
            fact = f'源帧 {timestamp(item["source_us"])}'
        else:
            picture = '<div class="missing-frame">无画格覆盖</div>'
            fact = '此采样点无源帧'
        shots.append(f'<figure>{picture}<figcaption><strong>{label}</strong><small>{fact}</small></figcaption></figure>')
    track = mapping['sound']
    sound = '<p class="muted">未选伴音轨</p>'
    if track is not None:
        filled = ', '.join(f'[{item["first"]}, {item["end"]})' for item in track['filled_sample_ranges'])
        sound = (f'<audio controls preload="metadata" src="{base}/sound.wav"></audio>'
                 f'<p>{track["sample_rate"]} Hz · {track["channels"]} 声道 · 源采样 '
                 f'[{track["first_source_sample"]}, {track["end_source_sample"]})</p>'
                 f'<p>补零区间 <code>{filled or "无"}</code></p>')
    return page('证据预览', 'scan', 'preview', scan=safe(mapping['scan_id']), padding=padding, frames=frames,
                hit=safe(mapping['hit_id']), span=interval(mapping), shots=''.join(shots), sound=sound)
