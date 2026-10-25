// Chrome 116+ keeps an extension worker alive while its WebSocket exchanges messages.
let socket;
let heartbeat;

async function probe() {
  const permissions = await chrome.permissions.getAll();
  if (!permissions.permissions.includes("tabs")) throw new Error("Chrome tabs permission is missing");
  const windows = await chrome.windows.getAll({ windowTypes: ["normal"] });
  const tabs = await chrome.tabs.query({ windowType: "normal" });
  return {
    connected: true,
    windowCount: windows.filter(window => !window.incognito).length,
    tabCount: tabs.filter(tab => !tab.incognito).length,
    permissions: permissions.permissions,
    extensionVersion: chrome.runtime.getManifest().version,
    stage: "v1",
  };
}

function safeUrl(raw) {
  try {
    const parsed = new URL(raw);
    return ["http:", "https:"].includes(parsed.protocol)
      ? parsed.origin
      : parsed.protocol === "chrome:" ? `${parsed.protocol}//${parsed.host}` : parsed.protocol;
  } catch { return ""; }
}

async function listTabs({ windowId, offset = 0, limit = 20 }) {
  const tabs = await chrome.tabs.query(windowId == null ? { windowType: "normal" } : { windowId });
  const normal = tabs.filter(tab => !tab.incognito);
  return { total: normal.length, offset, tabs: normal.slice(offset, offset + limit).map(tab => ({
    id: tab.id, windowId: tab.windowId, index: tab.index, title: (tab.title || "").slice(0, 200),
    url: safeUrl(tab.url || ""), active: Boolean(tab.active), pinned: Boolean(tab.pinned),
  })) };
}

async function waitForLoad(tabId) {
  const current = await chrome.tabs.get(tabId);
  if (current.status === "complete" && current.url) return current;
  await new Promise((resolve, reject) => {
    const finish = error => {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(onUpdated);
      error ? reject(error) : resolve();
    };
    const onUpdated = (id, change) => {
      if (id === tabId && change.status === "complete") finish();
    };
    const timer = setTimeout(() => finish(new Error("Timed out waiting for the new tab to finish loading")), 15000);
    chrome.tabs.onUpdated.addListener(onUpdated);
    chrome.tabs.get(tabId).then(tab => {
      if (tab.status === "complete" && tab.url) finish();
    }).catch(finish);
  });
  return chrome.tabs.get(tabId);
}

async function tabAction({ action, tabId, url, windowId, index }) {
  if (action === "open") {
    const expectedUrl = new URL(url);
    if (!["http:", "https:"].includes(expectedUrl.protocol)) throw new Error("Only http and https pages can be opened");
    const created = await chrome.tabs.create({ url, active: false });
    try {
      const verified = await waitForLoad(created.id);
      if (verified.url !== expectedUrl.href) throw new Error("Opened tab redirected; expected URL verification failed");
      return { action, tab: { id: verified.id, windowId: verified.windowId, index: verified.index, url: safeUrl(verified.url) } };
    } catch (error) {
      try {
        await chrome.tabs.remove(created.id);
      } catch (rollbackError) {
        throw new Error(`Open verification failed (${error.message}); disposable tab cleanup failed (${rollbackError.message})`);
      }
      throw new Error(`Open verification failed; disposable tab removed (${error.message})`);
    }
  }
  const before = await chrome.tabs.get(tabId);
  if (before.incognito) throw new Error("Incognito tabs are excluded");
  if (action === "close") {
    await chrome.tabs.remove(tabId);
    try { await chrome.tabs.get(tabId); throw new Error("Closed tab still exists"); }
    catch (error) { if (error.message === "Closed tab still exists") throw error; }
    return { action, tabId, verified: true };
  }
  if (action === "activate") {
    const previous = (await chrome.tabs.query({ active: true, windowId: before.windowId }))[0];
    await chrome.tabs.update(tabId, { active: true });
    const verified = await chrome.tabs.get(tabId);
    if (!verified.active) {
      if (previous) await chrome.tabs.update(previous.id, { active: true });
      throw new Error("Activation failed verification; previous active tab restored");
    }
    return { action, tabId, verified: true };
  }
  if (action === "move") {
    const targetWindow = await chrome.windows.get(windowId);
    if (targetWindow.type !== "normal" || targetWindow.incognito) throw new Error("Target must be a normal Chrome window");
    const count = (await chrome.tabs.query({ windowId })).length;
    const targetIndex = index == null ? count : index;
    if (!Number.isInteger(targetIndex) || targetIndex < 0 || targetIndex > count) throw new Error("Invalid move index");
    await chrome.tabs.move(tabId, { windowId, index: targetIndex });
    const verified = await chrome.tabs.get(tabId);
    if (verified.windowId !== windowId || verified.index !== Math.min(targetIndex, count - (before.windowId === windowId ? 1 : 0))) {
      await chrome.tabs.move(tabId, { windowId: before.windowId, index: before.index });
      const restored = await chrome.tabs.get(tabId);
      if (restored.windowId !== before.windowId) throw new Error("Move failed; rollback could not restore original window");
      throw new Error("Move failed verification; original position restored");
    }
    return { action, tabId, windowId: verified.windowId, index: verified.index, verified: true };
  }
  throw new Error("Unsupported action");
}

async function connect() {
  try {
    const response = await fetch(chrome.runtime.getURL("pairing.json"));
    if (!response.ok) throw new Error("Run the Fast-Tabber setup command before loading this extension");
    const pairing = await response.json();
    socket = new WebSocket("ws://127.0.0.1:8765/extension");
    socket.onopen = () => socket.send(JSON.stringify({ token: pairing.token }));
    socket.onmessage = async ({ data }) => {
      const message = JSON.parse(data);
      if (message.type === "paired") {
        heartbeat = setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "ping" }));
        }, 20000);
        return;
      }
      if (message.type === "pong") return;
      try {
        const handlers = { probe, list_tabs: listTabs, tab_action: tabAction };
        if (!handlers[message.operation]) throw new Error("Browser operation is not enabled");
        const result = await handlers[message.operation](message.arguments || {});
        socket.send(JSON.stringify({ id: message.id, result }));
      } catch (error) {
        socket.send(JSON.stringify({ id: message.id, error: error.message }));
      }
    };
    socket.onclose = () => {
      clearInterval(heartbeat);
      setTimeout(connect, 3000);
    };
    socket.onerror = () => socket.close();
  } catch (error) {
    console.error("Fast-Tabber setup error:", error.message);
  }
}

chrome.runtime.onStartup.addListener(connect);
connect();
