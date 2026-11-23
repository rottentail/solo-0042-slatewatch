"""Show how reviewed findings land on target rework frames and export an exchange."""

from ..clock import InputError
from ..exchange import TARGET_RATE_TEXT, build
from .render import fragment, page
from .text import KINDS, badge, interval, lane, safe


def statuses(view):
    return {item['id']: item['status'] for item in view['hits']}


def _ranges(ranges):
    return ', '.join(f'格 [{item["start_frame"]}, {item["end_frame"]})' for item in ranges)


def _extension(marker):
    names = {'head': '头', 'tail': '尾', 'both': '头尾'}
    return ', '.join(f"格 {item['frame']} {names.get(item['side'], item['side'])} +{item['extension_us']} µs"
                     for item in marker['extension']['frames']) or '无'


def _sequence(sequence):
    return f'格 [{sequence["start_frame"]}, {sequence["end_frame"]}) · {sequence["frames"]} 格'


def _material(picture):
    pieces = []
    if picture['empty_ranges']:
        pieces.append('无源画面 ' + _ranges(picture['empty_ranges']))
    if picture['partial_frames']:
        pieces.append('部分覆盖 ' + ', '.join(
            f"格 {item['frame']} ({item['covered_us']}/{item['frame_us']} µs)"
            for item in picture['partial_frames']))
    return '；'.join(pieces)


def table(scan, master, view):
    manifest = build(scan, master, view['hits'], statuses(view))
    rows = []
    for marker in manifest['markers']:
        source, sequence = marker['source'], marker['sequence']
        picture = marker['material']['picture']
        if picture['no_material']:
            state = '<span class="state held">无源画面</span>'
        elif picture['empty_ranges'] or picture['partial_frames']:
            state = '<span class="state pending">部分无源</span>'
        else:
            state = '整格可剪'
        rows.append(
            f'<tr><th scope="row"><label class="pick"><input type="checkbox" name="hit" '
            f'value="{safe(marker["hit_id"])}" checked> {KINDS[marker["kind"]]}</label>'
            f'<small>{safe(lane(marker))}</small></th>'
            f'<td>{badge(marker["review_status"])}</td>'
            f'<td class="time">{safe(interval(source))}</td>'
            f'<td class="time">{safe(source["start_timecode"])} – {safe(source["end_timecode"])}</td>'
            f'<td class="time">{safe(_sequence(sequence))}{" · 同帧" if marker["single_frame"] else ""}</td>'
            f'<td class="time">{safe(_extension(marker))}</td>'
            f'<td>{state}</td><td class="muted">{safe(_material(picture) or "无")}</td></tr>')
    return (''.join(rows) or '<tr><td colspan="8">此扫描没有可选候选。</td></tr>',
            manifest['source']['rate'])


def facts(scan, master, hit, view):
    manifest = build(scan, master, [hit], statuses(view))
    marker = manifest['markers'][0]
    source, sequence = marker['source'], marker['sequence']
    picture = marker['material']['picture']
    sound = marker['material'].get('sound')
    pieces = []
    if picture['no_material']:
        pieces.append('整段目标剪辑格都没有源画面')
    if picture['empty_ranges']:
        pieces.append('画面 ' + _ranges(picture['empty_ranges']) + ' 确实没有源画面')
    for item in picture['partial_frames']:
        pieces.append(f"画面格 {item['frame']} 只有 {item['covered_us']}/{item['frame_us']} µs 有源画面")
    if sound is not None and sound['empty_ranges']:
        pieces.append('伴音 ' + _ranges(sound['empty_ranges']) + ' 没有源采样')
    if sound is None:
        pieces.append('未选伴音轨，无法判断伴音可剪内容')
    if not pieces:
        pieces.append('整段目标剪辑格都有源画面')
    return fragment('marker', target=TARGET_RATE_TEXT, source=manifest['source']['rate'],
                    start=sequence['start_timecode'], end=sequence['end_timecode'],
                    sequence=safe(_sequence(sequence)),
                    same='是' if marker['single_frame'] else '否',
                    extension=safe(_extension(marker)), material=safe('；'.join(pieces)),
                    source_span=safe(f'{source["start_timecode"]} – {source["end_timecode"]}'),
                    source_frames=f'{source["start_frame"]} – {source["end_frame"]}')


def index(app, scan_id):
    scan = app.vault.get('scan', scan_id)
    master = app.vault.get('master', scan['master_id'])
    view = app.review(scan_id)
    rows, source = table(scan, master, view)
    return page('返修标记', 'scan', 'markers_page', scan=safe(scan_id), target=TARGET_RATE_TEXT,
                source=safe(source), rows=rows)


def download(app, scan_id, hits):
    if not hits:
        raise InputError('请至少选择一个候选。')
    return app.markers(scan_id, {'hit_ids': list(hits)})
