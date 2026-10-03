const status = document.getElementById("status");
const form = document.getElementById("pair-form");
const port = document.getElementById("port");

chrome.storage.local.get("bridgePort").then(({ bridgePort }) => {
  port.value = bridgePort || 8765;
});
port.addEventListener("change", async () => {
  const value = Number(port.value);
  if (Number.isInteger(value) && value >= 1024 && value <= 65535) {
    await chrome.storage.local.set({ bridgePort: value });
    await refresh();
  }
});

async function refresh() {
  const data = await chrome.runtime.sendMessage({ type: "STATUS" });
  form.hidden = data.paired !== false;
  if (data.paired === false) status.textContent = "在 VS Code 运行 Agent 后，输入终端显示的配对码。";
  else if (data.offline) status.textContent = "本地 Agent 未运行。";
  else if (data.connected) status.textContent = `已连接 · ${data.slug || "等待题目"}${data.active ? "" : "（已暂停）"}`;
  else status.textContent = "已配对，等待主 Edge 的力扣题目页。";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  const result = await chrome.runtime.sendMessage({ type: "PAIR", code: document.getElementById("code").value });
  button.disabled = false;
  if (result.paired) await refresh();
  else status.textContent = result.error || "配对失败";
});

refresh();
