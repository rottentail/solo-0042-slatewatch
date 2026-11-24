#!/usr/bin/env python3
"""Build a reproducible repair-desk handoff and the sender-side reference.

Run from the repository root with PyAV available:

    python repair-handoff/build_repair_handoff.py

The script deliberately decodes the generated MKV through Slatewatch's own
media/scan modules.  Every outgoing PNG and WAV sample is therefore extracted
from the generated source rather than drawn separately for the handoff.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import shutil
import struct
import subprocess
import sys
import wave
import zipfile
import zlib
from array import array
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    import av  # noqa: F401
except ImportError as exc:  # pragma: no cover - explicit operator guidance
    raise SystemExit(
        "PyAV is required to build the source sample. Install requirements.txt "
        "in Python 3.12+, or put an ABI-compatible PyAV on PYTHONPATH."
    ) from exc

from slatewatch.clock import SECOND  # noqa: E402
from slatewatch.evidence import write_wav  # noqa: E402
from slatewatch.media import inspect, decode  # noqa: E402
from slatewatch.picture import differences, nominal_duration  # noqa: E402
from slatewatch.rules import new_policy  # noqa: E402
from slatewatch.scan import measure  # noqa: E402

ITEM = "R045"
SCHEMA = "repair-desk-address-list/v1"
SOURCE_NAME = "repair-freeze-lab-r045.mkv"
RATE = 8000
SRC_W, SRC_H = 160, 96
THUMB_W, THUMB_H = 80, 48
TOTAL_US = 10 * SECOND
TOTAL_SAMPLES = 80_000
PICTURE_HOLE = (5_000_000, 6_000_000)

LONG_TARGETS = [("T-L1", 3_700_000), ("T-L2", 4_200_000)]
BEFORE_TARGET = ("T-B0", 4_750_000)
AFTER_TARGET = ("T-A0", 6_250_000)
ONSET_TARGET = ("T-P3", 3_250_000)
POST_SECOND_TARGET = ("T-A1", 6_750_000)

PRIMARY_AUDIO = (4_400_000, 7_600_000)
ALT_AUDIO = (4_500_000, 6_250_000)

AUDIO_GAPS_S = [(5.60, 5.80), (6.80, 7.10)]


# ---------------------------------------------------------------------------
# Small generic helpers
# ---------------------------------------------------------------------------

def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def tc(us: int) -> str:
    sign = "-" if us < 0 else ""
    us = abs(us)
    h, rem = divmod(us, 3_600_000_000)
    m, rem = divmod(rem, 60_000_000)
    s, micro = divmod(rem, 1_000_000)
    return f"{sign}{h:02d}:{m:02d}:{s:02d}.{micro:06d}"


def us_samples(us: int) -> int:
    return us * RATE // SECOND


def coalesce_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((a, b) for a, b in ranges if a < b)
    result: list[list[int]] = []
    for start, end in ordered:
        if not result or start > result[-1][1]:
            result.append([start, end])
        else:
            result[-1][1] = max(result[-1][1], end)
    return [(a, b) for a, b in result]


def uncovered_ranges(
    coverage: list[tuple[int, int]], start: int, end: int
) -> list[dict[str, int]]:
    cursor = start
    result: list[dict[str, int]] = []
    for cov_start, cov_end in coalesce_ranges(coverage):
        a, b = max(cov_start, start), min(cov_end, end)
        if a >= b:
            continue
        if cursor < a:
            result.append({"first": cursor, "end": a})
        cursor = max(cursor, b)
    if cursor < end:
        result.append({"first": cursor, "end": end})
    return result


def region_metrics(values: array, first: int, start: int, end: int) -> dict[str, int]:
    segment = values[start - first:end - first]
    if not segment:
        raise AssertionError(f"empty audio region {start}..{end}")
    squares = sum(int(v) * int(v) for v in segment)
    return {
        "rms": int(math.sqrt(squares / len(segment))),
        "peak": max(abs(int(v)) for v in segment),
        "samples": len(segment),
    }


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def gray_png(pixels: bytes, width: int = THUMB_W, height: int = THUMB_H) -> bytes:
    if len(pixels) != width * height:
        raise AssertionError(f"expected {width * height} gray bytes, got {len(pixels)}")
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 for this scanline
        raw.extend(pixels[y * width:(y + 1) * width])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + png_chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# Reproducible source master
# ---------------------------------------------------------------------------

def frame_times_ms() -> list[int]:
    # Regular 0.5 s cells until the long cell; omit 4000 ms to make V3500 a
    # full one-second presented cell.  5000/5500 ms are the picture hole.
    return list(range(0, 3501, 500)) + [4500] + list(range(6000, 10_000, 500))


def source_raster(pts_ms: int) -> bytes:
    width, height = SRC_W, SRC_H
    freeze = pts_ms >= 3000
    seed = 9001 if freeze else 101 + pts_ms
    pixels = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            value = 38 + ((x * 2 + y * 3 + seed * 7) % 168)
            if (x + y * 2 + seed) % 34 < 7:
                value = max(12, value - 34)
            if (x * 3 - y + seed) % 47 < 3:
                value = min(245, value + 38)
            # Frame-like border and a fixed registration block.  For the
            # freeze/twin cells every input byte is deliberately identical.
            if x < 5 or y < 5 or x >= width - 5 or y >= height - 5:
                value = 226
            if 12 <= x < 42 and 12 <= y < 22:
                value = 28 if ((x // 3 + y // 2) % 2) else 214
            if 112 <= x < 148 and 66 <= y < 82:
                value = 246 if ((x // 4 + y // 3) % 2) else 42
            pixels[y * width + x] = value & 0xFF
    return bytes(pixels)


def tone(sample: int, freq: float, amplitude: int) -> int:
    return round(amplitude * math.sin(2.0 * math.pi * freq * sample / RATE))


def source_sample_value(sample: int) -> int:
    t_ms = sample * 1000 / RATE
    if 4500 <= t_ms < 4700:
        return tone(sample, 660, 12_000)
    if 4700 <= t_ms < 5000:
        return 0  # muxed PCM: real recorded digital silence
    if 5000 <= t_ms < 5600:
        return tone(sample, 220, 5000)
    if 5800 <= t_ms < 6000:
        return tone(sample, 220, 5000)
    if 6000 <= t_ms < 6200:
        return tone(sample, 880, 12_000)
    if 6300 <= t_ms < 6600:
        return 0  # second covered zero run retained in sender reference
    if 7100 <= t_ms < 7300:
        return tone(sample, 1320, 10_000)
    return tone(sample, 330, 2500)


def in_audio_gap(first_sample: int) -> bool:
    t_ms = first_sample * 1000 / RATE
    return any(start * 1000 <= t_ms < end * 1000 for start, end in AUDIO_GAPS_S)


def generate_master(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    times = frame_times_ms()
    with av.open(str(path), mode="w", format="matroska") as container:
        video = container.add_stream("ffv1", rate=Fraction(2, 1))
        video.width = SRC_W
        video.height = SRC_H
        video.pix_fmt = "gray"

        audio = container.add_stream("pcm_s16le", rate=RATE)
        audio.layout = "mono"

        next_sample = 0
        frame_index = 0
        while frame_index < len(times) or next_sample < TOTAL_SAMPLES:
            if frame_index < len(times):
                pts_ms = times[frame_index]
                while next_sample < TOTAL_SAMPLES and next_sample / RATE <= pts_ms / 1000:
                    if not in_audio_gap(next_sample):
                        values = array(
                            "h",
                            (source_sample_value(n)
                             for n in range(next_sample, next_sample + 160)),
                        )
                        if sys.byteorder != "little":
                            values.byteswap()
                        block = av.AudioFrame(
                            format="s16", layout="mono", samples=160
                        )
                        block.planes[0].update(values.tobytes())
                        block.sample_rate = RATE
                        block.time_base = Fraction(1, RATE)
                        block.pts = next_sample
                        container.mux(audio.encode(block))
                    next_sample += 160

                frame = av.VideoFrame(SRC_W, SRC_H, "gray")
                plane = frame.planes[0]
                raw = bytearray(plane.buffer_size)
                raster = source_raster(pts_ms)
                for y in range(SRC_H):
                    start = y * plane.line_size
                    raw[start:start + SRC_W] = raster[y * SRC_W:(y + 1) * SRC_W]
                plane.update(raw)
                frame.pts = pts_ms
                frame.time_base = Fraction(1, 1000)
                container.mux(video.encode(frame))
                frame_index += 1
            else:
                while next_sample < TOTAL_SAMPLES:
                    if not in_audio_gap(next_sample):
                        values = array(
                            "h",
                            (source_sample_value(n)
                             for n in range(next_sample, next_sample + 160)),
                        )
                        if sys.byteorder != "little":
                            values.byteswap()
                        block = av.AudioFrame(
                            format="s16", layout="mono", samples=160
                        )
                        block.planes[0].update(values.tobytes())
                        block.sample_rate = RATE
                        block.time_base = Fraction(1, RATE)
                        block.pts = next_sample
                        container.mux(audio.encode(block))
                    next_sample += 160

        container.mux(video.encode())
        container.mux(audio.encode())


# ---------------------------------------------------------------------------
# Decoded source model
# ---------------------------------------------------------------------------

class SourceModel:
    def __init__(self, master: Path):
        self.master = master
        self.info = inspect(master, 0, "first")
        if self.info["sound"] is None:
            raise AssertionError("sample must contain an audio track")
        if self.info["sound"]["channels"] != 1:
            raise AssertionError("handoff WAV must be mono; source should be mono too")

        policy = new_policy({"label": "R045 reproducible handoff policy"})
        self.scan = measure({"id": "master-r045", "media": self.info}, master, policy)

        self.pictures: list[Any] = []
        self.pcm = array("h", [0]) * TOTAL_SAMPLES
        self.audio_coverage: list[tuple[int, int]] = []

        for block in decode(master, self.info):
            if hasattr(block, "pixels"):
                if (len(block.pixels) != THUMB_W * THUMB_H):
                    raise AssertionError("unexpected evidence thumbnail geometry")
                self.pictures.append(block)
                continue

            begin = max(int(block.first_sample), 0)
            finish = min(int(block.end_sample), TOTAL_SAMPLES)
            if begin >= finish:
                continue
            offset = begin - int(block.first_sample)
            count = finish - begin
            self.pcm[begin:finish] = block.samples[offset:offset + count]
            self.audio_coverage.append((begin, finish))

        if len(self.pictures) != len(frame_times_ms()):
            raise AssertionError(
                f"expected {len(frame_times_ms())} pictures, decoded {len(self.pictures)}"
            )

        self.audio_coverage = coalesce_ranges(self.audio_coverage)
        self.picture_coverage = [
            (int(s["start_us"]), int(s["end_us"]))
            for s in self.scan["coverage"]["picture"]
        ]
        self.next_pts = [p.timestamp_us for p in self.pictures[1:]] + [TOTAL_US]
        nominal = nominal_duration(self.info["picture"])
        gap_cutoff_base = int(self.scan["thresholds"]["picture_gap_ms"]) * 1000

        self.frame_ends: list[int] = []
        for i, picture in enumerate(self.pictures):
            start = picture.timestamp_us
            duration_hint = picture.duration_us or nominal
            cutoff = max(gap_cutoff_base, duration_hint + nominal)
            if i + 1 < len(self.pictures):
                step = self.next_pts[i] - start
                end = (min(start + duration_hint, self.next_pts[i])
                       if step > cutoff else self.next_pts[i])
            else:
                end = start + duration_hint
            self.frame_ends.append(end)

    def frame_id(self, index: int) -> str:
        pts_ms = self.pictures[index].timestamp_us // 1000
        return f"V{index + 1:02d}-{pts_ms:05d}ms"

    def target_in_picture_coverage(self, when_us: int) -> bool:
        return any(start <= when_us < end for start, end in self.picture_coverage)

    def frame_index_at(self, when_us: int) -> int:
        if not self.target_in_picture_coverage(when_us):
            raise AssertionError(f"target {when_us} lies in picture non-coverage")
        for i, picture in enumerate(self.pictures):
            if picture.timestamp_us <= when_us < self.next_pts[i]:
                return i
        raise AssertionError(f"no source frame maps target {when_us}")

    def source_frame_record(self, index: int) -> dict[str, Any]:
        picture = self.pictures[index]
        record = {
            "frame_id": self.frame_id(index),
            "stream_index": self.info["picture"]["index"],
            "decoded_pts_us": picture.timestamp_us,
            "decoded_pts_timecode": tc(picture.timestamp_us),
            "decoded_duration_hint_us": picture.duration_us,
            "next_decoded_pts_us": self.next_pts[index],
            "source_coverage_span_us": [picture.timestamp_us, self.frame_ends[index]],
            "source_coverage_timecode": [
                tc(picture.timestamp_us), tc(self.frame_ends[index])
            ],
            "raw_gray_80x48_sha256": sha_bytes(picture.pixels),
        }
        if index > 0:
            delta, edge_delta = differences(
                self.pictures[index - 1].pixels, picture.pixels
            )
            record["difference_from_previous_permille"] = {
                "mean_abs_luma": delta,
                "edge_delta": edge_delta,
                "note": "0/0 means the same measured gray pixels as the prior source frame.",
            }
        return record

    def build_slot(
        self,
        slot_no: int,
        key: str,
        title: str,
        targets: list[tuple[str, int]],
        package_dir: Path,
    ) -> dict[str, Any]:
        indices = {self.frame_index_at(when) for _, when in targets}
        if len(indices) != 1:
            raise AssertionError(f"slot {key} targets do not map to one source frame")
        index = next(iter(indices))
        picture = self.pictures[index]
        relative = Path("frames") / f"slot-{slot_no}.png"
        out = package_dir / relative
        out.parent.mkdir(parents=True, exist_ok=True)
        png = gray_png(picture.pixels)
        out.write_bytes(png)

        target_records = [
            {
                "label": label,
                "program_us": when,
                "program_timecode": tc(when),
                "source_frame_id": self.frame_id(self.frame_index_at(when)),
            }
            for label, when in targets
        ]
        return {
            "slot": slot_no,
            "key": key,
            "title": title,
            "file": relative.as_posix(),
            "composition": "single-source-frame",
            "composition_note": "Exactly one decoded source picture; no collage or tile.",
            "width": THUMB_W,
            "height": THUMB_H,
            "sha256": sha_bytes(png),
            "raw_gray_sha256": sha_bytes(picture.pixels),
            "targets": target_records,
            "source_frame": self.source_frame_record(index),
        }

    def audio_attachment(
        self,
        span_us: tuple[int, int],
        relative_wav: str,
        package_dir: Path,
        region_specs: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], array]:
        start_us, end_us = span_us
        first = us_samples(start_us)
        end = us_samples(end_us)
        values = self.pcm[first:end]
        wav_bytes = write_wav(values, RATE, 1)
        wav_path = package_dir / relative_wav
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        wav_path.write_bytes(wav_bytes)

        covered = [
            [max(a, first), min(b, end)]
            for a, b in self.audio_coverage
            if max(a, first) < min(b, end)
        ]
        filled = uncovered_ranges(self.audio_coverage, first, end)

        regions: list[dict[str, Any]] = []
        for spec in region_specs:
            a = us_samples(spec["start_us"])
            b = us_samples(spec["end_us"])
            if spec["kind"] == "extract_fill":
                coverage = "uncovered_by_source_extract_zero_fill"
            else:
                fully_covered = any(c0 <= a and b <= c1 for c0, c1 in covered)
                if not fully_covered:
                    raise AssertionError(f"region {spec['id']} is not source-covered")
                coverage = "source_covered"
            metrics = region_metrics(values, first, a, b)
            regions.append({
                "id": spec["id"],
                "label": spec["label"],
                "kind": spec["kind"],
                "coverage": coverage,
                "source_time_us": [spec["start_us"], spec["end_us"]],
                "source_timecode": [tc(spec["start_us"]), tc(spec["end_us"])],
                "source_samples": [a, b],
                "wav_relative_samples": [a - first, b - first],
                "metrics": metrics,
            })

        manifest = {
            "file": relative_wav,
            "sha256": sha_bytes(wav_bytes),
            "format": "WAV / PCM signed 16-bit little-endian",
            "channels": 1,
            "sample_rate": RATE,
            "first_source_sample": first,
            "end_source_sample": end,
            "source_sample_count": end - first,
            "first_source_time_us": start_us,
            "end_source_time_us": end_us,
            "source_timecode": [tc(start_us), tc(end_us)],
            "source_covered_sample_ranges": [
                {"first": a, "end": b,
                 "time_us": [round(a * SECOND / RATE), round(b * SECOND / RATE)]}
                for a, b in covered
            ],
            "filled_zero_sample_ranges": [
                {
                    **item,
                    "reason": "No source PCM coverage; evidence extractor inserted zero samples.",
                    "time_us": [
                        round(item["first"] * SECOND / RATE),
                        round(item["end"] * SECOND / RATE),
                    ],
                }
                for item in filled
            ],
            "regions": regions,
        }
        return manifest, values


# ---------------------------------------------------------------------------
# Manifest and document rendering
# ---------------------------------------------------------------------------

def base_manifest(model: SourceModel, package_name: str, selection: str) -> dict[str, Any]:
    master_hash = sha_file(model.master)
    return {
        "schema": SCHEMA,
        "item_id": ITEM,
        "package": package_name,
        "selection": selection,
        "reproducible_source": {
            "generator": "repair-handoff/build_repair_handoff.py",
            "command": "python repair-handoff/build_repair_handoff.py",
            "sample": SOURCE_NAME,
            "sha256": master_hash,
            "note": "Synthetic but deterministically generated source; all evidence is decoded from this MKV.",
        },
        "offline": {
            "server_required": False,
            "master_required_by_receiver": False,
            "network_required": False,
            "verifier": "python3 verify_handoff.py .",
        },
        "clock": {
            "unit": "integer microseconds",
            "interval_convention": "half-open [start_us, end_us)",
            "origin": "Matroska shared container origin",
        },
        "source_master": {
            "filename": SOURCE_NAME,
            "sha256": master_hash,
            "container_format": model.info["format"],
            "origin_us": model.info["origin_us"],
            "video_stream_index": model.info["picture"]["index"],
            "audio_stream_index": model.info["sound"]["index"],
        },
        "picture_hole": {
            "address": "P-GAP-5000000-6000000",
            "start_us": PICTURE_HOLE[0],
            "end_us": PICTURE_HOLE[1],
            "timecode": [tc(PICTURE_HOLE[0]), tc(PICTURE_HOLE[1])],
            "coverage_spans_us": model.picture_coverage,
        },
        "slots": [],
        "audio_attachment": None,
        "judgments": {},
    }


def render_addresses_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        f"# {manifest['item_id']} 随单源地址清单",
        "",
        "所有时间为整数微秒，区间按左闭右开 `[start_us,end_us)` 解释。",
        "本清单与三个格位、一个 WAV 一起离线使用；不需要母带或服务端。",
        "",
        "## 画格格位",
        "",
        "| 格位 | 文件 | 目标地址 | 唯一源格 | 源覆盖区间 | PNG SHA-256 前16位 |",
        "|---:|---|---|---|---|---|",
    ]
    for slot in manifest["slots"]:
        targets = "<br>".join(
            f"{t['label']}@{t['program_us']} → {t['source_frame_id']}"
            for t in slot["targets"]
        )
        frame = slot["source_frame"]
        lines.append(
            f"| {slot['slot']} | `{slot['file']}` | {targets} | "
            f"`{frame['frame_id']}` | `{frame['source_coverage_span_us'][0]}.."
            f"{frame['source_coverage_span_us'][1]}` | `{slot['sha256'][:16]}` |"
        )

    audio = manifest["audio_attachment"]
    lines += [
        "",
        "## WAV 地址",
        "",
        f"- 文件：`{audio['file']}`",
        f"- 格式：{audio['channels']} 声道 / {audio['sample_rate']} Hz / 16-bit PCM",
        f"- 绝对源采样：`[{audio['first_source_sample']}, {audio['end_source_sample']})`",
        f"- 源时间：`{audio['source_timecode'][0]}` → `{audio['source_timecode'][1]}`",
        "- 摘片补零区间（不是源内实录静音）：",
    ]
    for item in audio["filled_zero_sample_ranges"]:
        lines.append(
            f"  - 采样 `[{item['first']},{item['end']})`，"
            f"时间 `{tc(item['time_us'][0])}–{tc(item['time_us'][1])}`：{item['reason']}"
        )
    lines += [
        "",
        "| 区段 | 类型 | 覆盖 | 源采样 | RMS | Peak |",
        "|---|---|---|---:|---:|---:|",
    ]
    for region in audio["regions"]:
        m = region["metrics"]
        lines.append(
            f"| {region['id']} {region['label']} | `{region['kind']}` | "
            f"`{region['coverage']}` | `{region['source_samples'][0]}.."
            f"{region['source_samples'][1]}` | {m['rms']} | {m['peak']} |"
        )

    lines += ["", "## 判定地址", "", "```json",
              json.dumps(manifest["judgments"], ensure_ascii=False, indent=2),
              "```", ""]
    return "\n".join(lines)


CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans CJK SC','Microsoft YaHei',sans-serif;
 margin:0;background:#f4f1ea;color:#1f2933;line-height:1.55}
main{max-width:1180px;margin:0 auto;padding:28px}
header,section{background:#fffdf8;border:1px solid #d8d0bf;border-radius:10px;padding:20px;margin:18px 0;
 box-shadow:0 1px 2px rgba(60,45,20,.08)}
h1{margin:0 0 6px;font-size:28px} h2{margin-top:0;border-bottom:2px solid #b45309;padding-bottom:6px}
.offline{display:inline-block;background:#ecfdf5;border:1px solid #047857;color:#065f46;border-radius:999px;
 padding:4px 12px;font-weight:700}
.answer{border-left:5px solid #b45309;background:#fff7ed;padding:12px 16px;margin:12px 0;border-radius:6px}
.slots{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
figure{margin:0;border:2px solid #64748b;border-radius:8px;background:#0f172a;color:white;padding:10px}
figure img{width:100%;height:auto;image-rendering:pixelated;background:#94a3b8;border:1px solid #cbd5e1;display:block}
figcaption{font-size:13px;margin-top:8px}.slotno{font-weight:800;color:#fbbf24}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #d6d3d1;padding:6px 8px;
 vertical-align:top;text-align:left}th{background:#f5f5f4}code{background:#f5f5f4;padding:1px 4px;border-radius:4px}
audio{width:100%;margin:10px 0}.warn{background:#fef2f2;border-color:#b91c1c}.ok{color:#166534;font-weight:700}
.small{font-size:13px;color:#57534e}
"""


