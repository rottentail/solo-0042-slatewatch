"""Map reviewed findings onto whole frames of a 30000/1001 rework sequence.

The exchange keeps every selected finding as a separate marker even when its
start and end land inside one target frame.  Source clock values are retained
unchanged, and picture coverage holes are reported as target frames that have
no source picture to cut.
"""
from csv import writer
from fractions import Fraction
from io import BytesIO, StringIO
from math import ceil, floor
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .clock import InputError, SECOND, Span, coalesce
from .rules import canonical

TARGET_RATE = Fraction(30000, 1001)
TARGET_RATE_TEXT = '30000/1001'
TARGET_FRAME_US = Fraction(SECOND) / TARGET_RATE
MAX_MARKERS = 64
KIND_LABELS = {
    'black': '黑场', 'freeze': '冻帧', 'silence': '静音', 'clip': '削顶',
    'picture_gap': '画面缺口', 'sound_gap': '伴音缺口',
}

COLUMNS = (
    'number', 'hit_id', 'kind', 'label', 'channel', 'review_status', 'target_rate',
    'source_rate', 'source_start_us', 'source_end_us', 'source_start_frame',
    'source_end_frame', 'source_start_timecode', 'source_end_timecode',
    'sequence_start_frame', 'sequence_end_frame', 'sequence_frames',
    'sequence_start_timecode', 'sequence_end_timecode',
    'head_extension_us', 'tail_extension_us', 'extended_frames',
    'no_picture_material', 'picture_holes', 'note',
)

README = """Slatewatch 返修标记交换件

exchange.json 是完整清单；markers.csv 每行一个候选，可直接交给剪辑导入。
目标序列固定为 30000/1001，目标格从节目零点起算。所有区间左闭右开，
end_frame 与 end_timecode 为排他端点。

- source_start_us/source_end_us、源帧号和源时码保留扫描时的源时标。
- sequence_start_frame/sequence_end_frame 是目标剪辑格，不按整数 30 fps 建轴。
- 源时码与目标时码均为非丢帧，标称帧率分别是源平均帧率与 30。
- head_extension_us / tail_extension_us 是为覆盖异常而落在边界剪辑格内、
  但不属于异常本身的时长；extended_frames 列出具体格和侧别。
- 同一异常起止落在一个目标格内时，markers 仍保留独立一行，sequence_frames=1。
- material 按整段目标剪辑格统计源覆盖：full_frames 整格有内容，partial_frames
  列出格内只有一部分源覆盖的画面，empty_ranges 的格完全没有可剪内容。
- no_picture_material 为真的标记只覆盖画面洞，不能当作有画面可剪。
"""


def _rate(track):
    """Return the source picture rate and whether it came from a stored default."""
    if not track:
        raise InputError('this scan has no selected picture stream for marker exchange')
    raw = track.get('average_rate')
    if raw is None:
        return Fraction(25), 'default-25'
    try:
        rate = Fraction(raw)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise InputError('selected picture stream has no usable frame rate') from exc
    if rate <= 0:
        raise InputError('selected picture stream has no usable frame rate')
    return rate, 'stream-average-rate'


def _nominal(rate):
    return -(-rate.numerator // rate.denominator)


def _timecode(index, nominal):
    frames = index % nominal
    seconds = index // nominal
    return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}:{frames:02}'


def _boundaries(span, rate):
    """Snap to a frame boundary when microsecond rounding lands within one tick."""
    def index(us, rounding):
        exact = Fraction(us) * rate / SECOND
        nearest = round(exact)
        if abs(exact - nearest) <= Fraction(rate, SECOND):
            return int(nearest)
        return floor(exact) if rounding == 'floor' else ceil(exact)

    return index(span.start, 'floor'), index(span.end, 'ceil')


def _span(span, rate):
    start, end = _boundaries(span, rate)
    nominal = _nominal(rate)
    return {'start_us': span.start, 'end_us': span.end,
            'start_frame': start, 'end_frame': end, 'frames': end - start,
            'start_timecode': _timecode(start, nominal),
            'end_timecode': _timecode(end, nominal)}


def _frame_span(start_frame, end_frame):
    """Exact target frame boundaries rounded to the nearest programme microsecond."""
    return Span(round(start_frame * TARGET_FRAME_US), round(end_frame * TARGET_FRAME_US))


def _ranges(frames):
    result = []
    for frame in frames:
        if result and frame == result[-1][1]:
            result[-1][1] = frame + 1
        else:
            result.append([frame, frame + 1])
    return [{'start_frame': start, 'end_frame': end} for start, end in result]


