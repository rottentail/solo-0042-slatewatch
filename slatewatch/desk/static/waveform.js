/* Zoomable original-track evidence chart. Min–max buckets keep rare peaks
   visible at every compression level; positions are reported in source
   samples and programme microseconds, never rounded player seconds. */
"use strict";

const BUCKET = 16;
const FULL_SCALE = 32768;
const LITTLE_ENDIAN = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;

function formatTime(us) {
  const pad = (value, width) => String(value).padStart(width, "0");
  const minutes = Math.floor(us / 60000000);
  const seconds = Math.floor((us % 60000000) / 1000000);
  return `${pad(minutes, 2)}:${pad(seconds, 2)}.${pad(us % 1000000, 6)}`;
}

function parseWav(buffer) {
  const view = new DataView(buffer);
  const tag = (at) => String.fromCharCode(view.getUint8(at), view.getUint8(at + 1),
                                          view.getUint8(at + 2), view.getUint8(at + 3));
  if (view.byteLength < 12 || tag(0) !== "RIFF" || tag(8) !== "WAVE") throw new Error("not a WAVE file");
  let fmt = null;
  let data = null;
  let offset = 12;
  while (offset + 8 <= view.byteLength) {
    const size = view.getUint32(offset + 4, true);
    if (tag(offset) === "fmt ") fmt = offset + 8;
    if (tag(offset) === "data") data = { at: offset + 8, size };
    offset += 8 + size + (size % 2);
  }
  if (fmt === null || data === null) throw new Error("incomplete WAVE file");
  if (view.getUint16(fmt, true) !== 1 || view.getUint16(fmt + 14, true) !== 16) {
    throw new Error("unsupported WAVE encoding");
  }
  const channels = view.getUint16(fmt + 2, true);
  const rate = view.getUint32(fmt + 4, true);
  const count = Math.floor(data.size / (2 * channels));
  const lanes = Array.from({ length: channels }, () => new Int16Array(count));
  if (LITTLE_ENDIAN && data.at % 2 === 0) {
    const interleaved = new Int16Array(buffer, data.at, count * channels);
    for (let c = 0; c < channels; c++) {
      const lane = lanes[c];
      for (let i = 0; i < count; i++) lane[i] = interleaved[i * channels + c];
    }
  } else {
    for (let i = 0; i < count; i++) {
      for (let c = 0; c < channels; c++) lanes[c][i] = view.getInt16(data.at + 2 * (i * channels + c), true);
    }
  }
  return { rate, channels, count, lanes };
}

function buildLevels(samples) {
  const levels = [];
  let count = Math.ceil(samples.length / BUCKET);
  let mins = new Int16Array(count);
  let maxs = new Int16Array(count);
  for (let i = 0; i < count; i++) {
    let lo = 32767;
    let hi = -32768;
    const end = Math.min(samples.length, (i + 1) * BUCKET);
    for (let j = i * BUCKET; j < end; j++) {
      const value = samples[j];
      if (value < lo) lo = value;
      if (value > hi) hi = value;
    }
    mins[i] = lo;
    maxs[i] = hi;
  }
  levels.push({ mins, maxs });
  while (count > 1) {
    const parent = Math.ceil(count / 2);
    const parentMins = new Int16Array(parent);
    const parentMaxs = new Int16Array(parent);
    for (let i = 0; i < parent; i++) {
      const other = Math.min(2 * i + 1, count - 1);
      parentMins[i] = Math.min(mins[2 * i], mins[other]);
      parentMaxs[i] = Math.max(maxs[2 * i], maxs[other]);
    }
    levels.push({ mins: parentMins, maxs: parentMaxs });
    mins = parentMins;
    maxs = parentMaxs;
    count = parent;
  }
  return levels;
}

