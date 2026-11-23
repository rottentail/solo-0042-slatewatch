"""Clock self-check desk page: pick two scans, enter scene anchors, read verdict."""

from urllib.parse import parse_qsl

from ..clock import InputError
from ..clockcheck import VERDICT_LABELS
from .library import scan_choices
from .render import page
from .text import safe, timestamp

MAX_DESK_ANCHORS = 6


def show(app, before='', after=''):
    anchor_rows = ''.join(
        '<tr><td><input name="label" maxlength="60"{required} placeholder="场景锚点 {0}"></td>'
        '<td><input name="before_us" inputmode="numeric" pattern="[0-9]{{1,12}}"{required} '
        'placeholder="前版微秒"></td>'
        '<td><input name="after_us" inputmode="numeric" pattern="[0-9]{{1,12}}"{required} '
        'placeholder="后版微秒"></td></tr>'.format(index + 1, required=' required' if index == 0 else '')
        for index in range(MAX_DESK_ANCHORS)
    )
    return page('时钟自检', 'clockcheck', 'clockcheck',
                left=scan_choices(app.vault, before), right=scan_choices(app.vault, after),
                anchor_rows=anchor_rows, note='')


def _fields(raw):
    scalars = {}
    grouped = {}
    for name, value in parse_qsl(raw, keep_blank_values=True, strict_parsing=False):
        if name in ('label', 'before_us', 'after_us'):
            grouped.setdefault(name, []).append(value)
        else:
            scalars[name] = value
    return scalars, grouped


def _anchors(grouped):
    labels, befores, afters = grouped.get('label', []), grouped.get('before_us', []), grouped.get('after_us', [])
    if not (len(labels) == len(befores) == len(afters)) or not labels:
        raise InputError('每个锚点需要名称和两侧时标。')
    anchors = []
    for row_index, (label, before_us, after_us) in enumerate(zip(labels, befores, afters)):
        blank = not label.strip() and not before_us.strip() and not after_us.strip()
        if blank:
            continue
        if not label.strip() or not before_us.strip() or not after_us.strip():
            raise InputError(f'第 {row_index + 1} 行锚点的名称和两侧时标必须同时填写。')
        if not before_us.isascii() or not before_us.isdecimal() or not after_us.isascii() or not after_us.isdecimal():
            raise InputError('锚点时标必须是整数微秒。')
        anchors.append({'label': label.strip(), 'before_us': int(before_us), 'after_us': int(after_us)})
    if not anchors:
        raise InputError('至少填写一个可由值班员核对的场景锚点。')
    if len(anchors) > MAX_DESK_ANCHORS:
        raise InputError(f'工作台最多登记 {MAX_DESK_ANCHORS} 个锚点。')
    return anchors


def submit(app, raw):
    scalars, grouped = _fields(raw)
    before, after = scalars.get('before', ''), scalars.get('after', '')
    if not before or not after:
        raise InputError('需同时选定两台采集机的扫描。')
    payload = {'before': before, 'after': after, 'anchors': _anchors(grouped)}
    tolerance = scalars.get('tolerance_us', '').strip()
    if tolerance:
        if not tolerance.isascii() or not tolerance.isdecimal():
            raise InputError('容差必须是整数微秒。')
        payload['tolerance_us'] = int(tolerance)
    return report(app, app.clockcheck(payload))


def _range_list(items):
    if not items:
        return '<p class="muted">锚点覆盖范围内没有未证实区间。</p>'
    return '<ul class="ranges">' + ''.join(
        f'<li><code>{timestamp(item["start_us"])} – {timestamp(item["end_us"])}</code></li>'
        for item in items) + '</ul>'


def report(app, record):
    segments = ''.join(
        '<section class="model"><h3>{role}</h3><dl>'
        '<dt>时钟比例（‰）</dt><dd>{rate}</dd>'
        '<dt>原生时标外推起点差（微秒）</dt><dd>{offset}</dd>'
        '<dt>最大残差（微秒）</dt><dd>{residual}</dd></dl></section>'.format(
            role=safe(segment.get('role', '单段直线映射')),
            rate=segment['rate_permille'], offset=segment['origin_offset_us'],
            residual=segment['max_abs_residual_us'])
        for segment in record['segments']
    )
    cut = ''
    if record['verdict'] == 'cut_jump':
        detail = record['cut']
        cut = ('<section class="model"><h3>剪接跳变</h3><dl>'
               f'<dt>分界（前版节目时标，微秒）</dt><dd>{detail["boundary_before_us"]}</dd>'
               f'<dt>跳变量（微秒）</dt><dd>{detail["jump_us"]}</dd></dl></section>')
    origins = record['container_origins_us']
    origin_line = (
        '<section class="model"><h3>容器起点</h3><dl>'
        f'<dt>前版起点（微秒）</dt><dd>{origins["before"]}</dd>'
        f'<dt>后版起点（微秒）</dt><dd>{origins["after"]}</dd>'
        f'<dt>起点差（微秒）</dt><dd>{origins["start_delta_us"]}</dd></dl></section>'
    )
    anchor_table = ''.join(
        '<tr><td>{label}</td><td class="time">{before}</td><td class="time">{after}</td></tr>'.format(
            label=safe(anchor['label']), before=timestamp(anchor['before_us']),
            after=timestamp(anchor['after_us']))
        for anchor in record['anchors']
    )
    reason = f'<p role="status" class="warn">{safe(record["reason"])}</p>' if record['verdict'] == 'inconclusive' else ''
    body = f'''
<div class="wrap">
  <header class="striphead"><h1>时钟自检结论</h1></header>
  <section class="verdict {safe(record['verdict'])}">
    <h2>{safe(VERDICT_LABELS[record['verdict']])}</h2>
    <p class="muted">判定编号 <code>{safe(record['id'])}</code> · 容差 {record['tolerance_us']} 微秒 · 锚点 {record['anchor_count']} 个</p>
    {reason}
  </section>
  {origin_line}
  {segments}{cut}
  <section class="model"><h3>判定依据（可复跑）</h3>
    <div class="table-wrap"><table><thead><tr><th>场景锚点</th><th>前版时标</th><th>后版时标</th></tr></thead>
    <tbody>{anchor_table}</tbody></table></div></section>
  <section class="model"><h3>前版未证实范围</h3>{_range_list(record['unproven_ranges']['before'])}</section>
  <section class="model"><h3>后版未证实范围</h3>{_range_list(record['unproven_ranges']['after'])}</section>
  <p><a class="button" href="/desk/clockcheck?before={safe(record['before_scan'])}&after={safe(record['after_scan'])}">返回自检入口</a></p>
</div>'''
    return page('时钟自检', 'clockcheck', 'clockcheck-report', body=body)
