"""Render the independent incident timeline with track-level attribution."""

from ..incidents import CLEAR, INTERRUPTED, UNMEASURED
from .render import fragment
from .text import safe, timestamp
from .timeline import geometry

SUPPORT = {'joint': '共同中断', 'picture': '仅画面', 'sound': '仅伴音'}
STATE = {INTERRUPTED: '中断', CLEAR: '正常', UNMEASURED: '未实测'}
BASIS = {'black': '实测黑场', 'silence': '实测零声',
         'picture_gap': '画面无实测覆盖', 'sound_gap': '伴音无实测覆盖'}


def evidence(scan_id, item):
    basis = BASIS[item['kind']]
    if item['channel'] is not None:
        basis += f' · 声道 {item["channel"]}'
    if not item['measured'] and item['below_threshold']:
        basis += '（低于候选门槛，无候选）'
    links = '、'.join(
        f'<a href="/desk/scans/{scan_id}?hit={hit}#finding" title="回到候选 {safe(hit)}">{safe(hit[-7:])}</a>'
        for hit in item['hit_ids']
    )
    return basis + (f' · 候选 {links}' if links else '')


def row(scan_id, segment):
    states = ''.join(
        f'<td><span class="lane-state {segment["lanes"][lane]}">{STATE[segment["lanes"][lane]]}</span></td>'
        for lane in ('picture', 'sound')
    )
    basis = '；'.join(evidence(scan_id, item) for item in segment['evidence'])
    return (f'<tr><td class="time">{timestamp(segment["start_us"])} – {timestamp(segment["end_us"])}</td>'
            f'<td><span class="support {segment["support"]}">{SUPPORT[segment["support"]]}</span></td>'
            f'{states}<td>{basis}</td></tr>')


def show(scan, incidents):
    duration = scan['extent']['end_us']
    counts = incidents['counts']
    summary = (f'共同中断 {counts["joint"]} 段 · 仅画面 {counts["picture"]} 段 · '
               f'仅伴音 {counts["sound"]} 段')
    if not incidents['sound_selected']:
        summary += ' · 伴音未选轨，伴音侧全程未实测'
    segments = incidents['segments']
    if not segments:
        rows = '<tr><td colspan="5">没有事故分段。</td></tr>'
        strip = ''
    else:
        rows = ''.join(row(scan['id'], segment) for segment in segments)
        strip = ''.join(
            f'<span class="seg {segment["support"]}" style="{geometry(segment, duration)}" '
            f'title="{SUPPORT[segment["support"]]} · {timestamp(segment["start_us"])}"></span>'
            for segment in segments
        )
    return fragment('incidents', summary=safe(summary), strip=strip, rows=rows)
