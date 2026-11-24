// 主混 / 参考轨对听：切换时保持节目位置，两条短段都已随本页加载，不再请求媒体。
addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-audition]").forEach((box) => {
    const tracks = Array.from(box.querySelectorAll("audio"));
    const toggle = box.querySelector("[data-switch]");
    const current = box.querySelector("[data-current]");
    if (tracks.length !== 2 || !toggle || !current) return;
    let active = 0;
    const names = tracks.map((track) => track.dataset.name);
    const show = () => {
      tracks.forEach((track, index) => {
        track.hidden = index !== active;
        if (index !== active) track.pause();
      });
      current.textContent = names[active];
      toggle.textContent = `切换到${names[1 - active]}`;
    };
    toggle.addEventListener("click", () => {
      const from = tracks[active];
      const position = from.currentTime;
      const playing = !from.paused;
      active = 1 - active;
      const to = tracks[active];
      to.currentTime = position;
      show();
      if (playing) {
        const started = to.play();
        if (started) started.catch(() => {});
      }
    });
    show();
  });
});
