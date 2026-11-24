"""独立真值探测：只信媒体字节，不引用 builder 的判定 / 映射函数。

本模块刻意不导入 :mod:`training.lab.builder` 或 :mod:`training.lab.docs`，
只依赖 PyAV 与标准库。它从母带直接解出覆盖块、全零段归属、打点起振与
画面白闪，供 :mod:`training.lab.audit` 重算真值。
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import wave

import av

from . import model

MIN_ZERO_RUN_S = 0.20
ONSET_THRESHOLD = 2600
# 出题画格中白闪使用的灰度值；探测按该精确字节计数（不导入生成器）。
FLASH_BYTES = 240
FLASH_COUNT = 1500


def _us(pts, time_base) -> int:
    return round(Fraction(pts) * time_base * _SECOND) if pts is not None else 0


# 本地常量，避免对 slatewatch.clock 的依赖（保持探测独立）。
_SECOND = 1_000_000


def pts_us(frame) -> int:
    if frame.pts is None:
        return 0
    return round(Fraction(frame.pts) * frame.time_base * _SECOND)


@dataclass
class AudioObservation:
    stream_index: int
    rate: int
    channels: int
    first_sample: int
    block_spans_sample: list[tuple[int, int]]
    coverage_us: list[dict]
    gaps_us: list[dict]
    zero_runs: list[dict]
    onsets_s: list[float]
    leading_zero_samples: int
    peak: int
    sample_count: int


def coalesce_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    spans = sorted(spans)
    out: list[list[int]] = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def detect_onsets(pcm: array, rate: int) -> list[float]:
    win = max(1, rate // 100)  # 10 ms
    peaks = []
    for start in range(0, len(pcm), win):
        seg = pcm[start:start + win]
        peak = max((abs(v) for v in seg), default=0)
        peaks.append((start, peak))
    onsets: list[float] = []
    skip = 0
    refractory_windows = max(1, round(0.12 / 0.01))
    for i, (start, peak) in enumerate(peaks):
        if skip > 0:
            skip -= 1
            continue
        if peak >= ONSET_THRESHOLD:
            onsets.append(round(start / rate, 4))
            skip = refractory_windows
    return onsets


def zero_runs_with_coverage(pcm: array, covered: bytearray, rate: int) -> list[dict]:
    runs: list[dict] = []
    n = len(pcm)
    i = 0
    min_len = int(MIN_ZERO_RUN_S * rate)
    while i < n:
        if pcm[i] != 0:
            i += 1
            continue
        j = i
        while j < n and pcm[j] == 0:
            j += 1
        if j - i >= min_len:
            cov = covered[i:j].count(1)
            frac = cov / (j - i)
            kind = ("recorded_silence" if frac >= 0.99 else
                    "uncovered" if frac <= 0.01 else "mixed")
            runs.append({
                "start_sample": i, "end_sample": j,
                "start_us": round(i * _SECOND / rate),
                "end_us": round(j * _SECOND / rate),
                "kind": kind, "coverage_fraction": round(frac, 4),
            })
        i = j
    return runs


def probe_master_audio(path: Path, stream_index: int) -> AudioObservation:
    with av.open(str(path)) as container:
        target = next(s for s in container.streams if s.index == stream_index)
        rate = target.codec_context.sample_rate
        channels = target.codec_context.channels
        block_spans: list[tuple[int, int]] = []
        pieces: list[tuple[int, array]] = []
        first_sample = None
        cursor = 0
        for packet in container.demux(target):
            for frame in packet.decode():
                count = frame.samples
                raw = memoryview(frame.planes[0])[:count * channels * 2]
                vals = array("h")
                vals.frombytes(raw)
                base = round(Fraction(frame.pts) * frame.time_base * rate) \
                    if frame.pts is not None else cursor
                if first_sample is None:
                    first_sample = base
                mono = array("h", (vals[k * channels] for k in range(count)))
                pieces.append((base, mono))
                block_spans.append((base, base + count))
                cursor = base + count

    coverage = coalesce_spans(block_spans)
    length = max((b for _a, b in coverage), default=0)
    pcm = array("h", [0]) * length
    covered = bytearray(length)
    for base, mono in pieces:
        end = min(length, base + len(mono))
        pcm[base:end] = mono[0:end - base]
        covered[base:end] = b"\x01" * (end - base)

    gaps: list[dict] = []
    for (a0, a1), (b0, b1) in zip(coverage, coverage[1:]):
        if b0 > a1:
            gaps.append({"start_sample": a1, "end_sample": b0,
                         "start_us": round(a1 * _SECOND / rate),
                         "end_us": round(b0 * _SECOND / rate)})

    leading = 0
    while leading < len(pcm) and pcm[leading] == 0:
        leading += 1
    peak = max((abs(v) for v in pcm), default=0)
    return AudioObservation(
        stream_index=stream_index, rate=rate, channels=channels,
        first_sample=first_sample or 0,
        block_spans_sample=coverage,
        coverage_us=[{"start_us": round(a * _SECOND / rate),
                      "end_us": round(b * _SECOND / rate)} for a, b in coverage],
        gaps_us=gaps,
        zero_runs=zero_runs_with_coverage(pcm, covered, rate),
        onsets_s=detect_onsets(pcm, rate),
        leading_zero_samples=leading, peak=peak, sample_count=length,
    )


def probe_stream_tags(path: Path) -> dict:
    with av.open(str(path)) as container:
        dur = container.duration
        out = []
        for s in container.streams:
            disp_int = int(s.disposition) if s.disposition is not None else 0
            out.append({
                "index": s.index, "type": s.type,
                "codec": s.codec_context.name,
                "title": dict(s.metadata).get("title"),
                "duration_meta": dict(s.metadata).get("DURATION"),
                "disposition_int": disp_int,
                "default_flag": bool(disp_int & 0x1),  # AV_DISPOSITION_DEFAULT
            })
        return {"container_duration_us": dur, "streams": out}


def probe_master_flashes(path: Path) -> list[dict]:
    events: list[dict] = []
    with av.open(str(path)) as container:
        vs = container.streams.video[0]
        lit: list[tuple[int, int]] = []
        for packet in container.demux(vs):
            for frame in packet.decode():
                gray = frame.reformat(format="gray")
                raw = bytes(gray.planes[0])
                count = raw.count(FLASH_BYTES)
                if count >= FLASH_COUNT:
                    lit.append((pts_us(frame), count))
    # 相邻（<=120ms）的高亮帧合并为一次白闪事件。
    for t, count in lit:
        if events and t - events[-1]["end_us"] <= 120_000:
            events[-1]["end_us"] = t
            events[-1]["frames"] += 1
            events[-1]["max_lit_bytes"] = max(events[-1]["max_lit_bytes"], count)
        else:
            events.append({"start_us": t, "end_us": t, "frames": 1,
                           "max_lit_bytes": count, "time_us": t})
    return events


def probe_wav(path: Path) -> dict:
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        nframes = w.getnframes()
        raw = w.readframes(nframes)
    vals = array("h")
    vals.frombytes(raw)
    if width != 2:
        raise ValueError(f"{path}: expected 16-bit WAV, got width={width}")
    if __import__("sys").byteorder != "little":
        vals.byteswap()
    got_frames = len(vals) // channels
    mono = array("h", (vals[k * channels] for k in range(got_frames)))
    covered = bytearray(b"\x01") * len(mono)  # 文件 WAV 每个采样都“存在”
    leading = 0
    while leading < len(mono) and mono[leading] == 0:
        leading += 1
    return {
        "path": str(path), "channels": channels, "sampwidth": width,
        "rate": rate, "nframes": nframes, "decoded_frames": got_frames,
        "duration_s": got_frames / rate,
        "onsets_s": detect_onsets(mono, rate),
        "zero_runs": zero_runs_with_coverage(mono, covered, rate),
        "leading_zero_samples": leading,
        "peak": max((abs(v) for v in mono), default=0),
        "sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }


def probe_filmstrip_flashes(path: Path, cols: int, tile_w: int, tile_h: int) -> list[float]:
    """解码盲包胶片条，按 tile 统计 FLASH 字节，返回白闪时间（秒）。"""
    lit_frames: list[int] = []
    with av.open(str(path)) as container:
        vs = container.streams.video[0]
        for packet in container.demux(vs):
            for frame in packet.decode():
                gray = frame.reformat(format="gray")
                plane = gray.planes[0]
                ls = plane.line_size
                buf = bytes(plane)
                total_tiles = cols * ((frame.height + tile_h - 1) // tile_h)
                for fi in range(total_tiles):
                    cx, cy = fi % cols, fi // cols
                    cnt = 0
                    for y in range(tile_h):
                        start = (cy * tile_h + y) * ls + cx * tile_w
                        cnt += buf[start:start + tile_w].count(FLASH_BYTES)
                    if cnt >= FLASH_COUNT:
                        lit_frames.append(fi)
    # 连续 tile 合并，取首个 tile 作为事件帧。
    events: list[float] = []
    for fi in lit_frames:
        t = fi / model.FPS
        if events and fi - round(events[-1] * model.FPS) <= 2:
            continue
        events.append(round(t, 4))
    return events


def probe_mp4_basic(path: Path) -> dict:
    frames = 0
    duration_us = None
    with av.open(str(path)) as container:
        duration_us = container.duration
        vs = container.streams.video[0]
        for packet in container.demux(vs):
            for _frame in packet.decode():
                frames += 1
    return {"frames_decoded": frames, "container_duration_us": duration_us}


def match_count(onsets_s: list[float], target_us: list[int], tol_us: int) -> tuple[int, float]:
    """贪心一一匹配起振与目标时点，返回 (匹配数, 最大误差us)。"""
    ons_us = sorted(round(s * _SECOND) for s in onsets_s)
    used: set[int] = set()
    errors: list[int] = []
    matches = 0
    for target in sorted(target_us):
        best = None
        for j, on in enumerate(ons_us):
            if j in used:
                continue
            err = abs(on - target)
            if err <= tol_us and (best is None or err < best[1]):
                best = (j, err)
        if best is not None:
            used.add(best[0])
            matches += 1
            errors.append(best[1])
    return matches, (max(errors) if errors else 0)
