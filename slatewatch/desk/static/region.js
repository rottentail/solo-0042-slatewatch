document.querySelectorAll(".region-tool").forEach((tool) => {
  const form = tool.querySelector("form");
  const button = form.querySelector("button");
  const picture = new Image();
  picture.src = tool.dataset.image;
  picture.addEventListener("load", () => {
    const canvas = document.createElement("canvas");
    canvas.width = 80;
    canvas.height = 48;
    canvas.className = "region-canvas";
    canvas.setAttribute("aria-label", "在证据画格上框选局部区域");
    const context = canvas.getContext("2d");
    let origin = null;
    let box = null;
    const draw = () => {
      context.drawImage(picture, 0, 0);
      if (box) {
        context.fillStyle = "rgba(0, 97, 93, 0.45)";
        context.fillRect(box.x, box.y, box.width, box.height);
      }
    };
    const point = (event) => {
      const bounds = canvas.getBoundingClientRect();
      return {
        x: Math.min(79, Math.max(0, Math.floor((event.clientX - bounds.left) * 80 / bounds.width))),
        y: Math.min(47, Math.max(0, Math.floor((event.clientY - bounds.top) * 48 / bounds.height))),
      };
    };
    canvas.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      canvas.setPointerCapture(event.pointerId);
      origin = point(event);
      box = { x: origin.x, y: origin.y, width: 1, height: 1 };
      draw();
    });
    canvas.addEventListener("pointermove", (event) => {
      if (!origin) return;
      const now = point(event);
      box = {
        x: Math.min(origin.x, now.x),
        y: Math.min(origin.y, now.y),
        width: Math.abs(now.x - origin.x) + 1,
        height: Math.abs(now.y - origin.y) + 1,
      };
      draw();
    });
    canvas.addEventListener("pointerup", () => {
      if (!origin || !box) return;
      origin = null;
      form.elements.x.value = box.x;
      form.elements.y.value = box.y;
      form.elements.width.value = box.width;
      form.elements.height.value = box.height;
      button.disabled = false;
    });
    const image = tool.closest("figure").querySelector("img.region-source");
    if (image) image.replaceWith(canvas);
    draw();
  });
});
