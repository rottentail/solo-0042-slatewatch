"""Project recorded coverage and findings on one programme microsecond scale.

The timeline keeps two reading levels.  The overview always fits the whole
programme: findings whose projected spans fall under one reference pixel are
folded into an explicit cluster so the reviewer can see that several reasons
share a screen position.  A zoomed window renders every finding against its
real microsecond coverage, with no minimum-width expansion, so adjacent holes
and same-instant candidates on different lanes become separately selectable.
Text descriptions always carry full microsecond precision regardless of the
window width; only the geometric projection changes with zoom.
"""

from urllib.parse import urlencode

from ..clock import InputError
from .render import fragment
from .text import KINDS, lane, safe, timestamp

# Geometry is rendered against a virtual overview width, never the physical
# screen, so clustering is stable across window sizes.
REFERENCE_PIXELS = 1000
TOUCH_PIXELS = 10


def geometry(span, duration, origin=0):
    start = span['start_us'] - origin
    width = span['end_us'] - span['start_us']
    left = 100 * start / duration
    return f'left:{left:.9f}%;width:{100 * width / duration:.9f}%'


def read_window(params, duration):
    """Parse an optional zoom=start_us:end_us window for the programme clock."""
    raw = params.get('zoom', '').strip()
    if not raw:
        return None
    try:
        start_text, end_text = raw.split(':', 1)
        start, end = int(start_text), int(end_text)
    except (ValueError, TypeError) as exc:
        raise InputError('缩放窗口必须是 start_us:end_us 两个整数微秒。') from exc
    if start < 0 or end > duration or start >= end:
        raise InputError('缩放窗口超出节目时标范围或为空。')
    return start, end


def window_url(scan_id, start, end, hit=''):
    query = {'zoom': f'{start}:{end}'}
    if hit:
        query['hit'] = hit
    return f'/desk/scans/{scan_id}?' + urlencode(query) + '#timeline'


def hit_url(scan_id, hit_id, window=None):
    query = {'hit': hit_id}
    if window is not None:
        query['zoom'] = f'{window[0]}:{window[1]}'
    return f'/desk/scans/{scan_id}?' + urlencode(query) + '#finding'


def cluster_window(union, duration):
    """Open a window that places the clustered reasons in its middle third."""
    start, end = union
    padding = max(end - start, 1)
    return max(0, start - padding), min(duration, end + padding)


def fold(findings):
    """Group same-lane findings whose touch targets merge on the overview."""
    ordered = sorted(findings, key=lambda item: item['start_us'])
    groups, current = [], []

    def flush():
        if current:
            groups.append(list(current))
            current.clear()

    for item in ordered:
        if current and item['start_us'] >= current[-1]['_touch_end']:
            flush()
        current.append(item)
    flush()
    return groups


def touch_span(hit, duration):
    start = max(0, hit['start_us'] - TOUCH_PIXELS * duration // (2 * REFERENCE_PIXELS))
    end = min(duration, hit['end_us'] + TOUCH_PIXELS * duration // (2 * REFERENCE_PIXELS))
    return start, end


def cluster_mark(scan_id, group, duration):
    start = min(item['start_us'] for item in group)
    end = max(item['end_us'] for item in group)
    union = {'start_us': start, 'end_us': end}
    window = cluster_window((start, end), duration)
    reasons = '、'.join(KINDS[item['kind']] for item in group)
    title = f'{len(group)} 个事由（{reasons}）· {timestamp(start)} 起'
    return (f'<a class="mark cluster" style="{geometry(union, duration)}" '
            f'href="{window_url(scan_id, *window)}" title="{safe(title)}" aria-label="{safe(title)}">'
            f'<b>{len(group)}</b></a>')


def single_mark(scan_id, hit, duration, selected, window):
    status = ' current' if hit['id'] == selected else ''
    title = f'{KINDS[hit["kind"]]} · {timestamp(hit["start_us"])} – {timestamp(hit["end_us"])}'
    origin = window[0] if window else 0
    span_duration = window[1] - window[0] if window else duration
    return (f'<a class="mark {hit["kind"]}{status}" style="{geometry(hit, span_duration, origin)}" '
            f'href="{hit_url(scan_id, hit["id"], window)}" title="{safe(title)}" aria-label="{safe(title)}"></a>')


def strip(name, coverage, hits, duration, scan_id, selected, window):
    if coverage is None:
        layers = '<span class="unselected">未选轨</span>'
    else:
        origin = window[0] if window else 0
        span_duration = window[1] - window[0] if window else duration
        visible = [span for span in coverage
                   if span['end_us'] > origin and span['start_us'] < origin + span_duration]
        layers = ''.join(
            f'<span class="covered" style="{geometry(span, span_duration, origin)}"></span>'
            for span in visible
        )
    if window is None:
        prepared = [{**item, '_touch_end': touch_span(item, duration)[1]} for item in hits]
        for group in fold(prepared):
            layers += (single_mark(scan_id, group[0], duration, selected, None)
                       if len(group) == 1 else cluster_mark(scan_id, group, duration))
    else:
        for hit in hits:
            layers += single_mark(scan_id, hit, duration, selected, window)
    return f'<div class="lane"><span>{name}</span><div class="strip">{layers}</div></div>'


def ruler_ticks(start, end):
    return ''.join(
        f'<span>{safe(timestamp(start + (end - start) * i // 4))}</span>' for i in range(5)
    )


def zoom_controls(scan_id, window, duration):
    start, end = window
    full = start == 0 and end == duration
    half = max((end - start) // 2, 1)
    centre = (start + end) // 2
    in_start, in_end = max(0, centre - half // 2), min(duration, centre + half // 2)
    if in_end - in_start < 1:
        in_start, in_end = start, end
    out_start, out_end = max(0, centre - (end - start)), min(duration, centre + (end - start))
    in_link = (window_url(scan_id, in_start, in_end) if (in_start, in_end) != (start, end) else '')
    out_link = (window_url(scan_id, out_start, out_end) if not full else '')

    def tool(label, href):
        return f'<a class="button" href="{href}">{label}</a>' if href else f'<span class="button disabled" aria-disabled="true">{label}</span>'

    return fragment(
        'zoombar',
        in_button=tool('放大', in_link),
        out_button=tool('缩小', out_link),
        overview_href=window_url(scan_id, 0, duration),
        window=safe(f'{timestamp(start)} – {timestamp(end)}'),
    )


def draw(scan, selected, params):
    duration = scan['extent']['end_us']
    if duration <= 0:
        return '<p class="muted">扫描没有有效节目时段。</p>'
    window = read_window(params, duration)
    sources = [('画面', scan['coverage']['picture'])]
    sound = scan['tracks']['sound']
    if sound:
        sources += [(f'伴音 {i}', scan['coverage']['sound']) for i in range(1, sound['channels'] + 1)]
        if any(hit['kind'] == 'sound_gap' for hit in scan['hits']):
            sources.append(('伴音', scan['coverage']['sound']))
    else:
        sources.append(('伴音', None))
    lanes = ''.join(strip(name, coverage, [hit for hit in scan['hits'] if lane(hit) == name],
                          duration, scan['id'], selected, window) for name, coverage in sources)
    if window is None:
        ticks = ruler_ticks(0, duration)
        controls = '<p class="zoomstate">总览：同点多事由以编号簇标记，点入后按真实覆盖精查。</p>'
    else:
        ticks = ruler_ticks(*window)
        controls = zoom_controls(scan['id'], window, duration)
    return fragment('timeline', ticks=ticks, lanes=lanes, controls=controls, zoomed=' zoomed' if window else '')
