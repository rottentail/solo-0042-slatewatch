"""确定性画格 / PCM / 母带渲染。

本模块只负责把 :mod:`training.lab.model` 的设计变成真实字节，不做真值
判断。所有随机源都不使用：同样的输入永远得到同样的采样与画格。
"""
from __future__ import annotations

from array import array
from base64 import b64encode
from fractions import Fraction
from io import BytesIO
from math import pi, sin
import struct
import sys
import wave
import zlib

import av

from . import model

FLASH = 240          # 仅白闪矩形使用的灰度，便于独立核查按字节计数
STRIKER = 206
STATION_INK = 168
BEAM = 74
BACKGROUND = 44


# ---------------------------------------------------------------- 画布
class GrayCanvas:
    def __init__(self, w: int, h: int, bg: int = BACKGROUND):
        self.w, self.h = w, h
        self.buf = bytearray([bg]) * (w * h)

    def px(self, x: int, y: int, v: int) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.buf[y * self.w + x] = v

    def rect(self, x0: int, y0: int, x1: int, y1: int, v: int) -> None:
        x0, x1 = max(0, x0), min(self.w, x1)
        y0, y1 = max(0, y0), min(self.h, y1)
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes([v]) * (x1 - x0)
        for y in range(y0, y1):
            start = y * self.w + x0
            self.buf[start:start + len(row)] = row

    def disc(self, cx: int, cy: int, r: int, v: int, fill: bool = True) -> None:
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                d2 = (x - cx) ** 2 + (y - cy) ** 2
                if (fill and d2 <= r * r) or (not fill and (r - 2) ** 2 <= d2 <= r * r):
                    self.px(x, y, v)

    def triangle(self, cx: int, cy: int, r: int, v: int, fill: bool = False) -> None:
        apex = (cx, cy - r)
        left = (cx - r, cy + r - 2)
        right = (cx + r, cy + r - 2)
        if fill:
            for y in range(cy - r, cy + r):
                span = int(r * (y - (cy - r)) / (2 * r - 2))
                self.rect(cx - span, y, cx + span + 1, y + 1, v)
        else:
            self._line(apex[0], apex[1], left[0], left[1], v, 3)
            self._line(apex[0], apex[1], right[0], right[1], v, 3)
            self._line(left[0], left[1], right[0], right[1], v, 3)

    def _line(self, x0: int, y0: int, x1: int, y1: int, v: int, thick: int = 1) -> None:
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(steps + 1):
            u = i / steps
            x = round(x0 + (x1 - x0) * u)
            y = round(y0 + (y1 - y0) * u)
            self.rect(x - thick // 2, y - thick // 2,
                      x + thick // 2 + 1, y + thick // 2 + 1, v)

    def line(self, x0: int, y0: int, x1: int, y1: int, v: int, thick: int = 1) -> None:
        self._line(x0, y0, x1, y1, v, thick)


def _ease(u: float) -> float:
    return u * u * (3 - 2 * u)


def striker_x(item: model.Item, t: float) -> int:
    hits = item.correct
    ts = [h.t for h in hits]
    xs = [model.STATIONS[h.station]["x"] for h in hits]
    if t <= ts[0]:
        return xs[0]
    if t >= ts[-1]:
        return xs[-1]
    for k in range(len(ts) - 1):
        if ts[k] <= t <= ts[k + 1]:
            span = ts[k + 1] - ts[k]
            u = 0.0 if span == 0 else _ease((t - ts[k]) / span)
            return round(xs[k] + (xs[k + 1] - xs[k]) * u)
    return xs[-1]


def render_frame(item: model.Item, fi: int) -> bytes:
    """返回紧凑排列的 WIDTH*HEIGHT 灰度字节。"""
    t = fi / model.FPS
    cv = GrayCanvas(model.WIDTH, model.HEIGHT)

    # 背景上下面，制造一点层次（仍远离黑场阈值）。
    cv.rect(0, 0, model.WIDTH, 26, BACKGROUND + 8)
    cv.rect(0, 150, model.WIDTH, model.HEIGHT, BACKGROUND + 6)

    # 击打横梁。
    cv.rect(24, 128, model.WIDTH - 24, 133, BEAM)

    # 三个站位（形状 / 位置即制作记录里的音色对应）。
    for idx, st in enumerate(model.STATIONS):
        x = st["x"]
        if idx == 0:
            cv.disc(x, 118, 17, STATION_INK, fill=False)
            cv.disc(x, 118, 3, STATION_INK, fill=True)
        elif idx == 1:
            cv.rect(x - 16, 102, x + 16, 134, STATION_INK)
            cv.rect(x - 12, 106, x + 12, 130, BACKGROUND)
        else:
            cv.triangle(x, 116, 18, STATION_INK, fill=False)

    # 两段留白窗口在画面上也有低对比刻度括号（不含覆盖信息）。
    for a, b in model.PLATEAUS.values():
        xa = round(24 + (model.WIDTH - 48) * a / model.DURATION_S)
        xb = round(24 + (model.WIDTH - 48) * b / model.DURATION_S)
        cv.rect(xa, 150, xb, 152, 96)

    # 滑锤 / 击槌。
    x = striker_x(item, t)
    cv.line(x, 44, x, 100, STRIKER, thick=4)
    cv.rect(x - 11, 98, x + 11, 112, STRIKER)
    cv.rect(x - 7, 112, x + 7, 120, STRIKER - 30)

    # 接触白闪：每个打点恰好两帧、灰度恰为 FLASH。
    for hit in item.correct:
        hf = round(hit.t * model.FPS)
        if fi in (hf, hf + 1):
            sx = model.STATIONS[hit.station]["x"]
            cv.rect(sx - 34, 74, sx + 34, 150, FLASH)
            cv.disc(sx, 116, 15, FLASH, fill=True)

    return bytes(cv.buf)


def all_frames(item: model.Item) -> list[bytes]:
    return [render_frame(item, i) for i in range(model.FRAME_COUNT)]


# ---------------------------------------------------------------- PNG
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_gray(w: int, h: int, gray: bytes, level: int = 9) -> bytes:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(gray[y * w:(y + 1) * w])
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)  # 8-bit 灰度
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), level))
            + _png_chunk(b"IEND", b""))


