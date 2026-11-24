"""Deterministic, air-gapped two-vendor A/V alignment handoff lab.

This module uses only the Python standard library so the external paper and WAV
materials can be regenerated without PyAV or a network install.  The coordinator
ledger is deliberately separate from the two distributable ZIP files.

Sample addresses are zero-based sample-frame offsets from the first sample of
the ordinary PCM WAV.  The WAV contains only RIFF/fmt/data chunks; it carries no
custom metadata and no product timeline marker.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import struct
import wave
import zipfile
import zlib
from pathlib import Path

CASE_ID = "R044"
SCHEMA_VERSION = 1

FPS = 25
FRAME_STEP_US = 1_000_000 // FPS
SAMPLE_RATE = 8_000
SAMPLE_STEP_US = 1_000_000 // SAMPLE_RATE
TOTAL_SAMPLES = 32_000

TRUE_FRAME = 25
TRUE_SOUND_ADDRESS = 8_960

SECOND_CLUSTER_FRAME = 52
PICTURE_HOLE_FIRST = 51
PICTURE_HOLE_END_EXCLUSIVE = 55
PICTURE_FIRST_STRIP = tuple(range(21, 30))
PICTURE_SECOND_BEFORE = (47, 48, 49, 50)
PICTURE_SECOND_AFTER = (55, 56, 57, 58)

AUDIO_PATCH_FIRST = 16_400
AUDIO_PATCH_END_EXCLUSIVE = 17_200
PATCH_EDGE_ADDRESS = 17_200
SECOND_SOUND_ADDRESS = 17_680

ALIGNMENT_TOLERANCE_US = 80_000

PICTURE_DIR = "picture-team"
SOUND_DIR = "sound-team"
RETURN_DIR = "returns/inbox"
COORD_DIR = "coordinator"
PACK_DIR = "packs"

FONT = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    " ": ("00000",) * 7,
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65_536), b""):
            h.update(block)
    return h.hexdigest()


def frame_id(index: int) -> str:
    return f"F{index:03d}"


def frame_start_us(index: int) -> int:
    return index * FRAME_STEP_US


def sample_start_us(address: int) -> int:
    return address * SAMPLE_STEP_US


def rect(gray: bytearray, width: int, x0: int, y0: int, x1: int, y1: int, value: int) -> None:
    x0, x1 = max(0, x0), min(width, x1)
    y0, y1 = max(0, y0), min(len(gray) // width, y1)
    if x0 >= x1 or y0 >= y1:
        return
    span = bytes((value,)) * (x1 - x0)
    for y in range(y0, y1):
        start = y * width + x0
        gray[start:start + x1 - x0] = span


def draw_text(gray: bytearray, width: int, x: int, y: int, text: str,
              value: int = 25, scale: int = 2) -> None:
    cursor_x = x
    for char in text.upper():
        glyph = FONT.get(char, FONT[" "])
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    rect(gray, width,
                         cursor_x + col * scale, y + row * scale,
                         cursor_x + (col + 1) * scale, y + (row + 1) * scale,
                         value)
        cursor_x += 6 * scale


def render_frame_gray(index: int) -> tuple[int, int, bytes]:
    width, height = 320, 180
    gray = bytearray((242,)) * (width * height)

    # Rig border and registration marks.
    rect(gray, width, 0, 0, width, 7, 52)
    rect(gray, width, 0, height - 7, width, height, 52)
    rect(gray, width, 0, 0, 7, height, 52)
    rect(gray, width, width - 7, 0, width, height, 52)
    for x in (24, width - 24):
        rect(gray, width, x - 1, 18, x + 2, 34, 95)
        rect(gray, width, x - 6, 24, x + 7, 27, 95)

    # Anvil and floor.
    rect(gray, width, 112, 132, 208, 151, 74)
    rect(gray, width, 118, 136, 202, 140, 112)
    rect(gray, width, 100, 151, 220, 162, 43)
    rect(gray, width, 82, 162, 238, 167, 118)

    contact_frame: int | None = None
    if 18 <= index <= 34:
        contact_frame = TRUE_FRAME
    elif 44 <= index <= 61:
        contact_frame = SECOND_CLUSTER_FRAME

    if contact_frame is None:
        hammer_bottom = 76
    else:
        delta = index - contact_frame
        if delta < 0:
            hammer_bottom = 132 + delta * 15
        elif delta == 0:
            hammer_bottom = 132
        else:
            hammer_bottom = {1: 112, 2: 100, 3: 108, 4: 118}.get(delta, 126)
        hammer_bottom = max(42, min(132, hammer_bottom))

    hammer_top = hammer_bottom - 22
    if hammer_top > 31:
        rect(gray, width, 155, 28, 165, hammer_top, 61)
    rect(gray, width, 132, hammer_top, 188, hammer_bottom, 27)
    rect(gray, width, 137, hammer_top + 3, 183, hammer_top + 7, 93)
    rect(gray, width, 137, hammer_bottom - 5, 183, hammer_bottom - 2, 12)

    if contact_frame is not None:
        delta = index - contact_frame
        if delta == 0:
            tone = 15
            for dx, dy, size in (
                (0, -20, 5), (-24, -10, 4), (24, -10, 4),
                (-34, 2, 3), (34, 2, 3), (-14, -28, 3), (14, -28, 3),
            ):
                cx, cy = 160 + dx, 126 + dy
                rect(gray, width, cx - size, cy - size, cx + size, cy + size, tone)
        elif delta in (1, 2):
            tone = 70 if delta == 1 else 135
            for dx in (-28, -14, 14, 28):
                rect(gray, width, 160 + dx - 2, 112 - delta * 5,
                     160 + dx + 2, 124 - delta * 5, tone)

    # Paper-readable roll address, also visible inside the PNG.
    rect(gray, width, 13, 13, 104, 34, 230)
    draw_text(gray, width, 18, 17, frame_id(index), value=18, scale=2)
    return width, height, bytes(gray)


def png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def frame_png(index: int) -> bytes:
    width, height, gray = render_frame_gray(index)
    raw = bytearray()
    stride = width
    for y in range(height):
        raw.append(0)
        raw.extend(gray[y * stride:(y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + png_chunk(b"IEND", b""))


def deterministic_noise(i: int) -> float:
    value = math.sin(i * 12.9898 + 78.233) * 43758.5453
    return (value - math.floor(value)) * 2.0 - 1.0


def add_knock(samples: list[int], onset: int) -> None:
    for j in range(800):
        envelope = math.exp(-j / 175.0)
        body = 10_800.0 * envelope * math.sin(2.0 * math.pi * 155.0 * j / SAMPLE_RATE)
        click = 0.0
        if j < 48:
            click = 6_200.0 * math.exp(-j / 8.5) * deterministic_noise(onset + j)
        samples[onset + j] += int(body + click)
    samples[onset] += 9_500
    samples[onset + 1] += 4_200
    samples[onset + 2] += 1_800


def build_wav_bytes() -> bytes:
    samples = [0] * TOTAL_SAMPLES
    for i in range(TOTAL_SAMPLES):
        bed = 260.0 * math.sin(2.0 * math.pi * 220.0 * i / SAMPLE_RATE)
        room = 85.0 * deterministic_noise(i)
        samples[i] = int(bed + room)

    add_knock(samples, TRUE_SOUND_ADDRESS)
    add_knock(samples, SECOND_SOUND_ADDRESS)

    # Coordinator-known coverage hole, represented in the vendor WAV only as
    # ordinary zero PCM samples.  No chunk names or metadata identify it.
    for i in range(AUDIO_PATCH_FIRST, AUDIO_PATCH_END_EXCLUSIVE):
        samples[i] = 0

    # A hard splice/fill boundary.  It sounds click-like but lacks the ring of
    # the two actual strikes.
    edge_values = (14_500, -9_800, 7_100, -4_200, 2_200, -900)
    for j, value in enumerate(edge_values):
        samples[PATCH_EDGE_ADDRESS + j] = value

    samples = [max(-32768, min(32767, value)) for value in samples]
    pcm = struct.pack("<" + "h" * len(samples), *samples)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


class PdfWriter:
    def __init__(self) -> None:
        self.objects: list[bytes | None] = []

    def reserve(self) -> int:
        self.objects.append(None)
        return len(self.objects)

    def add(self, body: bytes) -> int:
        self.objects.append(body)
        return len(self.objects)

    def set(self, object_number: int, body: bytes) -> None:
        self.objects[object_number - 1] = body

    @staticmethod
    def stream(body: bytes) -> bytes:
        return (b"<< /Length " + str(len(body)).encode("ascii") + b" >>\nstream\n"
                + body + b"\nendstream")

    def render(self, root_number: int) -> bytes:
        output = bytearray(b"%PDF-1.4\n%\xff\xff\xff\xff\n")
        offsets: list[int] = []
        for number, body in enumerate(self.objects, start=1):
            if body is None:
                raise RuntimeError(f"PDF object {number} was not populated")
            offsets.append(len(output))
            output += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        xref_position = len(output)
        count = len(self.objects) + 1
        output += f"xref\n0 {count}\n".encode("ascii")
        output += b"0000000000 65535 f \n"
        for offset in offsets:
            output += f"{offset:010d} 00000 n \n".encode("ascii")
        output += (b"trailer\n<< /Size " + str(count).encode("ascii")
                   + b" /Root " + f"{root_number} 0 R".encode("ascii")
                   + b" >>\nstartxref\n"
                   + str(xref_position).encode("ascii") + b"\n%%EOF\n")
        return bytes(output)


def pdf_text(x: float, y: float, size: int, text: str) -> str:
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size} Tf {x:g} {y:g} Td ({safe}) Tj ET\n"


def build_picture_pdf(frame_numbers: list[int]) -> bytes:
    pdf = PdfWriter()
    pages_id = pdf.reserve()
    font_id = pdf.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []

    items: list[tuple[str, int | None]] = [("frame", n) for n in PICTURE_FIRST_STRIP]
    items += [("frame", n) for n in PICTURE_SECOND_BEFORE]
    items.append(("gap", None))
    items += [("frame", n) for n in PICTURE_SECOND_AFTER]

    for page_number, (kind, number) in enumerate(items, start=1):
        content = pdf_text(48, 804, 18, f"SLATEWATCH PICTURE DESK / CASE {CASE_ID}-P")
        content += pdf_text(48, 780, 12, "Paper-only roll copy. Mark the frame where contact is visible.")
        image_ref = ""
        resources = f"<< /Font << /F1 {font_id} 0 R >>"

        if kind == "frame" and number is not None:
            _, _, gray = render_frame_gray(number)
            compressed = zlib.compress(gray, 9)
            image_id = pdf.add(
                b"<< /Type /XObject /Subtype /Image /Width 320 /Height 180 "
                b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
                + f"/Length {len(compressed)} >>\nstream\n".encode("ascii")
                + compressed + b"\nendstream"
            )
            resources += f" /XObject << /Im0 {image_id} 0 R >>"
            content += f"q 480 0 0 270 57.5 445 cm /Im0 Do Q\n"
            content += pdf_text(57.5, 742, 15, f"Roll address: {frame_id(number)}")
            content += pdf_text(57.5, 420, 11, "Observation code: contact_confirmed / approach_only / no_relevant_action")
        else:
            content += "q [7 5] 0 d 2 w 0.45 0.45 0.45 RG 57.5 445 480 270 re S Q\n"
            content += pdf_text(125, 590, 20, "PICTURE RECORD GAP")
            content += pdf_text(103, 558, 14, "No paper frames for F051-F054")
            content += pdf_text(112, 532, 12, "Numbering resumes at F055. Do not invent a contact frame.")
            content += pdf_text(57.5, 742, 15, "Roll record: F050 then gap then F055")
            content += pdf_text(57.5, 420, 11, "Observation code: frame_gap_no_exact_contact")

        resources += " >>"
        content += pdf_text(48, 48, 9, f"Page {page_number} / {len(items)} - return the matching CSV or paper form")
        content_bytes = content.encode("latin-1", errors="replace")
        content_id = pdf.add(PdfWriter.stream(content_bytes))
        page_body = (
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] "
            f"/Resources {resources} /Contents {content_id} 0 R >>"
        ).encode("ascii")
        page_ids.append(pdf.add(page_body))

    kids = " ".join(f"{number} 0 R" for number in page_ids)
    pdf.set(pages_id, f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii"))
    catalog_id = pdf.add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii"))
    return pdf.render(catalog_id)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


PICTURE_FIELDS = [
    "report_id", "reviewer", "frame_id_first", "frame_id_last", "exact_frame_id",
    "observation_code", "observation_text", "confidence", "review_date",
]
SOUND_FIELDS = [
    "report_id", "listener", "wav_sha256", "address_zero_based_sample", "channel",
    "observation_code", "listening_text", "confidence", "review_date",
]


def picture_readme() -> str:
    return f"""# 画面方实际收件（演练编号 {CASE_ID}-P）