def render_html(manifest: dict[str, Any]) -> str:
    slot_cards: list[str] = []
    for slot in manifest["slots"]:
        targets = "<br>".join(
            f"{html.escape(t['label'])} @ <code>{t['program_us']}</code><br>→ "
            f"<code>{html.escape(t['source_frame_id'])}</code>"
            for t in slot["targets"]
        )
        frame = slot["source_frame"]
        slot_cards.append(f"""
        <figure>
          <div class="slotno">格位 {slot['slot']} / 3</div>
          <img src="{html.escape(slot['file'])}" alt="格位 {slot['slot']}：{html.escape(slot['title'])}">
          <figcaption>
            <strong>{html.escape(slot['title'])}</strong><br>
            目标：{targets}<br>
            唯一源格：<code>{html.escape(frame['frame_id'])}</code><br>
            源覆盖：<code>{frame['source_coverage_span_us'][0]}–{frame['source_coverage_span_us'][1]}</code> μs
          </figcaption>
        </figure>""")

    audio = manifest["audio_attachment"]
    fill_rows = "".join(
        f"<li><code>[{x['first']},{x['end']})</code> 采样 / "
        f"<code>{x['time_us'][0]}–{x['time_us'][1]}</code> μs：摘片补零，不是实录静音</li>"
        for x in audio["filled_zero_sample_ranges"]
    )
    region_rows = "".join(
        f"<tr><td>{html.escape(r['id'])}</td><td>{html.escape(r['label'])}</td>"
        f"<td><code>{html.escape(r['kind'])}</code></td>"
        f"<td><code>{r['source_samples'][0]}–{r['source_samples'][1]}</code></td>"
        f"<td>{r['metrics']['rms']}</td><td>{r['metrics']['peak']}</td></tr>"
        for r in audio["regions"]
    )
    j = manifest["judgments"]
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{ITEM} 远端返修棚交接单</title>
<style>{CSS}</style>
</head>
<body><main>
<header>
  <h1>{ITEM} 远端返修棚交接单</h1>
  <span class="offline">离线可核：3 个格位 · 1 个单声道 WAV · 随单地址清单 · 不需服务端/母带</span>
  <p class="small">源母带 SHA-256：<code>{manifest['source_master']['sha256']}</code><br>
  无图段地址：<code>{manifest['picture_hole']['start_us']}–{manifest['picture_hole']['end_us']} μs</code>
  （{html.escape(manifest['picture_hole']['timecode'][0])} → {html.escape(manifest['picture_hole']['timecode'][1])}）</p>
