// Native bookmark transactions. Tree snapshots stay in the local recovery journal.
async function bookmarkTree() {
  if (!await chrome.permissions.contains({ permissions: ["bookmarks"] }))
    throw new Error("Bookmark permission is not enabled. Click Fast-Tabber's extension button, then Enable bookmarks.");
  const flat = [];
  const visit = node => {
    flat.push({ id: node.id, parentId: node.parentId, index: node.index || 0, title: node.title || "", url: node.url,
      protected: !node.parentId || node.parentId === "0" || Boolean(node.unmodifiable), managed: Boolean(node.unmodifiable) });
    for (const child of node.children || []) visit(child);
  };
  for (const root of await chrome.bookmarks.getTree()) visit(root);
  return { nodes: flat };
}

function bookmarkFingerprint(nodes) {
  return JSON.stringify(nodes.map(n => [n.id, n.parentId || null, n.index || 0, n.title, n.url || null]).sort((a, b) => a[0].localeCompare(b[0])));
}

async function restoreBookmarks({ bookmarkBefore, bookmarkAfter }) {
  if (!bookmarkBefore || !bookmarkAfter) throw new Error("Bookmark outcome is uncertain; reconcile it before undoing");
  const current = (await bookmarkTree()).nodes;
  if (bookmarkFingerprint(current) !== bookmarkFingerprint(bookmarkAfter))
    throw new Error("Bookmarks changed outside this operation; undo stopped to preserve those changes");
  const now = new Map(current.map(n => [n.id, n]));
  const before = new Map(bookmarkBefore.map(n => [n.id, n]));
  const ids = {};
  for (const node of bookmarkBefore) {
    if (now.has(node.id)) { ids[node.id] = node.id; continue; }
    if (node.protected) throw new Error("A protected bookmark root is missing");
    const made = await chrome.bookmarks.create({ parentId: ids[node.parentId] || node.parentId, title: node.title,
      ...(node.url ? { url: node.url } : {}) });
    ids[node.id] = made.id;
    now.set(made.id, made);
  }
  // Rescue original bookmarks from newly created folders before removing those folders.
  for (const node of bookmarkBefore.filter(n => !n.protected)) {
    const id = ids[node.id];
    const actual = now.get(id);
    if (actual.parentId !== (ids[node.parentId] || node.parentId))
      await chrome.bookmarks.move(id, { parentId: ids[node.parentId] || node.parentId });
    if (actual.title !== node.title || actual.url !== node.url)
      await chrome.bookmarks.update(id, { title: node.title, ...(node.url ? { url: node.url } : {}) });
  }
  const extras = current.filter(n => !before.has(n.id));
  const extraIds = new Set(extras.map(n => n.id));
  for (const node of extras.filter(n => !extraIds.has(n.parentId))) {
    if (node.protected) throw new Error("Cannot remove a protected bookmark root");
    if (node.url) await chrome.bookmarks.remove(node.id); else await chrome.bookmarks.removeTree(node.id);
  }
  const positions = new Map((await bookmarkTree()).nodes.map(node => [node.id, node]));
  for (const node of bookmarkBefore.filter(n => !n.protected).sort((a, b) => a.index - b.index)) {
    const id = ids[node.id], parentId = ids[node.parentId] || node.parentId;
    const old = positions.get(id);
    if (old.parentId === parentId && old.index === node.index) continue;
    const moved = await chrome.bookmarks.move(id, { parentId, index: node.index });
    for (const item of positions.values()) if (item.id !== id && item.parentId === old.parentId && item.index > old.index) item.index--;
    for (const item of positions.values()) if (item.id !== id && item.parentId === moved.parentId && item.index >= moved.index) item.index++;
    positions.set(id, moved);
  }
  const expected = bookmarkBefore.map(n => ({ ...n, id: ids[n.id], parentId: ids[n.parentId] || n.parentId }));
  if (bookmarkFingerprint((await bookmarkTree()).nodes) !== bookmarkFingerprint(expected))
    throw new Error("Bookmark restoration failed verification");
  return { restored: bookmarkBefore.length, bookmarkIds: ids, verified: true };
}

