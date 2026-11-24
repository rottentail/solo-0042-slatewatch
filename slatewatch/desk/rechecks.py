"""Render window-by-window rechecks of earlier clipping findings on a later version."""

from ..clock import InputError, Missing, integer
from ..recheck import CLEARABLE, INSUFFICIENT, LABELS, STILL_CLIPPED, window_spans
from . import library
from .render import fragment, page
from .text import interval, lane, safe, timestamp

RECHECK_BADGE = {CLEARABLE: 'clear', STILL_CLIPPED: 'clipped', INSUFFICIENT: 'thin'}
SEPARATOR = '::'


def badge(status):
    return f'<span class="state {RECHECK_BADGE[status]}">{LABELS[status]}</span>'


def split_choice(value):
    if SEPARATOR not in value:
        raise InputError('请选择一条前版削顶候选。')
    before, hit = value.split(SEPARATOR, 1)
    if not before or not hit:
        raise InputError('请选择一条前版削顶候选。')
    return before, hit


def clip_choices(app, before_id='', hit_id=''):
    """Every earlier clipping finding, so one later version can switch between them."""
    seen, rows = set(), []
    scans = app.vault.recent('scan', limit=40)
    if before_id and all(scan['id'] != before_id for scan in scans):
        extra = app.vault.get('scan', before_id, required=False)
        scans = [*scans, extra] if extra else scans
    for scan in scans:
        for hit in scan['hits']:
            if hit['kind'] != 'clip':
                continue
            value = f'{scan["id"]}{SEPARATOR}{hit["id"]}'
            if value in seen:
                continue
            seen.add(value)
            mark = ' selected' if scan['id'] == before_id and hit['id'] == hit_id else ''
            name = f'{library.label(app.vault, scan)} · {lane(hit)} · {interval(hit)}'
            rows.append(f'<option value="{safe(value)}"{mark}>{safe(name)}</option>')
    return ''.join(rows)


def history(app, after_id, active=''):
    rows = []
    for record in app.rechecks(after_id)['rechecks']:
        earlier = app.vault.get('scan', record['before_scan'], required=False)
        title = library.label(app.vault, earlier) if earlier else record['before_scan']
        current = ' aria-current="true"' if record['id'] == active else ''
        first = record['windows'][0] if record['windows'] else {'start_us': 0, 'end_us': 0}
        rows.append(
            f'<a class="recheck-item"{current} href="/desk/scans/{safe(after_id)}/recheck?'
            f'recheck={safe(record["id"])}#recheck">'
            f'<strong>{safe(title)}</strong>'
            f'<small>伴音 {record["after_channel"]} · {timestamp(first["start_us"])} – '
            f'{timestamp(first["end_us"])}</small>'
            f'<small>{badge(record["status"])}</small></a>')
    return fragment('recheck_history', items=''.join(rows) or '<p class="muted">尚无核销记录</p>')


def window_rows(record):
    rows = []
    for window in record['windows']:
        permille = '—' if window['clip_permille'] is None else f'{window["clip_permille"]}‰'
        rows.append(
            f'<tr><td>{window["index"] + 1}</td><td class="time">{interval(window)}</td>'
            f'<td>{window["covered_samples"]} / {window["window_samples"]}</td>'
            f'<td>{window["clipped_samples"]}</td><td>{permille}</td>'
            f'<td>{badge(window["status"])}</td>'
            f'<td><a href="{safe(window["replay"])}">复听</a></td></tr>')
    return ''.join(rows)


def group_rows(record):
    return ''.join(
        f'<tr><td>{badge(group["status"])}</td><td class="time">{timestamp(group["start_us"])} – '
        f'{timestamp(group["end_us"])}</td><td>{group["windows"]} 窗</td>'
        f'<td><a href="{safe(group["replay"])}">复听此范围</a></td></tr>'
        for group in record['groups']) or '<tr><td colspan="4">没有可核销的测量窗。</td></tr>'


def measurement_note(app, record):
    """Say whether this record's later measurement is shared with another recheck."""
    signature = record['measurement']['signature']
    peers = [item['id'] for item in app.rechecks(record['after_scan'])['rechecks']
             if item['measurement']['signature'] == signature]
    if len(peers) > 1:
        return f'与另外 {len(peers) - 1} 条旧单共用同一次后版复测'
    return '本次新测后版伴音'