</header>

<section>
  <h2>复核结论</h2>
  <div class="answer">
    <strong>判断一：看似连续的冻帧没有同一个源格跨过无图段。</strong><br>
    格位 2 是洞前最后源格 <code>{j['cross_hole_twins']['before_frame_id']}</code>；
    格位 3 是洞后第一源格 <code>{j['cross_hole_twins']['after_frame_id']}</code>。
    两格灰阶像素 SHA-256 完全相同，但源地址不同，中间是无图段
    <code>{manifest['picture_hole']['start_us']}–{manifest['picture_hole']['end_us']} μs</code>。
    这是“同像素的两个源格”，不是一个长格跨洞。格位 1 的两个目标地址都映射到同一源格，
    用来说明清单中“多目标点 → 同一长格”的真实形态。
  </div>
  <div class="answer">
    <strong>判断二：无图段两边的伴音都确实来自源。</strong><br>
    洞前标记 <code>{j['audio_both_sides_in_source']['before_region_id']}</code> 与洞后标记
    <code>{j['audio_both_sides_in_source']['after_region_id']}</code> 均在清单列为
    <code>source_covered</code> 的原始 PCM 区间。WAV 中的零声有两类：
    清单标 <code>recorded_silence</code> 的是源内实录零声；标 <code>extract_fill</code>
    或 <code>filled_zero_sample_ranges</code> 的是无覆盖区间的摘片补零。
  </div>
  <p>可选离线自检：在本 ZIP 解压根目录运行 <code>python3 verify_handoff.py .</code>，
  或先运行 <code>sha256sum -c SHA256SUMS</code>。</p>
