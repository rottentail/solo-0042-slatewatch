"""Show exact evidence mappings and browser-compatible views of packaged assets."""

from array import array
from io import BytesIO
import json
from struct import pack
import sys
import wave
from zipfile import ZipFile
from zlib import compress, crc32

from .. import audition
from ..clock import InputError, SECOND, Span, sample_boundary
from ..evidence import write_wav
from ..media import HEIGHT, WIDTH
from ..rules import canonical
from .cache import PREVIEW_LIMIT
from .render import page
from .text import interval, options, safe, timestamp


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


def read_wav(data):
    with wave.open(BytesIO(data), 'rb') as source:
        pcm = array('h')
        pcm.frombytes(source.readframes(source.getnframes()))
        if sys.byteorder != 'little':
            pcm.byteswap()
        return pcm, source.getframerate(), source.getnchannels()


def create(app, cache, scan_id, hit_id, padding, frames, reference_id=None):
    selection = {'hit_ids': [hit_id], 'padding_ms': padding, 'frames': frames}
    raw = app.evidence(scan_id, selection)
    with ZipFile(BytesIO(raw)) as archive:
        if sum(item.file_size for item in archive.infolist()) > PREVIEW_LIMIT:
            raise InputError('摘片超过浏览器预览容量，请下载完整证据包。')
        mapping = json.loads(archive.read(f'{hit_id}/clock.json'))
        assets = {frame['file'].removesuffix('.pgm') + '.png': (png(archive.read(f'{hit_id}/{frame["file"]}')), 'image/png')
                  for frame in mapping['picture'] if frame['file']}
        native = archive.read(f'{hit_id}/sound.wav') if mapping['sound'] else None
    mapping['scan_id'] = scan_id
    mapping['padding_ms'] = padding
    mapping['frames'] = frames
    if reference_id is None:
        if native is not None:
            assets['sound.wav'] = (native, 'audio/wav')
    else:
        pair(app, scan_id, mapping, native, reference_id, assets)
    assets['clock.json'] = (canonical(mapping), 'application/json')
    return cache.put(mapping, assets)


def pair(app, scan_id, mapping, native_wav, reference_id, assets):
    """Prepare a same-span reference excerpt alongside the main mix for A/B listening."""
    if native_wav is None:
        raise InputError('此候选没有伴音证据，无法对听。')
    scan = app.vault.get('scan', scan_id)
    master = app.vault.get('master', scan['master_id'])
    reference = app.vault.get('master', reference_id)
    if reference['id'] == master['id']:
        raise InputError('参考轨需选用另一卷母带。')
    track = reference['media']['sound']
    if track is None:
        raise InputError('参考母带未选伴音轨。')
    main = mapping['sound']
    span = Span(mapping['start_us'], mapping['end_us'])
    rate = max(main['sample_rate'], track['rate'])
    first_out = sample_boundary(span.start, rate)
    out_count = sample_boundary(span.end, rate) - first_out
    estimate = sum(len(data) for data, _mime in assets.values())
    estimate += (44 + out_count * main['channels'] * 2) + (44 + out_count * track['channels'] * 2)
    if estimate > PREVIEW_LIMIT:
        raise InputError('对听摘片超过浏览器预览容量，请缩短留边或改用即时证据下载。')
    segment = audition.cut(app.vault.object_path(reference['sha256']), reference['media'], span)
    main_pcm, main_rate, main_channels = read_wav(native_wav)
    if rate == main_rate:
        main_out = main_pcm
    else:
        main_out = audition.convert(main_pcm, main_channels, main['first_source_sample'], main_rate,
                                    first_out, out_count, rate)
    if rate == segment.rate:
        reference_out = segment.pcm
    else:
        reference_out = audition.convert(segment.pcm, segment.channels, segment.first, segment.rate,
                                         first_out, out_count, rate)
    assets['main.wav'] = (write_wav(main_out, rate, main_channels), 'audio/wav')
    assets['reference.wav'] = (write_wav(reference_out, rate, segment.channels), 'audio/wav')
    mapping['audition'] = {
        'rate': rate,
        'main': {'file': 'main.wav', 'role': 'main', 'master_id': master['id'], 'label': master['label'],
                 'sha256': master['sha256'], 'stream_index': scan['tracks']['sound']['index'],
                 'source_rate': main['sample_rate'], 'channels': main['channels'],
                 'first_source_sample': main['first_source_sample'], 'end_source_sample': main['end_source_sample'],
                 'clock_offset_us': main['clock_offset_us'], 'filled_sample_ranges': main['filled_sample_ranges'],
                 'converted': rate != main_rate},
        'reference': {'file': 'reference.wav', 'role': 'reference', 'master_id': reference['id'],
                      'label': reference['label'], 'sha256': reference['sha256'],
                      'stream_index': track['index'], 'source_rate': segment.rate, 'channels': segment.channels,
                      'first_source_sample': segment.first, 'end_source_sample': segment.end,
                      'clock_offset_us': round(segment.first * SECOND / segment.rate),
                      'filled_sample_ranges': [{'first': s.start, 'end': s.end} for s in segment.filled],
                      'converted': rate != segment.rate},
    }