def _lane(coverage, sequence):
    """Classify every target cut frame as fully, partly or not covered by source."""
    span = _frame_span(sequence['start_frame'], sequence['end_frame'])
    merged = coalesce(coverage)
    overlap = [part.intersect(span) for part in merged]
    overlap = [part for part in overlap if part is not None]
    full, partial, empty = [], [], []
    for frame in range(sequence['start_frame'], sequence['end_frame']):
        area = _frame_span(frame, frame + 1)
        covered = sum(part.intersect(area).duration for part in overlap
                      if part.intersect(area) is not None)
        if covered >= area.duration:
            full.append(frame)
        elif covered > 0:
            partial.append({'frame': frame, 'covered_us': covered,
                            'frame_us': area.duration})
        else:
            empty.append(frame)
    available = sum(part.duration for part in overlap)
    return {'frames': sequence['frames'],
            'full_frames': len(full), 'partial_frames': partial,
            'empty_frames': len(empty), 'empty_ranges': _ranges(empty),
            'available_us': available, 'hole_us': span.duration - available,
            'no_material': available == 0}


def _extension(span, sequence):
    head = Fraction(span.start) - sequence['start_frame'] * TARGET_FRAME_US
    tail = sequence['end_frame'] * TARGET_FRAME_US - span.end
    frames = []
    if head > 0 and tail > 0 and sequence['frames'] == 1:
        frames.append({'frame': sequence['start_frame'], 'side': 'both',
                       'extension_us': round(head + tail)})
    else:
        if head > 0:
            frames.append({'frame': sequence['start_frame'], 'side': 'head',
                           'extension_us': round(head)})
        if tail > 0:
            frames.append({'frame': sequence['end_frame'] - 1, 'side': 'tail',
                           'extension_us': round(tail)})
    return {'head_us': round(head), 'tail_us': round(tail), 'frames': frames}


def _note(marker):
    sequence = marker['sequence']
    pieces = [f"目标格 [{sequence['start_frame']}, {sequence['end_frame']})"]
    if marker['single_frame']:
        pieces.append('起止同帧')
    for edge in marker['extension']['frames']:
        side = {'head': '头边界', 'tail': '尾边界', 'both': '头尾两端'}[edge['side']]
        pieces.append(f"格 {edge['frame']} {side}扩展 {edge['extension_us']} µs")
    picture = marker['material']['picture']
    for item in picture['empty_ranges']:
        piece = f"无源画面 格 [{item['start_frame']}, {item['end_frame']})"
        if piece not in pieces:
            pieces.append(piece)
    for item in picture['partial_frames']:
        pieces.append(f"格 {item['frame']} 仅 {item['covered_us']}/{item['frame_us']} µs 有源画面")
    return '；'.join(pieces)


def _marker(number, hit, status, picture, sound, rate):
    span = Span.parse(hit)
    sequence = _span(span, TARGET_RATE)
    target = _frame_span(sequence['start_frame'], sequence['end_frame'])
    sequence = {**sequence, 'start_us': target.start, 'end_us': target.end}
    lanes = {'picture': _lane(picture, sequence),
             'sound': _lane(sound, sequence) if sound is not None else None}
    return {
        'number': number, 'hit_id': hit['id'], 'kind': hit['kind'],
        'label': KIND_LABELS.get(hit['kind'], hit['kind']),
        'channel': hit['channel'], 'review_status': status,
        'source': _span(span, rate),
        'sequence': sequence,
        'single_frame': sequence['frames'] == 1,
        'extension': _extension(span, sequence),
        'material': lanes,
    }


def _warnings(markers):
    result = []
    single = [marker['number'] for marker in markers if marker['single_frame']]
    if single:
        result.append({'code': 'single-target-frame', 'markers': single})
    empty = [marker['number'] for marker in markers
             if marker['material']['picture']['no_material']]
    if empty:
        result.append({'code': 'no-picture-material', 'markers': empty})
    partial = [marker['number'] for marker in markers
               if marker['material']['picture']['partial_frames']
               or (marker['material']['picture']['empty_ranges']
                   and not marker['material']['picture']['no_material'])]
    if partial:
        result.append({'code': 'partial-picture-frames', 'markers': partial})
    groups = {}
    for marker in markers:
        key = (marker['sequence']['start_frame'], marker['sequence']['end_frame'])
        groups.setdefault(key, []).append(marker['number'])
    shared = [{'start_frame': key[0], 'end_frame': key[1], 'markers': numbers}
              for key, numbers in sorted(groups.items()) if len(numbers) > 1]
    if shared:
        result.append({'code': 'shared-target-frames', 'groups': shared})
    return result