</section>

<section>
  <h2>三个静态画格格位（禁止拼图并入同一格）</h2>
  <div class="slots">{''.join(slot_cards)}</div>
  <p class="small">三个文件均为从源解码缩略平面写出的单个 80×48 灰阶 PNG；CSS 仅做放大显示，不改变格位数量或拼接内容。格位 2、3 文件哈希相同是本样件故意保留的“同像素、不同源格”情况。</p>
</section>

<section>
  <h2>唯一 WAV 附件：可直接打开</h2>
  <audio controls preload="none" src="{html.escape(audio['file'])}">当前环境不支持 audio 播放，请直接打开 {html.escape(audio['file'])}</audio>
  <p><a href="{html.escape(audio['file'])}"><code>{html.escape(audio['file'])}</code></a>
     （mono / {audio['sample_rate']} Hz / 16-bit，源采样 <code>[{audio['first_source_sample']},{audio['end_source_sample']})</code>）</p>
  <h3>必须按地址清单区分的补零段</h3>
  <ul>{fill_rows}</ul>
  <table><thead><tr><th>ID</th><th>区段</th><th>类型</th><th>源采样地址</th><th>RMS</th><th>Peak</th></tr></thead>
  <tbody>{region_rows}</tbody></table>
</section>

<section>
  <h2>随单地址清单（机器可读原件：addresses.json）</h2>
  <p>
    <a href="addresses.json"><code>addresses.json</code></a> ·
    <a href="addresses.md"><code>addresses.md</code> 可读地址表</a> ·
    <a href="SHA256SUMS"><code>SHA256SUMS</code></a>
  </p>
  <p>完整 JSON 与本单一起交付；上表为伴音区段，画格地址见各格位说明。</p>
  <p class="ok">本件不包含服务端 URL、取证件 token 或要求回服务端的步骤。</p>
