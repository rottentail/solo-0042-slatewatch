"""Show exact evidence mappings and browser-compatible views of packaged assets."""

from io import BytesIO
import json
from struct import pack
import wave
from zipfile import ZipFile
from zlib import compress, crc32

from ..clock import SECOND, InputError, sample_boundary
from ..media import HEIGHT, WIDTH
from .cache import PREVIEW_LIMIT
from .render import fragment, page
from .text import KINDS, interval, lane, safe, timestamp


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


def shares(sizes, total):
    """Split a byte budget so panels already under an even share keep their size."""
    result = [0] * len(sizes)
    remaining, waiting = total, list(range(len(sizes)))
    while waiting:
        share = remaining // len(waiting)
        small = [index for index in waiting if sizes[index] <= share]
        if not small:
            for index in waiting:
                result[index] = share
            break
        for index in small:
            result[index] = sizes[index]
            remaining -= sizes[index]
        waiting = [index for index in waiting if index not in small]
    return result


def window(hit, track, budget):
    """Keep at most budget source samples centered on the finding itself."""
    first, end = track['first_source_sample'], track['end_source_sample']
    if end - first <= budget:
        return first, end
    rate = track['sample_rate']
    hit_first = sample_boundary(hit['start_us'], rate)
    hit_end = sample_boundary(hit['end_us'], rate)
    extra = budget - (hit_end - hit_first)
    start = hit_first - extra // 2 if extra >= 0 else (hit_first + hit_end - budget) // 2
    start = min(max(start, first), end - budget)
    return start, start + budget


def cut_wav(data, offset, count):
    with wave.open(BytesIO(data), 'rb') as source:
        rate, channels = source.getframerate(), source.getnchannels()
        source.setpos(offset)
        payload = source.readframes(count)
    out = BytesIO()
    with wave.open(out, 'wb') as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(payload)
    return out.getvalue()


def trim(hit, mapping, assets, budget):
    """Bound one panel's online sound, recording the source range left offline."""
    size = sum(len(data) for data, _mime in assets.values())
    if size <= budget:
        return
    track = mapping['sound']
    name = f"{mapping['hit_id']}-sound.wav"
    if track is None or name not in assets:
        return
    wav, mime = assets[name]
    first, end = track['first_source_sample'], track['end_source_sample']
    header = len(wav) - (end - first) * track['channels'] * 2
    room = budget - (size - len(wav)) - header
    samples = room // (track['channels'] * 2)
    if samples >= end - first:
        return
    if samples < 1:
        del assets[name]
        track['file'] = None
        track['dropped_sample_ranges'] = [{'first': first, 'end': end}]
        return
    keep_first, keep_end = window(hit, track, samples)
    assets[name] = (cut_wav(wav, keep_first - first, keep_end - keep_first), mime)
    track['filled_sample_ranges'] = [
        {'first': max(item['first'], keep_first), 'end': min(item['end'], keep_end)}
        for item in track['filled_sample_ranges']
        if max(item['first'], keep_first) < min(item['end'], keep_end)]
    track['dropped_sample_ranges'] = [
        {'first': begin, 'end': stop}
        for begin, stop in ((first, keep_first), (keep_end, end)) if begin < stop]
    track['first_source_sample'] = keep_first
    track['end_source_sample'] = keep_end
    track['clock_offset_us'] = round(keep_first * SECOND / track['sample_rate'])


