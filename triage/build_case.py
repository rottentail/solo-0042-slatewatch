"""Maintainer-side generator for the Slatewatch attribution-triage drill.

NOT part of the product and NOT an investigator input.

  triage/inputs/   artifacts the next shift receives (no answers)
  triage/key/      generator truth (gitignored), never read by run_triage.py

Masters are assembled from the repository's own synthetic primitives
(``slatewatch.reels.raster`` / ``audio_frame``) plus a little PyAV, so the
long-frame and picture-hole exhibits are reproducible from the repo without
changing product code.  The frozen package, the browser-saved still and the
current-master evidence entries all go through the product's real
``service`` / ``desk.preview`` paths.

Case shape
----------
Archive master A: one 2.0 s "slate card" picture spanning 1.00-3.00 s, then a
real picture hole 3.20-3.60 s; sound holes at 2.0-2.2 s and 3.2-3.4 s.
Current master B: the same card one frame later (1.04-3.04 s) with a period-2
+/-1 checkerboard, continuous pictures, and the plain card re-inserted as a
still over 3.20-3.60 s; sound hole only at 2.0-2.2 s.  A and B differ in bytes
and in every content hash.

Frozen ZIP (one scan from A, two evidence entries, padding 0, 3 frames):
  * card entry for the sound gap 2.0-2.2 s -> targets 2.033/2.100/2.167 s,
    all resolving to the single source frame at 1.000 s.
  * hole entry for the sound gap 3.2-3.4 s -> targets 3.233/3.300/3.367 s,
    all inside the picture hole (null pictures); the whole WAV span is a
    zero-filled source sound hole.

Web PNG: saved at page label 3.300 s.  Its 80x48 pixels are byte-identical to
* the ZIP's card frame (targets 2.033/2.100/2.167, source 1.000 s),
* B's long card frame after 2x2 browse downsampling (source 1.040 s), and
* B's re-inserted card frame at 3.300 s (source 3.280 s, full-res identical).
"""
from __future__ import annotations

import argparse
from fractions import Fraction
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import shutil
import sys
import tempfile
from zipfile import ZipFile

import av

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from slatewatch.desk.preview import png                              # noqa: E402
from slatewatch.desk.text import timestamp                           # noqa: E402
from slatewatch.media import Picture, decode                         # noqa: E402
from slatewatch.reels import SAMPLE_RATE, SIZE, audio_frame, raster  # noqa: E402
from slatewatch.service import Slatewatch                            # noqa: E402

FPS = Fraction(25)
CARD_INDEX = 25
CARD_DURATION = 50                     # 2.0 s at 25 fps
AUDIO_END = 32000                      # 4.0 s at 8 kHz
CARD_EVIDENCE_LABEL = 2_100_000
WEB_LABEL = 3_300_000
HOLES = {
    'archive': ((16000, 17600), (25600, 27200)),   # sound 2.0-2.2 s, 3.2-3.4 s
    'current': ((16000, 17600),),                  # sound 2.0-2.2 s only
}
CARD_HEADER = b'P5\n80 48\n255\n'


def _checkerboard(frame):
    """Add a period-2 +/-1 code checkerboard: gone in a 2x2 box average."""
    plane = frame.planes[0]
    raw = bytearray(memoryview(plane))
    for y in range(SIZE[1]):
        start = y * plane.line_size
        for x in range(SIZE[0]):
            raw[start + x] = max(0, min(255, raw[start + x] + (1 if (x + y) % 2 == 0 else -1)))
    plane.update(raw)
    return frame


def _card():
    return raster(CARD_INDEX, False, False)


def _frame_indices(kind):
    if kind == 'archive':
        out = [(i, 1) for i in range(CARD_INDEX)]
        out.append((CARD_INDEX, CARD_DURATION))      # card, 1.00-3.00 s
        out.extend((i, 1) for i in range(75, 80))    # 3.00-3.20 s
        out.extend((i, 1) for i in range(90, 100))   # 3.60-3.96 s -> hole 3.20-3.60
        return out
    out = [(i, 1) for i in range(CARD_INDEX)]
    out.append((CARD_INDEX + 1, CARD_DURATION))      # card, 1.04-3.04 s
    out.extend((i, 1) for i in range(76, 100))       # 3.04-3.96 s, continuous
    return out


def _frame_pixels(kind, index, duration):
    if duration == CARD_DURATION:
        frame = _card()
        return _checkerboard(frame) if kind == 'current' else frame
    if kind == 'current' and 80 <= index < 90:
        return _card()          # plain card re-inserted over 3.20-3.60 s
    return raster(index, False, False)