</section>
</main></body></html>
"""


def render_review_markdown(manifest: dict[str, Any]) -> str:
    slot_images = "\n\n".join(
        f"### 格位 {s['slot']}：{s['title']}\n\n"
        f"![格位 {s['slot']}]({s['file']})\n\n"
        f"- 文件：`{s['file']}`\n"
        f"- 唯一源格：`{s['source_frame']['frame_id']}`\n"
        f"- 源覆盖：`{s['source_frame']['source_coverage_span_us'][0]}.."
        f"{s['source_frame']['source_coverage_span_us'][1]}` μs\n"
        + "".join(
            f"- 目标 `{t['label']}` @ `{t['program_us']}` → `{t['source_frame_id']}`\n"
            for t in s["targets"]
        )
        for s in manifest["slots"]
    )
    audio = manifest["audio_attachment"]
    return f"""# {ITEM} 交接单（三格位 / 单声道 WAV）

离线复核，不需要母带或服务端。机器地址见 [`addresses.json`](addresses.json)，
可读地址表见 [`addresses.md`](addresses.md)。

## 结论

1. **冻帧没有同一个源格跨过无图段。** 格位 2、3 像素和 PNG 哈希相同，但地址清单给出不同源格；中间是 `{PICTURE_HOLE[0]}..{PICTURE_HOLE[1]}` μs 无图段。
2. **无图段两边伴音均在源 PCM 覆盖内。** 核对 `{manifest['judgments']['audio_both_sides_in_source']['before_region_id']}` 与 `{manifest['judgments']['audio_both_sides_in_source']['after_region_id']}`。
3. WAV 中实录零声标为 `recorded_silence`；摘片补零标为 `extract_fill` / `filled_zero_sample_ranges`，不能只凭“听起来是零”判断。

