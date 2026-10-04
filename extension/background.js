let reconnecting = null;
let reconnectAfter = 0;

async function reconnect(port) {
  if (Date.now() < reconnectAfter) return null;
  if (reconnecting) return reconnecting;
  reconnecting = (async () => {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/v1/reconnect`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      const data = await response.json();
      if (response.ok && typeof data.token === "string" && data.token) {
        await chrome.storage.local.set({ bridgeToken: data.token });
        reconnectAfter = 0;
        return data.token;
      }
    } catch (_) {}
    reconnectAfter = Date.now() + 10000;
    return null;
  })();
  try {
    return await reconnecting;
  } finally {
    reconnecting = null;
  }
}

async function request(path, body, authenticated = true) {
  let { bridgeToken, bridgePort } = await chrome.storage.local.get(["bridgeToken", "bridgePort"]);
  const port = Number.isInteger(bridgePort) && bridgePort >= 1024 && bridgePort <= 65535 ? bridgePort : 8765;
  if (authenticated && !bridgeToken) {
    bridgeToken = await reconnect(port);
    if (!bridgeToken) return { paired: false };
  }
  const headers = { "Content-Type": "application/json" };
  if (authenticated) headers.Authorization = `Bearer ${bridgeToken}`;
  try {
    let response = await fetch(`http://127.0.0.1:${port}${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (authenticated && response.status === 401) {
      const replacement = await reconnect(port);
      if (!replacement) {
        await chrome.storage.local.remove("bridgeToken");
        return { paired: false };
      }
      headers.Authorization = `Bearer ${replacement}`;
      response = await fetch(`http://127.0.0.1:${port}${path}`, {
        method: body === undefined ? "GET" : "POST", headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    }
    const data = await response.json();
    if (response.status === 401) return { paired: false };
    return { ...data, httpStatus: response.status, paired: authenticated ? true : undefined };
  } catch (_) {
    return { paired: !!bridgeToken, offline: true };
  }
}

async function handle(message, sender) {
  if (message.type === "PAIR") {
    const result = await request("/v1/pair", { code: message.code }, false);
    if (result.token) {
      await chrome.storage.local.set({ bridgeToken: result.token });
      reconnectAfter = 0;
      const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      if (tabs[0]?.url?.startsWith("https://leetcode.cn/problems/")) {
        await chrome.tabs.reload(tabs[0].id);
      }
      return { paired: true };
    }
    return { paired: false, error: result.error || "Local runner is unavailable" };
  }
  if (message.type === "STATUS") return request("/v1/status");
  if (!sender.tab?.id || !sender.tab.url?.startsWith("https://leetcode.cn/problems/")) {
    return { error: "not a LeetCode problem tab" };
  }
  if (message.type === "HELLO") {
    const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    const active = tabs[0]?.id === sender.tab.id && message.page.visible !== false;
    return request("/v1/hello", { ...message.page, tab_id: sender.tab.id, active });
  }
  if (message.type === "RESULT") {
    return request("/v1/result", { ...message.result, tab_id: sender.tab.id });
  }
  if (message.type === "PHASE") {
    return request("/v1/phase", { ...message.phase, tab_id: sender.tab.id });
  }
  if (message.type === "ACTIVE") {
    const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    return { active: tabs[0]?.id === sender.tab.id };
  }
  if (message.type === "READ_CODE") {
    const [execution] = await chrome.scripting.executeScript({
      target: { tabId: sender.tab.id },
      world: "MAIN",
      args: [message.any === true],
      func: (anyModel) => {
        const models = window.monaco?.editor.getModels()
          .filter((model) => anyModel ? model.getLanguageId() !== "plaintext" : model.getLanguageId() === "python3") || [];
        return models.length === 1 ? models[0].getValue() : null;
      },
    });
    return { code: execution?.result ?? null };
  }
  if (message.type === "WRITE_CODE") {
    const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (tabs[0]?.id !== sender.tab.id) {
      return { ok: false, error: "bound tab became inactive before code write (another Edge tab is active)" };
    }
    const live = await request("/v1/command-active", {
      command_id: message.command_id, slug: message.slug,
      document_id: message.document_id, tab_id: sender.tab.id,
    });
    if (live.active !== true) return { ok: false, error: "run command expired before code write" };
    const [execution] = await chrome.scripting.executeScript({
      target: { tabId: sender.tab.id },
      world: "MAIN",
      args: [message.code, message.slug],
      func: (code, slug) => {
        if (document.visibilityState === "hidden") {
          return { ok: false, error: "bound tab became inactive before code write (document hidden)" };
        }
        if (location.pathname.match(/^\/problems\/([a-z0-9-]+)\/?/)?.[1] !== slug) {
          return { ok: false, error: "bound tab left the expected problem before code write" };
        }
        const models = window.monaco?.editor.getModels()
          .filter((model) => model.getLanguageId() === "python3") || [];
        if (models.length !== 1) return { ok: false, error: "Python3 Monaco model unavailable" };
        models[0].setValue(code.endsWith("\n") ? code : `${code}\n`);
        return { ok: models[0].getValue().trim() === code.trim() };
      },
    });
    return execution?.result || { ok: false, error: "editor injection failed" };
  }
  return { error: "unknown message type" };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handle(message, sender).then(sendResponse).catch((error) => {
    sendResponse({ error: String(error?.message || error) });
  });
  return true;
});