def filmstrip(frames: list[bytes], cols: int = 16) -> tuple[bytes, int, int, int]:
    n = len(frames)
    rows = (n + cols - 1) // cols
    fw, fh = cols * model.WIDTH, rows * model.HEIGHT
    out = bytearray(fw * fh)
    for i, frame in enumerate(frames):
        cx, cy = i % cols, i // cols
        for y in range(model.HEIGHT):
            src = y * model.WIDTH
            dst = (cy * model.HEIGHT + y) * fw + cx * model.WIDTH
            out[dst:dst + model.WIDTH] = frame[src:src + model.WIDTH]
    return png_gray(fw, fh, bytes(out), level=6), cols, rows


# ---------------------------------------------------------------- 音频
def build_mono(hits: tuple[model.Hit, ...]) -> array:
    n = model.TOTAL_SAMPLES
    a = array("h", [0]) * n
    for i in range(n):
        # 对称、低电平但在平台之外几乎不为零的环境声床。
        v = 150 * sin(2 * pi * 165 * i / model.RATE) \
            + 70 * sin(2 * pi * 331 * i / model.RATE + 0.7)
        a[i] = int(v)

    for hit in hits:
        s = int(round(hit.t * model.RATE))
        d = int(hit.dur_ms / 1000 * model.RATE)
        for k in range(d):
            idx = s + k
            if idx >= n:
                break
            env = (1 - k / d) ** 2
            attack = min(1.0, k / 12)
            add = int(hit.amp * env * attack
                      * sin(2 * pi * hit.freq * k / model.RATE))
            merged = a[idx] + add
            a[idx] = max(-32_000, min(32_000, merged))

    # 两处平台强制为精确全零（实录静音 / 待补零空洞在盲 WAV 中完全同形）。
    for p0, p1 in model.PLATEAU_SAMPLES.values():
        for i in range(p0, p1):
            a[i] = 0
        guard = [240, -240, 240, -240, 240, -240]
        for j, g in enumerate(guard):
            if p0 - len(guard) + j >= 0:
                a[p0 - len(guard) + j] = g
            if p1 + j < n:
                a[p1 + j] = g
    return a


def wav_mono(samples: array, rate: int = model.RATE) -> bytes:
    values = array("h", samples)
    if sys.byteorder != "little":
        values.byteswap()
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(values.tobytes())
    return buf.getvalue()


def data_uri(mime: str, payload: bytes) -> str:
    return f"data:{mime};base64,{b64encode(payload).decode('ascii')}"