def build_master(path, kind):
    holes = HOLES[kind]
    with av.open(str(path), mode='w', format='matroska') as container:
        video = container.add_stream('ffv1', rate=FPS)
        video.width, video.height = SIZE
        video.pix_fmt = 'gray'
        audio = container.add_stream('pcm_s16le', rate=SAMPLE_RATE)
        audio.layout = 'stereo'
        next_sample = 0
        for index, duration in _frame_indices(kind):
            seconds = Fraction(index, 25)
            frame = _frame_pixels(kind, index, duration)
            frame.pts = index
            frame.time_base = Fraction(1, 25)
            frame.duration = duration
            for packet in video.encode(frame):
                if packet.duration == 0:
                    packet.duration = duration
                container.mux(packet)
            while next_sample < AUDIO_END and Fraction(next_sample, SAMPLE_RATE) <= seconds:
                if not any(low <= next_sample < high for low, high in holes):
                    container.mux(audio.encode(audio_frame(next_sample, 160, False)))
                next_sample += 160
        for packet in video.encode():
            container.mux(packet)
        for packet in audio.encode():
            container.mux(packet)
    return path


def pick(scan, kind, start):
    return next(hit for hit in scan['hits']
                if hit['kind'] == kind and hit.get('channel') is None and hit['start_us'] == start)


def digest(data):
    return sha256(data).hexdigest()


def cover(frames, target_us):
    for entry in frames:
        if entry.timestamp_us <= target_us < entry.timestamp_us + (entry.duration_us or 40000):
            return entry
    return None


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