你只负责纸上画格和画面记录，按卷号回报可见接触格或缺口。

## 包里有什么

- `picture-handout.pdf`：可直接打印的画格与缺口记录页。
- `print-sheet.html`：浏览器打开后可打印的同一套画格。
- `frames/`：逐张 PNG 画格，画格内左上角有卷号。
- `picture-roll.csv`：纸面卷号记录，包含实际画格和编号缺口。
- `picture-return-form.csv` / `picture-return-form.md`：可填写回报表。

## 你要回报什么

只回报：

1. 能清楚看到“锤面与砧面接触”的准确画格号，格式如 `F012`；
2. 如果动作跨过缺画格位置、无法指出唯一接触格，就填写首末可见格，并选择
   `frame_gap_no_exact_contact`；
3. 你的观察描述、置信度和日期。

不要根据前后姿势猜一个“最可能”的接触格。缺证本身是有效回报。
"""


def picture_form_md() -> str:
    return f"""# 画面方回报表 {CASE_ID}-P

可直接编辑同目录 `picture-return-form.csv`，或打印本页填写后交回交付方。

- 回报编号：`P-R044-____`
- 班组/审阅人：________________
- 日期：________

## 逐次观察

| 观察序号 | 首格 | 末格 | 唯一准确接触格（没有则留空） | 观察代码 | 肉眼记录 | 置信度 |
|---|---|---|---|---|---|---|
| 1 |  |  |  | `contact_confirmed` / `approach_only` / `frame_gap_no_exact_contact` / `no_relevant_action` |  | high / medium / low |
| 2 |  |  |  | 同上 |  | high / medium / low |