async function bookmarkAction({ action, ids = [], parentId, title, url, index, items = [], groups = [], windowId, validateOnly = false }) {
  const before = (await bookmarkTree()).nodes;
  const nodes = new Map(before.map(n => [n.id, n]));
  const selected = (action === "save_tabs" ? [] : [...new Set(ids)]).map(id => {
    const node = nodes.get(id);
    if (!node || node.protected) throw new Error("Bookmark is missing, protected, or managed");
    return node;
  });
  const parent = nodes.get(parentId);
  if (["create", "move", "group", "save_tabs"].includes(action) && (!parent || parent.url || parent.id === "0" || parent.managed))
    throw new Error("Choose an existing bookmark folder as parent_id");
  if (index != null && (!Number.isInteger(index) || index < 0 || index > before.filter(n => n.parentId === parentId).length))
    throw new Error("Invalid bookmark index");
  if (["rename", "move", "delete", "open"].includes(action) && !selected.length) throw new Error("Select bookmark IDs first");
  if (action === "rename" && (selected.length !== 1 || !title?.trim())) throw new Error("Rename needs one ID and a nonempty title");
  if (action === "move") for (const node of selected) {
    let ancestor = parent;
    while (ancestor) {
      if (ancestor.id === node.id) throw new Error("Cannot move a folder inside itself");
      ancestor = nodes.get(ancestor.parentId);
    }
  }
  const validUrl = value => {
    const parsed = new URL(value);
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password)
      throw new Error("Only ordinary HTTP/HTTPS URLs without embedded credentials can be opened or created");
    return parsed.href;
  };
  if (action === "open") {
    if (selected.some(n => !n.url)) throw new Error("Select bookmark URLs, not folders, for opening");
    selected.forEach(n => validUrl(n.url));
    if (validateOnly) return { validated: true };
    const created = [];
    try {
      for (const node of selected) created.push((await chrome.tabs.create({ url: node.url, active: false, ...(windowId != null ? { windowId } : {}) })).id);
      for (const id of created) await waitForLoad(id);
      if (title) {
        const groupId = await chrome.tabs.group({ tabIds: created });
        await chrome.tabGroups.update(groupId, { title });
        if ((await Promise.all(created.map(id => chrome.tabs.get(id)))).some(tab => tab.groupId !== groupId))
          throw new Error("Opened bookmark group failed verification");
      }
      return { opened: created.length, verified: true, before: { tabs: [], groups: {}, closeIds: created } };
    } catch (error) {
      if (created.length) await chrome.tabs.remove(created);
      await verifyTabsAbsent(created);
      throw error;
    }
  }
  if (action === "save_tabs") {
    if (!ids.length) throw new Error("Select tab IDs first");
    const tabs = (await captureTabs(ids.map(Number))).tabs;
    items = tabs.map(tab => ({ title: tab.title, url: validUrl(tab.url) }));
  }
  if (action === "create") items = [{ title: title || "", ...(url ? { url: validUrl(url) } : {}) }];
  if (["create", "save_tabs"].includes(action)) for (const item of items) if (item.url) validUrl(item.url);
  if (action === "group") {
    if (!groups.length) throw new Error("Select groups first");
    const used = new Set();
    for (const group of groups) {
      if (!group.title?.trim() || !group.ids?.length) throw new Error("Groups need titles and bookmark IDs");
      for (const id of group.ids) {
        if (used.has(id) || !nodes.get(id)?.url || nodes.get(id).protected) throw new Error("Invalid or overlapping bookmark groups");
        used.add(id);
      }
    }
  }
  if (validateOnly) return { validated: true };
  // Predict our own tree changes so rollback cannot erase concurrent user/sync edits.
  let expected = before.map(node => ({ ...node }));
  const put = (node, moving = false) => {
    if (moving) {
      const old = expected.find(item => item.id === node.id);
      expected = expected.filter(item => item.id !== node.id);
      for (const item of expected) if (item.parentId === old.parentId && item.index > old.index) item.index--;
    }
    for (const item of expected) if (item.parentId === node.parentId && item.index >= node.index) item.index++;
    expected.push({ id: node.id, parentId: node.parentId, index: node.index || 0, title: node.title || "", url: node.url });
  };
  const create = async values => { const node = await chrome.bookmarks.create(values); put(node); return node; };
  const move = async (id, values) => { const node = await chrome.bookmarks.move(id, values); put(node, true); return node; };
  const update = async (id, values) => {
    const node = await chrome.bookmarks.update(id, values);
    Object.assign(expected.find(item => item.id === id), { title: node.title, url: node.url });
    return node;
  };
  const remove = async node => {
    if (node.url) await chrome.bookmarks.remove(node.id); else await chrome.bookmarks.removeTree(node.id);
    const removed = new Set([node.id]);
    let count;
    do { count = removed.size; for (const item of expected) if (removed.has(item.parentId)) removed.add(item.id); } while (count !== removed.size);
    const original = expected.find(item => item.id === node.id);
    expected = expected.filter(item => !removed.has(item.id));
    for (const item of expected) if (item.parentId === original.parentId && item.index > original.index) item.index--;
  };
  let result = {};
  try {
    if (["create", "save_tabs"].includes(action)) {
      const made = [];
      for (const item of items) {
        const node = await create({ parentId, title: item.title || "", ...(item.url ? { url: item.url } : {}), ...(index != null ? { index: index + made.length } : {}) });
        const actual = (await chrome.bookmarks.get(node.id))[0];
        if (actual.parentId !== parentId || actual.title !== (item.title || "") || actual.url !== item.url) throw new Error("Bookmark creation failed verification");
        made.push(node.id);
      }
      result = { created: made.length, ids: made.slice(0, 100) };
    } else if (action === "rename") {
      await update(selected[0].id, { title });
      if ((await chrome.bookmarks.get(selected[0].id))[0].title !== title) throw new Error("Bookmark rename failed verification");
      result = { renamed: 1 };
    } else if (action === "move") {
      for (let i = 0; i < selected.length; i++) {
        const node = await move(selected[i].id, { parentId, ...(index != null ? { index: index + i } : {}) });
        if (node.parentId !== parentId || index != null && node.index !== index + i) throw new Error("Bookmark move failed verification");
      }
      result = { moved: selected.length };
    } else if (action === "delete") {
      const selectedIds = new Set(selected.map(n => n.id));
      for (const node of selected.filter(n => {
        let parent = nodes.get(n.parentId);
        while (parent) { if (selectedIds.has(parent.id)) return false; parent = nodes.get(parent.parentId); }
        return true;
      })) await remove(node);
      const remaining = new Set((await bookmarkTree()).nodes.map(n => n.id));
      if (selected.some(n => remaining.has(n.id))) throw new Error("Bookmark deletion failed verification");
      result = { deleted: selected.length };
    } else if (action === "group") {
      for (const group of groups) {
        const folder = await create({ parentId, title: group.title });
        for (const id of group.ids) {
          await move(id, { parentId: folder.id });
          if ((await chrome.bookmarks.get(id))[0].parentId !== folder.id) throw new Error("Bookmark folder assignment failed verification");
        }
      }
      result = { grouped: groups.reduce((n, group) => n + group.ids.length, 0), groups: groups.length };
    } else throw new Error("Unsupported bookmark action");
    if (bookmarkFingerprint((await bookmarkTree()).nodes) !== bookmarkFingerprint(expected))
      throw new Error("Bookmarks changed concurrently; reconcile before further mutations");
  } catch (error) {
    if (bookmarkFingerprint((await bookmarkTree()).nodes) !== bookmarkFingerprint(expected))
      throw new Error(`${error.message}; concurrent or uncertain bookmark changes preserved; automatic rollback stopped`);
    try { await restoreBookmarks({ bookmarkBefore: before, bookmarkAfter: (await bookmarkTree()).nodes }); }
    catch (rollback) { throw new Error(`${error.message}; bookmark restoration incomplete: ${rollback.message}`); }
    throw new Error(`${error.message}; previous bookmarks restored`);
  }
  return { ...result, verified: true, before: { bookmarkBefore: before, bookmarkAfter: (await bookmarkTree()).nodes } };
}