def filled_ranges(track):
    return ', '.join(f'[{item["first"]}, {item["end"]})' for item in track['filled_sample_ranges']) or '无'


def pair_section(base, mapping):
    box = mapping['audition']
    facts = []
    for role, name in (('main', '主混'), ('reference', '参考轨')):
        track = box[role]
        conversion = (f'试听转换 {track["source_rate"]} → {box["rate"]} Hz' if track['converted']
                      else '原采样率直达')
        facts.append(f'<dt>{name} · {safe(track["label"])}</dt><dd>流 {track["stream_index"]} · '
                     f'源采样 [{track["first_source_sample"]}, {track["end_source_sample"]}) @ '
                     f'{track["source_rate"]} Hz · {track["channels"]} 声道 · {conversion} · '
                     f'补零区间 <code>{filled_ranges(track)}</code><br>母带 <code>{track["sha256"]}</code></dd>')
    return (f'<section class="form-section" aria-label="主混与参考轨对听"><h2>主混 / 参考轨对听</h2>'
            f'<div class="audition" data-audition>'
            f'<p>正在放：<strong data-current>主混</strong> · 对听采样率 {box["rate"]} Hz · 切换保持节目位置，不用重新定位</p>'
            f'<audio controls preload="auto" data-name="主混" src="{base}/main.wav"></audio>'
            f'<audio controls preload="auto" data-name="参考轨" src="{base}/reference.wav"></audio>'
            f'<p><button type="button" data-switch>切换到参考轨</button> '
            f'<a href="{base}/clock.json">对听映射 clock.json</a></p></div>'
            f'<dl>{"".join(facts)}</dl>'
            f'<p class="muted">试听信号为顺畅切听可能经采样率转换，补零区间不是原声；'
            f'原速率主混证据请在复核页用「下载即时证据」。</p></section>')


def prepare_section(mapping, vault):
    scan = vault.get('scan', mapping['scan_id'])
    masters = [(item['id'], item['label']) for item in vault.recent('master')
               if item['media']['sound'] and item['id'] != scan['master_id']]
    if not masters:
        return ''
    return (f'<section class="form-section" aria-label="准备对听"><h2>准备主混 / 参考轨对听</h2>'
            f'<form method="post" action="/desk/scans/{safe(mapping["scan_id"])}/preview" class="actions">'
            f'<input name="hit" value="{safe(mapping["hit_id"])}" type="hidden">'
            f'<input name="padding_ms" value="{mapping["padding_ms"]}" type="hidden">'
            f'<input name="frames" value="{mapping["frames"]}" type="hidden">'
            f'<label>参考轨<select name="reference">{options(masters)}</select></label>'
            f'<button class="primary">准备对听</button></form>'
            f'<p class="muted">按同一节目区间切取参考轨短段，与主混一起留在本预览内；'
            f'两轨可能转换为统一试听采样率，原轨与原采样区间在映射中列出。</p></section>')


def show(key, mapping, vault):
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
    prepare = ''
    if 'audition' in mapping:
        sound = pair_section(base, mapping)
    elif track is not None:
        sound = (f'<section class="form-section" aria-label="原轨伴音"><h2>原轨伴音</h2>'
                 f'<audio controls preload="metadata" src="{base}/sound.wav"></audio>'
                 f'<p>{track["sample_rate"]} Hz · {track["channels"]} 声道 · 源采样 '
                 f'[{track["first_source_sample"]}, {track["end_source_sample"]})</p>'
                 f'<p>补零区间 <code>{filled_ranges(track)}</code> · '
                 f'<a href="{base}/clock.json">预览映射 clock.json</a></p></section>')
        prepare = prepare_section(mapping, vault)
    else:
        sound = '<section class="form-section" aria-label="原轨伴音"><h2>原轨伴音</h2><p class="muted">未选伴音轨</p></section>'
    return page('证据预览', 'scan', 'preview', scan=safe(mapping['scan_id']),
                hit=safe(mapping['hit_id']), span=interval(mapping), frames=''.join(frames),
                sound=sound, prepare=prepare)
