"""Project recorded coverage and findings on one programme microsecond scale."""

from .render import fragment
from .text import KINDS, lane, safe, timestamp


def geometry(span, duration):
    width = 100 * (span['end_us'] - span['start_us']) / duration
    left = 100 * span['start_us'] / duration
    return f'left:{left:.7f}%;width:{width:.7f}%'


def strip(name, coverage, hits, duration, scan_id, selected):
    if coverage is None:
        layers = '<span class="unselected">未选轨</span>'
    else:
        layers = ''.join(f'<span class="covered" style="{geometry(span, duration)}"></span>' for span in coverage)
    for hit in hits:
        status = ' current' if hit['id'] == selected else ''
        title = f'{KINDS[hit["kind"]]} · {timestamp(hit["start_us"])}'
        layers += (f'<a class="mark {hit["kind"]}{status}" style="{geometry(hit, duration)}" '
                   f'href="/desk/scans/{scan_id}?hit={hit["id"]}#finding" title="{title}" '
                   f'aria-label="{title}"></a>')
    return f'<div class="lane"><span>{name}</span><div class="strip">{layers}</div></div>'


def draw(scan, selected):
    duration = scan['extent']['end_us']
    if duration <= 0:
        return '<p class="muted">扫描没有有效节目时段。</p>'
    sources = [('画面', scan['coverage']['picture'])]
    sound = scan['tracks']['sound']
    if sound:
        sources += [(f'伴音 {i}', scan['coverage']['sound']) for i in range(1, sound['channels'] + 1)]
        if any(hit['kind'] in ('sound_gap', 'duplicate') for hit in scan['hits']):
            sources.append(('伴音', scan['coverage']['sound']))
    else:
        sources.append(('伴音', None))
    lanes = ''.join(strip(name, coverage, [hit for hit in scan['hits'] if lane(hit) == name],
                          duration, scan['id'], selected) for name, coverage in sources)
    ticks = ''.join(f'<span>{safe(timestamp(duration * i // 4)[:-3])}</span>' for i in range(5))
    return fragment('timeline', ticks=ticks, lanes=lanes)
