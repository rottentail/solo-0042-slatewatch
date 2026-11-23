const form = document.getElementById("upload-form");
const feedback = document.getElementById("upload-status");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = document.getElementById("asset-file").files[0];
  const submit = form.querySelector("button");
  if (file.size > 48 * 1024 * 1024) {
    feedback.textContent = "母带大小不能超过 48 MiB。";
    return;
  }
  const parameters = new URLSearchParams({
    label: document.getElementById("asset-label").value,
    sound: document.getElementById("asset-sound").value,
  });
  const picture = document.getElementById("asset-picture").value;
  if (picture) parameters.set("picture", picture);
  submit.disabled = true;
  feedback.textContent = "正在读取母带…";
  try {
    const response = await fetch("/desk/upload?" + parameters, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    if (!response.ok) {
      feedback.textContent = response.status === 503 ? "审片槽位已满，请稍后重试。" : "母带登记失败，请核对文件和轨道选择。";
      submit.disabled = false;
      return;
    }
    window.location.assign("/desk/intake");
  } catch {
    feedback.textContent = "未收到服务响应，请先核对母带列表。";
    submit.disabled = false;
  }
});
