"""Render the pre-delivery testimony check for one shipment combination."""

from .render import page
from .text import KINDS, interval, lane, safe

CAPABILITY = {
    'picture': '实存画格',
    'picture_pair': '前后画格对照',
    'sound': '伴音听审',
    'stereo': '双声道伴音',
}


def _material(hit):
    evidence = hit['evidence']
    picture = f"画格 {evidence['picture_captured']}/{evidence['picture_targets']}"
    sound = evidence['sound']
    if sound is None:
        return picture + ' · 未选伴音轨'
    shipped = '含 WAV' if sound['shipped'] else '不含 WAV'
    return f"{picture} · {shipped} · {sound['channels']} 声道 · 补零段 {sound['filled_ranges']}"


def show(scan_id, report):
    plan = report['delivery']
    sound = '含伴音 WAV' if plan['sound'] else '不含伴音 WAV'
    rows = []
    for hit in report['hits']:
        lacks = '、'.join(CAPABILITY[key] for key in hit['missing'])
        state = ('<span class="state pass">可作证</span>' if hit['status'] == 'supported'
                 else '<span class="state held">证据不足</span>')
        rows.append(f'<tr><th scope="row">{KINDS[hit["kind"]]}</th><td>{lane(hit)}</td>'
                    f'<td class="time">{interval(hit)}</td><td>{state}</td>'
                    f'<td>{safe(lacks) if lacks else "—"}</td><td>{safe(_material(hit))}</td></tr>')
    if report['ready']:
        banner = '<p role="status" class="good">组合可投递：每个待复核结论在实际选出的材料里都有证据。</p>'
    else:
        lacks = '、'.join(CAPABILITY[key] for key in report['missing'])
        banner = f'<p role="status" class="error">组合不足，缺作证能力：{safe(lacks)}。</p>'
    return page('投递前检查', 'scan', 'delivery', scan=safe(scan_id),
                plan=safe(f'画格 {plan["frames"]} 张 · {sound} · 留边 {plan["padding_ms"]} 毫秒'),
                banner=banner, rows=''.join(rows))