## 不能确定时

若编号从某格跳到另一格、而接触动作落在缺口内：

- “首格”填缺口前最后一个可见格；
- “末格”填缺口后第一个可见格；
- “唯一准确接触格”留空；
- 观察代码填 `frame_gap_no_exact_contact`。

本回报只需描述纸面可见事实；不要根据相邻姿势猜测缺口内的准确画格。
"""


def sound_readme(wav_hash: str) -> str:
    return f"""# 伴音方实际收件（演练编号 {CASE_ID}-A）

你只收到一个普通 PCM WAV 和回报表。你不需要产品时标或任何自定义元数据。

## 声音文件

- 文件：`alignment-call.wav`
- 标准格式：RIFF / WAVE，PCM 单声道，16-bit，{SAMPLE_RATE} Hz
- SHA-256：`{wav_hash}`

## 样本地址规则

回报的地址必须是 **WAV 起点后的零基样本地址**：

- WAV 中第一个采样的地址是 `0`；
- 第 `n+1` 个采样的地址是 `n`；
- 不要回报播放器的产品内时间码，也不要读取任何自定义元数据。本文件没有自定义元数据块。

Audacity 可在底部把选择位置格式切到 “samples”，其起点样本数即本地址。`ffprobe` 一类工具的时间读数只能作辅助；正式字段仍填样本地址。

## 听辨任务

完整听至少两遍，找出两次相似的敲击/撞击声，以及任何类似敲击起点的短咔声或断口声。对每个候选回报：

- 攻击点样本地址；
- 听辨结果：`confirmed_strike`、`strike_like_click`、`drop_out` 或 `other`；
- 是否有低频主体和余响、是否像断口/补样边缘；
- 置信度。

可使用任意播放器或 DAW；交付方只用标准 WAV 采样坐标汇合。
"""


def sound_form_md(wav_hash: str) -> str:
    return f"""# 伴音方听辨回报表 {CASE_ID}-A

可直接编辑同目录 `listening-return-form.csv`，或打印本页填写。

- 回报编号：`A-R044-____`
- 听辨人/班组：________________
- 所听 WAV SHA-256：`{wav_hash}`
- 日期：________

## 逐条回报

| 条目 | WAV 零基样本地址 | 声道 | 听辨代码 | 听感记录（攻击、主体、余响、断口） | 置信度 |
|---|---:|---|---|---|---|
| 1 |  | mono | `confirmed_strike` / `strike_like_click` / `drop_out` / `other` |  | high / medium / low |
| 2 |  | mono | 同上 |  | high / medium / low |
| 3 |  | mono | 同上 |  | high / medium / low |

地址示例规则：WAV 第一个采样为 `0`。不要填写时:分:秒:帧作为正式地址。
"""


def print_html(frame_numbers: list[int]) -> str:
    figures: list[str] = []
    for number in frame_numbers:
        fid = frame_id(number)
        figures.append(
            '<figure><img src="frames/' + fid + '.png" alt="' + fid + '">'
            f'<figcaption>{fid}</figcaption></figure>'
        )
        if number == 50:
            figures.append(
                '<figure class="gap"><div class="gapbox">PICTURE RECORD GAP<br>'
                '<strong>F051–F054</strong><br>无纸面画格；编号在 F055 恢复</div>'
                '<figcaption>F051–F054 missing record</figcaption></figure>'
            )
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Picture handoff R044-P</title>
<style>
@page { size: A4; margin: 14mm; }
body { font: 13px/1.45 system-ui, "Noto Sans CJK SC", sans-serif; color:#111; }
header { border-bottom:2px solid #222; margin-bottom:12px; }
.grid { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
figure { margin:0; break-inside:avoid; border:1px solid #999; padding:6px; }
img { width:100%; image-rendering:pixelated; background:#eee; }
figcaption { font-family:ui-monospace,monospace; margin-top:4px; }
.gapbox { height:169px; border:3px dashed #777; display:grid; place-content:center;
 text-align:center; font-family:ui-monospace,monospace; color:#555; background:#fafafa; }
@media print { .noprint { display:none; } figure { page-break-inside:avoid; } }
</style></head><body>
<header><h1>Slatewatch 画面方纸质交接 R044-P</h1>
<p>只含画格与卷号记录。请在回报表中填写准确接触格；缺口内不得猜格。</p>
<button class="noprint" onclick="window.print()">打印 / 另存 PDF</button></header>
<section class="grid">
""" + "\n".join(figures) + """
</section></body></html>
"""


