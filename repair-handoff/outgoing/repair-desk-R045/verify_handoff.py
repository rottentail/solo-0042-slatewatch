#!/usr/bin/env python3
"""Offline checker for a three-slot Slatewatch repair-desk handoff.

This file uses only the Python standard library.  It never opens the master,
contacts a service, or fetches anything from the network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
import wave
import zlib
from array import array
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_gray_png(path: Path) -> tuple[int, int, bytes]:
    """Return (width, height, unfiltered 8-bit grayscale pixels)."""
    data = path.read_bytes()
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        raise ValueError(f"{path}: not a PNG file")

    pos = len(signature)
    width = height = bit_depth = color_type = None
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, _, _, _ = struct.unpack(
                ">IIBBBBB", chunk
            )
        elif kind == b"IDAT":
            idat.extend(chunk)
        elif kind == b"IEND":
            break

    if width is None or height is None:
        raise ValueError(f"{path}: missing IHDR")
    if bit_depth != 8 or color_type != 0:
        raise ValueError(f"{path}: expected 8-bit grayscale PNG")

    raw = zlib.decompress(bytes(idat))
    stride = width
    rows: list[bytearray] = []
    cursor = 0
    previous = bytearray(stride)
    for _ in range(height):
        filter_kind = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor:cursor + stride])
        cursor += stride
        if filter_kind == 1:  # Sub
            for x in range(stride):
                left = row[x - 1] if x else 0
                row[x] = (row[x] + left) & 0xFF
        elif filter_kind == 2:  # Up
            for x in range(stride):
                row[x] = (row[x] + previous[x]) & 0xFF
        elif filter_kind == 3:  # Average
            for x in range(stride):
                left = row[x - 1] if x else 0
                row[x] = (row[x] + (left + previous[x]) // 2) & 0xFF
        elif filter_kind == 4:  # Paeth
            for x in range(stride):
                left = row[x - 1] if x else 0
                up_left = previous[x - 1] if x else 0
                row[x] = (row[x] + paeth(left, previous[x], up_left)) & 0xFF
        elif filter_kind != 0:
            raise ValueError(f"{path}: unsupported PNG filter {filter_kind}")
        rows.append(row)
        previous = row

    return width, height, b"".join(bytes(row) for row in rows)


def region_stats(samples: array, first: int, begin: int, end: int) -> dict[str, int]:
    start = max(0, begin - first)
    stop = min(len(samples), end - first)
    values = samples[start:stop]
    if not values:
        raise AssertionError(f"empty region {begin}..{end}")
    squares = sum(int(v) * int(v) for v in values)
    return {
        "rms": int(math.sqrt(squares / len(values))),
        "peak": max(abs(int(v)) for v in values),
        "samples": len(values),
        "all_zero": all(v == 0 for v in values),
    }


def check(root: Path, manifest_name: str) -> list[str]:
    notes: list[str] = []
    manifest_path = root / manifest_name
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if manifest.get("schema") != "repair-desk-address-list/v1":
        raise AssertionError("unexpected address-list schema")
    if manifest.get("offline", {}).get("server_required") is not False:
        raise AssertionError("manifest does not declare offline verification")

    slots = manifest.get("slots", [])
    if len(slots) != 3:
        raise AssertionError(f"expected exactly three slots, found {len(slots)}")

    frame_dirs: set[Path] = set()
    slot_by_key: dict[str, dict[str, Any]] = {}
    for slot in slots:
        key = slot.get("key") or f"slot-{slot['slot']}"
        slot_by_key[key] = slot
        rel = slot["file"]
        path = root / rel
        if not path.is_file():
            raise AssertionError(f"missing slot file: {rel}")
        if sha256_file(path) != slot["sha256"]:
            raise AssertionError(f"file hash mismatch: {rel}")

        width, height, pixels = read_gray_png(path)
        if (width, height) != (slot["width"], slot["height"]):
            raise AssertionError(f"wrong PNG dimensions for {rel}")
        if hashlib.sha256(pixels).hexdigest() != slot["raw_gray_sha256"]:
            raise AssertionError(f"raw source-gray hash mismatch: {rel}")
        if slot.get("source_frame", {}).get("frame_id") is None:
            raise AssertionError(f"slot has no single source-frame address: {rel}")
        if slot.get("composition") != "single-source-frame":
            raise AssertionError(f"slot is not declared as one source frame: {rel}")
        frame_dirs.add(path.parent)
        notes.append(f"slot {slot['slot']}: {rel} OK -> {slot['source_frame']['frame_id']}")

    image_exts = {".png", ".jpg", ".jpeg", ".pgm", ".gif", ".webp"}
    for frame_dir in frame_dirs:
        images = [p for p in frame_dir.iterdir()
                  if p.is_file() and p.suffix.lower() in image_exts]
        if len(images) != 3:
            raise AssertionError(
                f"{frame_dir}: expected exactly three frame images, found {len(images)}"
            )

    audio_info = manifest["audio_attachment"]
    wav_path = root / audio_info["file"]
    if not wav_path.is_file():
        raise AssertionError(f"missing WAV: {audio_info['file']}")
    if sha256_file(wav_path) != audio_info["sha256"]:
        raise AssertionError("WAV SHA-256 mismatch")

    with wave.open(str(wav_path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        rate = wav.getframerate()
        count = wav.getnframes()
        pcm = array("h")
        pcm.frombytes(wav.readframes(count))

    if channels != 1:
        raise AssertionError(f"WAV must be mono, found {channels} channels")
    if sample_width != 2:
        raise AssertionError("WAV must be 16-bit PCM")
    if rate != audio_info["sample_rate"]:
        raise AssertionError("WAV sample-rate mismatch")
    first = int(audio_info["first_source_sample"])
    end = int(audio_info["end_source_sample"])
    if count != end - first:
        raise AssertionError("WAV sample count does not match source address span")
    notes.append(f"WAV OK: mono/{rate} Hz, {count} samples, source samples [{first},{end})")

    filled = audio_info.get("filled_zero_sample_ranges", [])
    for fill in filled:
        begin, finish = int(fill["first"]), int(fill["end"])
        stats = region_stats(pcm, first, begin, finish)
        if not stats["all_zero"]:
            raise AssertionError(f"declared fill range {begin}..{finish} is not zero")
        notes.append(f"fill-zero range [{begin},{finish}) verified in WAV")

    for region in audio_info.get("regions", []):
        begin, finish = (int(v) for v in region["source_samples"])
        stats = region_stats(pcm, first, begin, finish)
        expected = region.get("metrics", {})
        if "rms" in expected and abs(stats["rms"] - int(expected["rms"])) > 1:
            raise AssertionError(f"RMS mismatch in region {region['id']}")
        if "peak" in expected and stats["peak"] != int(expected["peak"]):
            raise AssertionError(f"peak mismatch in region {region['id']}")
        kind = region.get("kind")
        if kind == "recorded_silence" and not stats["all_zero"]:
            raise AssertionError(f"recorded silence is not zero: {region['id']}")
        if kind == "extract_fill" and not stats["all_zero"]:
            raise AssertionError(f"extract fill is not zero: {region['id']}")
        if kind == "source_tone" and stats["rms"] < 500:
            raise AssertionError(f"source tone is too quiet: {region['id']}")
        notes.append(
            f"region {region['id']}: {kind}, rms={stats['rms']}, peak={stats['peak']}"
        )

    judgments = manifest.get("judgments", {})
    twins = judgments.get("cross_hole_twins")
    if twins:
        before = slot_by_key[twins["before_slot_key"]]
        after = slot_by_key[twins["after_slot_key"]]
        if before["raw_gray_sha256"] != after["raw_gray_sha256"]:
            raise AssertionError("twin frames do not have identical source-gray pixels")
        if before["source_frame"]["frame_id"] == after["source_frame"]["frame_id"]:
            raise AssertionError("twin frames were incorrectly attributed to one source cell")
        before_span = before["source_frame"].get("presentation_us")
        if before_span is None:
            before_span = before["source_frame"]["source_coverage_span_us"]
        after_span = after["source_frame"].get("presentation_us")
        if after_span is None:
            after_span = after["source_frame"]["source_coverage_span_us"]
        if not (before_span[1] <= manifest["picture_hole"]["start_us"]
                and after_span[0] >= manifest["picture_hole"]["end_us"]):
            raise AssertionError("boundary slots are not on opposite sides of the hole")
        notes.append("twin check OK: equal pixels, distinct source frame addresses, hole between")

    long_cell = judgments.get("same_long_cell")
    if long_cell:
        source_id = long_cell["source_frame_id"]
        for target in long_cell["targets"]:
            if target["source_frame_id"] != source_id:
                raise AssertionError("long-cell target does not map to the declared frame")
        notes.append(f"long-cell check OK: {len(long_cell['targets'])} targets -> {source_id}")

    audio_judgment = judgments.get("audio_both_sides_in_source")
    if audio_judgment:
        region_ids = {r["id"]: r for r in audio_info.get("regions", [])}
        for side in ("before_region_id", "after_region_id"):
            region = region_ids[audio_judgment[side]]
            if region.get("coverage") != "source_covered":
                raise AssertionError(f"{side} is not source-covered")
            if region.get("metrics", {}).get("rms", 0) < 500:
                raise AssertionError(f"{side} has no audible source marker")
        notes.append("audio both-sides check OK: both marker regions are covered source PCM")

    notes.append("OFFLINE VERIFY PASS: 3 slots, one mono WAV, and address list agree")
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", nargs="?", type=Path,
        help="handoff package root containing addresses.json (default: script directory)",
    )
    parser.add_argument("--root", type=Path, default=None,
                        help="handoff package root containing addresses.json")
    parser.add_argument("--manifest", default="addresses.json",
                        help="manifest file name relative to the package root")
    args = parser.parse_args(argv)
    root = (args.root or args.path or Path(__file__).resolve().parent).resolve()
    try:
        for note in check(root, args.manifest):
            print(note)
    except (OSError, ValueError, AssertionError, KeyError, json.JSONDecodeError, wave.Error) as exc:
        print(f"VERIFY FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
