// Explicit, bounded page evidence for the connected agent; never read form values.
const inspectionTabs = new Set();
async function executeScriptBounded(tabId, func, timeout = 20000, stage = "inspection") {
  let timer;
  try {
    return await Promise.race([
      chrome.scripting.executeScript({ target: { tabId }, injectImmediately: true, func }),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`Page ${stage} timed out`)), timeout); }),
    ]);
  } finally { clearTimeout(timer); }
}

async function waitForDocument(tabId) {
  const [{ result }] = await executeScriptBounded(tabId, () => new Promise(resolve => {
    if (document.body && document.readyState !== "loading") return resolve(true);
    const finish = ready => {
      clearTimeout(timer);
      document.removeEventListener("DOMContentLoaded", onReady);
      resolve(ready);
    };
    const onReady = () => finish(Boolean(document.body));
    const timer = setTimeout(() => finish(false), 10000);
    document.addEventListener("DOMContentLoaded", onReady, { once: true });
  }), 15000, "DOM readiness");
  if (!result) throw new Error(`Page DOM was not ready for inspection within 10 seconds (tab ${tabId})`);
}

async function inspectPage({ tabId, bookmarkId, detail = "text", offset = 0, limit = 20 }) {
  let created;
  const originalTabId = bookmarkId == null ? tabId : undefined;
  if (bookmarkId != null) {
    const node = (await bookmarkTree()).nodes.find(n => n.id === bookmarkId);
    if (!node?.url || !/^https?:\/\//i.test(node.url) || new URL(node.url).username || new URL(node.url).password)
      throw new Error("Select an ordinary bookmark URL without embedded credentials");
    created = (await chrome.tabs.create({ url: node.url, active: false })).id;
    tabId = created;
  } else {
    const original = await chrome.tabs.get(tabId);
    if (original.incognito || !/^https?:\/\//i.test(original.url || "") || new URL(original.url).username || new URL(original.url).password)
      throw new Error("Only ordinary web pages without embedded credentials can be inspected");
    if (original.discarded || original.status === "unloaded") {
      // Chrome can replace tab IDs on discard; inspect a bounded background copy instead.
      created = (await chrome.tabs.create({ url: original.url, windowId: original.windowId, active: false })).id;
      tabId = created;
    }
  }
  if (created) inspectionTabs.add(created);
  let tab;
  try {
    if (created) await waitForDocument(tabId);
    tab = await chrome.tabs.get(tabId);
    if (tab.incognito || !/^https?:\/\//i.test(tab.url || "") || new URL(tab.url).username || new URL(tab.url).password)
      throw new Error("Only ordinary web pages without embedded credentials can be inspected");
    if (detail === "image") return { ...await capturePage({ tabId }), id: originalTabId ?? tabId, temporary: Boolean(created) };
    if (tab.discarded || tab.status === "unloaded") throw new Error("Page became unloaded during inspection; its original state was left unchanged");
    if (tab.status !== "complete") await waitForDocument(tabId);
    if (detail === "text") {
      const result = await extractTabs({ tabIds: [tabId] });
      if (result.failures.length) throw new Error(result.failures[0].reason);
      return { ...result.items[0], id: originalTabId ?? tabId, temporary: Boolean(created) };
    }
    if (detail === "links") {
      const [{ result }] = await chrome.scripting.executeScript({ target: { tabId }, injectImmediately: true, func: () =>
        [...document.querySelectorAll("a[href]")].map(n => ({ title: (n.innerText || n.getAttribute("aria-label") || "").trim().slice(0, 200), url: n.href }))
          .filter(n => { try { const u = new URL(n.url); return ["http:", "https:"].includes(u.protocol) && !u.username && !u.password; } catch { return false; } }).slice(0, 5000) });
      return { id: originalTabId ?? tabId, total: result.length, offset, links: result.slice(offset, offset + limit), temporary: Boolean(created) };
    }
    if (detail === "accessibility") {
      if (!await chrome.permissions.contains({ permissions: ["debugger"] })) throw new Error("Accessibility inspection needs debugger permission");
      const target = { tabId };
      await chrome.debugger.attach(target, "1.3");
      try {
        let timer;
        let tree;
        try {
          tree = await Promise.race([chrome.debugger.sendCommand(target, "Accessibility.getFullAXTree"),
            new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("Accessibility capture timed out")), 15000); })]);
        } finally { clearTimeout(timer); }
        const nodes = tree.nodes.filter(n => !n.ignored).map(n => ({ id: n.nodeId, role: n.role?.value,
          name: String(n.name?.value || "").slice(0, 300) }));
        return { id: originalTabId ?? tabId, total: nodes.length, offset, nodes: nodes.slice(offset, offset + limit), temporary: Boolean(created) };
      } finally { await chrome.debugger.detach(target); }
    }
    throw new Error("detail must be text, links, accessibility, or image");
  } finally {
    if (created) { await chrome.tabs.remove(created); await verifyTabsAbsent([created]); inspectionTabs.delete(created); }
  }
}