function rangeMinMax(samples, levels, a, b) {
  let lo = 32767;
  let hi = -32768;
  const head = Math.min(b, Math.ceil(a / BUCKET) * BUCKET);
  for (let i = a; i < head; i++) {
    const value = samples[i];
    if (value < lo) lo = value;
    if (value > hi) hi = value;
  }
  const tail = Math.max(head, Math.floor(b / BUCKET) * BUCKET);
  for (let i = tail; i < b; i++) {
    const value = samples[i];
    if (value < lo) lo = value;
    if (value > hi) hi = value;
  }
  let start = head;
  while (start < tail) {
    let level = 0;
    let size = BUCKET;
    while (level + 1 < levels.length && size * 2 <= tail - start && start % (size * 2) === 0) {
      level++;
      size *= 2;
    }
    const bucket = levels[level];
    const index = start / size;
    if (bucket.mins[index] < lo) lo = bucket.mins[index];
    if (bucket.maxs[index] > hi) hi = bucket.maxs[index];
    start += size;
  }
  return [lo, hi];
}

function boot() {
  const panel = document.getElementById("waveform");
  if (!panel) return;
  const base = panel.dataset.base;
  const canvas = panel.querySelector("canvas");
  const context = canvas.getContext("2d");
  const windowLabel = panel.querySelector("[data-window]");
  const cursorLabel = panel.querySelector("[data-cursor]");
  const playLabel = panel.querySelector("[data-playhead]");
  const audio = document.getElementById("evidence-audio");
  const figures = Array.from(document.querySelectorAll("figure[data-frame]"));
  const state = {
    mapping: null, lanes: [], levels: [], rate: 0, channels: 0, count: 0, first: 0,
    start: 0, span: 1, cursor: null,
    hitStartUs: Number(panel.dataset.hitStart || 0), hitEndUs: Number(panel.dataset.hitEnd || 0),
  };

  const fail = (message) => { windowLabel.textContent = message; };

  function clampView() {
    state.span = Math.min(Math.max(state.span, 1), state.count);
    state.start = Math.min(Math.max(state.start, 0), state.count - state.span);
  }

  function zoom(factor, anchor) {
    const point = state.start + state.span * anchor;
    state.span /= factor;
    state.start = point - state.span * anchor;
    clampView();
    render();
  }

  function fit() {
    state.start = 0;
    state.span = state.count;
    render();
  }

  function centerOn(sample) {
    state.start = sample - state.first - state.span / 2;
    clampView();
  }

  function highlightFrame(us) {
    const mapping = state.mapping;
    const total = mapping.picture.length;
    let active = -1;
    if (total > 0 && us >= mapping.start_us && us < mapping.end_us) {
      active = Math.min(total - 1,
                        Math.floor((us - mapping.start_us) * total / (mapping.end_us - mapping.start_us)));
    }
    figures.forEach((figure, index) => figure.classList.toggle("current", index === active));
    if (active >= 0) figures[active].scrollIntoView({ block: "nearest" });
  }

  function setCursor(sample, seek = true) {
    state.cursor = Math.min(Math.max(sample, state.first), state.first + state.count);
    const local = state.cursor - state.first;
    const us = Math.round(state.cursor * 1000000 / state.rate);
    let text = `光标 · 源采样 ${state.cursor} · 节目时刻 ${formatTime(us)} · WAV 位置 ${(local / state.rate).toFixed(3)} s`;
    if (local < state.count) {
      text += " · " + state.lanes.map((lane, channel) => `声道${channel + 1} ${lane[local]}`).join(" · ");
    }
    cursorLabel.textContent = text;
    if (seek && audio) {
      const at = local / state.rate;
      audio.currentTime = isFinite(audio.duration) ? Math.min(at, audio.duration) : at;
    }
    highlightFrame(us);
    render();
  }

  function render() {
    if (!state.mapping) return;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    context.clearRect(0, 0, width, height);
    const spp = state.span / width;
    const laneHeight = height / state.channels;
    const toX = (sample) => (sample - state.first - state.start) / spp;
    const usToX = (us) => toX(us * state.rate / 1000000);

    if (state.hitEndUs > state.hitStartUs) {
      const a = usToX(state.hitStartUs);
      const b = usToX(state.hitEndUs);
      if (b >= 0 && a <= width) {
        context.fillStyle = "rgba(226, 162, 58, 0.10)";
        context.fillRect(Math.max(0, a), 0, Math.min(width, b) - Math.max(0, a), height);
        context.fillStyle = "rgba(226, 162, 58, 0.45)";
        if (a >= 0 && a <= width) context.fillRect(a - 0.5, 0, 1, height);
        if (b >= 0 && b <= width) context.fillRect(b - 0.5, 0, 1, height);
      }
    }
    for (const range of state.mapping.sound.filled_sample_ranges) {
      const a = toX(range.first);
      const b = toX(range.end);
      if (b < 0 || a > width) continue;
      context.fillStyle = "rgba(235, 117, 106, 0.14)";
      context.fillRect(Math.max(0, a), 0, Math.min(width, b) - Math.max(0, a), height);
      context.fillStyle = "rgba(235, 117, 106, 0.55)";
      if (a >= 0 && a <= width) context.fillRect(a - 0.5, 0, 1, height);
      if (b >= 0 && b <= width) context.fillRect(b - 0.5, 0, 1, height);
    }

    for (let c = 0; c < state.channels; c++) {
      const top = c * laneHeight + 7;
      const bottom = (c + 1) * laneHeight - 7;
      const yFor = (value) => bottom - (value + FULL_SCALE) * (bottom - top) / (2 * FULL_SCALE);
      context.fillStyle = "rgba(240, 245, 245, 0.16)";
      context.fillRect(0, yFor(0) - 0.5, width, 1);
      context.strokeStyle = "rgba(235, 117, 106, 0.35)";
      context.setLineDash([3, 3]);
      context.beginPath();
      context.moveTo(0, yFor(32767) + 0.5);
      context.lineTo(width, yFor(32767) + 0.5);
      context.moveTo(0, yFor(-32768) + 0.5);
      context.lineTo(width, yFor(-32768) + 0.5);
      context.stroke();
      context.setLineDash([]);
      context.fillStyle = "#8fd6cd";
      if (spp > 2) {
        for (let x = 0; x < width; x++) {
          const a = Math.max(0, Math.floor(state.start + x * spp));
          const b = Math.min(state.count, Math.ceil(state.start + (x + 1) * spp));
          if (a >= b) continue;
          const [lo, hi] = rangeMinMax(state.lanes[c], state.levels[c], a, b);
          context.fillRect(x, yFor(hi), 1, Math.max(1, yFor(lo) - yFor(hi)));
        }
      } else {
        const zero = yFor(0);
        const first = Math.max(0, Math.floor(state.start));
        const last = Math.min(state.count, Math.ceil(state.start + state.span));
        for (let i = first; i < last; i++) {
          const x = (i - state.start) / spp;
          const y = yFor(state.lanes[c][i]);
          context.fillRect(x - 0.5, Math.min(zero, y), 1, Math.max(1, Math.abs(zero - y)));
          context.fillRect(x - 1.5, y - 1.5, 3, 3);
        }
      }
      context.fillStyle = "rgba(240, 245, 245, 0.6)";
      context.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
      context.fillText(`声道 ${c + 1}`, 8, c * laneHeight + 13);
    }

    if (state.cursor !== null) {
      const x = toX(state.cursor);
      if (x >= 0 && x <= width) {
        context.fillStyle = "#f0f5f5";
        context.fillRect(x - 0.5, 0, 1, height);
      }
    }
    if (audio && audio.readyState > 0) {
      const x = (audio.currentTime * state.rate - state.start) / spp;
      if (x >= 0 && x <= width) {
        context.fillStyle = "#e2a23a";
        context.fillRect(x - 0.5, 0, 1, height);
      }
    }
    const from = state.first + Math.round(state.start);
    const to = state.first + Math.round(state.start + state.span);
    const perPixel = spp >= 1 ? String(Math.round(spp)) : spp.toFixed(2);
    windowLabel.textContent = `源采样窗口 [${from}, ${to}) · 每像素 ${perPixel} 采样`;
  }

  function updatePlayhead() {
    if (!state.mapping || !audio || audio.readyState === 0) return;
    const sample = state.first + Math.round(audio.currentTime * state.rate);
    const us = Math.round(sample * 1000000 / state.rate);
    playLabel.textContent = `播放位置 · 源采样 ${sample} · 节目时刻 ${formatTime(us)} · WAV ${audio.currentTime.toFixed(3)} s`;
    render();
  }

  panel.querySelector('[data-zoom="in"]').addEventListener("click", () => zoom(2, 0.5));
  panel.querySelector('[data-zoom="out"]').addEventListener("click", () => zoom(0.5, 0.5));
  panel.querySelector('[data-zoom="fit"]').addEventListener("click", fit);

  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    const rect = canvas.getBoundingClientRect();
    zoom(event.deltaY < 0 ? 1.25 : 0.8, (event.clientX - rect.left) / rect.width);
  }, { passive: false });

  let drag = null;
  canvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    drag = { x: event.clientX, start: state.start, moved: false };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const delta = event.clientX - drag.x;
    if (Math.abs(delta) > 3) drag.moved = true;
    if (drag.moved) {
      state.start = drag.start - delta * state.span / canvas.clientWidth;
      clampView();
      render();
    }
  });
  canvas.addEventListener("pointerup", (event) => {
    if (!drag) return;
    const moved = drag.moved;
    drag = null;
    if (!moved) {
      const rect = canvas.getBoundingClientRect();
      const sample = state.first + Math.round(state.start + (event.clientX - rect.left) * state.span / rect.width);
      setCursor(sample);
    }
  });
  canvas.addEventListener("pointercancel", () => { drag = null; });
  canvas.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      state.start += (event.key === "ArrowLeft" ? -1 : 1) * state.span * 0.1;
      clampView();
      render();
    } else if (event.key === "+" || event.key === "=") zoom(1.5, 0.5);
    else if (event.key === "-") zoom(1 / 1.5, 0.5);
    else if (event.key === "0") fit();
    else return;
    event.preventDefault();
  });

  document.querySelectorAll("[data-jump-sample]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!state.mapping) return;
      const sample = Number(button.dataset.jumpSample);
      centerOn(sample);
      setCursor(sample);
      panel.scrollIntoView({ block: "nearest" });
    });
  });

  figures.forEach((figure) => {
    const link = figure.querySelector("[data-frame-link]");
    if (!link) return;
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (!state.mapping) return;
      const us = Number(figure.dataset.sourceUs || figure.dataset.targetUs);
      const sample = Math.round(us * state.rate / 1000000);
      centerOn(sample);
      setCursor(sample);
      panel.scrollIntoView({ block: "nearest" });
      canvas.focus({ preventScroll: true });
    });
  });

  if (audio) {
    audio.addEventListener("timeupdate", updatePlayhead);
    audio.addEventListener("seeked", updatePlayhead);
    audio.addEventListener("loadedmetadata", updatePlayhead);
    audio.addEventListener("play", () => {
      const step = () => {
        updatePlayhead();
        if (!audio.paused && !audio.ended) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    });
  }

  function resize() {
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    render();
  }
  new ResizeObserver(resize).observe(canvas);

  Promise.all([
    fetch(`${base}/clock.json`).then((response) => {
      if (!response.ok) throw new Error("mapping unavailable");
      return response.json();
    }),
    fetch(`${base}/sound.wav`).then((response) => {
      if (!response.ok) throw new Error("sound unavailable");
      return response.arrayBuffer();
    }),
  ]).then(([mapping, buffer]) => {
    const wav = parseWav(buffer);
    const track = mapping.sound;
    if (!track || track.sample_rate !== wav.rate || track.channels !== wav.channels || wav.count < 1
        || track.end_source_sample - track.first_source_sample !== wav.count) {
      fail("波形与证据映射不一致，请重新摘片。");
      return;
    }
    state.mapping = mapping;
    state.lanes = wav.lanes;
    state.rate = wav.rate;
    state.channels = wav.channels;
    state.count = wav.count;
    state.first = track.first_source_sample;
    state.levels = wav.lanes.map(buildLevels);
    state.start = 0;
    state.span = wav.count;
    resize();
    updatePlayhead();
  }).catch(() => fail("原轨证据图加载失败，请重新打开预览。"));
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { BUCKET, formatTime, parseWav, buildLevels, rangeMinMax };
} else {
  boot();
}