def browse(frame):
    return CARD_HEADER + frame.pixels


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force', action='store_true', help='overwrite existing inputs/key')
    options = parser.parse_args()
    inputs = ROOT / 'inputs'
    key_dir = ROOT / 'key'
    if inputs.exists() and not options.force:
        raise SystemExit('inputs already exist; pass --force to rebuild')
    for directory in (inputs, key_dir):
        if directory.exists():
            shutil.rmtree(directory)
    (inputs / 'png').mkdir(parents=True)
    (inputs / 'zip').mkdir()
    (inputs / 'current').mkdir()
    (inputs / 'control').mkdir()
    (inputs / 'notes').mkdir()
    key_dir.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix='slatewatch-case-') as temporary:
        work = Path(temporary)
        archive_path = build_master(work / 'archive.mkv', 'archive')
        current_path = build_master(work / 'current.mkv', 'current')
        app = Slatewatch(work / 'vault')
        policy = app.policy({'label': 'triage-frozen-thresholds'})
        archive = app.master(archive_path.read_bytes(), 'archive-source', None, 'first')
        current = app.master(current_path.read_bytes(), 'current-source', None, 'first')
        archive_scan = app.scan({'master_id': archive['id'], 'policy_id': policy['id']})
        current_scan = app.scan({'master_id': current['id'], 'policy_id': policy['id']})

        card_gap = pick(archive_scan, 'sound_gap', 2_000_000)
        hole_gap = pick(archive_scan, 'sound_gap', 3_200_000)
        seal = app.seal(archive_scan['id'], {'hit_ids': [card_gap['id'], hole_gap['id']],
                                             'padding_ms': 0, 'frames': 3})
        bundle = app.vault.object_path(seal['sha256']).read_bytes()
        (inputs / 'zip' / f'{seal["id"]}.zip').write_bytes(bundle)
        shutil.copyfile(current_path, inputs / 'current' / 'current.mkv')

        with ZipFile(BytesIO(bundle)) as archive_zip:
            entries = {item['hit_id']: item for item in json.loads(archive_zip.read('evidence.json'))}
            card_entry = entries[card_gap['id']]
            hole_entry = entries[hole_gap['id']]
            card_pgm = {frame['target_us']: archive_zip.read(
                f'evidence/{card_gap["id"]}/{frame["file"]}') for frame in card_entry['picture']}
            index = json.loads(archive_zip.read('index.json'))

        current_frames = [entry for entry in decode(app.vault.object_path(current['sha256']),
                                                    current_scan['tracks'])
                          if isinstance(entry, Picture)]
        current_card = cover(current_frames, WEB_LABEL)      # 3.28 s, plain card insert
        current_long = cover(current_frames, 1_500_000)     # 1.04 s, checkerboarded card
        assert current_card is not None and current_long is not None

        (inputs / 'png' / 'saved-03-300000.png').write_bytes(png(browse(current_card)))
        (inputs / 'png' / 'saved-03-300000.txt').write_text(
            'source: browser evidence preview (link expired)\n'
            'programme time shown on page: ' + timestamp(WEB_LABEL) + '\n'
            'not copied: source-frame line, preview token, mapping\n',
            encoding='utf-8')

        current_map = app.evidence(current_scan['id'],
                                   {'hit_ids': [pick(current_scan, 'sound_gap', 2_000_000)['id']],
                                    'padding_ms': 0, 'frames': 3})
        with ZipFile(BytesIO(current_map)) as current_zip:
            current_entry = json.loads(current_zip.read(
                f'{pick(current_scan, "sound_gap", 2_000_000)["id"]}/clock.json'))
        controls = {
            'frozen-card-target-2.100.json': {
                'note': 'frozen ZIP entry: complete mapping, target + source',
                'master_sha256': index['master_sha256'], 'scan_id': index['scan_id'],
                'hit_id': card_gap['id'], 'kind': 'sound_gap',
                'span_us': [card_entry['start_us'], card_entry['end_us']],
                'frame': next(frame for frame in card_entry['picture']
                              if frame['target_us'] == CARD_EVIDENCE_LABEL),
            },
            'current-long-target-2.100.json': {
                'note': 'fresh derivation from current/current.mkv; not historical evidence',
                'master_sha256': current['sha256'], 'scan_id': current_scan['id'],
                'hit_id': current_entry['hit_id'],
                'frame': next(frame for frame in current_entry['picture']
                              if frame['target_us'] == CARD_EVIDENCE_LABEL),
            },
        }
        for name, value in controls.items():
            (inputs / 'control' / name).write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        (inputs / 'control' / 'README.md').write_text(CONTROL_NOTE, encoding='utf-8')

        card_plane = card_pgm[CARD_EVIDENCE_LABEL][len(CARD_HEADER):]
        archive_full_card = full_frame(archive_path, 1_000_000)[1]
        current_full_long = full_frame(current_path, current_long.timestamp_us)[1]
        current_full_insert = full_frame(current_path, current_card.timestamp_us)[1]
        key = {
            'generator': 'triage/build_case.py',
            'warning': 'generator truth -- never an investigator input',
            'policy': {'id': policy['id'], 'thresholds': policy['thresholds']},
            'archive': {'master_id': archive['id'], 'sha256': archive['sha256'],
                        'scan_id': archive_scan['id'], 'coverage': archive_scan['coverage'],
                        'extent': archive_scan['extent'],
                        'hits': [{k: h[k] for k in ('id', 'kind', 'channel', 'start_us', 'end_us')}
                                 for h in archive_scan['hits']]},
            'current': {'master_id': current['id'], 'sha256': current['sha256'],
                        'scan_id': current_scan['id'], 'coverage': current_scan['coverage'],
                        'extent': current_scan['extent'],
                        'hits': [{k: h[k] for k in ('id', 'kind', 'channel', 'start_us', 'end_us')}
                                 for h in current_scan['hits']]},
            'seal': seal['id'],
            'truth': {
                'card_targets_us': [frame['target_us'] for frame in card_entry['picture']],
                'card_sources_us': [frame['source_us'] for frame in card_entry['picture']],
                'card_pixels_identical': len(set(card_pgm.values())) == 1,
                'hole_targets_us': [frame['target_us'] for frame in hole_entry['picture']],
                'hole_pictures_null': all(frame['file'] is None and frame['source_us'] is None
                                          for frame in hole_entry['picture']),
                'hole_sound_fill': hole_entry['sound']['filled_sample_ranges'],
                'archive_picture_hole_us': [3_200_000, 3_600_000],
                'archive_sound_holes_us': [[2_000_000, 2_200_000], [3_200_000, 3_400_000]],
                'web_png_equals_zip_card': current_card.pixels == card_plane,
                'zip_card_equals_current_card_browse': card_plane == current_card.pixels,
                'zip_card_equals_current_long_browse': card_plane == current_long.pixels,
                'archive_full_card_equals_current_full_long':
                    archive_full_card == current_full_long,
                'current_full_long_equals_current_full_insert':
                    current_full_long == current_full_insert,
                'archive_full_card_size': [160, 96],
                'current_card_source_us': current_card.timestamp_us,
                'current_long_source_us': current_long.timestamp_us,
            },
        }
        (key_dir / 'answer_key.json').write_text(
            json.dumps(key, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')

        received = []
        for path in sorted(p for p in inputs.rglob('*') if p.is_file()):
            relative = path.relative_to(inputs).as_posix()
            if relative == 'chain.json':
                continue
            if relative.startswith('png/') and path.suffix == '.png':
                kind = 'browser-saved-picture'
            elif relative.startswith('png/'):
                kind = 'hand-copied-page-label'
            elif relative.startswith('zip/'):
                kind = 'frozen-evidence-bundle'
            elif relative.endswith('.mkv'):
                kind = 'current-master'
            elif relative.startswith('control/'):
                kind = 'complete-mapping-control'
            elif relative.startswith('notes/'):
                kind = 'shift-note'
            else:
                kind = 'artifact'
            received.append({'path': relative, 'kind': kind,
                             'bytes': path.stat().st_size, 'sha256': digest(path.read_bytes())})
        chain = {
            'case': 'slatewatch-attribution-triage',
            'provenance': 'repository-generated drill media; no production data',
            'received': received,
            'available_but_not_evidence': ['current/current.mkv (the only master bytes on hand)'],
            'not_available': ['archive master bytes',
                              'preview cache, token and per-preview mapping',
                              'seal descriptor record carrying the whole-bundle sha256'],
        }
        (inputs / 'chain.json').write_text(
            json.dumps(chain, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        (inputs / 'notes' / 'RECEIVED.md').write_text(NOTE, encoding='utf-8')

        # The drill must actually contain every required group.
        assert archive['sha256'] != current['sha256']
        assert archive_scan['coverage']['picture'] == [
            {'start_us': 0, 'end_us': 3200000}, {'start_us': 3600000, 'end_us': 4000000}]
        assert current_scan['coverage']['picture'] == [{'start_us': 0, 'end_us': 4000000}]
        assert key['truth']['card_sources_us'] == [1_000_000] * 3
        assert key['truth']['card_pixels_identical']
        assert key['truth']['hole_pictures_null'] and key['truth']['hole_sound_fill']
        assert key['truth']['web_png_equals_zip_card']
        assert key['truth']['zip_card_equals_current_card_browse']
        assert key['truth']['zip_card_equals_current_long_browse']
        assert not key['truth']['archive_full_card_equals_current_full_long']
        assert not key['truth']['current_full_long_equals_current_full_insert']
        assert current_card.timestamp_us == 3_280_000
        assert current_long.timestamp_us == 1_040_000
        assert index['master_sha256'] == archive['sha256']
        print(json.dumps({
            'inputs': str(inputs), 'key': str(key_dir / 'answer_key.json'),
            'seal': seal['id'],
            'card_targets_us': key['truth']['card_targets_us'],
            'card_sources_us': key['truth']['card_sources_us'],
            'hole_targets_us': key['truth']['hole_targets_us'],
            'current_card_source_us': current_card.timestamp_us,
            'current_long_source_us': current_long.timestamp_us,
            'archive_sha256': archive['sha256'], 'current_sha256': current['sha256'],
        }, ensure_ascii=False, indent=2))


CONTROL_NOTE = """\
# 对照：完整的浏览映射长什么样

这两份 JSON 都带 `target_us`（页面显示时标）和 `source_us`（源帧时标）。
它们不是本次争议的答案，只用来展示“缺了哪一项就无法归属”。

- `frozen-card-target-2.100.json`：冻结包内的映射。target 2.100 s → source 1.000 s。
- `current-long-target-2.100.json`：在当前母带上重新摘片。同一 target 2.100 s，
  但 source 是 1.040 s。可见相同页面时标在两版上对应不同源帧。

若网页另存时多抄一行 `source_us`：1.000 s 指向归档版，1.040 s 指向当前版，
同一像素的归属即可判。若只有 `target_us`，同一像素在两版、不同源帧上都出现过，
归属就只能停在未知。
"""


NOTE = """\
# 接班交接：两张画格的取证归属

## 收到的材料

两张画格初看像同一候选：

- `png/saved-03-300000.png`：从浏览器证据预览另存的画格。同名 `.txt` 只抄了
  页面上显示的节目时标 **00:03.300000**；没有抄源帧时标、没有映射、没有预览 token。
- `zip/`：一枚当时冻结的证据包，索引与成员都在，包含两条证据。包名就是封包 ID。
  一条证据有画格，另一条落在画面洞。另一张“画格”即取自这里。
- `current/current.mkv`：现在还能读取的母带。它只是**当前**登记的这一版，
  不能默认当作当时审片所看的那一版。
- `control/`：两份带完整映射（页面时标 + 源帧时标）的对照。
- `chain.json`：收到时的文件大小与 SHA-256。

## 已失效或未随件

- 原预览链接打不开；进程内预览缓存里的映射已经取不到。
- 封包描述符记录（带整包 SHA-256 的那条）没有随件；ZIP 内只有成员索引。
- 当时的归档母带字节没有随件，只有冻结索引里记的 `master_sha256`。

## 要求

1. 只依据 `inputs/` 与仓库现有代码下结论；把生成侧答案当作输入是违规的。
2. 用 `python -m triage.run_triage` 重跑，结果写到 `triage/report/`。
3. 明确区分：
   - 可以用现存材料排除为“只是留边或抽图配置不同”的争议；
   - 可以确认来自另一版源的争议；
   - 即使像素相同也不能恢复当时取证归属的争议。
4. 不要修改产品，也不要为历史数据补发新的冻结包或新的扫描。
"""


if __name__ == '__main__':
    main()