def build_case(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    picture_root = root / PICTURE_DIR
    sound_root = root / SOUND_DIR
    returns_root = root / RETURN_DIR
    coord_root = root / COORD_DIR
    pack_root = root / PACK_DIR
    for directory in (picture_root / "frames", sound_root, returns_root,
                      coord_root / "repair-orders", coord_root / "verification-cases", pack_root):
        directory.mkdir(parents=True, exist_ok=True)

    all_print_frames = list(PICTURE_FIRST_STRIP) + list(PICTURE_SECOND_BEFORE) + list(PICTURE_SECOND_AFTER)
    frame_hashes: dict[str, str] = {}
    for number in all_print_frames:
        data = frame_png(number)
        relative = f"{PICTURE_DIR}/frames/{frame_id(number)}.png"
        (root / relative).write_bytes(data)
        frame_hashes[relative] = sha256_bytes(data)

    roll_rows = []
    for number in PICTURE_FIRST_STRIP:
        roll_rows.append({
            "frame_id": frame_id(number), "source_frame_index": str(number),
            "status": "paper_frame", "paper_file": f"frames/{frame_id(number)}.png",
        })
    for number in PICTURE_SECOND_BEFORE:
        roll_rows.append({
            "frame_id": frame_id(number), "source_frame_index": str(number),
            "status": "paper_frame", "paper_file": f"frames/{frame_id(number)}.png",
        })
    for number in range(PICTURE_HOLE_FIRST, PICTURE_HOLE_END_EXCLUSIVE):
        roll_rows.append({
            "frame_id": frame_id(number), "source_frame_index": str(number),
            "status": "missing_picture_hole", "paper_file": "",
        })
    for number in PICTURE_SECOND_AFTER:
        roll_rows.append({
            "frame_id": frame_id(number), "source_frame_index": str(number),
            "status": "paper_frame", "paper_file": f"frames/{frame_id(number)}.png",
        })
    write_csv(picture_root / "picture-roll.csv",
              ["frame_id", "source_frame_index", "status", "paper_file"], roll_rows)
    write_text(picture_root / "README-画面方.md", picture_readme())
    write_text(picture_root / "picture-return-form.md", picture_form_md())
    write_csv(picture_root / "picture-return-form.csv", PICTURE_FIELDS, [])
    write_text(picture_root / "print-sheet.html", print_html(all_print_frames))
    pdf_bytes = build_picture_pdf(all_print_frames)
    (picture_root / "picture-handout.pdf").write_bytes(pdf_bytes)

    wav_bytes = build_wav_bytes()
    wav_hash = sha256_bytes(wav_bytes)
    (sound_root / "alignment-call.wav").write_bytes(wav_bytes)
    write_text(sound_root / "README-伴音方.md", sound_readme(wav_hash))
    write_text(sound_root / "listening-return-form.md", sound_form_md(wav_hash))
    write_csv(sound_root / "listening-return-form.csv", SOUND_FIELDS, [])

    picture_return_rows = [
        {
            "report_id": "P-R044-01",
            "reviewer": "External Picture Desk / deterministic exercise return",
            "frame_id_first": frame_id(TRUE_FRAME),
            "frame_id_last": frame_id(TRUE_FRAME),
            "exact_frame_id": frame_id(TRUE_FRAME),
            "observation_code": "contact_confirmed",
            "observation_text": "Hammer base and anvil edge meet in this paper frame; impact burst is visible.",
            "confidence": "high",
            "review_date": "2026-11-24",
        },
        {
            "report_id": "P-R044-02",
            "reviewer": "External Picture Desk / deterministic exercise return",
            "frame_id_first": frame_id(50),
            "frame_id_last": frame_id(55),
            "exact_frame_id": "",
            "observation_code": "frame_gap_no_exact_contact",
            "observation_text": "Roll numbering jumps F050 to F055. Approaching pose before gap and rebound pose after gap; unique contact frame is absent.",
            "confidence": "high",
            "review_date": "2026-11-24",
        },
    ]
    sound_return_rows = [
        {
            "report_id": "A-R044-01",
            "listener": "External Listening Desk / deterministic exercise return",
            "wav_sha256": wav_hash,
            "address_zero_based_sample": str(TRUE_SOUND_ADDRESS),
            "channel": "mono",
            "observation_code": "confirmed_strike",
            "listening_text": "Sharp attack with low wooden body and decaying ring; two careful passes agree at this sample.",
            "confidence": "high",
            "review_date": "2026-11-24",
        },
        {
            "report_id": "A-R044-02",
            "listener": "External Listening Desk / deterministic exercise return",
            "wav_sha256": wav_hash,
            "address_zero_based_sample": str(PATCH_EDGE_ADDRESS),
            "channel": "mono",
            "observation_code": "strike_like_click",
            "listening_text": "Very short hard click at a silent-to-resumed edge. Attack resembles a strike but body and ring are missing.",
            "confidence": "medium",
            "review_date": "2026-11-24",
        },
        {
            "report_id": "A-R044-03",
            "listener": "External Listening Desk / deterministic exercise return",
            "wav_sha256": wav_hash,
            "address_zero_based_sample": str(SECOND_SOUND_ADDRESS),
            "channel": "mono",
            "observation_code": "confirmed_strike",
            "listening_text": "Second full attack with low body and ring, 480 samples (60 ms) after the short edge click.",
            "confidence": "high",
            "review_date": "2026-11-24",
        },
    ]
    write_csv(returns_root / "picture-return-R044.csv", PICTURE_FIELDS, picture_return_rows)
    write_csv(returns_root / "sound-return-R044.csv", SOUND_FIELDS, sound_return_rows)
    write_text(returns_root / "README-回件说明.md", """# 回件收件箱

这两个 CSV 是本次可复现演练中班组交回的实际回报。真实外发时只发送两个 ZIP；
班组填回自己的 CSV 后，可替换本目录文件并重新运行：

```sh
python3 -m slatewatch.handoff_lab reconcile --root handoff/case-r044
python3 -m slatewatch.handoff_lab verify --root handoff/case-r044
```

交付方不得把一方回报转发给另一方。汇合和开单只在 `coordinator/` 中进行。
""")

    picture_pack = pack_root / "picture-team-pack.zip"
    sound_pack = pack_root / "sound-team-pack.zip"
    if picture_pack.exists():
        picture_pack.unlink()
    if sound_pack.exists():
        sound_pack.unlink()

    picture_files = [
        picture_root / "README-画面方.md",
        picture_root / "picture-roll.csv",
        picture_root / "picture-return-form.csv",
        picture_root / "picture-return-form.md",
        picture_root / "print-sheet.html",
        picture_root / "picture-handout.pdf",
    ] + [picture_root / "frames" / f"{frame_id(n)}.png" for n in all_print_frames]
    sound_files = [
        sound_root / "README-伴音方.md",
        sound_root / "alignment-call.wav",
        sound_root / "listening-return-form.csv",
        sound_root / "listening-return-form.md",
    ]
    make_zip(picture_pack, root, picture_files)
    make_zip(sound_pack, root, sound_files)

    ledger = {
        "case_id": CASE_ID,
        "schema_version": SCHEMA_VERSION,
        "generated_by": "python -m slatewatch.handoff_lab build",
        "address_conventions": {
            "wav_first_sample_address": 0,
            "sample_to_source_us": "address * 125",
            "frame_to_source_us": "source_frame_index * 40000",
            "frame_display_interval": "[frame_start_us, frame_start_us + 40000)",
        },
        "wav": {
            "relative_path": f"{SOUND_DIR}/alignment-call.wav",
            "sha256": wav_hash,
            "format": "RIFF/WAVE PCM16 mono 8000 Hz",
            "channels": 1,
            "sample_rate": SAMPLE_RATE,
            "total_samples": TOTAL_SAMPLES,
            "custom_metadata_chunks": [],
            "wav_origin_source_sample": 0,
        },
        "picture_frames": frame_hashes,
        "picture_gaps": [{
            "first_frame": PICTURE_HOLE_FIRST,
            "end_exclusive_frame": PICTURE_HOLE_END_EXCLUSIVE,
            "interval_us": [frame_start_us(PICTURE_HOLE_FIRST),
                            frame_start_us(PICTURE_HOLE_END_EXCLUSIVE)],
            "reason": "generated picture decode hole",
        }],
        "audio_patch_ranges": [{
            "first_wav_sample": AUDIO_PATCH_FIRST,
            "end_exclusive_wav_sample": AUDIO_PATCH_END_EXCLUSIVE,
            "wav_interval_us": [sample_start_us(AUDIO_PATCH_FIRST),
                                sample_start_us(AUDIO_PATCH_END_EXCLUSIVE)],
            "boundary_addresses": [AUDIO_PATCH_FIRST, PATCH_EDGE_ADDRESS],
            "provenance": "coordinator-known zero fill for an audio coverage hole",
        }],
        "sealed_signal_anchors": {
            "confirmed_delay": {
                "picture_frame": TRUE_FRAME,
                "picture_time_us": frame_start_us(TRUE_FRAME),
                "sound_address": TRUE_SOUND_ADDRESS,
                "sound_time_us": sample_start_us(TRUE_SOUND_ADDRESS),
                "delta_sound_minus_picture_us": sample_start_us(TRUE_SOUND_ADDRESS) - frame_start_us(TRUE_FRAME),
            },
            "second_cluster": {
                "missing_contact_frame": SECOND_CLUSTER_FRAME,
                "picture_bracket": [frame_id(50), frame_id(55)],
                "patch_edge_address": PATCH_EDGE_ADDRESS,
                "full_strike_address": SECOND_SOUND_ADDRESS,
            },
        },
        "rules": {
            "alignment_tolerance_us": ALIGNMENT_TOLERANCE_US,
            "require_exact_picture_frame_for_repair": True,
            "reject_patch_boundary_addresses": True,
        },
        "packs": {
            "picture": {
                "relative_path": f"{PACK_DIR}/picture-team-pack.zip",
                "sha256": sha256_file(picture_pack),
            },
            "sound": {
                "relative_path": f"{PACK_DIR}/sound-team-pack.zip",
                "sha256": sha256_file(sound_pack),
            },
        },
    }
    write_text(root / COORD_DIR / "private-ledger.json",
               json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")
    write_root_checksums(root)
    return ledger


def make_zip(zip_path: Path, root: Path, files: list[Path]) -> None:
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in sorted(files):
            arcname = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())


def write_root_checksums(root: Path) -> None:
    skip = {"SHA256SUMS", "coordinator/verification-result.json"}
    lines = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in skip:
            continue
        lines.append(f"{sha256_file(path)}  {relative}\n")
    (root / "SHA256SUMS").write_text("".join(lines), encoding="utf-8", newline="\n")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_frame(value: str) -> int | None:
    value = (value or "").strip()
    if len(value) == 4 and value.startswith("F") and value[1:].isdigit():
        return int(value[1:])
    return None


def in_patch_or_boundary(address: int, ledger: dict, slack: int = 2) -> dict | None:
    for patch in ledger["audio_patch_ranges"]:
        first = patch["first_wav_sample"]
        end = patch["end_exclusive_wav_sample"]
        if first <= address < end:
            return {"reason": "inside_zero_fill", "patch": patch}
        if min(abs(address - first), abs(address - end)) <= slack:
            return {"reason": "patch_boundary", "patch": patch}
    return None


def reconcile(root: Path) -> dict:
    ledger_path = root / COORD_DIR / "private-ledger.json"
    ledger = load_json(ledger_path)
    wav_path = root / ledger["wav"]["relative_path"]
    actual_wav_hash = sha256_file(wav_path)
    if actual_wav_hash != ledger["wav"]["sha256"]:
        raise ValueError("WAV hash does not match coordinator ledger")

    picture_rows = read_csv_rows(root / RETURN_DIR / "picture-return-R044.csv")
    sound_rows = read_csv_rows(root / RETURN_DIR / "sound-return-R044.csv")

    picture_events = []
    for row in picture_rows:
        exact = parse_frame(row.get("exact_frame_id", ""))
        first = parse_frame(row.get("frame_id_first", ""))
        last = parse_frame(row.get("frame_id_last", ""))
        picture_events.append({
            "report_id": row["report_id"],
            "exact_frame": exact,
            "exact_time_us": frame_start_us(exact) if exact is not None else None,
            "first_frame": first,
            "last_frame": last,
            "bracket_start_us": frame_start_us(first) if first is not None else None,
            # Half-open display interval of the last listed frame.
            "bracket_end_us": frame_start_us(last + 1) if last is not None else None,
            "code": row["observation_code"],
            "text": row["observation_text"],
            "confidence": row["confidence"],
        })

    sound_events = []
    for row in sound_rows:
        address = int(row["address_zero_based_sample"])
        if row.get("wav_sha256") and row["wav_sha256"] != actual_wav_hash:
            raise ValueError(f"sound return {row['report_id']} has wrong WAV hash")
        patch_flag = in_patch_or_boundary(address, ledger)
        sound_events.append({
            "report_id": row["report_id"],
            "address": address,
            "time_us": sample_start_us(address),
            "code": row["observation_code"],
            "text": row["listening_text"],
            "confidence": row["confidence"],
            "patch_flag": patch_flag,
        })

    exact_picture = next(e for e in picture_events if e["exact_frame"] == TRUE_FRAME)
    true_sound = next(e for e in sound_events if e["address"] == TRUE_SOUND_ADDRESS)
    delta_us = true_sound["time_us"] - exact_picture["exact_time_us"]
    if abs(delta_us) <= ALIGNMENT_TOLERANCE_US:
        raise AssertionError("seeded R044 event is expected to exceed sync tolerance")

    repair_id = "RO-R044-01"
    repair_order = {
        "repair_order_id": repair_id,
        "case_id": CASE_ID,
        "status": "issued_single_repair",
        "created_for": "confirmed_audio_late_alignment_fault",
        "picture_evidence": {
            "report_id": exact_picture["report_id"],
            "frame_id": frame_id(TRUE_FRAME),
            "frame_time_us": exact_picture["exact_time_us"],
            "png": f"{PICTURE_DIR}/frames/{frame_id(TRUE_FRAME)}.png",
            "sha256": ledger["picture_frames"][f"{PICTURE_DIR}/frames/{frame_id(TRUE_FRAME)}.png"],
        },
        "sound_evidence": {
            "report_id": true_sound["report_id"],
            "wav": ledger["wav"]["relative_path"],
            "wav_sha256": actual_wav_hash,
            "address_zero_based_sample": TRUE_SOUND_ADDRESS,
            "sound_time_us_on_coordinator_origin": true_sound["time_us"],
        },
        "measurement": {
            "delta_sound_minus_picture_us": delta_us,
            "tolerance_us": ALIGNMENT_TOLERANCE_US,
            "sample_delta": TRUE_SOUND_ADDRESS - TRUE_FRAME * (SAMPLE_RATE // FPS),
        },
        "instruction": (
            "Advance/move the sound earlier by 960 zero-based WAV-equivalent samples "
            "at 8000 Hz (120 ms), or apply the equivalent picture-late correction "
            "chosen by post production. Recheck both similar strikes after repair."
        ),
        "not_included": ["VC-R044-02"],
    }
    repair_md = f"""# 返修单 {repair_id}

状态：**准予一件返修**

## 已汇合且可实物核对的证据

- 画面回报 `{exact_picture['report_id']}`：准确接触格 `{frame_id(TRUE_FRAME)}`，画面坐标 `{exact_picture['exact_time_us']} us`。
- 伴音回报 `{true_sound['report_id']}`：WAV 零基样本地址 `{TRUE_SOUND_ADDRESS}`，汇合方映射为 `{true_sound['time_us']} us`。
- 声画差：声音晚 **{delta_us // 1000} ms**；允许容差 `±{ALIGNMENT_TOLERANCE_US // 1000} ms`。

## 返修要求

将该敲击声/伴音相对画面提前 **960 个 8 kHz 样本（120 ms）**，或由后期采用等效的画面延后处理。返修后必须复查两次相似动作，不能只看波形外观。

## 范围边界

本单只处理上述证据完整的一处。`VC-R044-02` 仍缺画面准确接触格且含补样边缘相似咔声，**未并入本返修单**。
"""
    write_text(root / COORD_DIR / "repair-orders" / f"{repair_id}.json",
               json.dumps(repair_order, ensure_ascii=False, indent=2) + "\n")
    write_text(root / COORD_DIR / "repair-orders" / f"{repair_id}.md", repair_md)

    gap_picture = next(e for e in picture_events if e["report_id"] == "P-R044-02")
    edge_sound = next(e for e in sound_events if e["address"] == PATCH_EDGE_ADDRESS)
    second_sound = next(e for e in sound_events if e["address"] == SECOND_SOUND_ADDRESS)
    naive_picture_time = frame_start_us(50)
    verification_id = "VC-R044-02"
    verification_case = {
        "verification_case_id": verification_id,
        "case_id": CASE_ID,
        "status": "held_needs_verification",
        "do_not_issue_repair_yet": True,
        "held_reports": [
            {
                "report_id": gap_picture["report_id"],
                "reason_code": "missing_evidence",
                "detail": "No unique exact contact frame; F051-F054 picture hole lies inside the action.",
                "bracket_frame_ids": [frame_id(50), frame_id(55)],
                "bracket_time_us": [gap_picture["bracket_start_us"], gap_picture["bracket_end_us"]],
            },
            {
                "report_id": edge_sound["report_id"],
                "reason_code": "similar_but_mismatched",
                "detail": "Reported strike-like click is on/within 2 samples of a coordinator-known zero-fill boundary.",
                "address": PATCH_EDGE_ADDRESS,
                "mapped_time_us": sample_start_us(PATCH_EDGE_ADDRESS),
                "patch_flag": edge_sound["patch_flag"],
            },
        ],
        "corroborating_report": {
            "report_id": second_sound["report_id"],
            "address": SECOND_SOUND_ADDRESS,
            "mapped_time_us": sample_start_us(SECOND_SOUND_ADDRESS),
            "note": "Full strike exists after the patch edge, but picture side still lacks an exact contact frame.",
        },
        "appearance_only_mispair_example": {
            "picture_frame": frame_id(50),
            "picture_time_us": naive_picture_time,
            "sound_address": PATCH_EDGE_ADDRESS,
            "sound_time_us": sample_start_us(PATCH_EDGE_ADDRESS),
            "apparent_delta_us": sample_start_us(PATCH_EDGE_ADDRESS) - naive_picture_time,
            "why_invalid": "F050 is only the last pre-hole approach frame and 17200 is an audio fill boundary; neither is an exact event anchor.",
        },
        "next_checks": [
            "Re-decode the original picture stream around source frames 51-54; do not infer the contact from adjacent paper poses.",
            "Check source audio coverage for samples 16400-17200 and compare the boundary click at 17200 with the full strike at 17680.",
            "Only after both exact anchors exist, decide whether this is a second fault or the same systematic offset.",
        ],
    }
    verification_md = f"""# 待核查夹 {verification_id}

状态：**扣留，暂不开返修单**

## 缺证回报

- `{gap_picture['report_id']}`：画面编号 `F050 → F055` 跳号，`F051–F054` 为画面洞；班组没有回报唯一接触格。
- 可见括号：`F050`（{gap_picture['bracket_start_us']} us）至 `F055` 显示区间结束（{gap_picture['bracket_end_us']} us）。

## 相似但错配的回报

- `{edge_sound['report_id']}` 把 WAV 样本 `{PATCH_EDGE_ADDRESS}` 报为类似敲击的短咔声。
- 汇合映射：`{sample_start_us(PATCH_EDGE_ADDRESS)} us`。
- 该地址是交付方专用台账中零补样区间 `[{AUDIO_PATCH_FIRST}, {AUDIO_PATCH_END_EXCLUSIVE})` 的后边界，不是完整敲击主体。
- 单凭外观/最近姿势可能把 `F050` 与该样本配成 +{(sample_start_us(PATCH_EDGE_ADDRESS) - naive_picture_time) // 1000} ms 的“第二处延迟”；该配对无效。

## 可留作核查的真声响

- `{second_sound['report_id']}` 在样本 `{SECOND_SOUND_ADDRESS}`（{sample_start_us(SECOND_SOUND_ADDRESS)} us）听到完整敲击。
- 但画面准确接触格仍缺，因此不能据此开返修。

## 下一步

{chr(10).join('- ' + item for item in verification_case['next_checks'])}
"""
    write_text(root / COORD_DIR / "verification-cases" / f"{verification_id}.json",
               json.dumps(verification_case, ensure_ascii=False, indent=2) + "\n")
    write_text(root / COORD_DIR / "verification-cases" / f"{verification_id}.md", verification_md)

    report = {
        "case_id": CASE_ID,
        "status": "reconciled",
        "repair_order_ids": [repair_id],
        "verification_case_ids": [verification_id],
        "decisions": [
            {
                "cluster": "confirmed_delay",
                "disposition": "issue_single_repair",
                "repair_order_id": repair_id,
                "delta_us": delta_us,
            },
            {
                "cluster": "second_hole_and_patch_cluster",
                "disposition": "hold_for_verification",
                "verification_case_id": verification_id,
                "reason_codes": ["missing_evidence", "similar_but_mismatched"],
            },
        ],
        "return_hashes": {
            "picture": sha256_file(root / RETURN_DIR / "picture-return-R044.csv"),
            "sound": sha256_file(root / RETURN_DIR / "sound-return-R044.csv"),
        },
    }
    write_text(root / COORD_DIR / "reconciliation-report.json",
               json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    write_text(root / COORD_DIR / "reconciliation-report.md", f"""# 汇合结论 {CASE_ID}

| 簇 | 结论 | 单据 |
|---|---|---|
| 第一次相似动作/声响 | 证据齐全，声音晚 {delta_us // 1000} ms，超过 ±{ALIGNMENT_TOLERANCE_US // 1000} ms | `{repair_id}` |
| 第二次附近画面洞/音频补样 | 缺证 + 相似但错配，留待核查 | `{verification_id}` |

只下发一件返修；不得把待核查夹中的推测配对写入返修单。
""")
    write_root_checksums(root)
    return report


def parse_wav_chunks(data: bytes) -> list[dict]:
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AssertionError("not a RIFF/WAVE file")
    position = 12
    chunks = []
    while position + 8 <= len(data):
        chunk_id = data[position:position + 4]
        size = struct.unpack("<I", data[position + 4:position + 8])[0]
        chunks.append({"id": chunk_id.decode("ascii"), "size": size, "offset": position + 8})
        position += 8 + size + (size & 1)
    return chunks


def read_wav_samples(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
            raise AssertionError("unexpected WAV format")
        frames = wav.readframes(wav.getnframes())
    return list(struct.unpack("<" + "h" * (len(frames) // 2), frames))


def energy(samples: list[int], first: int, end: int) -> float:
    first = max(0, first)
    end = min(len(samples), end)
    if end <= first:
        return 0.0
    return sum(x * x for x in samples[first:end]) / (end - first)


def verify(root: Path) -> dict:
    ledger = load_json(root / COORD_DIR / "private-ledger.json")
    reconciliation = load_json(root / COORD_DIR / "reconciliation-report.json")
    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        if not ok:
            raise AssertionError(f"{name}: {detail}")
        checks.append({"name": name, "ok": True, "detail": detail})

    for relative, expected in ledger["picture_frames"].items():
        check(f"hash {relative}", sha256_file(root / relative) == expected, "SHA-256 matches ledger")
    check("wav hash", sha256_file(root / ledger["wav"]["relative_path"]) == ledger["wav"]["sha256"],
          "SHA-256 matches ledger")
    for key in ("picture", "sound"):
        relative = ledger["packs"][key]["relative_path"]
        check(f"pack hash {relative}", sha256_file(root / relative) == ledger["packs"][key]["sha256"],
              "ZIP SHA-256 matches ledger")

    wav_path = root / ledger["wav"]["relative_path"]
    raw = wav_path.read_bytes()
    chunks = parse_wav_chunks(raw)
    chunk_ids = [c["id"] for c in chunks]
    check("standard wav chunks", chunk_ids == ["fmt ", "data"], f"chunks={chunk_ids}")
    check("no custom metadata", not any(x.lower() in {"list", "id3 ", "bext", "axml"} for x in chunk_ids),
          "only fmt/data are present")

    samples = read_wav_samples(wav_path)
    check("wav sample count", len(samples) == TOTAL_SAMPLES, f"{len(samples)} samples")
    patch_zero = all(x == 0 for x in samples[AUDIO_PATCH_FIRST:AUDIO_PATCH_END_EXCLUSIVE])
    check("zero fill region", patch_zero, f"[{AUDIO_PATCH_FIRST},{AUDIO_PATCH_END_EXCLUSIVE}) all zero")

    baseline = energy(samples, 4_000, 4_800)
    true_attack = energy(samples, TRUE_SOUND_ADDRESS, TRUE_SOUND_ADDRESS + 64)
    true_ring = energy(samples, TRUE_SOUND_ADDRESS + 120, TRUE_SOUND_ADDRESS + 420)
    edge_attack = energy(samples, PATCH_EDGE_ADDRESS, PATCH_EDGE_ADDRESS + 12)
    edge_body = energy(samples, PATCH_EDGE_ADDRESS + 80, PATCH_EDGE_ADDRESS + 360)
    second_attack = energy(samples, SECOND_SOUND_ADDRESS, SECOND_SOUND_ADDRESS + 64)
    second_ring = energy(samples, SECOND_SOUND_ADDRESS + 120, SECOND_SOUND_ADDRESS + 420)
    check("true attack measurable", true_attack > baseline * 20 + 1_000_000,
          f"attack={true_attack:.1f}, baseline={baseline:.1f}")
    check("true ring measurable", true_ring > baseline * 4 + 100_000,
          f"ring={true_ring:.1f}")
    check("patch edge click measurable", edge_attack > 2_000_000,
          f"edge_attack={edge_attack:.1f}")
    check("patch edge lacks strike ring", edge_body < true_ring * 0.25,
          f"edge_body={edge_body:.1f}, true_ring={true_ring:.1f}")
    check("second full strike measurable", second_attack > baseline * 20 + 1_000_000
          and second_ring > baseline * 4 + 100_000,
          f"attack={second_attack:.1f}, ring={second_ring:.1f}")

    with zipfile.ZipFile(root / ledger["packs"]["picture"]["relative_path"]) as zf:
        picture_names = zf.namelist()
    with zipfile.ZipFile(root / ledger["packs"]["sound"]["relative_path"]) as zf:
        sound_names = zf.namelist()
    check("picture pack has frames", any(n.endswith(".png") for n in picture_names),
          f"{len(picture_names)} members")
    check("picture pack has no audio/coordinator material",
          not any(n.lower().endswith((".wav", ".mp3", ".mka", ".json")) or COORD_DIR in n
                  for n in picture_names),
          "no WAV/JSON/coordinator members")
    check("sound pack has WAV and form",
          any(n.endswith("alignment-call.wav") for n in sound_names)
          and any(n.endswith(".csv") for n in sound_names),
          f"{len(sound_names)} members")
    check("sound pack has no picture/coordinator material",
          not any(n.lower().endswith((".png", ".jpg", ".jpeg", ".pdf", ".html", ".json"))
                  or COORD_DIR in n or RETURN_DIR in n for n in sound_names),
          "no picture/PDF/HTML/JSON/return members")

    def zip_text_hit(zip_path: Path, forbidden: list[str]) -> str:
        text_suffixes = {".md", ".csv", ".html", ".txt"}
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if Path(name).suffix.lower() not in text_suffixes:
                    continue
                text = zf.read(name).decode("utf-8", errors="ignore").lower()
                for token in forbidden:
                    if token.lower() in text:
                        return f"{name} contains {token}"
        return ""

    picture_leak = zip_text_hit(root / ledger["packs"]["picture"]["relative_path"], [
        "alignment-call", ".wav", "8960", "17200", "17680", "A-R044",
        "RO-R044", "VC-R044", "伴音", "声音", "听辨",
    ])
    check("picture pack has no sound return/conclusion", picture_leak == "",
          picture_leak or "text members contain no sound-side anchors")
    sound_leak = zip_text_hit(root / ledger["packs"]["sound"]["relative_path"], [
        "F025", "F050", "F051", "F055", "P-R044", "picture-team",
        "picture-roll", "picture-handout", "RO-R044", "VC-R044",
        "画格", "画面", "frame",
    ])
    check("sound pack has no picture return/conclusion", sound_leak == "",
          sound_leak or "text members contain no picture-side anchors")

    check("one repair order", reconciliation["repair_order_ids"] == ["RO-R044-01"],
          json.dumps(reconciliation["repair_order_ids"], ensure_ascii=False))
    check("one verification case", reconciliation["verification_case_ids"] == ["VC-R044-02"],
          json.dumps(reconciliation["verification_case_ids"], ensure_ascii=False))
    vc = load_json(root / COORD_DIR / "verification-cases" / "VC-R044-02.json")
    reasons = {item["reason_code"] for item in vc["held_reports"]}
    check("hold reasons", reasons == {"missing_evidence", "similar_but_mismatched"},
          ",".join(sorted(reasons)))

    result = {
        "ok": True,
        "case_id": CASE_ID,
        "check_count": len(checks),
        "checks": checks,
        "measurements": {
            "baseline_energy": baseline,
            "true_attack_energy": true_attack,
            "true_ring_energy": true_ring,
            "patch_edge_attack_energy": edge_attack,
            "patch_edge_body_energy": edge_body,
            "second_attack_energy": second_attack,
            "second_ring_energy": second_ring,
        },
        "independent_rerun": [
            "python3 -m slatewatch.handoff_lab build --root <clean-dir>",
            "python3 -m slatewatch.handoff_lab reconcile --root <clean-dir>",
            "python3 -m slatewatch.handoff_lab verify --root <clean-dir>",
        ],
    }
    write_text(root / COORD_DIR / "verification-result.json",
               json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    default_root = Path("handoff/case-r044")
    for name in ("build", "reconcile", "verify", "all"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, default=default_root)
    args = parser.parse_args(argv)

    if args.command in ("build", "all"):
        build_case(args.root)
    if args.command in ("reconcile", "all"):
        reconcile(args.root)
    if args.command in ("verify", "all"):
        result = verify(args.root)
        print(json.dumps({"ok": True, "check_count": result["check_count"]}, ensure_ascii=False))
    elif args.command == "reconcile":
        report = reconcile(args.root)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "build":
        print(json.dumps({"ok": True, "root": str(args.root)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
