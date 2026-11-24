"""Re-runnable attribution triage for the two received Slatewatch pictures.

Reads only ``triage/inputs`` and the repository's own read-only code paths.
Never reads ``triage/key`` and never imports the case generator.  Writes

  triage/report/triage.json   machine-readable observations and verdicts
  triage/report/REPORT.md     shift hand-over note

Run from the repository root:

  python -m triage.run_triage

Everything is derived from the received files plus the current master.  The
current master is scanned into a throw-away vault to obtain a *fresh*
observation; it is never written back as historical evidence, and no seal or
product record is created.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from io import BytesIO
from hashlib import sha256
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from zipfile import ZipFile
import zlib

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import av                                                        # noqa: E402

from slatewatch.clock import Span                                # noqa: E402
from slatewatch.evidence import Excerpt                          # noqa: E402
from slatewatch.media import Picture, decode                     # noqa: E402
from fractions import Fraction                                   # noqa: E402

from slatewatch.rules import identifier                          # noqa: E402
from slatewatch.service import Slatewatch                        # noqa: E402

WIDTH, HEIGHT = 80, 48
PGM_HEADER = b'P5\n80 48\n255\n'
CARD_HEADER = b'P5\n80 48\n255\n'
WEB_LABEL = 3_300_000
PRODUCT_FILES = (
    'slatewatch/clock.py', 'slatewatch/evidence.py', 'slatewatch/hits.py',
    'slatewatch/media.py', 'slatewatch/picture.py', 'slatewatch/rules.py',
    'slatewatch/scan.py', 'slatewatch/service.py', 'slatewatch/sound.py',
    'slatewatch/desk/preview.py', 'slatewatch/reels.py',
)


def digest(data):
    return sha256(data).hexdigest()


def read_png(path):
    """Decode the received grayscale PNG back to its 8-bit pixel plane."""
    data = path.read_bytes()
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError(f'{path.name} is not a PNG')
    offset, header, compressed = 8, None, bytearray()
    while offset < len(data):
        length = struct.unpack('!I', data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        offset += 12 + length
        if kind == b'IHDR':
            header = struct.unpack('!2I5B', payload)
        elif kind == b'IDAT':
            compressed += payload
        elif kind == b'IEND':
            break
    width, height, depth, color, compression, filtering, interlace = header
    if (depth, color, compression, filtering, interlace) != (8, 0, 0, 0, 0):
        raise ValueError(f'{path.name}: unsupported PNG layout {header}')
    if (width, height) != (WIDTH, HEIGHT):
        raise ValueError(f'{path.name}: unexpected geometry {width}x{height}')
    raw = zlib.decompress(bytes(compressed))
    stride, bpp = width, 1
    rows, cursor, prior = [], 0, bytearray(width)
    for _ in range(height):
        method = raw[cursor]
        line = bytearray(raw[cursor + 1:cursor + 1 + stride])
        cursor += stride + 1
        for i in range(stride):
            left = line[i - bpp] if i >= bpp else 0
            up = prior[i]
            upper_left = prior[i - bpp] if i >= bpp else 0
            if method == 1:
                line[i] = (line[i] + left) & 0xFF
            elif method == 2:
                line[i] = (line[i] + up) & 0xFF
            elif method == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif method == 4:
                base = left + up - upper_left
                pa, pb, pc = abs(base - left), abs(base - up), abs(base - upper_left)
                line[i] = (line[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else upper_left)) & 0xFF
            elif method != 0:
                raise ValueError(f'{path.name}: unsupported PNG filter {method}')
        rows.append(bytes(line))
        prior = line
    plane = b''.join(rows)
    if len(plane) != WIDTH * HEIGHT:
        raise ValueError(f'{path.name}: decoded plane has wrong size')
    return plane


def read_pgm(data):
    if not data.startswith(PGM_HEADER) or len(data) != len(PGM_HEADER) + WIDTH * HEIGHT:
        raise ValueError('unexpected PGM geometry')
    return data[len(PGM_HEADER):]


def load_seal(path):
    with ZipFile(path) as archive:
        names = archive.namelist()
        index = json.loads(archive.read('index.json'))
        recomputed = identifier('seal', {k: v for k, v in index.items() if k != 'id'})
        members_ok = all(
            len(archive.read(name)) == expected['size']
            and digest(archive.read(name)) == expected['sha256']
            for name, expected in index['members'].items())
        return {
            'file': path.name,
            'seal_id': index['id'],
            'seal_id_recomputed': recomputed,
            'id_matches_name': path.stem == index['id'],
            'id_matches_recomputation': recomputed == index['id'],
            'bundle_sha256_present': 'sha256' in index,
            'members_ok': members_ok,
            'files_in_zip': len(names),
            'declared_members': len(index['members']),
            'declared_members_present': set(index['members']) <= set(names),
            'files_not_in_member_table': sorted(set(names) - set(index['members'])),
            'master_sha256': index['master_sha256'],
            'scan': json.loads(archive.read('scan.json')),
            'review_status': json.loads(archive.read('review.json'))['status'],
            'evidence': json.loads(archive.read('evidence.json')),
            'members': {name: archive.read(name) for name in names},
        }


def configs_matching(scan, labels, picture_rails):
    matches = []
    for hit in scan['hits']:
        for padding in range(2001):
            for frames in range(1, 9):
                if Excerpt(hit, scan['extent']['end_us'], None, padding, frames).targets != labels:
                    continue
                covered = all(any(rail.start <= label < rail.end for rail in picture_rails)
                              for label in labels)
                matches.append({'hit_id': hit['id'], 'kind': hit['kind'],
                                'channel': hit.get('channel'),
                                'start_us': hit['start_us'], 'end_us': hit['end_us'],
                                'padding_ms': padding, 'frames': frames,
                                'all_labels_in_picture_coverage': covered})
    return matches


def configs_hitting(scan, labels, picture_rails):
    """Configs whose target grid contains every label; flag hole-only targets."""
    found = []
    for hit in scan['hits']:
        for padding in range(2001):
            for frames in range(1, 9):
                targets = Excerpt(hit, scan['extent']['end_us'], None, padding, frames).targets
                if not all(label in targets for label in labels):
                    continue
                covered = all(any(rail.start <= label < rail.end for rail in picture_rails)
                              for label in labels)
                found.append({'hit_id': hit['id'], 'kind': hit['kind'],
                              'channel': hit.get('channel'),
                              'start_us': hit['start_us'], 'end_us': hit['end_us'],
                              'padding_ms': padding, 'frames': frames,
                              'all_labels_in_picture_coverage': covered})
    return found


def product_state():
    try:
        head = subprocess.run(['git', '-C', str(REPO), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, check=True).stdout.strip()
        status = subprocess.run(['git', '-C', str(REPO), 'status', '--porcelain', '--',
                                 'slatewatch'], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        head, status = 'unknown', ''
    return {
        'head': head,
        'slatewatch_worktree_clean': status.strip() == '',
        'files': {name: digest((REPO / name).read_bytes()) for name in PRODUCT_FILES},
    }


def full_frame(path, target_us):
    """Full-resolution 160x96 raster whose display segment covers target."""
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        origin = container.start_time or 0
        for packet in container.demux(stream):
            for frame in packet.decode():
                if frame.pts is None:
                    continue
                start = round(Fraction(frame.pts) * frame.time_base * 1_000_000 - origin)
                duration = round((frame.duration or 0) * frame.time_base * 1_000_000) or 40000
                if start <= target_us < start + duration:
                    gray = frame.reformat(width=160, height=96, format='gray')
                    plane = gray.planes[0]
                    raw = memoryview(plane)
                    return start, b''.join(
                        bytes(raw[y * plane.line_size:y * plane.line_size + 160])
                        for y in range(96))
    raise ValueError(f'no full-resolution frame covers {target_us}')


def current_observation(path):
    """Scan the current master in a throw-away vault (fresh, not historical)."""
    with tempfile.TemporaryDirectory(prefix='slatewatch-triage-') as temporary:
        app = Slatewatch(Path(temporary) / 'vault')
        policy = app.policy({'label': 'triage-replay'})
        master = app.master(path.read_bytes(), 'current-replay', None, 'first')
        scan = app.scan({'master_id': master['id'], 'policy_id': policy['id']})
        frames = [entry for entry in decode(app.vault.object_path(master['sha256']), scan['tracks'])
                  if isinstance(entry, Picture)]
        mapping = app.evidence(scan['id'],
                               {'hit_ids': [next(hit['id'] for hit in scan['hits']
                                                 if hit['kind'] == 'sound_gap'
                                                 and hit['start_us'] == 2_000_000)],
                                'padding_ms': 0, 'frames': 3})
        with ZipFile(BytesIO(mapping)) as archive:
            entry = json.loads(archive.read(next(name for name in archive.namelist()
                                                 if name.endswith('/clock.json'))))
        return master, scan, frames, entry


def cover(frames, target_us):
    for entry in frames:
        if entry.timestamp_us <= target_us < entry.timestamp_us + (entry.duration_us or 40000):
            return entry
    return None


def build():
    inputs = ROOT / 'inputs'
    chain = json.loads((inputs / 'chain.json').read_text(encoding='utf-8'))
    inventory = []
    chain_ok = True
    for item in chain['received']:
        path = inputs / item['path']
        actual = digest(path.read_bytes())
        ok = actual == item['sha256'] and path.stat().st_size == item['bytes']
        chain_ok &= ok
        inventory.append({**item, 'sha256_now': actual, 'unchanged': ok})

    seal = load_seal(next((inputs / 'zip').glob('*.zip')))
    scan = seal['scan']
    card_entry = next(item for item in seal['evidence'] if any(f['file'] for f in item['picture']))
    hole_entry = next(item for item in seal['evidence'] if item is not card_entry)
    card_labels = [frame['target_us'] for frame in card_entry['picture']]
    card_sources = [frame['source_us'] for frame in card_entry['picture']]
    card_pixels = {frame['target_us']: read_pgm(
        seal['members'][f'evidence/{card_entry["hit_id"]}/{frame["file"]}'])
        for frame in card_entry['picture']}

    picture_rails = [Span.parse(v) for v in scan['coverage']['picture']]
    unique_config = configs_matching(scan, card_labels, picture_rails)
    configs_other_hits = [item for item in unique_config
                          if item['hit_id'] != card_entry['hit_id']]
    label_web = configs_hitting(scan, [WEB_LABEL], picture_rails)
    every_web_config_in_hole = bool(label_web) and not any(
        item['all_labels_in_picture_coverage'] for item in label_web)
    web_target_in_zip_evidence = [
        {'entry': item['hit_id'], 'target_us': frame['target_us'], 'source_us': frame['source_us'],
         'file': frame['file']}
        for item in seal['evidence'] for frame in item['picture']
        if frame['target_us'] == WEB_LABEL]
    covers_web = any(span.start <= WEB_LABEL < span.end for span in picture_rails)
    covers_card = any(span.start <= card_labels[1] < span.end for span in picture_rails)

    web_png = read_png(inputs / 'png' / 'saved-03-300000.png')
    web_label_note = (inputs / 'png' / 'saved-03-300000.txt').read_text(encoding='utf-8')

    current_master, current_scan, current_frames, current_entry = current_observation(
        inputs / 'current' / 'current.mkv')
    current_at_web = cover(current_frames, WEB_LABEL)
    current_at_card = cover(current_frames, card_labels[1])
    current_long = cover(current_frames, 1_500_000)

    long_full = full_frame(inputs / 'current' / 'current.mkv', current_long.timestamp_us)
    insert_full = full_frame(inputs / 'current' / 'current.mkv', current_at_web.timestamp_us)
    assert current_long.pixels == current_at_web.pixels
    assert long_full[1] != insert_full[1]
    controls = {path.name: json.loads(path.read_text(encoding='utf-8'))
                for path in sorted((inputs / 'control').glob('*.json'))}

    observations = {
        'integrity': {
            'chain_ok': chain_ok,
            'inventory': inventory,
            'seal': {key: seal[key] for key in
                     ('file', 'seal_id', 'seal_id_recomputed', 'id_matches_name',
                      'id_matches_recomputation', 'bundle_sha256_present', 'members_ok',
                      'files_in_zip', 'declared_members', 'declared_members_present',
                      'files_not_in_member_table', 'master_sha256', 'review_status')},
            'archived_master_sha256': seal['master_sha256'],
            'current_sha256': current_master['sha256'],
            'current_equals_archived_master': current_master['sha256'] == seal['master_sha256'],
            'archive_bytes_available': False,
        },
        'frozen_scan': {
            'scan_id': scan['id'],
            'coverage_picture': scan['coverage']['picture'],
            'coverage_sound': scan['coverage']['sound'],
            'extent': scan['extent'],
            'hits': [{key: hit[key] for key in ('id', 'kind', 'channel', 'start_us', 'end_us')}
                     for hit in scan['hits']],
            'picture_metrics': scan['metrics']['picture'],
            'covers_web_label': covers_web,
            'covers_card_label': covers_card,
        },
        'zip_card_entry': {
            'hit_id': card_entry['hit_id'],
            'span': {'start_us': card_entry['start_us'], 'end_us': card_entry['end_us']},
            'targets_us': card_labels,
            'sources_us': card_sources,
            'three_targets_share_one_source': len(set(card_sources)) == 1,
            'members_pixel_identical': len({digest(value) for value in card_pixels.values()}) == 1,
            'unique_config_for_three_labels': unique_config,
            'same_labels_from_other_hits': configs_other_hits,
            'unique_config_carries_picture_coverage':
                len(unique_config) == 1 and unique_config[0]['all_labels_in_picture_coverage'],
            'sound': card_entry['sound'],
        },
        'zip_hole_entry': {
            'hit_id': hole_entry['hit_id'],
            'span': {'start_us': hole_entry['start_us'], 'end_us': hole_entry['end_us']},
            'targets_us': [frame['target_us'] for frame in hole_entry['picture']],
            'pictures_null': all(frame['file'] is None and frame['source_us'] is None
                                 for frame in hole_entry['picture']),
            'sound_fill': hole_entry['sound']['filled_sample_ranges'],
            'sound_span_samples': [hole_entry['sound']['first_source_sample'],
                                   hole_entry['sound']['end_source_sample']],
        },
        'web_png': {
            'file': 'png/saved-03-300000.png',
            'label_us': WEB_LABEL,
            'label_copied_by_hand': web_label_note,
            'pixel_sha256': digest(web_png),
            'equals_zip_card_frame': any(web_png == value for value in card_pixels.values()),
            'equals_zip_card_at_2_100': web_png == card_pixels[card_labels[1]],
            'equals_zip_card_at_1_500': web_png == card_pixels[card_labels[0]],
            'equals_zip_card_at_2_167': web_png == card_pixels[card_labels[2]],
            'zip_card_members_all_identical': len({digest(value) for value in card_pixels.values()}) == 1,
            'equals_current_derivation_at_web_label':
                current_at_web is not None and web_png == current_at_web.pixels,
            'current_source_at_web_label_us': current_at_web.timestamp_us if current_at_web else None,
            'equals_current_derivation_at_card_label':
                current_at_card is not None and web_png == current_at_card.pixels,
            'current_source_at_card_label_us': current_at_card.timestamp_us if current_at_card else None,
            'configs_on_frozen_scan_with_web_label_only': len(label_web),
            'every_such_config_falls_in_picture_hole': every_web_config_in_hole,
            'zip_targets_equal_to_web_label': web_target_in_zip_evidence,
        },
        'full_resolution_probe': {
            'source': 'current/current.mkv (available bytes, fresh decode)',
            'long_source_us': long_full[0],
            'insert_source_us': insert_full[0],
            'browse_pixels_equal': current_long.pixels == current_at_web.pixels,
            'full_pixels_equal': long_full[1] == insert_full[1],
            'differing_codes': sum(1 for a, b in zip(long_full[1], insert_full[1]) if a != b),
            'codes_compared': len(long_full[1]),
        },
        'cross_version': {
            'archive_card_source_us': card_sources[1],
            'current_card_source_at_3_300_us': current_at_web.timestamp_us if current_at_web else None,
            'current_long_source_us': current_long.timestamp_us if current_long else None,
            'browse_pixels_identical_archive_vs_current':
                web_png == current_at_web.pixels if current_at_web else False,
            'current_long_pixels_equal_to_web':
                web_png == current_long.pixels if current_long else False,
            'archive_full_resolution_available': False,
            'full_resolution_comparison': '无法进行：归档母带字节未随件',
            'source_time_delta_us': (current_long.timestamp_us - card_sources[1])
                                    if current_long else None,
        },
        'controls': controls,
        'current_observation': {
            'is_historical_evidence': False,
            'sha256': current_master['sha256'],
            'scan_id': current_scan['id'],
            'coverage': current_scan['coverage'],
            'hits': [{key: hit[key] for key in ('kind', 'channel', 'start_us', 'end_us')}
                     for hit in current_scan['hits']],
            'evidence_entry_targets_us': [frame['target_us'] for frame in current_entry['picture']],
            'evidence_entry_sources_us': [frame['source_us'] for frame in current_entry['picture']],
        },
    }

    result = {
        'case': 'slatewatch-attribution-triage',
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'product': product_state(),
        'inputs_root': str(inputs),
        'observations': observations,
        'classification': classify(observations),
        'not_done': [
            '未修改任何产品代码或产品保管库。',
            '未读取 triage/key，也未导入案例生成器。',
            '未为历史数据补建扫描或冻结包；current 扫描只存在于临时目录。',
            '未把当前母带视图当作当时视图。',
        ],
    }
    result['conclusions_sha256'] = digest(
        json.dumps(result['classification'], ensure_ascii=False, sort_keys=True).encode('utf-8'))
    return result


def classify(observations):
    card = observations['zip_card_entry']
    hole = observations['zip_hole_entry']
    web = observations['web_png']
    cross = observations['cross_version']
    integrity = observations['integrity']
    frozen = observations['frozen_scan']
    probe = observations['full_resolution_probe']

    excluded = [
        {
            'id': 'E1',
            'claim': 'ZIP 卡片证据里 2.033 / 2.100 / 2.167 三个显示时标对应三张不同画格。',
            'verdict': '排除：三者共用源帧 1.000 s，且三份成员像素逐字节相同。',
            'basis': [f'映射 source_us = {card["sources_us"]}',
                      f'成员像素一致 = {card["members_pixel_identical"]}',
                      f'唯一配置 = {card["unique_config_for_three_labels"]}',
                      f'该唯一配置的每一个目标都落在归档画面覆盖内 = '
                      f'{card["unique_config_carries_picture_coverage"]}',
                      f'其它候选能否产出同样三个时标 = {card["same_labels_from_other_hits"]}'],
            'class': 'configuration-or-extraction',
        },
        {
            'id': 'E2',
            'claim': '网页画格与 ZIP 卡片画格属于同一候选、同一画面，只是留边或抽图配置不同。',
            'verdict': '排除“同一候选同一画面”：ZIP 自己的证据已把 3.300 s 记在洞条目下，画格为 null；'
                       '归档扫描在该时标也无画面，任何配置都取不出画格。',
            'basis': [f'ZIP 内 3.300 s 的目标 = {web["zip_targets_equal_to_web_label"]}',
                      f'归档画面覆盖 = {frozen["coverage_picture"]}，覆盖 3.300 s = '
                      f'{frozen["covers_web_label"]}',
                      f'冻结扫描上命中 3.300 s 的配置数 = {web["configs_on_frozen_scan_with_web_label_only"]}；'
                      f'这些配置在该时标都落在画面洞 = {web["every_such_config_falls_in_picture_hole"]}'],
            'class': 'configuration-or-extraction',
        },
        {
            'id': 'E3',
            'claim': '3.300 s 那张网页画格可以靠调留边或帧数从归档版取出。',
            'verdict': '排除：归档画面覆盖在 3.2–3.6 s 为空，该时标不可能产出画格。',
            'basis': [f'画面覆盖 = {frozen["coverage_picture"]}',
                      f'覆盖 3.300 s = {frozen["covers_web_label"]}',
                      f'冻结扫描上命中 3.300 s 的配置数 = {web["configs_on_frozen_scan_with_web_label_only"]}；'
                      f'这些配置在该时标都落在画面洞 = {web["every_such_config_falls_in_picture_hole"]}'],
            'class': 'configuration-or-extraction',
        },
    ]
    confirmed = [
        {
            'id': 'C1',
            'claim': '3.300 s 的网页画格来自与归档版不同的另一版源（条件：手抄时标无误）。',
            'verdict': '在“手抄时标 3.300 s 无误”的前提下确认：归档版在该时标无画面；'
                       '现存 current 母带可逐像素复现同一画格。',
            'basis': [f'归档画面覆盖 = {frozen["coverage_picture"]}',
                      f'current 在 3.300 s 的源帧 = {web["current_source_at_web_label_us"]}',
                      f'与 current 派生画格相同 = {web["equals_current_derivation_at_web_label"]}'],
            'class': 'other-source-version',
        },
    ]
    unattributable = [
        {
            'id': 'U1',
            'claim': '仅凭网页这一份像素，判定它出自归档源帧 1.000 s，还是当前源帧 1.040 / 3.280 s。',
            'verdict': '像素相同也不能判定：该 80x48 像素同时等于归档卡片帧和当前两处画面；'
                       'current 全分辨率已证明 1.04 与 3.28 两帧并非同一栅格，归档全分辨率不可得。',
            'basis': [f'网页像素 = 归档卡片帧 = {web["equals_zip_card_at_2_100"]}',
                      f'网页像素 = 当前长格派生帧 = {web["equals_current_derivation_at_card_label"]}',
                      f'网页像素 = 当前 3.300 s 派生帧 = {web["equals_current_derivation_at_web_label"]}',
                      f'源帧：归档 {cross["archive_card_source_us"]}、当前长格 '
                      f'{cross["current_long_source_us"]}、当前 3.300 s '
                      f'{cross["current_card_source_at_3_300_us"]}',
                      f'current 上 1.040 s 与 3.280 s 浏览像素相同但全分辨率不同：'
                      f'{observations["full_resolution_probe"]["differing_codes"]}/'
                      f'{observations["full_resolution_probe"]["codes_compared"]} 个代码值不同',
                      '归档母带字节未随件，无法对归档做全分辨率比对'],
            'class': 'pixel-identical-unattributable',
        },
        {
            'id': 'U2',
            'claim': '把网页上看到的手抄时标 3.300 s 当作可靠输入。',
            'verdict': '无法独立校验手抄值：只有一行字，没有源帧或 token；若该值抄错，C1 也要降级。',
            'basis': [f'手抄记录 = {web["label_copied_by_hand"].strip()!r}',
                      '预览缓存与 token 已失效'],
            'class': 'pixel-identical-unattributable',
        },
    ]
    record_only = [
        {
            'id': 'R1',
            'claim': '归档母带在 3.2–3.6 s 存在画面洞、3.2–3.4 s 存在伴音洞。',
            'verdict': '只能复核冻结记录自洽，不能复核母带本身：母带字节未随件。',
            'basis': [f'洞条目 targets = {hole["targets_us"]}，pictures_null = {hole["pictures_null"]}',
                      f'补零区间 = {hole["sound_fill"]}',
                      f'封包 scan.json 的 picture_gap 候选 = '
                      f'{[hit for hit in frozen["hits"] if hit["kind"] in ("picture_gap", "sound_gap")]}',
                      f'归档母带 sha256 = {integrity["archived_master_sha256"]}'],
            'class': 'frozen-record-only',
        },
        {
            'id': 'R2',
            'claim': '这枚 ZIP 在冻结时就已作为整包字节通过验真。',
            'verdict': '未知：描述符未随件，索引只有成员摘要；只能确认成员与索引一致。',
            'basis': [f'索引含整包 sha256 = {integrity["seal"]["bundle_sha256_present"]}',
                      f'成员核对 = {integrity["seal"]["members_ok"]}；'
                      f'索引声明 {integrity["seal"]["declared_members"]} 个成员，'
                      f'ZIP 内 {integrity["seal"]["files_in_zip"]} 个文件'],
            'class': 'frozen-record-only',
        },
    ]
    return {
        'excluded_as_configuration': excluded,
        'confirmed_other_source_version': confirmed,
        'pixel_identical_unattributable': unattributable,
        'frozen_record_only': record_only,
        'what_would_decide': [
            {'class': 'U1',
             'added_observation': '另存画格时一并抄下“源帧”时标，或保留预览 token 与映射',
             'effect': '1.000000 归归档版；1.040000/3.280000 归 current；U1 从未知变可判',
             'control': 'control/frozen-card-target-2.100.json 与 '
                        'control/current-long-target-2.100.json'},
            {'class': 'U2',
             'added_observation': '保存时截下含“节目时标 + 源帧”两行的整页，或保留 token 对应的映射',
             'effect': '手抄值可与映射对齐核对，C1/U2 不再依赖单一手抄行'},
            {'class': 'U1 的全分辨率分支',
             'added_observation': '归档母带字节，或封包内保留源栅格而不只是 80x48 缩略图',
             'effect': '可做全分辨率比对，决定两帧是否同一物理帧'},
            {'class': 'R1',
             'added_observation': '按索引 master_sha256 核对归档母带字节',
             'effect': '可直接复测覆盖与空洞，R1 由记录级升为母带级'},
            {'class': 'R2',
             'added_observation': '封包描述符（含整包 sha256）',
             'effect': '可重算整包摘要，R2 变可判'},
        ],
        'reconciliation': [
            'C1 与 U1 不矛盾：C1 只用“手抄页面时标 3.300 s + 归档画面覆盖为空”判定版本，'
            '不依赖像素归属到某一源帧；U1 说的是反过来——单看这份浏览像素，'
            '无法指认它究竟由归档 1.000 s、当前 1.040 s 还是当前 3.280 s 的源栅格生成。',
            '同一条浏览像素在 current 内部就对应两个全分辨率不同的源帧（1.040 s 与 3.280 s），'
            '因此浏览尺寸像素相同本身不构成“同一物理帧”的证据。',
        ],
        'limits': [
            '冻结 scan 是当时测量的记录，不是母带本身；若怀疑当时测量或选流有误，必须用母带字节复测。',
            '网页画格只携带像素；手抄时标可能有笔误，本次以手抄值为输入。',
            'current 扫描是本次新生成的观察，不是历史证据，也未封包。',
            '浏览尺寸是 80x48 灰阶缩略图；其上“像素相同”不等于源栅格相同。',
        ],
    }


def render_markdown(result):
    obs = result['observations']
    cls = result['classification']
    card, hole, web = obs['zip_card_entry'], obs['zip_hole_entry'], obs['web_png']
    cross, integrity, frozen = obs['cross_version'], obs['integrity'], obs['frozen_scan']
    probe = obs['full_resolution_probe']
    lines = [
        '# 排查记录：网页 PNG 与冻结 ZIP 画格的取证归属',
        '',
        f'- 生成时间：{result["generated_at"]}',
        f'- 产品版本：`{result["product"]["head"]}`，`slatewatch/` 工作区干净 '
        f'= {result["product"]["slatewatch_worktree_clean"]}',
        f'- 结论摘要：`{result["conclusions_sha256"]}`',
        f'- 输入：`{result["inputs_root"]}`（只读；未读取 `triage/key`）',
        '',
        '## 1. 两张画格是什么',
        '',
        f'- 网页另存：`{web["file"]}`，手抄页面时标 `{web["label_us"]}` µs。',
        f'- 冻结 ZIP：`{integrity["seal"]["file"]}`，同一扫描下两条证据；'
        f'卡片证据显示时标 `{card["targets_us"]}`，洞证据显示时标 `{hole["targets_us"]}`'
        f'（洞证据画格全为 null）。',
        f'- 网页 PNG 与 ZIP 卡片证据的画格在浏览尺寸上是否同一像素：'
        f'**{web["equals_zip_card_frame"]}**。',
        '',
        '## 2. 收到材料与完整性',
        '',
        f'- `chain.json` 全部文件未变：**{integrity["chain_ok"]}**。',
        f'- 封包 ID 可重算且与包名一致：**{integrity["seal"]["id_matches_recomputation"]}**。',
        f'- 成员摘要核对：**{integrity["seal"]["members_ok"]}**'
        f'（ZIP 内 {integrity["seal"]["files_in_zip"]} 个文件，成员表列 '
        f'{integrity["seal"]["declared_members"]} 个，全部在包内 = '
        f'{integrity["seal"]["declared_members_present"]}；不在成员表的文件仅 '
        f'{integrity["seal"]["files_not_in_member_table"]}，这是封包格式本身）。',
        f'- 索引内含整包 SHA-256：**{integrity["seal"]["bundle_sha256_present"]}**'
        '（描述符未随件）。',
        f'- 归档母带 `master_sha256 = {integrity["archived_master_sha256"]}`；'
        f'当前母带 `{integrity["current_sha256"]}`；两者相同 = '
        f'**{integrity["current_equals_archived_master"]}**。当前视图不能默认当作当时视图。',
        '',
        '## 3. 冻结扫描（归档版）',
        '',
        f'- 画面覆盖：`{frozen["coverage_picture"]}`',
        f'- 伴音覆盖：`{frozen["coverage_sound"]}`',
        f'- 画面量测：`{frozen["picture_metrics"]}`',
        f'- 候选：' + '；'.join(
            f'{hit["kind"]}{"/c" + str(hit["channel"]) if hit["channel"] is not None else ""} '
            f'[{hit["start_us"]}, {hit["end_us"]})' for hit in frozen['hits']),
        '',
        '## 4. 不同目标共用一格（ZIP 卡片证据）',
        '',
        f'- 显示时标 `{card["targets_us"]}` → 源帧 `{card["sources_us"]}`。',
        f'- 三张成员画格像素逐字节相同：**{card["members_pixel_identical"]}**。',
        f'- 穷举留边 0–2000 ms、帧数 1–8，能同时给出这三个时标的配置：'
        f'`{card["unique_config_for_three_labels"]}`。',
        '',
        '## 5. 画面洞与补零（ZIP 洞证据）',
        '',
        f'- 显示时标 `{hole["targets_us"]}`，画格均为空：**{hole["pictures_null"]}**。',
        f'- WAV 源采样 `{hole["sound_span_samples"]}`，补零区间 `{hole["sound_fill"]}`。',
        '',
        '## 6. 两版不同源格在浏览尺寸上像素相同',
        '',
        f'- 归档卡片源帧 `{cross["archive_card_source_us"]}`；'
        f'current 长格源帧 `{cross["current_long_source_us"]}`；'
        f'current 在 3.300 s 的卡片源帧 `{cross["current_card_source_at_3_300_us"]}`。',
        f'- 网页像素 = 归档卡片帧：**{web["equals_zip_card_at_2_100"]}**；'
        f'= current 长格：**{cross["current_long_pixels_equal_to_web"]}**；'
        f'= current 卡片：**{web["equals_current_derivation_at_card_label"]}**。',
        f'- current 内部：1.040 s 与 3.280 s 浏览像素是否相同：'
        f'**{probe["browse_pixels_equal"]}**；同两帧全分辨率是否相同：'
        f'**{probe["full_pixels_equal"]}**'
        f'（{probe["differing_codes"]}/{probe["codes_compared"]} 个代码值不同）。',
        f'- 源帧时间差 `{cross["source_time_delta_us"]}` µs；'
        f'归档侧全分辨率比对：{cross["full_resolution_comparison"]}。',
        '',
        '## 7. 对照：带完整浏览映射',
        '',
    ]
    for name, value in obs['controls'].items():
        if 'frame' in value:
            frame = value['frame']
            lines.append(f'- `{name}`：target `{frame.get("target_us")}` → source `{frame.get("source_us")}`。')
        else:
            lines.append(f'- `{name}`：target `{value.get("target_us")}` → source `{value.get("source_us")}`。')
    lines += ['', '对照说明：多收一行“源帧时标”，U1 类的未知即可判；'
              '只有页面时标时，同一像素在 1.000/1.040/3.280 s 都能出现。', '',
              '## 8. 分类结论', '',
              '### 8.1 可用现存材料排除“只是留边或抽图配置不同”', '']
    for item in cls['excluded_as_configuration']:
        lines += [f'- **{item["id"]}** {item["claim"]}  ', f'  结论：{item["verdict"]}  ',
                  f'  依据：' + '；'.join(item['basis'])]
    lines += ['', '### 8.2 可确认来自另一版源', '']
    for item in cls['confirmed_other_source_version']:
        lines += [f'- **{item["id"]}** {item["claim"]}  ', f'  结论：{item["verdict"]}  ',
                  f'  依据：' + '；'.join(item['basis'])]
    lines += ['', '### 8.3 即使像素相同也不能恢复当时取证归属', '']
    for item in cls['pixel_identical_unattributable']:
        lines += [f'- **{item["id"]}** {item["claim"]}  ', f'  结论：{item["verdict"]}  ',
                  f'  依据：' + '；'.join(item['basis'])]
    lines += ['', '### 8.4 只剩冻结记录、母带字节不在手', '']
    for item in cls['frozen_record_only']:
        lines += [f'- **{item["id"]}** {item["claim"]}  ', f'  结论：{item["verdict"]}  ',
                  f'  依据：' + '；'.join(item['basis'])]
    lines += ['', '## 9. 增加哪条观察可把未知变成可判', '']
    for item in cls['what_would_decide']:
        lines.append(f'- **{item["class"]}**：增加“{item["added_observation"]}” → {item["effect"]}'
                     + (f'（对照 {item["control"]}）' if 'control' in item else ''))
    lines += ['', '## 9.1 三类结论之间的关系', '']
    lines += [f'- {item}' for item in cls['reconciliation']]
    lines += ['', '## 10. 边界', '']
    lines += [f'- {item}' for item in cls['limits']]
    lines += ['', '## 11. 本次没有做的事', '']
    lines += [f'- {item}' for item in result['not_done']]
    lines += ['', '## 12. 重跑', '', '```sh', 'python -m triage.run_triage', '```', '',
              '重跑只读 `triage/inputs`；`current` 母带在临时目录中重新扫描，结果写入 '
              '`triage/report/`。产品代码与产品保管库均未改动。', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=ROOT / 'report')
    options = parser.parse_args()
    result = build()
    options.report.mkdir(parents=True, exist_ok=True)
    (options.report / 'triage.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    (options.report / 'REPORT.md').write_text(render_markdown(result) + '\n', encoding='utf-8')
    print(json.dumps({
        'report': str(options.report),
        'conclusions_sha256': result['conclusions_sha256'],
        'excluded_as_configuration': len(result['classification']['excluded_as_configuration']),
        'confirmed_other_source_version': len(result['classification']['confirmed_other_source_version']),
        'pixel_identical_unattributable': len(result['classification']['pixel_identical_unattributable']),
        'frozen_record_only': len(result['classification']['frozen_record_only']),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