def result(app, record, after_scan):
    lane_note = (f'前版伴音 {record["before_channel"]} → 后版伴音 {record["after_channel"]}'
                 f'（后版源流 {record["measurement"]["sound_index"]}，'
                 f'{record["measurement"]["sample_rate"]} Hz）')
    alignment = record['alignment']
    alignment_note = ('等长换轨' if alignment['equal_length']
                      else f'长度相差 {alignment["delta_us"]} 微秒')
    counts = record['counts']
    note = (f'{record["reviewer"]} · {record["reason"]}' if record['reviewer']
            else '尚未记录复听人')
    return fragment('recheck_result', key=safe(record['id']), hit=safe(record['before_hit_id']),
                    status=badge(record['status']), lane=safe(lane_note),
                    alignment=safe(alignment_note), reuse=safe(measurement_note(app, record)),
                    window_us=record['window_us'] // 1000,
                    clearable=counts[CLEARABLE], clipped=counts[STILL_CLIPPED],
                    thin=counts[INSUFFICIENT], groups=group_rows(record),
                    windows=window_rows(record), note=safe(note),
                    before_scan=safe(record['before_scan']))


def plan(app, before_scan, hit, after_scan, channel):
    """Show the earlier finding's own windows before any later measurement is taken."""
    spans = window_spans(before_scan, hit)
    step_us = before_scan['thresholds']['audio_window_ms'] * 1000
    rows = ''.join(
        f'<tr><td>{index + 1}</td><td class="time">{timestamp(start)} – {timestamp(end)}</td>'
        f'<td>沿用前版第 {index + 1} 窗</td></tr>'
        for index, (start, end) in enumerate(spans))
    return fragment('recheck_plan', scan=safe(after_scan['id']), before=safe(before_scan['id']),
                    hit=safe(hit['id']), channel=channel, windows=rows,
                    count=len(spans), window_us=step_us // 1000,
                    threshold=before_scan['thresholds']['clip_permille'],
                    before_span=safe(interval(hit)),
                    after_note=safe(f'{after_scan["tracks"]["sound"]["rate"]} Hz · '
                                    f'{after_scan["tracks"]["sound"]["channels"]} 声道'))


def show(app, after_id, params):
    """Render the recheck workspace for one later version."""
    after_scan = app.vault.get('scan', after_id)
    active = None
    if params.get('recheck'):
        active = app.recheck_record(params['recheck'])
        if active['after_scan'] != after_id:
            raise InputError('该核销记录不属于这个后版扫描。')
    if active:
        before_id, hit_id = active['before_scan'], active['before_hit_id']
        channel = str(active['after_channel'])
    elif params.get('candidate'):
        before_id, hit_id = split_choice(params['candidate'])
        channel = params.get('after_channel', '')
    else:
        before_id, hit_id = params.get('before', ''), params.get('hit', '')
        channel = params.get('after_channel', '')
    sound = after_scan['tracks']['sound']
    body = empty_note(after_scan)
    if active:
        body = result(app, active, after_scan)
    elif before_id and hit_id:
        before_scan = app.vault.get('scan', before_id, required=False)
        if before_scan is None:
            raise Missing('找不到所选前版扫描。')
        hit = next((item for item in before_scan['hits']
                    if item['id'] == hit_id and item['kind'] == 'clip'), None)
        if hit is None:
            raise Missing('所选候选不是这条前版扫描的削顶单。')
        if not sound:
            raise InputError('这个后版扫描没有选伴音轨，无法核销。')
        channel = integer(int(channel), 'after_channel', 1, sound['channels']) if channel else 1
        body = plan(app, before_scan, hit, after_scan, channel)
    lane_options = ''.join(
        f'<option value="{number}"{" selected" if str(number) == str(channel) else ""}>'
        f'伴音 {number}</option>' for number in range(1, (sound['channels'] if sound else 0) + 1))
    return page('核销复核', 'compare', 'recheck',
                sidebar=library.sidebar(app.vault, after_id),
                master=safe(app.vault.get('master', after_scan['master_id'])['label']),
                after=safe(library.label(app.vault, after_scan)),
                after_key=safe(after_id),
                channels=sound['channels'] if sound else 0,
                lane_options=lane_options,
                candidates=clip_choices(app, before_id, hit_id),
                hit=safe(hit_id),
                compare=compare_link(before_id, after_id),
                history=history(app, after_id, active['id'] if active else ''),
                result=body)


def empty_note(after_scan):
    sound = after_scan['tracks']['sound']
    detail = (f'已选伴音流 {sound["index"]} · {sound["rate"]} Hz · {sound["channels"]} 声道'
              if sound else '未选伴音轨')
    return fragment('recheck_empty', after=safe(detail))


def compare_link(before_id, after_id):
    if not before_id:
        return ''
    return (f'<p class="muted">原有对照结果不变：'
                f'<a href="/desk/compare?before={safe(before_id)}&amp;after={safe(after_id)}">查看版次比对</a></p>')