## 三个格位

{slot_images}

## 唯一 WAV

[`{audio['file']}`]({audio['file']})

- mono / {audio['sample_rate']} Hz / 16-bit
- 绝对源采样：`[{audio['first_source_sample']},{audio['end_source_sample']})`
- 自检：`python3 verify_handoff.py .`
- 校验和：`sha256sum -c SHA256SUMS`
"""


def write_checksums(root: Path) -> None:
    lines: list[str] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.name == "SHA256SUMS":
            continue
        lines.append(f"{sha_file(path)}  {path.relative_to(root).as_posix()}")
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_zip(stage: Path, zip_path: Path, root_name: str) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in stage.rglob("*") if p.is_file()):
            arc = Path(root_name) / path.relative_to(stage)
            info = zipfile.ZipInfo(arc.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def run_verifier(root: Path, manifest_name: str) -> tuple[int, str]:
    verifier = root / "verify_handoff.py"
    proc = subprocess.run(
        [sys.executable, str(verifier), "--root", str(root), "--manifest", manifest_name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return proc.returncode, proc.stdout


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def build(out_root: Path) -> dict[str, Any]:
    outgoing_root = out_root / "outgoing"
    sender = out_root / "sender-reference"
    stage_name = f"repair-desk-{ITEM}"
    stage = outgoing_root / stage_name
    zip_path = outgoing_root / f"{stage_name}.zip"
    alt_dir = sender / "alternative-selection"

    shutil.rmtree(outgoing_root, ignore_errors=True)
    shutil.rmtree(sender, ignore_errors=True)
    stage.mkdir(parents=True)
    (sender / "source").mkdir(parents=True)
    (sender / "frame-ledger" / "png").mkdir(parents=True)
    alt_dir.mkdir(parents=True)

    master = sender / "source" / SOURCE_NAME
    generate_master(master)
    model = SourceModel(master)

    write_json(sender / "source" / "inspect.json", model.info)
    write_json(sender / "source" / "scan.json", model.scan)

    # Full source WAV for sender-side convenience.
    full_wav = sender / "source" / "full-source-8k-mono.wav"
    full_wav.write_bytes(write_wav(model.pcm, RATE, 1))

    # Complete frame ledger remains on sender side.
    ledger: list[dict[str, Any]] = []
    for i, picture in enumerate(model.pictures):
        record = model.source_frame_record(i)
        png = gray_png(picture.pixels)
        png_rel = Path("frame-ledger/png") / f"{record['frame_id']}.png"
        (sender / png_rel).write_bytes(png)
        record["ledger_png"] = png_rel.as_posix()
        record["ledger_png_sha256"] = sha_bytes(png)
        ledger.append(record)
    write_json(sender / "frame-ledger" / "frame-ledger.json", ledger)
    with (sender / "frame-ledger" / "frame-ledger.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frame_id", "pts_us", "coverage_start_us", "coverage_end_us",
                        "raw_gray_sha256", "ledger_png"])
        for r in ledger:
            writer.writerow([
                r["frame_id"], r["decoded_pts_us"],
                r["source_coverage_span_us"][0], r["source_coverage_span_us"][1],
                r["raw_gray_80x48_sha256"], r["ledger_png"],
            ])

    # Primary outgoing selection.
    primary = base_manifest(model, stage_name, "primary-long-cell-plus-boundary-twins")
    primary["slots"] = [
        model.build_slot(1, "long-cell", "跨两个目标点的同一长格", LONG_TARGETS, stage),
        model.build_slot(2, "before-boundary", "无图段前最后源格（孪生像素）", [BEFORE_TARGET], stage),
        model.build_slot(3, "after-boundary", "无图段后第一源格（另一源格）", [AFTER_TARGET], stage),
    ]
    primary_regions = [
        {"id": "L-BEFORE", "label": "洞前源内标记音", "kind": "source_tone",
         "start_us": 4_500_000, "end_us": 4_700_000},
        {"id": "SIL-REC", "label": "实录数字零声", "kind": "recorded_silence",
         "start_us": 4_700_000, "end_us": 5_000_000},
        {"id": "BED-IN-HOLE", "label": "无图段内源内低鸣", "kind": "source_tone",
         "start_us": 5_000_000, "end_us": 5_600_000},
        {"id": "FILL-INNER", "label": "摘片补零（内部声洞）", "kind": "extract_fill",
         "start_us": 5_600_000, "end_us": 5_800_000},
        {"id": "R-AFTER", "label": "洞后源内标记音", "kind": "source_tone",
         "start_us": 6_000_000, "end_us": 6_200_000},
        {"id": "SIL-REC-2", "label": "第二段实录零声", "kind": "recorded_silence",
         "start_us": 6_300_000, "end_us": 6_600_000},
        {"id": "FILL-LATER", "label": "摘片补零（后段声洞）", "kind": "extract_fill",
         "start_us": 6_800_000, "end_us": 7_100_000},
        {"id": "CHIRP-RESUME", "label": "补零后源内恢复音", "kind": "source_tone",
         "start_us": 7_100_000, "end_us": 7_300_000},
    ]
    primary["audio_attachment"], _ = model.audio_attachment(
        PRIMARY_AUDIO, "audio/R045-primary-mono.wav", stage, primary_regions
    )
    primary["judgments"] = {
        "same_long_cell": {
            "source_frame_id": primary["slots"][0]["source_frame"]["frame_id"],
            "targets": primary["slots"][0]["targets"],
            "note": "Both target addresses are inside the same decoded source coverage cell.",
        },
        "cross_hole_twins": {
            "before_slot": 2,
            "after_slot": 3,
            "before_slot_key": "before-boundary",
            "after_slot_key": "after-boundary",
            "before_frame_id": primary["slots"][1]["source_frame"]["frame_id"],
            "after_frame_id": primary["slots"][2]["source_frame"]["frame_id"],
            "same_raw_gray_hash": True,
            "same_source_frame": False,
            "conclusion": "Equal pixels are two source addresses; no single source cell covers the picture hole.",
        },
        "audio_both_sides_in_source": {
            "before_region_id": "L-BEFORE",
            "after_region_id": "R-AFTER",
            "conclusion": "Both immediate boundary markers are covered native-rate source PCM.",
        },
    }
    write_json(stage / "addresses.json", primary)
    (stage / "addresses.md").write_text(
        render_addresses_markdown(primary), encoding="utf-8"
    )
    (stage / "handoff-sheet.html").write_text(render_html(primary), encoding="utf-8")
    (stage / "handoff-sheet.md").write_text(
        render_review_markdown(primary), encoding="utf-8"
    )
    shutil.copy2(Path(__file__).with_name("verify_handoff.py"),
                 stage / "verify_handoff.py")
    (stage / "verify_handoff.py").chmod(0o755)
    write_checksums(stage)
    make_zip(stage, zip_path, stage_name)

    # Alternative sender-side selection: boundary twins + pre-freeze onset.
    alt = base_manifest(model, "alternative-selection",
                        "alt-boundary-twins-plus-pre-onset")
    alt["offline"]["verifier"] = "python3 verify_handoff.py --manifest alternative-addresses.json ."
    alt["slots"] = [
        model.build_slot(1, "before-boundary", "无图段前最后源格（保留边界）", [BEFORE_TARGET], alt_dir),
        model.build_slot(2, "after-boundary", "无图段后第一源格（保留边界）", [AFTER_TARGET], alt_dir),
        model.build_slot(3, "pre-onset-witness", "冻帧起点附近的第三源格", [ONSET_TARGET], alt_dir),
    ]
    alt_regions = [
        primary_regions[0],
        primary_regions[1],
        primary_regions[2],
        primary_regions[3],
        primary_regions[4],
    ]
    alt["audio_attachment"], _ = model.audio_attachment(
        ALT_AUDIO, "audio/R045-alt-boundary-mono.wav", alt_dir, alt_regions
    )
    alt["judgments"] = {
        "cross_hole_twins": {
            "before_slot": 1,
            "after_slot": 2,
            "before_slot_key": "before-boundary",
            "after_slot_key": "after-boundary",
            "before_frame_id": alt["slots"][0]["source_frame"]["frame_id"],
            "after_frame_id": alt["slots"][1]["source_frame"]["frame_id"],
            "same_raw_gray_hash": True,
            "same_source_frame": False,
        },
        "audio_both_sides_in_source": {
            "before_region_id": "L-BEFORE",
            "after_region_id": "R-AFTER",
        },
        "tradeoff": "Keeps both boundary cells and adds freeze-onset context; drops the attached long-cell exemplar.",
    }
    write_json(alt_dir / "alternative-addresses.json", alt)
    (alt_dir / "alternative-addresses.md").write_text(
        render_addresses_markdown(alt).replace("# R045 随单源地址清单",
                                               "# R045 备选取法地址清单"),
        encoding="utf-8",
    )
    shutil.copy2(Path(__file__).with_name("verify_handoff.py"),
                 alt_dir / "verify_handoff.py")
    (alt_dir / "verify_handoff.py").chmod(0o755)
    write_checksums(alt_dir)

    # Verify both offline packages with stdlib only.
    primary_code, primary_output = run_verifier(stage, "addresses.json")
    alt_code, alt_output = run_verifier(alt_dir, "alternative-addresses.json")
    if primary_code:
        raise AssertionError("primary verifier failed\n" + primary_output)
    if alt_code:
        raise AssertionError("alternative verifier failed\n" + alt_output)

    # Copy the verified outgoing tree and ZIP into sender reference.
    shutil.copytree(stage, sender / "primary-selection")
    shutil.copy2(zip_path, sender / zip_path.name)

    hit_summary = [
        {k: h.get(k) for k in ("id", "kind", "channel", "start_us", "end_us",
                               "bins", "minimum", "maximum")}
        for h in model.scan["hits"]
    ]
    report = f"""R045 sender-side verification report
