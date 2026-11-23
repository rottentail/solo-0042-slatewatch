"""Render the rolling delivery check: measured windows, lower bounds, blockers."""

from ..clock import InputError
from .library import scan_choices
from .render import page
from .text import safe, timestamp

STATE_TEXT = {'measured': '实测完整', 'bounded': '仅知下界', 'blank': '无实测'}
REASON_TEXT = {
    'unmeasured_window': '整窗无实测采样，阻塞送审',
    'coverage_hole': '覆盖洞占比过高，只有下界，阻塞送审',
    'clip_ratio': '满幅采样比例超限',
    'peak_code': '峰值代码超限',
}


def channel_options(channels, current):
    return ''.join(
        f'<option value="{index}"{" selected" if index == current else ""}>伴音 {index}</option>'
        for index in range(1, channels + 1))


def _number(data, key, default):
    value = data.get(key)
    if not value:
        return default
    if not value.isascii() or not value.isdecimal():
        raise InputError(f'{key} 需要正整数。')
    return int(value)


def _metric_row(window):
    metrics = window['metrics']
    if metrics is None:
        cells = '<td colspan="5" class="muted">无实测量测（断流不按安静计入）</td>'
    else:
        cells = (
            f'<td>{metrics["peak_code"]}</td>'
            f'<td>{metrics["rms_code"]}</td>'
            f'<td>{metrics["silence_permille"] / 10:.1f}%</td>'
            f'<td>{metrics["clip_permille"] / 10:.1f}%</td>'
            f'<td>{"精确值" if metrics["bounds"] == "exact" else "下界"}</td>'
        )
    reasons = '、'.join(REASON_TEXT.get(reason, reason) for reason in window['reasons']) or '—'
    holes = '；'.join(f"{span['start_us']}–{span['end_us']}" for span in window['hole_sample_ranges']) or '—'
    blocking = '<span class="state held">阻塞</span>' if window['blocking'] else '<span class="state pass">可送审</span>'
    return (
        f'<tr class="{safe(window["state"])}">'
        f'<th scope="row">{window["index"]}</th>'
        f'<td><span class="dstate {safe(window["state"])}">{STATE_TEXT[window["state"]]}</span></td>'
        f'<td class="time">{timestamp(window["start_us"])} – {timestamp(window["end_us"])}</td>'
        f'<td>{window["measured_permille"] / 10:.1f}%</td>'
        f'{cells}'
        f'<td><code>{window["first_source_sample"]}…{window["end_source_sample"]}</code></td>'
        f'<td class="muted">{safe(holes)}</td>'
        f'<td>{blocking}<small class="muted">{safe(reasons)}</small></td>'
        f'</tr>'
    )


def show(app, scan_id, params):
    if not scan_id:
        recent = app.vault.recent('scan', limit=1)
        if not recent:
            raise InputError('还没有扫描，请先在「母带与规则」登记并扫描。')
        scan_id = recent[0]['id']
    scan = app.vault.get('scan', scan_id)
    sound = scan['tracks']['sound']
    if sound is None:
        raise InputError('该扫描未选伴音轨，无法做滚动交付检查。')
    request = {
        'channel': _number(params, 'channel', 1),
        'window_ms': _number(params, 'window_ms', 250),
        'hop_ms': _number(params, 'hop_ms', _number(params, 'window_ms', 250)),
    }
    result = app.delivery(scan_id, request)
    rows = ''.join(_metric_row(window) for window in result['windows'])
    summary = (f'实测完整 {result["state_counts"]["measured"]} 窗 · '
               f'仅知下界 {result["state_counts"]["bounded"]} 窗 · '
               f'无实测 {result["state_counts"]["blank"]} 窗 · '
               f'阻塞 {len(result["blocked_window_indexes"])} 窗')
    header = ('<p class="blocker">存在阻塞送审的滚动窗</p>' if result['blocking']
              else '<p class="passline">全部完整窗满足交付限值</p>')
    return page('滚动交付检查', 'delivery', 'delivery',
                scans=scan_choices(app.vault, scan_id), scan=safe(scan_id),
                channel_options=channel_options(sound['channels'], request['channel']),
                channel=request['channel'], channels=sound['channels'],
                window_ms=request['window_ms'], hop_ms=request['hop_ms'],
                rate=sound['rate'], summary=safe(summary), header=header, rows=rows)
