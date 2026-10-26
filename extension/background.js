// Chrome 116+ keeps an extension worker alive while its WebSocket exchanges messages.
importScripts("bookmarks.js", "content.js");
let socket;
let heartbeat;
let connecting = false;
let captureStatus = { stage: "idle" };

async function probe() {
  const permissions = await chrome.permissions.getAll();
  if (!permissions.permissions.includes("tabs") || !permissions.permissions.includes("tabGroups") ||
      !permissions.permissions.includes("scripting") || !(permissions.origins.includes("<all_urls>") ||
      permissions.origins.includes("http://*/*") && permissions.origins.includes("https://*/*")))
    throw new Error("Chrome tabs, tabGroups, scripting, and ordinary-site access are required");
  const windows = await chrome.windows.getAll({ windowTypes: ["normal"] });
  const tabs = await chrome.tabs.query({ windowType: "normal" });
  return {
    connected: true,
    windowCount: windows.filter(window => !window.incognito).length,
    windows: windows.filter(window => !window.incognito).slice(0, 20).map(window => ({ id: window.id, state: window.state, focused: window.focused })),
    tabCount: tabs.filter(tab => !tab.incognito && !inspectionTabs.has(tab.id)).length,
    permissions: permissions.permissions,
    extensionVersion: chrome.runtime.getManifest().version,
    stage: "v5",
    visualCapture: permissions.permissions.includes("debugger"),
    captureBackend: "chrome.debugger/Page.captureScreenshot",
    captureStatus,
    operations: ["list_tabs", "tab_action", "group_action", "move_tabs", "collection_action",
      "close_tabs", "find_duplicates", "restore_snapshot", "undo_last", "extract_tabs", "filter_tabs", "group_categories", "capture_page", "capture_browser", "reload_extension"],
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

async function verifyTabsAbsent(tabIds) {
  const remaining = new Set((await chrome.tabs.query({})).map(tab => tab.id));
  const found = tabIds.filter(id => remaining.has(id));
  if (found.length) throw new Error(`Tabs remain after close: ${found.join(", ")}`);
}

async function getTabIfPresent(id) {
  try { return await chrome.tabs.get(id); }
  catch (error) {
    if (/no tab with id/i.test(error.message)) return null;
    throw error;
  }
}

async function listTabs({ windowId, offset = 0, limit = 20 }) {
  const tabs = await chrome.tabs.query(windowId == null ? { windowType: "normal" } : { windowId });
  const normal = tabs.filter(tab => !tab.incognito && !inspectionTabs.has(tab.id));
  const page = normal.slice(offset, offset + limit);
  const groupIds = [...new Set(page.map(tab => tab.groupId).filter(id => id >= 0))];
  const groups = await Promise.all(groupIds.map(async id => {
    const group = await chrome.tabGroups.get(id);
    return { id, title: group.title, color: group.color, collapsed: group.collapsed, windowId: group.windowId };
  }));
  return { total: normal.length, offset, groups, tabs: page.map(tab => ({
    id: tab.id, windowId: tab.windowId, index: tab.index, title: (tab.title || "").slice(0, 200),
    url: safeUrl(tab.url || ""), active: Boolean(tab.active), pinned: Boolean(tab.pinned), groupId: tab.groupId,
  })) };
}

async function captureTabs(tabIds) {
  const tabs = [];
  const groups = {};
  for (const id of [...new Set(tabIds)]) {
    if (inspectionTabs.has(id)) throw new Error("Temporary inspection tabs cannot be saved or changed");
    const tab = await chrome.tabs.get(id);
    if (tab.incognito || !tab.url) throw new Error("Incognito or inaccessible tabs cannot be saved or changed");
    tabs.push({ id, windowId: tab.windowId, index: tab.index, url: tab.url, title: (tab.title || "").slice(0, 200), status: tab.status, discarded: Boolean(tab.discarded), active: tab.active,
      pinned: tab.pinned, groupId: tab.groupId });
    if (tab.groupId >= 0 && !groups[tab.groupId]) {
      const group = await chrome.tabGroups.get(tab.groupId);
      groups[tab.groupId] = { title: group.title, color: group.color, collapsed: group.collapsed, windowId: group.windowId };
    }
  }
  return { tabs, groups };
}

async function restoreSnapshot(snapshot) {
  if (snapshot.activation) {
    const tab = await chrome.tabs.get(snapshot.activation.tabId);
    if (tab.incognito) throw new Error("Incognito tabs cannot be restored");
    await chrome.tabs.update(tab.id, { active: true });
    if (!(await chrome.tabs.get(tab.id)).active) throw new Error("Previous active tab was not restored");
    return { restored: 1, ids: {} };
  }
  const ids = new Map();
  const restoredGroups = new Map();
  for (const windowId of new Set(snapshot.tabs.map(tab => tab.windowId))) {
    const window = await chrome.windows.get(windowId);
    if (window.incognito || window.type !== "normal") throw new Error("Saved tabs require their original normal window");
  }
  const closeIds = snapshot.closeIds || [];
  if (closeIds.length) {
    await chrome.tabs.remove(closeIds);
    await verifyTabsAbsent(closeIds);
  }
  for (const tab of snapshot.tabs) {
    let id = tab.id;
    if (!await getTabIfPresent(id)) {
      id = (await chrome.tabs.create({ windowId: tab.windowId, url: tab.url, active: false })).id;
      const created = await waitForLoad(id);
      if (created.url !== new URL(tab.url).href) throw new Error("Restored tab URL verification failed");
    }
    ids.set(tab.id, id);
  }
  if (ids.size) await chrome.tabs.ungroup([...ids.values()]);
  const ordered = [...snapshot.tabs].sort((a, b) => a.windowId - b.windowId || a.index - b.index);
  // Park selected tabs at the end first so restoring sparse positions cannot shift an earlier restored tab.
  for (const tab of ordered) {
    const id = ids.get(tab.id);
    await chrome.tabs.update(id, { pinned: Boolean(tab.pinned), active: false });
    const actual = await chrome.tabs.get(id);
    const pinned = tab.pinned ? (await chrome.tabs.query({ windowId: tab.windowId, pinned: true })).length : 0;
    await chrome.tabs.move(id, { windowId: tab.windowId, index: tab.pinned ? pinned - (actual.windowId === tab.windowId ? 1 : 0) : -1 });
  }
  for (const tab of ordered) await chrome.tabs.move(ids.get(tab.id), { windowId: tab.windowId, index: tab.index });
  const grouped = new Map();
  for (const tab of ordered) {
    if (tab.groupId < 0) continue;
    const key = `${tab.windowId}:${tab.groupId}`;
    if (!grouped.has(key)) grouped.set(key, { oldId: tab.groupId, windowId: tab.windowId, tabs: [] });
    grouped.get(key).tabs.push(ids.get(tab.id));
  }
  for (const item of grouped.values()) {
    let groupId = item.oldId;
    try {
      const existing = await chrome.tabGroups.get(groupId);
      if (existing.windowId !== item.windowId) throw new Error("Group belongs to a different window");
      await chrome.tabs.group({ tabIds: item.tabs, groupId });
    } catch {
      groupId = await chrome.tabs.group({ tabIds: item.tabs, createProperties: { windowId: item.windowId } });
    }
    const group = snapshot.groups[item.oldId];
    if (group) await chrome.tabGroups.update(groupId, { title: group.title, color: group.color, collapsed: group.collapsed });
    restoredGroups.set(item.oldId, groupId);
  }
  for (const active of snapshot.tabs.filter(tab => tab.active)) await chrome.tabs.update(ids.get(active.id), { active: true });
  for (const tab of snapshot.tabs.filter(tab => tab.discarded)) {
    if (!(await chrome.tabs.get(ids.get(tab.id))).discarded) await chrome.tabs.discard(ids.get(tab.id));
  }
  for (const tab of snapshot.tabs) {
    const actual = await chrome.tabs.get(ids.get(tab.id));
    const expectedGroup = tab.groupId < 0 ? -1 : restoredGroups.get(tab.groupId);
    const differences = [];
    if (actual.windowId !== tab.windowId) differences.push("window");
    if (actual.index !== tab.index) differences.push("index");
    if (actual.url !== new URL(tab.url).href) differences.push("url");
    if (Boolean(actual.pinned) !== Boolean(tab.pinned)) differences.push("pinned");
    if (actual.groupId !== expectedGroup) differences.push("group");
    if (Boolean(actual.active) !== Boolean(tab.active)) differences.push("active");
    if (tab.discarded && !actual.discarded) differences.push("discarded");
    if (differences.length) throw new Error(`Browser state restoration failed for tab ${tab.id}: ${differences.join(", ")}`);
  }
  for (const [oldId, newId] of restoredGroups) {
    const expected = snapshot.groups[oldId];
    const actual = await chrome.tabGroups.get(newId);
    if (expected && (actual.title !== expected.title || actual.color !== expected.color || actual.collapsed !== expected.collapsed))
      throw new Error("Tab group restoration failed verification");
  }
  return { restored: ids.size, ids: Object.fromEntries(ids) };
}

async function restoreMovedGroups(snapshot, tabIds, windowId) {
  const selected = new Set(tabIds);
  const groups = new Map();
  for (const tab of snapshot.tabs) {
    if (tab.groupId < 0 || !selected.has(tab.id)) continue;
    const key = `${tab.windowId}:${tab.groupId}`;
    if (!groups.has(key)) groups.set(key, { oldId: tab.groupId, tabs: [] });
    groups.get(key).tabs.push(tab.id);
  }
  for (const item of groups.values()) {
    const info = snapshot.groups[item.oldId];
    if (!info) throw new Error("Original group metadata is unavailable");
    let id = item.oldId;
    try {
      const existing = await chrome.tabGroups.get(id);
      if (existing.windowId !== windowId) throw new Error("Original group is in another window");
    } catch {
      id = undefined;
    }
    const groupId = id === undefined
      ? await chrome.tabs.group({ tabIds: item.tabs })
      : await chrome.tabs.group({ tabIds: item.tabs, groupId: id });
    await chrome.tabGroups.update(groupId, { title: info.title, color: info.color, collapsed: info.collapsed });
    const group = await chrome.tabGroups.get(groupId);
    const tabs = await Promise.all(item.tabs.map(tabId => chrome.tabs.get(tabId)));
    if (group.windowId !== windowId || group.title !== info.title || group.color !== info.color ||
        group.collapsed !== info.collapsed || tabs.some(tab => tab.groupId !== groupId))
      throw new Error("Moved group failed restoration verification");
  }
}

async function groupAction({ action, tabIds = [], groupId, title, color, collapsed, mode = "batch" }) {
  if (color !== undefined && !["grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan", "orange"].includes(color))
    throw new Error("Invalid Chrome group color");
  const ids = [...new Set(tabIds)];
  const beforeTabs = action === "update" || action === "delete"
    ? (await chrome.tabs.query({})).filter(tab => tab.groupId === groupId).map(tab => tab.id) : ids;
  if (action === "create" || action === "add") {
    if (!ids.length) throw new Error(`${action} requires tab_ids`);
    const tabs = await Promise.all(ids.map(id => chrome.tabs.get(id)));
    if (tabs.some(tab => tab.incognito)) throw new Error("Incognito tabs are excluded");
    if (new Set(tabs.map(tab => tab.windowId)).size !== 1) throw new Error("One native group must stay within a single window");
    if (action === "add" && (await chrome.tabGroups.get(groupId)).windowId !== tabs[0].windowId)
      throw new Error("Target group must be in the same window as the selected tabs");
    if (action === "create" && (!title || title.length > 80)) throw new Error("create requires a title of 1 to 80 characters");
    if (color !== undefined && !["grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan", "orange"].includes(color))
      throw new Error("Invalid Chrome group color");
  }
  if (action === "remove" && (await Promise.all(ids.map(id => chrome.tabs.get(id)))).some(tab => tab.groupId < 0))
    throw new Error("All selected tabs must belong to a group");
  const before = await captureTabs(beforeTabs);
  let resultGroupId = groupId;
  if (action === "add" || action === "update") await chrome.tabGroups.get(groupId);
  try {
    if (action === "create") {
      if (mode === "batch") resultGroupId = await chrome.tabs.group({ tabIds: ids });
      else if (mode === "sequential") {
        resultGroupId = await chrome.tabs.group({ tabIds: [ids[0]] });
        for (const id of ids.slice(1)) await chrome.tabs.group({ tabIds: [id], groupId: resultGroupId });
      } else throw new Error("mode must be batch or sequential");
      await chrome.tabGroups.update(resultGroupId, { title, color: color || "grey" });
    } else if (action === "add") {
      if (mode === "batch") await chrome.tabs.group({ tabIds: ids, groupId });
      else if (mode === "sequential") for (const id of ids) await chrome.tabs.group({ tabIds: [id], groupId });
      else throw new Error("mode must be batch or sequential");
    }
    else if (action === "remove") {
      if (!ids.length) throw new Error("remove requires tab_ids");
      if (mode === "batch") await chrome.tabs.ungroup(ids);
      else if (mode === "sequential") for (const id of ids) await chrome.tabs.ungroup(id);
      else throw new Error("mode must be batch or sequential");
    } else if (action === "update") {
      const changes = {};
      if (title !== undefined) changes.title = title;
      if (color !== undefined) changes.color = color;
      if (collapsed !== undefined) changes.collapsed = collapsed;
      if (!Object.keys(changes).length) throw new Error("update requires title, color, or collapsed");
      await chrome.tabGroups.update(groupId, changes);
    } else if (action === "delete") {
      if (!beforeTabs.length) throw new Error("Group does not exist or is empty");
      if (mode === "batch") await chrome.tabs.ungroup(beforeTabs);
      else if (mode === "sequential") for (const id of beforeTabs) await chrome.tabs.ungroup(id);
      else throw new Error("mode must be batch or sequential");
    } else throw new Error("action must be create, add, remove, update, or delete");
    if (action === "create" || action === "add") {
      const after = await Promise.all(ids.map(id => chrome.tabs.get(id)));
      if (after.some(tab => tab.groupId !== resultGroupId)) throw new Error("Group membership failed verification");
      if (action === "create") {
        const group = await chrome.tabGroups.get(resultGroupId);
        if (group.title !== title || group.color !== (color || "grey")) throw new Error("New group metadata failed verification");
      }
    }
    if (action === "remove" || action === "delete") {
      const after = await Promise.all(beforeTabs.map(id => chrome.tabs.get(id)));
      if (after.some(tab => tab.groupId >= 0)) throw new Error("Ungroup failed verification");
    }
    if (action === "update") {
      const after = await chrome.tabGroups.get(groupId);
      if ((title !== undefined && after.title !== title) || (color !== undefined && after.color !== color) ||
          (collapsed !== undefined && after.collapsed !== collapsed)) throw new Error("Group update failed verification");
    }
  } catch (error) {
    try { await restoreSnapshot(before); }
    catch (rollbackError) { throw new Error(`Group operation failed (${error.message}); restoration incomplete (${rollbackError.message})`); }
    throw new Error(`Group operation failed; previous state restored (${error.message})`);
  }
  return { action, groupId: resultGroupId, affected: beforeTabs.length || ids.length, before };
}

async function moveTabs({ tabIds, windowId, index, mode = "batch" }) {
  const ids = [...new Set(tabIds || [])];
  if (!ids.length) throw new Error("tab_ids must not be empty");
  const before = await captureTabs(ids);
  const target = await chrome.windows.get(windowId);
  if (target.incognito || target.type !== "normal") throw new Error("Target must be a normal Chrome window");
  const targetTabs = await chrome.tabs.query({ windowId });
  const requested = index == null ? targetTabs.length : index;
  if (!Number.isInteger(requested) || requested < 0 || requested > targetTabs.length) throw new Error("Invalid move index");
  if (!["batch", "sequential"].includes(mode)) throw new Error("mode must be batch or sequential");
  const alreadyThere = before.tabs.filter(tab => tab.windowId === windowId).length;
  const start = Math.min(requested, targetTabs.length - alreadyThere);
  let moved;
  try {
    if (mode === "batch") await chrome.tabs.move(ids, { windowId, index: start });
    else for (let i = 0; i < ids.length; i++) await chrome.tabs.move(ids[i], { windowId, index: start + i });
    await restoreMovedGroups(before, ids, windowId);
    moved = await Promise.all(ids.map(id => chrome.tabs.get(id)));
    const indexes = moved.map(tab => tab.index).sort((a, b) => a - b);
    if (moved.some(tab => tab.windowId !== windowId) || indexes.some((value, i) => value !== start + i))
      throw new Error("Moved tabs failed window/order verification");
  } catch (error) {
    try { await restoreSnapshot(before); }
    catch (rollbackError) { throw new Error(`Move failed (${error.message}); rollback incomplete (${rollbackError.message})`); }
    throw new Error(`Move failed; prior tab positions restored (${error.message})`);
  }
  return { moved: moved.length, windowId, verified: true, before };
}

async function closeTabs({ tabIds, mode = "batch" }) {
  const ids = [...new Set(tabIds || [])];
  if (!ids.length) throw new Error("tab_ids must not be empty");
  const before = await captureTabs(ids);
  if (before.tabs.some(tab => !/^https?:\/\//i.test(tab.url))) throw new Error("Only ordinary web tabs can be saved for restoration");
  if (!["batch", "sequential"].includes(mode)) throw new Error("mode must be batch or sequential");
  try {
    if (mode === "batch") await chrome.tabs.remove(ids);
    else if (mode === "sequential") for (const id of ids) await chrome.tabs.remove(id);
    else throw new Error("mode must be batch or sequential");
    await verifyTabsAbsent(ids);
  } catch (error) {
    try { await restoreSnapshot(before); }
    catch (rollbackError) { throw new Error(`Close failed (${error.message}); restoration incomplete (${rollbackError.message})`); }
    throw new Error(`Close failed; previous tabs restored (${error.message})`);
  }
  return { closed: ids.length, verified: true, before };
}

async function closeSavedCollection({ tabIds, mode = "batch" }) {
  const present = [];
  for (const id of tabIds) {
    if (await getTabIfPresent(id)) present.push(id);
  }
  if (!present.length) return { closed: 0, missing: tabIds.length, verified: true, before: { tabs: [], groups: {} } };
  const result = await closeTabs({ tabIds: present, mode });
  return { closed: result.closed, missing: tabIds.length - present.length, verified: result.verified, before: result.before };
}

async function findDuplicates({ windowId, remove = false, mode = "batch" }) {
  const tabs = (await chrome.tabs.query(windowId == null ? { windowType: "normal" } : { windowId }))
    .filter(tab => !tab.incognito && !inspectionTabs.has(tab.id) && /^https?:\/\//i.test(tab.url || ""));
  const seen = new Set();
  const duplicates = [];
  for (const tab of tabs) {
    if (seen.has(tab.url)) duplicates.push(tab.id);
    else seen.add(tab.url);
  }
  if (!remove) return { count: duplicates.length, samples: duplicates.slice(0, 20) };
  const result = await closeTabs({ tabIds: duplicates, mode });
  return { removed: result.closed, verified: result.verified, before: result.before };
}

async function extractTabs({ tabIds, windowId, wakeSleeping = false }) {
  const tabs = tabIds ? await Promise.all(tabIds.map(id => chrome.tabs.get(id))) :
    await chrome.tabs.query({ windowType: "normal", ...(windowId == null ? {} : { windowId }) });
  const normal = tabs.filter(tab => !tab.incognito && (tabIds || !inspectionTabs.has(tab.id)));
  if (normal.length > 500) throw new Error("Text extraction is limited to 500 tabs per request");
  if (wakeSleeping) {
    const combined = { items: [], failures: [] };
    for (const tab of normal) {
      try {
        combined.items.push(await inspectPage({ tabId: tab.id, detail: "text" }));
      } catch (error) {
        combined.failures.push({ id: tab.id, reason: String(error.message || error).slice(0, 160) });
      }
    }
    return combined;
  }
  const items = [];
  const failures = [];
  const extract = () => {
    const root = document.querySelector("article,[itemprop=articleBody]") || document.querySelector("[role=main]") ||
      document.querySelector("main") || document.body;
    const headings = [];
    for (const node of root.querySelectorAll("h1,h2,h3")) {
      const value = (node.innerText || node.textContent || "").trim();
      if (value) headings.push(value.slice(0, 120));
      if (headings.length === 10) break;
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const chunks = [];
    let previousBlock;
    let size = 0;
    let visited = 0;
    while (visited < 5000 && size < 12000 && walker.nextNode()) {
      visited++;
      const node = walker.currentNode;
      const parent = node.parentElement;
      if (!parent || parent.closest("script,style,noscript,nav,aside,footer,header,form,input,textarea,select,button,svg,dialog,[role=navigation],[role=complementary],[role=contentinfo],[role=banner],[role=menu],[role=menubar],[aria-hidden=true],[contenteditable=true]")) continue;
      const value = node.nodeValue.replace(/\s+/g, " ").trim();
      if (!value) continue;
      const block = parent.closest("address,article,blockquote,dd,div,dl,dt,figure,h1,h2,h3,h4,li,main,ol,p,pre,section,table,td,tr,ul") || root;
      const part = (block !== previousBlock ? " " : "") + value.slice(0, 12000 - size);
      chunks.push(part);
      size += part.length;
      previousBlock = block;
    }
    return { title: document.title.slice(0, 200), headings, text: chunks.join("").trim() };
  };
  for (let offset = 0; offset < normal.length; offset += 8) {
    await Promise.all(normal.slice(offset, offset + 8).map(async tab => {
      if (tab.discarded) {
        failures.push({ id: tab.id, reason: "discarded_tab_not_woken" });
        return;
      }
      if (!/^https?:\/\//i.test(tab.url || "")) {
        failures.push({ id: tab.id, reason: "unsupported_url" });
        return;
      }
      try {
        const [{ result }] = await executeScriptBounded(tab.id, extract, 20000, "text extraction");
        if (!result || !result.text && !result.headings.length) {
          failures.push({ id: tab.id, reason: "no_rendered_text" });
          return;
        }
        if ((await chrome.tabs.get(tab.id)).url !== tab.url) {
          failures.push({ id: tab.id, reason: "navigation_changed" });
          return;
        }
        items.push({ id: tab.id, windowId: tab.windowId, url: tab.url, title: result.title, headings: result.headings, text: result.text });
      } catch (error) {
        const message = String(error?.message || "");
        const reason = /timed out/i.test(message) ? "text_extraction_timed_out"
          : /permission|cannot access|cannot be scripted|host permission/i.test(message) ? "missing_site_permission"
          : /frame.*(removed|not found)|no tab with id/i.test(message) ? "page_unavailable"
          : "text_extraction_failed";
        failures.push({ id: tab.id, reason });
      }
    }));
  }
  return { items, failures };
}

async function filterTabs({ tabIds }) {
  const existing = [];
  for (const id of tabIds) {
    const tab = await getTabIfPresent(id);
    if (tab && !tab.incognito && /^https?:\/\//i.test(tab.url || "")) existing.push(id);
  }
  return { tabIds: existing };
}

async function capturePage({ tabId }) {
  if (!await chrome.permissions.contains({ permissions: ["debugger"] }))
    throw new Error("Native screenshots require Chrome debugger permission; reload/approve Fast-Tabber V5");
  const tab = await chrome.tabs.get(tabId);
  if (tab.incognito || !/^https?:\/\//i.test(tab.url || "") || new URL(tab.url).username || new URL(tab.url).password)
    throw new Error("Only ordinary HTTP/HTTPS pages without embedded credentials can be captured");
  const target = { tabId };
  const previous = (await chrome.tabs.query({ active: true, windowId: tab.windowId }))[0];
  const captureWindow = await chrome.windows.get(tab.windowId);
  let prepared = false;
  let attached = false;
  let image;
  let pageText;
  const command = async (method, params) => {
    captureStatus = { stage: method, tabId };
    let timer;
    try {
      return await Promise.race([chrome.debugger.sendCommand(target, method, params),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`Chrome ${method} timed out`)), 15000); })]);
    } finally { clearTimeout(timer); }
  };
  try {
    captureStatus = { stage: "activate", tabId };
    if (captureWindow.state === "minimized") await chrome.windows.update(tab.windowId, { state: "normal" });
    await chrome.tabs.update(tabId, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
    await waitForLoad(tabId);
    captureStatus = { stage: "attach", tabId };
    await chrome.debugger.attach(target, "1.3");
    attached = true;
    await command("Page.bringToFront");
    captureStatus = { stage: "mask", tabId };
    prepared = true;
    await chrome.scripting.executeScript({ target: { tabId }, func: () => {
      if (globalThis.__fastTabberCapture) throw new Error("Screenshot capture is already active");
      const state = { hidden: [] };
      const style = document.createElement("style");
      style.textContent = "form,input,textarea,select,[contenteditable],iframe,object,embed { visibility:hidden !important; }";
      state.style = style;
      const maskShadowHosts = () => {
        for (const node of document.querySelectorAll("*")) if (node.shadowRoot && !state.hidden.some(item => item.node === node)) {
          state.hidden.push({ node, value: node.style.getPropertyValue("visibility"), priority: node.style.getPropertyPriority("visibility") });
          node.style.setProperty("visibility", "hidden", "important");
        }
      };
      globalThis.__fastTabberCapture = state;
      document.documentElement.append(style);
      maskShadowHosts();
      state.observer = new MutationObserver(maskShadowHosts);
      state.observer.observe(document.documentElement, { childList: true, subtree: true });
    }});
    await new Promise(resolve => setTimeout(resolve, 150));
    let actual = await chrome.tabs.get(tabId);
    if (actual.url !== tab.url) throw new Error("Page changed before screenshot");
    const metrics = await command("Page.getLayoutMetrics");
    const width = Math.min(metrics.cssLayoutViewport.clientWidth, 1280);
    const height = Math.min(metrics.cssContentSize.height, 900);
    if (!(width > 0 && height > 0)) throw new Error("Page has no capturable layout");
    const { data } = await command("Page.captureScreenshot", {
      format: "jpeg", quality: 80, fromSurface: true, captureBeyondViewport: true,
      clip: { x: 0, y: 0, width, height, scale: Math.min(1, 1024 / width, 1024 / height) },
    });
    actual = await chrome.tabs.get(tabId);
    if (actual.url !== tab.url) throw new Error("Page changed during screenshot");
    if (typeof data !== "string" || !data.length || data.length > 6 * 1024 * 1024)
      throw new Error("Native screenshot format or size is invalid");
    image = data;
    pageText = (await extractTabs({ tabIds: [tabId] })).items[0];
  } finally {
    captureStatus = { stage: "cleanup", tabId };
    const errors = [];
    if (prepared) try {
      const [{ result }] = await chrome.scripting.executeScript({ target: { tabId }, func: () => {
        const state = globalThis.__fastTabberCapture;
        if (!state) return true;
        state.observer.disconnect();
        state.style.remove();
        for (const item of state.hidden) item.node.style.setProperty("visibility", item.value, item.priority);
        delete globalThis.__fastTabberCapture;
        return true;
      }});
      if (!result) throw new Error("Capture masks were not removed");
    } catch (error) { errors.push(error.message); }
    try {
      if (attached) await chrome.debugger.detach(target);
    } catch (error) { errors.push(error.message); }
    try {
      if (previous) {
        await chrome.tabs.update(previous.id, { active: true });
        if (!(await chrome.tabs.get(previous.id)).active) throw new Error("Previous active tab was not restored");
      }
      if (captureWindow.state === "normal" || captureWindow.state === "minimized") {
        await chrome.windows.update(tab.windowId, { left: captureWindow.left, top: captureWindow.top,
          width: captureWindow.width, height: captureWindow.height });
      }
      await chrome.windows.update(tab.windowId, { state: captureWindow.state });
      if (tab.discarded) {
        await chrome.tabs.discard(tabId);
        if (!(await chrome.tabs.get(tabId)).discarded) throw new Error("Originally sleeping tab was not returned to sleep");
      }
    } catch (error) { errors.push(error.message); }
    captureStatus = { stage: errors.length ? "restoration_failed" : "idle" };
    if (errors.length) throw new Error(`Screenshot restoration incomplete: ${errors.join("; ")}`);
  }
  return { id: tab.id, windowId: tab.windowId, url: tab.url, title: pageText?.title || tab.title || "",
    headings: pageText?.headings || [], text: pageText?.text || "", image };
}

async function groupCategories({ categories, mode = "batch", expectedUrls = {} }) {
  if (!Array.isArray(categories) || !categories.length || categories.length > 100) throw new Error("Provide 1 to 100 categories");
  if (!["batch", "sequential"].includes(mode)) throw new Error("mode must be batch or sequential");
  const used = new Set();
  const groups = [];
  for (const category of categories) {
    if (!category.title || category.title.length > 80 || !Array.isArray(category.tabIds)) throw new Error("Invalid category grouping");
    const selected = await Promise.all(category.tabIds.map(id => chrome.tabs.get(id)));
    if (selected.some(tab => tab.incognito || !/^https?:\/\//i.test(tab.url || "")))
      throw new Error("Only ordinary web tabs can be grouped from page text");
    if (selected.some(tab => expectedUrls[String(tab.id)] !== tab.url))
      throw new Error("A tab navigated after text classification; no tabs were grouped");
    for (const tab of selected) {
      if (used.has(tab.id)) throw new Error("A tab was assigned to multiple categories");
      used.add(tab.id);
      const key = `${category.title}:${tab.windowId}`;
      let group = groups.find(item => item.key === key);
      if (!group) groups.push(group = { key, title: category.title, windowId: tab.windowId, tabIds: [] });
      group.tabIds.push(tab.id);
    }
  }
  if (!used.size) throw new Error("No tabs matched the categories");
  const before = await captureTabs([...used]);
  try {
    for (const group of groups) {
      const groupId = await chrome.tabs.group({ tabIds: mode === "batch" ? group.tabIds : [group.tabIds[0]] });
      if (mode === "sequential") for (const id of group.tabIds.slice(1))
        await chrome.tabs.group({ tabIds: [id], groupId });
      await chrome.tabGroups.update(groupId, { title: group.title, color: "grey" });
      const actual = await Promise.all(group.tabIds.map(id => chrome.tabs.get(id)));
      const metadata = await chrome.tabGroups.get(groupId);
      if (actual.some(tab => tab.groupId !== groupId || expectedUrls[String(tab.id)] !== tab.url) ||
          metadata.title !== group.title || metadata.color !== "grey")
        throw new Error("Page-text group failed verification");
    }
  } catch (error) {
    try { await restoreSnapshot(before); }
    catch (rollbackError) { throw new Error(`Text grouping failed (${error.message}); restoration incomplete (${rollbackError.message})`); }
    throw new Error(`Text grouping failed; previous state restored (${error.message})`);
  }
  return { grouped: used.size, groups: groups.length, before };
}

async function restoreCollection({ collection, mode = "batch" }) {
  const records = collection?.tabs || [];
  if (!records.length || records.length > 500) throw new Error("Collection must contain 1 to 500 tabs");
  if (!["batch", "sequential"].includes(mode)) throw new Error("mode must be batch or sequential");
  for (const tab of records) {
    const parsed = new URL(tab.url);
    if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("Collection contains a non-web URL and cannot be restored");
    const window = await chrome.windows.get(tab.windowId);
    if (window.incognito || window.type !== "normal") throw new Error("Collection target window is unavailable or private");
    if (await getTabIfPresent(tab.id)) throw new Error("Collection tabs are still open; close them before restoring");
  }
  const created = [];
  try {
    if (mode === "sequential") {
      for (const tab of records) {
        const item = await chrome.tabs.create({ windowId: tab.windowId, url: tab.url, active: false });
        created.push({ source: tab, id: item.id });
        const loaded = await waitForLoad(item.id);
        if (loaded.url !== new URL(tab.url).href) throw new Error("Collection tab redirected; URL verification failed");
      }
    } else {
      const items = await Promise.all(records.map(tab => chrome.tabs.create({ windowId: tab.windowId, url: tab.url, active: false })));
      created.push(...items.map((item, i) => ({ source: records[i], id: item.id })));
      const loaded = await Promise.all(created.map(item => waitForLoad(item.id)));
      if (loaded.some((tab, i) => tab.url !== new URL(records[i].url).href)) throw new Error("Collection tab redirected; URL verification failed");
    }
    for (const item of [...created].sort((a, b) => a.source.windowId - b.source.windowId || a.source.index - b.source.index))
      {
        await chrome.tabs.update(item.id, { pinned: Boolean(item.source.pinned) });
        await chrome.tabs.move(item.id, { windowId: item.source.windowId, index: item.source.index });
      }
    const groups = new Map();
    const restoredGroups = new Map();
    for (const item of created) {
      if (item.source.groupId < 0) continue;
      const key = `${item.source.windowId}:${item.source.groupId}`;
      if (!groups.has(key)) groups.set(key, { source: item.source, ids: [] });
      groups.get(key).ids.push(item.id);
    }
    for (const group of groups.values()) {
      const id = await chrome.tabs.group({ tabIds: group.ids, createProperties: { windowId: group.source.windowId } });
      const info = collection.groups[group.source.groupId];
      if (info) await chrome.tabGroups.update(id, { title: info.title, color: info.color, collapsed: info.collapsed });
      const actualGroup = await chrome.tabGroups.get(id);
      if (info && (actualGroup.title !== info.title || actualGroup.color !== info.color || actualGroup.collapsed !== info.collapsed))
        throw new Error("Restored group metadata failed verification");
      restoredGroups.set(`${group.source.windowId}:${group.source.groupId}`, id);
      const actual = await Promise.all(group.ids.map(tabId => chrome.tabs.get(tabId)));
      if (actual.some(tab => tab.groupId !== id)) throw new Error("Restored group membership failed verification");
    }
    const active = created.find(item => item.source.active);
    if (active) await chrome.tabs.update(active.id, { active: true });
    const verified = await Promise.all(created.map(item => chrome.tabs.get(item.id)));
    if (verified.some((tab, i) => tab.url !== new URL(records[i].url).href || tab.windowId !== records[i].windowId ||
        tab.index !== records[i].index || Boolean(tab.pinned) !== Boolean(records[i].pinned) ||
        Boolean(tab.active) !== Boolean(records[i].active) || tab.groupId !== (records[i].groupId < 0 ? -1 : restoredGroups.get(`${records[i].windowId}:${records[i].groupId}`))))
      throw new Error("Restored collection failed browser-state verification");
    const idMap = Object.fromEntries(created.map(item => [String(item.source.id), item.id]));
    return { restored: created.length, closeIds: created.map(item => item.id), ids: idMap };
  } catch (error) {
    try {
      if (created.length) await chrome.tabs.remove(created.map(item => item.id));
      await verifyTabsAbsent(created.map(item => item.id));
    } catch (rollbackError) {
      throw new Error(`Collection restore failed (${error.message}); cleanup incomplete (${rollbackError.message})`);
    }
    throw new Error(`Collection restore failed; created tabs removed (${error.message})`);
  }
}

async function waitForLoad(tabId) {
  const current = await chrome.tabs.get(tabId);
  if (current.status === "complete" && current.url && !current.discarded) return current;
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
      if (tab.status === "complete" && tab.url && !tab.discarded) finish();
    }).catch(finish);
  });
  return chrome.tabs.get(tabId);
}

async function tabAction({ action, tabId, url, windowId, index }) {
  if (action === "open") {
    const expectedUrl = new URL(url);
    if (!["http:", "https:"].includes(expectedUrl.protocol) || expectedUrl.username || expectedUrl.password)
      throw new Error("Only http and https URLs without embedded credentials can be opened");
    const options = { url, active: false };
    if (windowId !== undefined && windowId !== null) {
      const window = await chrome.windows.get(windowId);
      if (window.type !== "normal" || window.incognito) throw new Error("Target must be a normal Chrome window");
      options.windowId = windowId;
    }
    const created = await chrome.tabs.create(options);
    try {
      const verified = await waitForLoad(created.id);
      if (verified.url !== expectedUrl.href) throw new Error("Opened tab redirected; expected URL verification failed");
      if (windowId != null && verified.windowId !== windowId)
        throw new Error(`Opened tab moved to unexpected window ${verified.windowId} (wanted ${windowId})`);
      if (verified.active) throw new Error("Chrome activated the new tab despite active:false");
      return { action, tab: { id: verified.id, windowId: verified.windowId, index: verified.index, url: safeUrl(verified.url) },
        before: { tabs: [], groups: {}, closeIds: [verified.id] } };
    } catch (error) {
      try {
        await chrome.tabs.remove(created.id);
        await verifyTabsAbsent([created.id]);
      } catch (rollbackError) {
        throw new Error(`Open verification failed (${error.message}); disposable tab cleanup failed (${rollbackError.message})`);
      }
      throw new Error(`Open verification failed; disposable tab removed (${error.message})`);
    }
  }
  const current = await chrome.tabs.get(tabId);
  if (current.incognito) throw new Error("Incognito tabs are excluded");
  const before = await captureTabs([tabId]);
  if (action === "close") {
    if (!/^https?:\/\//i.test(current.url || "")) throw new Error("Only ordinary web tabs can be closed with undo support");
    const result = await closeTabs({ tabIds: [tabId] });
    return { action, tabId, verified: result.verified, before: result.before };
  }
  if (action === "activate") {
    const previous = (await chrome.tabs.query({ active: true, windowId: current.windowId }))[0];
    before.tabs = [];
    before.groups = {};
    before.activation = { tabId: previous.id };
    try {
      await chrome.tabs.update(tabId, { active: true });
      const verified = await chrome.tabs.get(tabId);
      if (!verified.active) throw new Error("Activation did not make the selected tab active");
    } catch (error) {
      try { await restoreSnapshot(before); }
      catch (rollbackError) { throw new Error(`Activation failed (${error.message}); rollback incomplete (${rollbackError.message})`); }
      throw new Error(`Activation failed; prior state restored (${error.message})`);
    }
    return { action, tabId, verified: true, before };
  }
  if (action === "move") {
    const targetWindow = await chrome.windows.get(windowId);
    if (targetWindow.type !== "normal" || targetWindow.incognito) throw new Error("Target must be a normal Chrome window");
    const count = (await chrome.tabs.query({ windowId })).length;
    const targetIndex = index == null ? count : index;
    if (!Number.isInteger(targetIndex) || targetIndex < 0 || targetIndex > count) throw new Error("Invalid move index");
    const previous = before.tabs[0];
    try {
      await chrome.tabs.move(tabId, { windowId, index: targetIndex });
      await restoreMovedGroups(before, [tabId], windowId);
      const verified = await chrome.tabs.get(tabId);
      if (verified.windowId !== windowId || verified.index !== Math.min(targetIndex, count - (previous.windowId === windowId ? 1 : 0)))
        throw new Error("Move did not reach the requested position");
      return { action, tabId, windowId: verified.windowId, index: verified.index, verified: true, before };
    } catch (error) {
      try { await restoreSnapshot(before); }
      catch (rollbackError) { throw new Error(`Move failed (${error.message}); rollback incomplete (${rollbackError.message})`); }
      throw new Error(`Move failed; original position restored (${error.message})`);
    }
  }
  throw new Error("Unsupported action");
}

async function connect() {
  if (connecting || socket && socket.readyState < 2) return;
  connecting = true;
  try {
    await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
    const saved = await chrome.storage.local.get(["pairing", "legacyChecked"]);
    let pairing = saved.pairing;
    // Migrate existing unpacked installs; release packages never contain this file.
    if (!pairing && !saved.legacyChecked && chrome.runtime.getManifest().key) {
      await chrome.storage.local.set({ legacyChecked: true });
      const response = await fetch(chrome.runtime.getURL("pairing.json"));
      if (response.ok) pairing = await response.json();
    }
    if (!pairing) return;
    socket = new WebSocket("ws://127.0.0.1:8765/extension");
    socket.onopen = () => socket.send(JSON.stringify({ ...pairing, version: chrome.runtime.getManifest().version }));
    socket.onmessage = async ({ data }) => {
      const message = JSON.parse(data);
      if (message.type === "paired") {
        if (message.token) await chrome.storage.local.set({ pairing: { token: message.token } });
        heartbeat = setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "ping" }));
        }, 20000);
        return;
      }
      if (message.type === "pong") return;
      try {
        const handlers = { probe, list_tabs: listTabs, tab_action: tabAction, group_action: groupAction,
          move_tabs: moveTabs, close_tabs: closeTabs, close_saved_collection: closeSavedCollection, restore_snapshot: restoreSnapshot,
          capture_tabs: ({ tabIds }) => captureTabs(tabIds || []), find_duplicates: findDuplicates,
          extract_tabs: extractTabs, filter_tabs: filterTabs, group_categories: groupCategories, capture_page: capturePage,
          reload_extension: () => { setTimeout(() => chrome.runtime.reload(), 500); return { scheduled: true }; },
          capture_browser: async () => captureTabs((await chrome.tabs.query({ windowType: "normal" })).filter(tab => !tab.incognito && !inspectionTabs.has(tab.id)).map(tab => tab.id)),
          restore_collection: restoreCollection, bookmark_tree: bookmarkTree, bookmark_action: bookmarkAction,
          restore_bookmarks: restoreBookmarks, inspect_page: inspectPage };
        if (!handlers[message.operation]) throw new Error("Browser operation is not enabled");
        const result = await handlers[message.operation](message.arguments || {});
        socket.send(JSON.stringify({ id: message.id, result }));
      } catch (error) {
        socket.send(JSON.stringify({ id: message.id, error: error.message }));
      }
    };
    socket.onclose = async event => {
      clearInterval(heartbeat);
      if (event.code === 1008) {
        await chrome.storage.local.remove("pairing");
        return;
      }
      setTimeout(connect, 3000);
    };
    socket.onerror = () => socket.close();
  } catch (error) {
    console.error("Fast-Tabber setup error:", error.message);
  } finally { connecting = false; }
}

chrome.runtime.onStartup.addListener(connect);
function handlePair(message, respond) {
  if (message.type !== "pair") return;
  (async () => {
    try {
      if (!/^[0-9a-f]{32}$/.test(message.code)) throw new Error("Paste the 32-character code supplied by your agent");
      if (socket && socket.readyState === WebSocket.OPEN) { respond({ started: true }); return; }
      await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
      await chrome.storage.local.set({ pairing: { pairingCode: message.code } });
      if (socket) socket.close();
      await connect();
      respond({ started: true });
    } catch (error) { respond({ error: error.message }); }
  })();
  return true;
}
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id) return;
  return handlePair(message, respond);
});
chrome.runtime.onMessageExternal.addListener((message, sender, respond) => {
  if (sender.id || sender.frameId !== 0 || sender.origin !== "http://127.0.0.1:8765" || sender.url !== "http://127.0.0.1:8765/setup") return;
  return handlePair(message, respond);
});
chrome.alarms.create("fast-tabber-reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === "fast-tabber-reconnect") connect(); });
connect();