=====================================

Source master
- file: {master.relative_to(out_root)}
- sha256: {sha_file(master)}
- picture coverage: {model.picture_coverage}
- sound coverage (sample ranges): {model.audio_coverage}

Scan hits
{json.dumps(hit_summary, ensure_ascii=False, indent=2)}

Primary offline verifier
$ python3 verify_handoff.py --root {stage} --manifest addresses.json
{primary_output}

Alternative offline verifier
$ python3 verify_handoff.py --root {alt_dir} --manifest alternative-addresses.json
{alt_output}

Outgoing ZIP
- {zip_path}
- sha256: {sha_file(zip_path)}
"""
    (sender / "verification-report.txt").write_text(report, encoding="utf-8")

    memo = render_selection_memo(model, primary, alt, zip_path)
    (sender / "selection-memo.md").write_text(memo, encoding="utf-8")

    sender_zip = out_root / "sender-reference-R045.zip"
    make_zip(sender, sender_zip, "sender-reference")

    return {"primary_manifest": primary, "alternative_manifest": alt,
            "zip": zip_path, "sender_zip": sender_zip, "report": report}


def render_selection_memo(
    model: SourceModel,
    primary: dict[str, Any],
    alt: dict[str, Any],
    zip_path: Path,
) -> str:
    before = primary["slots"][1]
    after = primary["slots"][2]
    long_slot = primary["slots"][0]
    onset = alt["slots"][2]

    def frame_line(slot: dict[str, Any]) -> str:
        f = slot["source_frame"]
        diff = f.get("difference_from_previous_permille")
        diff_text = (
            f", 与前格差异 `{diff['mean_abs_luma']}/{diff['edge_delta']}`‰"
            if diff else ""
        )
        return (f"`{f['frame_id']}` @ `{f['source_coverage_span_us'][0]}.."
                f"{f['source_coverage_span_us'][1]}` μs, "
                f"raw SHA `{slot['raw_gray_sha256'][:16]}…`{diff_text}")

    selected_ids = {s["source_frame"]["frame_id"] for s in primary["slots"]}
    post_second_index = model.frame_index_at(POST_SECOND_TARGET[1])
    post_second = model.source_frame_record(post_second_index)
    return f"""# R045 发件端备查：两种三格位取法与伴音取舍

