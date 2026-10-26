const byId = id => document.getElementById(id);
const selected = new Set();
let bookmarks = [];
let folders = [];
const folderChecks = new Map();
const selectedGroups = new Set();
function cleanUrl(raw) {
  try { const url = new URL(raw); url.username = ""; url.password = ""; return url.href; }
  catch { return ""; }
}
function download(rows, name, grouped = false) {
  if (!rows.length) throw new Error("No URLs selected");
  const format = byId("format").value;
  const url = URL.createObjectURL(new Blob([exportRows(rows, format, grouped)], { type: format === "csv" ? "text/csv;charset=utf-8" : "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url; link.download = `${name}.${format}`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  byId("status").textContent = `Downloaded ${rows.length} URLs.`;
}
function render() {
  const query = byId("filter").value.toLowerCase();
  const fragment = document.createDocumentFragment();
  for (const row of bookmarks.filter(row => `${row.title} ${row.url} ${row.folder}`.toLowerCase().includes(query))) {
    const label = document.createElement("label");
    const check = document.createElement("input"); check.type = "checkbox"; check.checked = selected.has(row.id);
    check.addEventListener("change", () => { check.checked ? selected.add(row.id) : selected.delete(row.id); update(); });
    label.append(check, document.createTextNode(" " + (row.title || row.url)));
    const path = document.createElement("small"); path.textContent = row.folder; label.append(path); fragment.append(label);
  }
  byId("bookmarks").replaceChildren(fragment); update();
}
function renderFolders() {
  folderChecks.clear();
  const fragment = document.createDocumentFragment();
  for (const folder of folders) {
    const ids = bookmarks.filter(row => row.folderIds.includes(folder.id)).map(row => row.id);
    if (!ids.length) continue;
    const label = document.createElement("label");
    const check = document.createElement("input"); check.type = "checkbox";
    check.addEventListener("change", () => {
      for (const id of ids) check.checked ? selected.add(id) : selected.delete(id);
      render();
    });
    folderChecks.set(folder.id, { check, ids });
    label.append(check, document.createTextNode(` ${folder.path} (${ids.length})`)); fragment.append(label);
  }
  byId("folders").replaceChildren(fragment);
}
function update() {
  byId("download-all").disabled = !bookmarks.length;
  byId("download").disabled = !selected.size;
  byId("download").textContent = `Download selected bookmarks (${selected.size})`;
  byId("all").checked = bookmarks.length > 0 && selected.size === bookmarks.length;
  byId("all").indeterminate = selected.size > 0 && selected.size < bookmarks.length;
  for (const { check, ids } of folderChecks.values()) {
    const count = ids.filter(id => selected.has(id)).length;
    check.checked = count === ids.length;
    check.indeterminate = count > 0 && count < ids.length;
  }
}
async function loadBookmarks() {
  const allowed = await chrome.permissions.contains({ permissions: ["bookmarks"] });
  byId("enable").hidden = allowed;
  if (!allowed) { byId("status").textContent = "Enable bookmark access to select and export bookmarks. Tab export is available."; return; }
  bookmarks = []; folders = [];
  const visit = (node, path, folderIds) => {
    if (node.url) bookmarks.push({ id: node.id, title: node.title, url: cleanUrl(node.url), folder: path.join(" / "), folderIds });
    else {
      const next = node.title ? [...path, node.title] : path;
      if (node.id !== "0") folders.push({ id: node.id, path: next.join(" / ") || "Unnamed folder" });
      for (const child of node.children || []) visit(child, next, [...folderIds, node.id]);
    }
  };
  for (const root of await chrome.bookmarks.getTree()) visit(root, [], []);
  renderFolders();
  render();
}
async function groupRows() {
  const groups = new Map((await chrome.tabGroups.query({})).map(group => [group.id, group.title || "Unnamed group"]));
  const tabs = (await chrome.tabs.query({ windowType: "normal" })).filter(tab => !tab.incognito && tab.url && tab.groupId !== chrome.tabGroups.TAB_GROUP_ID_NONE);
  tabs.sort((a, b) => a.windowId - b.windowId || a.index - b.index);
  return tabs.map(tab => ({ title: tab.title || "", url: cleanUrl(tab.url), folder: groups.get(tab.groupId) || "Unnamed group", groupKey: `${tab.windowId}:${tab.groupId}` }));
}
async function loadGroups() {
  const groups = new Map();
  for (const row of await groupRows()) {
    if (!groups.has(row.groupKey)) groups.set(row.groupKey, { title: row.folder, count: 0 });
    groups.get(row.groupKey).count++;
  }
  const fragment = document.createDocumentFragment();
  for (const [key, group] of groups) {
    const label = document.createElement("label");
    const check = document.createElement("input"); check.type = "checkbox";
    check.addEventListener("change", () => {
      check.checked ? selectedGroups.add(key) : selectedGroups.delete(key);
      byId("selected-groups").disabled = !selectedGroups.size;
    });
    label.append(check, document.createTextNode(` ${group.title} (${group.count}, window ${key.split(":")[0]})`)); fragment.append(label);
  }
  byId("tab-groups").replaceChildren(fragment);
  if (!groups.size) byId("tab-groups").textContent = "No grouped tabs.";
}
async function run(action) { try { await action(); } catch (error) { byId("status").textContent = error.message; } }
byId("pair").onclick = () => run(async () => {
  const result = await chrome.runtime.sendMessage({ type: "pair", code: byId("pair-code").value.trim() });
  byId("pair-code").value = "";
  if (result.error) throw new Error(result.error);
  byId("status").textContent = "Pairing requested. Ask your agent to confirm the live Chrome connection. If it fails, request a new code.";
});
byId("tabs").onclick = () => run(async () => download((await chrome.tabs.query({ windowType: "normal" })).filter(tab => !tab.incognito && tab.url)
  .map(tab => ({ title: tab.title || "", url: cleanUrl(tab.url), folder: "" })), "open-tabs"));
byId("groups").onclick = () => run(async () => download(await groupRows(), "grouped-tabs", true));
byId("selected-groups").onclick = () => run(async () => download((await groupRows()).filter(row => selectedGroups.has(row.groupKey)), "selected-groups", true));
byId("enable").onclick = () => run(async () => { if (await chrome.permissions.request({ permissions: ["bookmarks"] })) await loadBookmarks(); });
byId("all").onchange = () => { selected.clear(); if (byId("all").checked) bookmarks.forEach(row => selected.add(row.id)); render(); };
byId("filter").oninput = render;
byId("download").onclick = () => run(() => download(bookmarks.filter(row => selected.has(row.id)), "bookmarks"));
byId("download-all").onclick = () => run(() => download(bookmarks, "all-bookmarks"));
run(loadBookmarks);
run(loadGroups);
