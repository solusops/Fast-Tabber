// Chrome 116+ keeps an extension worker alive while its WebSocket exchanges messages.
let socket;
let heartbeat;

async function probe() {
  const permissions = await chrome.permissions.getAll();
  if (!permissions.permissions.includes("tabs") || !permissions.permissions.includes("tabGroups"))
    throw new Error("Chrome tabs and tabGroups permissions are required");
  const windows = await chrome.windows.getAll({ windowTypes: ["normal"] });
  const tabs = await chrome.tabs.query({ windowType: "normal" });
  return {
    connected: true,
    windowCount: windows.filter(window => !window.incognito).length,
    tabCount: tabs.filter(tab => !tab.incognito).length,
    permissions: permissions.permissions,
    extensionVersion: chrome.runtime.getManifest().version,
    stage: "v2",
    operations: ["list_tabs", "tab_action", "group_action", "move_tabs", "collection_action",
      "close_tabs", "find_duplicates", "restore_snapshot", "undo_last"],
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
  const normal = tabs.filter(tab => !tab.incognito);
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
    const tab = await chrome.tabs.get(id);
    if (tab.incognito || !tab.url) throw new Error("Incognito or inaccessible tabs cannot be saved or changed");
    tabs.push({ id, windowId: tab.windowId, index: tab.index, url: tab.url, active: tab.active,
      pinned: tab.pinned, groupId: tab.groupId });
    if (tab.groupId >= 0 && !groups[tab.groupId]) {
      const group = await chrome.tabGroups.get(tab.groupId);
      groups[tab.groupId] = { title: group.title, color: group.color, collapsed: group.collapsed, windowId: group.windowId };
    }
  }
  return { tabs, groups };
}

async function restoreSnapshot(snapshot) {
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
  for (const tab of [...snapshot.tabs].sort((a, b) => a.windowId - b.windowId || a.index - b.index)) {
    const id = ids.get(tab.id);
    await chrome.tabs.update(id, { pinned: Boolean(tab.pinned), active: false });
    await chrome.tabs.move(id, { windowId: tab.windowId, index: tab.index });
  }
  if (ids.size) await chrome.tabs.ungroup([...ids.values()]);
  const grouped = new Map();
  for (const tab of snapshot.tabs) {
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
  for (const tab of snapshot.tabs) {
    const actual = await chrome.tabs.get(ids.get(tab.id));
    const expectedGroup = tab.groupId < 0 ? -1 : restoredGroups.get(tab.groupId);
    if (actual.windowId !== tab.windowId || actual.index !== tab.index || actual.url !== new URL(tab.url).href ||
        Boolean(actual.pinned) !== Boolean(tab.pinned) || actual.groupId !== expectedGroup ||
        Boolean(actual.active) !== Boolean(tab.active)) throw new Error("Browser state restoration failed verification");
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
    .filter(tab => !tab.incognito && /^https?:\/\//i.test(tab.url || ""));
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
        const handlers = { probe, list_tabs: listTabs, tab_action: tabAction, group_action: groupAction,
          move_tabs: moveTabs, close_tabs: closeTabs, close_saved_collection: closeSavedCollection, restore_snapshot: restoreSnapshot,
          capture_tabs: ({ tabIds }) => captureTabs(tabIds || []), find_duplicates: findDuplicates,
          restore_collection: restoreCollection };
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