## 实际源材料

- 母带：`sender-reference/source/{SOURCE_NAME}`
- SHA-256：`{primary['source_master']['sha256']}`
- 扫描原件：`sender-reference/source/scan.json`
- 全部源格 PNG 与地址：`sender-reference/frame-ledger/`
- 整段源 WAV：`sender-reference/source/full-source-8k-mono.wav`
- 可发 ZIP：`{zip_path.relative_to(zip_path.parents[1])}`
- ZIP SHA-256：`{sha_file(zip_path)}`

无图段为 `{PICTURE_HOLE[0]}..{PICTURE_HOLE[1]}` μs。洞前、洞后都保留了多个可取源格；
完整台账共有 {len(model.pictures)} 个源格。

## 必须保留的三个“陷阱”事实

1. **同一长格跨多个目标点**：{frame_line(long_slot)}。目标
   `{', '.join(t['label'] + '@' + str(t['program_us']) for t in long_slot['targets'])}`
   都映射到这一个源格。
2. **洞两侧同像素但不是同一源格**：
   - 洞前：{frame_line(before)}
   - 洞后：{frame_line(after)}
   - 两者 raw gray SHA-256 相同：`{before['raw_gray_sha256'] == after['raw_gray_sha256']}`；
     源格 ID 相同：`{before['source_frame']['frame_id'] == after['source_frame']['frame_id']}`（应为否）。
3. **两种零声不能混写**：主 WAV 的 `SIL-REC` 是 muxed PCM 全零、源覆盖内的实录零声；
   `FILL-INNER` / `FILL-LATER` 是源无覆盖、摘片器填零。地址清单给了绝对采样区间。

## 首选发件取法（已封入 outgoing ZIP）

三个格位：

1. 长格证据：{frame_line(long_slot)}
2. 洞前边界：{frame_line(before)}
3. 洞后边界：{frame_line(after)}

这段组合在三个格位内同时完成两件事：

- 给收件方一个“两个目标地址确实可落到同一源格”的正例，避免把地址清单中的同格映射误读成缺图；
- 保留无图段两侧的精确边界格，用相同像素测试“看似连续”，再用不同源地址和覆盖洞否定“同一格跨洞”。

伴音首选 `audio/R045-primary-mono.wav`：`{PRIMARY_AUDIO[0]}..{PRIMARY_AUDIO[1]}` μs。
它同时包含洞前/洞后强标记、实录零声、源内低鸣、两个摘片补零段和补零后恢复音。
当返修棚可能继续争论“零声是不是源里的”“后面的断音是否同源”时，优先发这段较宽 WAV。

## 另一种同样满足三格位限制的取法

目录：`sender-reference/alternative-selection/`

1. 洞前边界：{frame_line(alt['slots'][0])}
2. 洞后边界：{frame_line(alt['slots'][1])}
3. 冻帧起点附近见证格：{frame_line(onset)}

它仍保留判断无图段所需的两侧边界和地址清单，因此足以判断“同像素但两个源格、中间有洞”。
它把首选方案中的“长格正例”换成更早的第三源格；适合以下情况：

- 收件方不再争论目标点为何映射同一格，而争论洞前冻帧是否只是最后一帧的偶发/解码残影；
- 需要证明静止图案在边界之前已持续一段，而不是只靠最后一个边界格；
- 首选长格格位的地址映射已由发件端台账确认，且本次返修只要求判定跨洞事实。

如果争论点变成“洞后第一格是否为孤立解码重复格”，还可以把第三格换成洞后第二格
`{post_second['frame_id']}`（`{post_second['source_coverage_span_us'][0]}..{post_second['source_coverage_span_us'][1]}` μs，
raw SHA `{post_second['raw_gray_80x48_sha256'][:16]}…`）。该候选已在完整台账中，
但不要在未重新生成三格位包时直接加塞成第四格。

## 伴音何时换另一段

备选 WAV：`audio/R045-alt-boundary-mono.wav`，地址窗
`{ALT_AUDIO[0]}..{ALT_AUDIO[1]}` μs。它更短，仍包含：

- 洞前标记 `L-BEFORE` 与实录零声 `SIL-REC`；
- 无图段内源内低鸣和一个明确的摘片补零段 `FILL-INNER`；
- 洞后标记 `R-AFTER`。

当返修单只关心洞两侧是否有源伴音、表单/邮件对附件时长或大小敏感，或较宽 WAV 中
第二个补零段会让收件人把“后段声洞”与本次画格洞混淆时，优先使用这段紧边界 WAV。
若对方要求一并判断后段声音覆盖、补零后恢复或需要额外 RMS 校准材料，则应保留首选宽 WAV。

## 复跑

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python repair-handoff/build_repair_handoff.py
```

本工作区实际构建时若使用系统 Python 3.11，可用 ABI 兼容的 PyAV 18.x：

```sh
PYTHONPATH=/tmp/slatewatch-pydeps:/workspace \\
  python3 repair-handoff/build_repair_handoff.py
```

复跑会重建 `outgoing/` 与 `sender-reference/`。媒体内容由固定时码、采样函数和种子生成；
ZIP 时间戳已固定，但容器字节/ID 若由不同 FFmpeg/PyAV 版本写出可能变化，应以当次
`addresses.json` 中的 SHA-256 为准。
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent,
                        help="repair-handoff output root")
    args = parser.parse_args()
    result = build(args.out.resolve())
    print(f"built outgoing ZIP: {result['zip']}")
    print(f"built sender reference ZIP: {result['sender_zip']}")
    print("primary and alternative offline verifiers: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