def build(scan, master, hits, statuses=None):
    """Return the complete exchange manifest for the selected findings."""
    rate, basis = _rate(scan['tracks']['picture'])
    picture = [Span.parse(value) for value in scan['coverage']['picture']]
    sound = None
    if scan['tracks']['sound'] and scan['coverage']['sound'] is not None:
        sound = [Span.parse(value) for value in scan['coverage']['sound']]
    statuses = statuses or {}
    ordered = sorted(hits, key=lambda hit: (hit['start_us'], hit['kind'],
                                            hit['channel'] or 0, hit['end_us'], hit['id']))
    markers = [_marker(number, hit, statuses.get(hit['id'], 'pending'), picture, sound, rate)
               for number, hit in enumerate(ordered, 1)]
    return {
        'format': 'slatewatch-marker-exchange',
        'version': 1,
        'generated_from': {'scan_id': scan['id'], 'master_id': master['id'],
                           'master_label': master.get('label'),
                           'master_sha256': master.get('sha256'),
                           'master_origin_us': master.get('media', {}).get('origin_us'),
                           'analysis_version': scan.get('analysis_version'),
                           'markers': len(markers),
                           'review_projection': 'current at export'},
        'source': {'rate': str(rate), 'numerator': rate.numerator,
                   'denominator': rate.denominator, 'rate_basis': basis,
                   'timecode': {'nominal_fps': _nominal(rate), 'drop_frame': False},
                   'track': {'index': scan['tracks']['picture'].get('index'),
                             'time_base': scan['tracks']['picture'].get('time_base'),
                             'start_pts': scan['tracks']['picture'].get('start_pts'),
                             'average_rate': scan['tracks']['picture'].get('average_rate')}},
        'target': {'rate': TARGET_RATE_TEXT, 'numerator': TARGET_RATE.numerator,
                   'denominator': TARGET_RATE.denominator,
                   'timecode': {'nominal_fps': 30, 'drop_frame': False},
                   'origin': 'programme-zero'},
        'conventions': {
            'interval': '所有区间左闭右开 [start_us, end_us)',
            'frame_index': '帧号从 0 起；end_frame 为排他端点',
            'source_timecode': '非丢帧，按源平均帧率标称值；帧号相对容器公共起点',
            'target_timecode': '非丢帧，标称 30 fps；帧号相对节目零点',
            'sequence_span': '整段目标剪辑格按 1001/30000 秒取整到最近微秒',
            'extension_us': '边界目标格内不属于异常本身的时长，取整到微秒',
            'material': '按整段目标剪辑格统计：empty_ranges 的格没有可剪内容',
            'no_picture_material': '源画面覆盖洞里没有可剪画面',
            'review_status': '导出当时的裁决投影，不随之后的裁决改写本清单',
        },
        'markers': markers,
        'warnings': _warnings(markers),
    }


def _picture_holes(marker):
    return ', '.join(f'[{item["start_frame"]}, {item["end_frame"]})'
                     for item in marker['material']['picture']['empty_ranges'])


def _csv(manifest):
    buffer = StringIO()
    table = writer(buffer, lineterminator='\n')
    table.writerow(COLUMNS)
    for marker in manifest['markers']:
        source, sequence = marker['source'], marker['sequence']
        table.writerow((
            marker['number'], marker['hit_id'], marker['kind'], marker['label'],
            '' if marker['channel'] is None else marker['channel'],
            marker['review_status'], manifest['target']['rate'],
            manifest['source']['rate'], source['start_us'], source['end_us'],
            source['start_frame'], source['end_frame'], source['start_timecode'],
            source['end_timecode'], sequence['start_frame'], sequence['end_frame'],
            sequence['frames'], sequence['start_timecode'], sequence['end_timecode'],
            marker['extension']['head_us'], marker['extension']['tail_us'],
            ';'.join(f"{edge['frame']}:{edge['side']}:{edge['extension_us']}"
                     for edge in marker['extension']['frames']),
            'true' if marker['material']['picture']['no_material'] else 'false',
            _picture_holes(marker), _note(marker),
        ))
    return buffer.getvalue().encode('utf-8')


def pack(scan, master, hits, statuses=None):
    """Build a deterministic, importable marker exchange archive."""
    manifest = build(scan, master, hits, statuses)
    files = {'README.txt': README.encode('utf-8'),
             'exchange.json': canonical(manifest),
             'markers.csv': _csv(manifest)}
    buffer = BytesIO()
    with ZipFile(buffer, 'w') as archive:
        for name in sorted(files):
            entry = ZipInfo(name)
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, files[name])
    return buffer.getvalue()
