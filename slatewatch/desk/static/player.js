"use strict";

/* 证据试听：播放光标始终落在源采样网格上，坐标换算用 BigInt 精确完成。 */
const island = document.getElementById("evidence-clock");
if (island) {
  const data = JSON.parse(island.textContent);
  const audio = document.getElementById("player-audio");
  const toggle = document.getElementById("player-toggle");
  const toHit = document.getElementById("player-hit");
  const clock = document.getElementById("player-clock");
  const seek = document.getElementById("player-seek");
  const cursorEl = document.getElementById("player-cursor");
  const factTime = document.getElementById("fact-time");
  const factSound = document.getElementById("fact-sound");
  const factPicture = document.getElementById("fact-picture");
  const factFrame = document.getElementById("fact-frame");
  const figures = Array.from(document.querySelectorAll(".frames figure"));

  const RATE = BigInt(data.rate);
  const MICROS = 1000000n;
  const FIRST = data.first;
  const LAST = data.end - 1;
  const SPAN = data.span[1] - data.span[0];

  /* 源采样 → 节目微秒，与服务端 round(Fraction) 相同的偶数舍入 */
  function usOf(sample) {
    const num = BigInt(sample) * MICROS;
    const quotient = num / RATE;
    const twice = (num % RATE) * 2n;
    const rounded = twice < RATE ? quotient : twice > RATE ? quotient + 1n : quotient + (quotient % 2n);
    return Number(rounded);
  }

  /* 节目微秒 → 覆盖该时刻的源采样（下取整后夹到摘片范围内） */
  function sampleAt(us) {
    const sample = Number((BigInt(us) * RATE) / MICROS);
    return Math.min(Math.max(sample, FIRST), LAST);
  }

  function format(us) {
    const pad = (value, size) => String(value).padStart(size, "0");
    const micros = us % 1000000;
    const seconds = ((us - micros) / 1000000) % 60;
    const minutes = (us - micros - seconds * 1000000) / 60000000;
    return `${pad(minutes, 2)}:${pad(seconds, 2)}.${pad(micros, 6)}`;
  }

  let cursor = FIRST;
  let dragging = false;
  let raf = 0;

  function percent(us) {
    return Math.min(100, Math.max(0, ((us - data.span[0]) * 100) / SPAN));
  }

  function filledAt(sample) {
    return data.filled.some(([first, end]) => first <= sample && sample < end);
  }

  function coveredAt(sample) {
    const point = BigInt(sample) * MICROS;
    return data.picture.some(([start, end]) => BigInt(start) * RATE <= point && point < BigInt(end) * RATE);
  }

  function segmentAt(us) {
    for (let index = 0; index < data.frames.length; index += 1) {
      if (us < data.span[0] + Math.floor((SPAN * (index + 1)) / data.frames.length)) return index;
    }
    return data.frames.length - 1;
  }

  function render() {
    const us = usOf(cursor);
    clock.textContent = format(us);
    cursorEl.style.left = percent(us) + "%";
    seek.setAttribute("aria-valuenow", String(Math.min(Math.max(us - data.span[0], 0), SPAN)));
    seek.setAttribute("aria-valuetext", format(us));
    factTime.textContent = format(us);
    factSound.textContent = `源采样 ${cursor} · ${filledAt(cursor) ? "补零区间" : "源内实测"}`;
    factPicture.textContent = coveredAt(cursor) ? "已覆盖" : "未覆盖";
    const segment = segmentAt(us);
    figures.forEach((figure, index) => figure.classList.toggle("current", index === segment));
    const source = data.frames[segment][1];
    factFrame.textContent = source === null
      ? `第 ${segment + 1}/${data.frames.length} 格 · 无采样画格`
      : `第 ${segment + 1}/${data.frames.length} 格 · 采样源帧 ${format(source)}`;
  }

  function place(sample, autoplay) {
    cursor = Math.min(Math.max(sample, FIRST), LAST);
    audio.currentTime = (cursor - FIRST) / data.rate;
    render();
    if (autoplay) {
      const attempt = audio.play();
      if (attempt) attempt.catch(() => {});
    }
  }

  function syncFromAudio() {
    if (dragging) return;
    cursor = Math.min(FIRST + Math.floor(audio.currentTime * data.rate + 1e-6), LAST);
    render();
  }

  function loop() {
    syncFromAudio();
    raf = requestAnimationFrame(loop);
  }

  toggle.addEventListener("click", () => {
    if (audio.paused) {
      const attempt = audio.play();
      if (attempt) attempt.catch(() => {});
    } else {
      audio.pause();
    }
  });
  toHit.addEventListener("click", () => place(sampleAt(data.hit[0]), false));

  audio.addEventListener("play", () => {
    toggle.textContent = "暂停";
    cancelAnimationFrame(raf);
    loop();
  });
  audio.addEventListener("pause", () => {
    toggle.textContent = "播放";
    cancelAnimationFrame(raf);
    syncFromAudio();
  });
  audio.addEventListener("ended", () => {
    cancelAnimationFrame(raf);
    if (dragging) return;
    cursor = LAST;
    render();
  });

  function pointSample(event) {
    const box = seek.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
    return sampleAt(Math.round(data.span[0] + ratio * SPAN));
  }

  seek.addEventListener("pointerdown", (event) => {
    dragging = true;
    seek.setPointerCapture(event.pointerId);
    place(pointSample(event), false);
  });
  seek.addEventListener("pointermove", (event) => {
    if (dragging) place(pointSample(event), false);
  });
  seek.addEventListener("pointerup", () => { dragging = false; });
  seek.addEventListener("pointercancel", () => { dragging = false; });

  seek.addEventListener("keydown", (event) => {
    const us = usOf(cursor);
    let target = null;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      const step = event.key === "ArrowLeft" ? -1 : 1;
      target = event.shiftKey ? cursor + step : sampleAt(us + step * 1000);
    } else if (event.key === "Home") {
      target = FIRST;
    } else if (event.key === "End") {
      target = LAST;
    } else if (event.key === "PageUp" || event.key === "PageDown") {
      const targets = data.frames.map((frame) => frame[0]);
      const choices = event.key === "PageDown"
        ? targets.filter((point) => point > us)
        : targets.filter((point) => point < us).reverse();
      if (choices.length) target = sampleAt(choices[0]);
    } else {
      return;
    }
    event.preventDefault();
    if (target !== null) place(target, false);
  });

  document.querySelectorAll(".frame-play").forEach((button) => {
    button.addEventListener("click", () => place(sampleAt(Number(button.dataset.targetUs)), true));
  });

  render();
}