def waveform_svg(samples: array, buckets: int = 460, w: int = 720, h: int = 116) -> str:
    """由峰值包络生成中性波形 SVG（不含任何答案 / 覆盖标记）。"""
    n = len(samples)
    step = max(1, n // buckets)
    half = h / 2 - 6
    scale = half / 13_000
    tops, bots = [], []
    for b in range(buckets):
        lo = b * step
        hi = min(n, lo + step)
        peak = 0
        for i in range(lo, hi, 4):       # 抽点求峰值，足够绘制
            v = samples[i]
            if abs(v) > peak:
                peak = abs(v)
        mag = min(half, peak * scale)
        x = round(b * w / buckets, 1)
        tops.append((x, round(h / 2 - mag, 1)))
        bots.append((x, round(h / 2 + mag, 1)))
    env = "M " + " L ".join(f"{x},{y}" for x, y in tops)
    env += " L " + " L ".join(f"{x},{y}" for x, y in reversed(bots)) + " Z"
    return (
        f'<svg viewBox="0 0 {w} {h}" class="wave" role="img" '
        f'aria-label="候选音轨波形"><rect x="0" y="0" width="{w}" height="{h}" '
        f'fill="#0e1726" rx="8"/><line x1="0" y1="{h/2}" x2="{w}" y2="{h/2}" '
        f'stroke="#33415c" stroke-width="1"/><path d="{env}" fill="#5b9dff" '
        f'fill-opacity="0.9"/></svg>'
    )


# ---------------------------------------------------------------- 母带
def _stereo(samples: array) -> array:
    st = array("h", [0]) * (len(samples) * 2)
    for i, v in enumerate(samples):
        st[2 * i] = v
        st[2 * i + 1] = v
    if sys.byteorder != "little":
        st.byteswap()
    return st


def mux_master(path, item: model.Item, audio_tracks: list[tuple[array, str]]) -> None:
    """生成双音轨 Matroska 母带。

    ``audio_tracks`` 按物理流添加顺序给出 ``(单声道采样, 作为空洞的平台号)``。
    两条音轨写入完全相同的流名 :data:`model.SHARED_STREAM_NAME`。
    """
    stereo = [_stereo(samples) for samples, _hole in audio_tracks]
    with av.open(str(path), mode="w", format="matroska") as container:
        video = container.add_stream("ffv1", rate=model.FPS)
        video.width, video.height = model.WIDTH, model.HEIGHT
        video.pix_fmt = "gray"

        audios = []
        for _ in audio_tracks:
            stream = container.add_stream("pcm_s16le", rate=model.RATE)
            stream.layout = "stereo"
            stream.metadata["title"] = model.SHARED_STREAM_NAME
            audios.append(stream)

        for fi in range(model.FRAME_COUNT):
            gray = render_frame(item, fi)
            vf = av.VideoFrame(model.WIDTH, model.HEIGHT, "gray")
            plane = vf.planes[0]
            raw = bytearray(plane.buffer_size)
            for y in range(model.HEIGHT):
                start = y * plane.line_size
                raw[start:start + model.WIDTH] = gray[y * model.WIDTH:(y + 1) * model.WIDTH]
            plane.update(raw)
            vf.pts = fi
            vf.time_base = Fraction(1, model.FPS)
            container.mux(video.encode(vf))

            first = fi * model.CHUNK
            for ti, ((_mono, hole_name), st) in enumerate(zip(audio_tracks, stereo)):
                h0, h1 = model.PLATEAU_SAMPLES[hole_name]
                if h0 <= first < h1:
                    continue  # 整块不 mux，PTS 跳到块后 -> 真实覆盖空洞
                af = av.AudioFrame(format="s16", layout="stereo", samples=model.CHUNK)
                af.planes[0].update(st[first * 2:(first + model.CHUNK) * 2].tobytes())
                af.sample_rate = model.RATE
                af.time_base = Fraction(1, model.RATE)
                af.pts = first
                container.mux(audios[ti].encode(af))

        container.mux(video.encode())
        for stream in audios:
            container.mux(stream.encode())


def encode_picture_mp4(path, frames: list[bytes]) -> None:
    """生成无音轨、浏览器可直接播放的 H.264 短段画面。"""
    with av.open(str(path), mode="w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=model.FPS)
        stream.width, stream.height = model.WIDTH, model.HEIGHT
        stream.pix_fmt = "yuv420p"
        stream.options = {"preset": "veryfast", "crf": "23"}
        for fi, gray in enumerate(frames):
            g = av.VideoFrame(model.WIDTH, model.HEIGHT, "gray")
            plane = g.planes[0]
            raw = bytearray(plane.buffer_size)
            for y in range(model.HEIGHT):
                start = y * plane.line_size
                raw[start:start + model.WIDTH] = gray[y * model.WIDTH:(y + 1) * model.WIDTH]
            plane.update(raw)
            yuv = g.reformat(format="yuv420p")
            yuv.pts = fi
            yuv.time_base = Fraction(1, model.FPS)
            for packet in stream.encode(yuv):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
