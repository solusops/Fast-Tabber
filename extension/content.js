// Explicit, bounded page evidence for the connected agent; never read form values.
async function inspectPage({ tabId, bookmarkId, detail = "text", offset = 0, limit = 20 }) {
  let created;
  if (bookmarkId != null) {
    const node = (await bookmarkTree()).nodes.find(n => n.id === bookmarkId);
    if (!node?.url || !/^https?:\/\//i.test(node.url) || new URL(node.url).username || new URL(node.url).password)
      throw new Error("Select an ordinary bookmark URL without embedded credentials");
    created = (await chrome.tabs.create({ url: node.url, active: false })).id;
    tabId = created;
  }
  let tab;
  try {
    if (created) await waitForLoad(tabId);
    tab = await chrome.tabs.get(tabId);
    if (tab.incognito || !/^https?:\/\//i.test(tab.url || "") || new URL(tab.url).username || new URL(tab.url).password)
      throw new Error("Only ordinary web pages without embedded credentials can be inspected");
    if (detail === "image") return await capturePage({ tabId });
    if (tab.discarded) await chrome.tabs.reload(tabId);
    await waitForLoad(tabId);
    if (detail === "text") {
      const result = await extractTabs({ tabIds: [tabId] });
      if (result.failures.length) throw new Error(JSON.stringify(result.failures));
      return result.items[0];
    }
    if (detail === "links") {
      const [{ result }] = await chrome.scripting.executeScript({ target: { tabId }, func: () =>
        [...document.querySelectorAll("a[href]")].map(n => ({ title: (n.innerText || n.getAttribute("aria-label") || "").trim().slice(0, 200), url: n.href }))
          .filter(n => { try { const u = new URL(n.url); return ["http:", "https:"].includes(u.protocol) && !u.username && !u.password; } catch { return false; } }).slice(0, 5000) });
      return { id: tabId, total: result.length, offset, links: result.slice(offset, offset + limit) };
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
        return { id: tabId, total: nodes.length, offset, nodes: nodes.slice(offset, offset + limit) };
      } finally { await chrome.debugger.detach(target); }
    }
    throw new Error("detail must be text, links, accessibility, or image");
  } finally {
    if (created) { await chrome.tabs.remove(created); await verifyTabsAbsent([created]); }
    else if (tab?.discarded && !(await chrome.tabs.get(tabId)).discarded) {
      await chrome.tabs.discard(tabId);
      if (!(await chrome.tabs.get(tabId)).discarded) throw new Error("Inspected page was not returned to sleep");
    }
  }
}
