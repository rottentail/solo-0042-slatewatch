"""Show exact evidence mappings and browser-compatible views of packaged assets."""

from fractions import Fraction
from io import BytesIO
import json
from struct import pack
from zipfile import ZipFile
from zlib import compress, crc32

from ..clock import SECOND, InputError
from ..media import HEIGHT, WIDTH
from ..review import find_hit
from ..rules import canonical
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
            assets['clock.json'] = (canonical(mapping), 'application/json')
    hit = find_hit(app.vault.get('scan', scan_id), hit_id)
    mapping['scan_id'] = scan_id
    mapping['hit_span'] = {'start_us': hit['start_us'], 'end_us': hit['end_us']}
    return cache.put(mapping, assets)


def moment(sample, rate):
    return timestamp(round(Fraction(sample * SECOND, rate)))


def frames(mapping, base, linked):
    figures = []
    for index, item in enumerate(mapping['picture']):
        label = timestamp(item['target_us'])
        if item['file']:
            name = item['file'].removesuffix('.pgm') + '.png'
            picture = f'<img src="{base}/{name}" alt="证据画格">'
            fact = f'源帧 {timestamp(item["source_us"])}'
        else:
            picture = '<div class="missing-frame">无画格覆盖</div>'
            fact = '此采样点无源帧'
        if linked:
            picture = f'<a href="#waveform" data-frame-link title="在原轨证据图中跳回它真正所处的时刻">{picture}</a>'
        source = '' if item['source_us'] is None else item['source_us']
        figures.append(f'<figure data-frame="{index}" data-target-us="{item["target_us"]}" data-source-us="{source}">'
                       f'{picture}<figcaption><strong>{label}</strong><small>{fact}</small></figcaption></figure>')
    return ''.join(figures)


def boundaries(track):
    rows = []
    for item in track['filled_sample_ranges']:
        rows.append(f'<tr><td class="time">{item["first"]}</td><td class="time">{item["end"]}</td>'
                    f'<td class="time">{moment(item["first"], track["sample_rate"])} – '
                    f'{moment(item["end"], track["sample_rate"])}</td>'
                    f'<td>{item["end"] - item["first"]} 采样</td>'
                    f'<td class="jumps"><button type="button" data-jump-sample="{item["first"]}">跳到起点</button>'
                    f'<button type="button" data-jump-sample="{item["end"]}">跳到终点</button></td></tr>')
    if not rows:
        return '<p class="muted">无 —— 摘片内低电平均为源内实录，不是覆盖洞补零。</p>'
    return ('<div class="table-wrap"><table class="boundaries"><thead><tr>'
            '<th>源采样起点</th><th>源采样终点</th><th>节目时刻</th><th>长度</th><th>定位</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def sound(mapping, base):
    track = mapping['sound']
    if track is None:
        return '<p class="muted">未选伴音轨</p>'
    span = mapping['hit_span']
    return (f'<audio controls preload="metadata" src="{base}/sound.wav" id="evidence-audio"></audio>'
            f'<p>{track["sample_rate"]} Hz · {track["channels"]} 声道 · 源采样 '
            f'[{track["first_source_sample"]}, {track["end_source_sample"]})</p>'
            '<h3>原轨证据图</h3>'
            f'<div class="wave" id="waveform" data-base="{safe(base)}" '
            f'data-hit-start="{span["start_us"]}" data-hit-end="{span["end_us"]}">'
            '<div class="wave-toolbar">'
            '<button type="button" data-zoom="in">放大</button>'
            '<button type="button" data-zoom="out">缩小</button>'
            '<button type="button" data-zoom="fit">全部</button>'
            '<span class="wave-window muted" data-window>原轨证据图加载中…</span>'
            '</div>'
            '<canvas tabindex="0" role="img" aria-label="可放缩的原轨证据图，红色区间为补零覆盖洞"></canvas>'
            '<p class="wave-legend muted">橙带 候选区间 · 红带 补零覆盖洞 · 白线 光标 · 黄线 播放位置</p>'
            '<p class="wave-readout" data-cursor aria-live="polite">点击波形定位源采样并回跳节目时刻，滚轮放缩，拖动平移。</p>'
            '<p class="wave-readout muted" data-playhead></p>'
            '</div>'
            '<noscript><p class="muted">启用 JavaScript 可放缩原轨证据图；补零边界仍可按源采样查阅下表。</p></noscript>'
            '<h3>补零区间（覆盖空洞，区别于源内实录静音）</h3>'
            f'{boundaries(track)}'
            '<script src="/desk/static/waveform.js" defer></script>')


def show(key, mapping):
    base = f'/desk/previews/{key}'
    return page('证据预览', 'scan', 'preview', scan=safe(mapping['scan_id']),
                hit=safe(mapping['hit_id']), span=interval(mapping),
                frames=frames(mapping, base, linked=mapping['sound'] is not None),
                sound=sound(mapping, base))