def create(app, cache, scan_id, hit_ids, padding, frames):
    scan = app.vault.get('scan', scan_id)
    findings = {hit['id']: hit for hit in scan['hits']}
    ordered = sorted(hit_ids, key=lambda key: findings.get(key, {}).get('start_us', 0))
    raw = app.evidence(scan_id, {'hit_ids': ordered, 'padding_ms': padding, 'frames': frames})
    panels = []
    with ZipFile(BytesIO(raw)) as archive:
        for hit_id in ordered:
            mapping = json.loads(archive.read(f'{hit_id}/clock.json'))
            assets = {f"{hit_id}-{frame['file'].removesuffix('.pgm')}.png":
                      (png(archive.read(f'{hit_id}/{frame["file"]}')), 'image/png')
                      for frame in mapping['picture'] if frame['file']}
            if mapping['sound']:
                assets[f'{hit_id}-sound.wav'] = (archive.read(f'{hit_id}/sound.wav'), 'audio/wav')
            panels.append({'hit': findings[hit_id], 'mapping': mapping, 'assets': assets})
    sizes = [sum(len(data) for data, _mime in panel['assets'].values()) for panel in panels]
    for panel, budget in zip(panels, shares(sizes, PREVIEW_LIMIT)):
        trim(panel['hit'], panel['mapping'], panel['assets'], budget)
    session = {'scan_id': scan_id, 'padding_ms': padding, 'frames': frames,
               'panels': [{'hit': panel['hit'], 'mapping': panel['mapping']} for panel in panels]}
    assets = {name: content for panel in panels for name, content in panel['assets'].items()}
    return cache.put(session, assets)


def sound(base, hit_id, track):
    if track is None:
        return '<p class="muted">未选伴音轨</p>'
    rate = track['sample_rate']
    parts = []
    if track.get('file'):
        parts.append(f'<audio controls preload="metadata" src="{base}/{hit_id}-sound.wav"></audio>')
    filled = ', '.join(f'[{item["first"]}, {item["end"]})' for item in track['filled_sample_ranges'])
    parts.append(f'<p>{rate} Hz · {track["channels"]} 声道 · 源采样 '
                 f'[{track["first_source_sample"]}, {track["end_source_sample"]})</p>'
                 f'<p>补零区间 <code>{filled or "无"}</code></p>')
    dropped = track.get('dropped_sample_ranges')
    if dropped:
        spans = '；'.join(
            f'源采样 [{item["first"]}, {item["end"]})（节目时间 '
            f'{timestamp(round(item["first"] * SECOND / rate))} – '
            f'{timestamp(round(item["end"] * SECOND / rate))}）'
            for item in dropped)
        parts.append(f'<p class="warn">超出在线预览容量，无法在线播放：{spans}。完整伴音请下载即时证据。</p>')
    return ''.join(parts)


def panel(base, scan_id, entry):
    hit, mapping = entry['hit'], entry['mapping']
    hit_id = hit['id']
    frames = []
    for item in mapping['picture']:
        label = timestamp(item['target_us'])
        if item['file']:
            name = f"{hit_id}-{item['file'].removesuffix('.pgm')}.png"
            picture = f'<img src="{base}/{name}" alt="证据画格">'
            fact = f'源帧 {timestamp(item["source_us"])}'
        else:
            picture = '<div class="missing-frame">无画格覆盖</div>'
            fact = '此采样点无源帧'
        frames.append(f'<figure>{picture}<figcaption><strong>{label}</strong><small>{fact}</small></figcaption></figure>')
    return fragment('panel', scan=safe(scan_id), hit=safe(hit_id), kind=KINDS[hit['kind']],
                    lane=lane(hit), span=interval(hit), excerpt=interval(mapping),
                    frames=''.join(frames), sound=sound(base, hit_id, mapping['sound']))


def show(key, session):
    base = f'/desk/previews/{key}'
    scan_id = session['scan_id']
    panels = ''.join(panel(base, scan_id, entry) for entry in session['panels'])
    download = ''.join(f'<input name="hit" value="{safe(entry["hit"]["id"])}" type="hidden">'
                       for entry in session['panels'])
    download += (f'<input name="padding_ms" value="{session["padding_ms"]}" type="hidden">'
                 f'<input name="frames" value="{session["frames"]}" type="hidden">')
    return page('证据预览', 'scan', 'preview', scan=safe(scan_id), count=len(session['panels']),
                panels=panels, download=download)
