"""Show exact evidence mappings and browser-compatible views of packaged assets."""

from fractions import Fraction
from io import BytesIO
import json
from struct import pack
from zipfile import ZipFile
from zlib import compress, crc32

from ..clock import SECOND, InputError, Span
from ..media import HEIGHT, WIDTH
from ..review import find_hit
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
    scan = app.vault.get('scan', scan_id)
    hit = find_hit(scan, hit_id)
    mapping['hit'] = {'start_us': hit['start_us'], 'end_us': hit['end_us']}
    excerpt = Span(mapping['start_us'], mapping['end_us'])
    mapping['picture_coverage'] = [clipped.as_dict() for clipped in
                                   (Span.parse(span).intersect(excerpt) for span in scan['coverage']['picture'])
                                   if clipped is not None]
    return cache.put(mapping, assets)


def layer(mapping, css, start_us, end_us):
    duration = mapping['end_us'] - mapping['start_us']
    left = float((start_us - mapping['start_us']) * 100 / duration)
    width = float((end_us - start_us) * 100 / duration)
    return f'<span class="{css}" style="left:{left:.7f}%;width:{width:.7f}%"></span>'


def tick(mapping, css, us, title):
    duration = mapping['end_us'] - mapping['start_us']
    left = float((us - mapping['start_us']) * 100 / duration)
    return f'<span class="{css}" style="left:{left:.7f}%" title="{safe(title)}"></span>'


def player(key, mapping):
    track = mapping['sound']
    rate = track['sample_rate']
    base = f'/desk/previews/{key}'
    fills = ''.join(layer(mapping, 'lane-fill', Fraction(item['first'] * SECOND, rate),
                          Fraction(item['end'] * SECOND, rate))
                    for item in track['filled_sample_ranges'])
    covered = ''.join(layer(mapping, 'lane-ok', span['start_us'], span['end_us'])
                      for span in mapping['picture_coverage'])
    hit = mapping['hit']
    band = layer(mapping, 'seek-hit', max(hit['start_us'], mapping['start_us']),
                 min(hit['end_us'], mapping['end_us']))
    ticks = ''.join(tick(mapping, 'seek-frame', item['target_us'], f'画格 {index} · {timestamp(item["target_us"])}')
                    for index, item in enumerate(mapping['picture'], 1))
    clock = json.dumps({'rate': rate, 'first': track['first_source_sample'], 'end': track['end_source_sample'],
                        'span': [mapping['start_us'], mapping['end_us']],
                        'hit': [hit['start_us'], hit['end_us']],
                        'filled': [[item['first'], item['end']] for item in track['filled_sample_ranges']],
                        'picture': [[span['start_us'], span['end_us']] for span in mapping['picture_coverage']],
                        'frames': [[item['target_us'], item['source_us']] for item in mapping['picture']]},
                       separators=(',', ':'))
    duration = mapping['end_us'] - mapping['start_us']
    return (f'<div class="player" id="player">'
            f'<audio id="player-audio" preload="metadata" src="{base}/sound.wav"></audio>'
            f'<div class="transport">'
            f'<button type="button" id="player-toggle" class="primary">播放</button>'
            f'<button type="button" id="player-hit" title="定位到异常起点 {timestamp(hit["start_us"])}">回到异常</button>'
            f'<span class="playback-clock" id="player-clock"></span></div>'
            f'<div class="seekbar" id="player-seek" role="slider" tabindex="0" aria-label="试听进度，按节目微秒定位" '
            f'aria-valuemin="0" aria-valuemax="{duration}" aria-valuenow="0">'
            f'<div class="seek-lanes" aria-hidden="true">'
            f'<div class="seek-lane"><span class="lane-ok" style="left:0%;width:100%"></span>{fills}</div>'
            f'<div class="seek-lane void">{covered}</div>'
            f'<div class="seek-marks">{band}{ticks}</div>'
            f'</div><span class="seek-cursor" id="player-cursor"></span></div>'
            f'<div class="legend player-legend"><span><i class="key measured"></i>源内实测</span>'
            f'<span><i class="key filled"></i>摘片补零</span><span><i class="key void"></i>画面未覆盖</span>'
            f'<span><i class="key anomaly"></i>异常区间</span><span><i class="key grid"></i>画格采样点</span></div>'
            f'<dl class="cursor-facts">'
            f'<div><dt>节目时标</dt><dd id="fact-time"></dd></div>'
            f'<div><dt>伴音</dt><dd id="fact-sound"></dd></div>'
            f'<div><dt>画面</dt><dd id="fact-picture"></dd></div>'
            f'<div><dt>画格</dt><dd id="fact-frame"></dd></div></dl>'
            f'<p class="muted player-help">拖动进度条或点画格定位；方向键 ±1 毫秒，Shift+方向键 ±1 源采样，'
            f'PageUp/PageDown 相邻画格。暂停后读数保持为同一源采样坐标。</p>'
            f'<script type="application/json" id="evidence-clock">{clock}</script>'
            f'<script src="/desk/static/player.js" defer></script>'
            f'</div>')


def show(key, mapping):
    base = f'/desk/previews/{key}'
    audible = mapping['sound'] is not None
    frames = []
    for index, item in enumerate(mapping['picture'], 1):
        label = timestamp(item['target_us'])
        if item['file']:
            name = item['file'].removesuffix('.pgm') + '.png'
            picture = f'<img src="{base}/{name}" alt="证据画格">'
            fact = f'源帧 {timestamp(item["source_us"])}'
        else:
            picture = '<div class="missing-frame">无画格覆盖</div>'
            fact = '此采样点无源帧'
        action = (f'<button type="button" class="frame-play" data-target-us="{item["target_us"]}">试听此画格</button>'
                  if audible else '')
        frames.append(f'<figure data-frame="{index}">{picture}<figcaption><strong>{label}</strong>'
                      f'<small>{fact}</small>{action}</figcaption></figure>')
    track = mapping['sound']
    sound = '<p class="muted">未选伴音轨</p>'
    if track is not None:
        filled = ', '.join(f'[{item["first"]}, {item["end"]})' for item in track['filled_sample_ranges'])
        sound = (player(key, mapping)
                 + f'<p>{track["sample_rate"]} Hz · {track["channels"]} 声道 · 源采样 '
                 f'[{track["first_source_sample"]}, {track["end_source_sample"]})</p>'
                 f'<p>补零区间 <code>{filled or "无"}</code></p>')
    return page('证据预览', 'scan', 'preview', scan=safe(mapping['scan_id']),
                hit=safe(mapping['hit_id']), span=interval(mapping), frames=''.join(frames), sound=sound)
