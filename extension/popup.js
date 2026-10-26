const byId = id => document.getElementById(id);
const selected = new Set();
let bookmarks = [];
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
function update() {
  byId("download-all").disabled = !bookmarks.length;
  byId("download").disabled = !selected.size;
  byId("download").textContent = `Download selected bookmarks (${selected.size})`;
  byId("all").checked = bookmarks.length > 0 && selected.size === bookmarks.length;
  byId("all").indeterminate = selected.size > 0 && selected.size < bookmarks.length;
}
async function loadBookmarks() {
  const allowed = await chrome.permissions.contains({ permissions: ["bookmarks"] });
  byId("enable").hidden = allowed;
  if (!allowed) { byId("status").textContent = "Enable bookmark access to select and export bookmarks. Tab export is available."; return; }
  bookmarks = [];
  const visit = (node, folders) => {
    if (node.url) bookmarks.push({ id: node.id, title: node.title, url: cleanUrl(node.url), folder: folders.join(" / ") });
    else for (const child of node.children || []) visit(child, node.title ? [...folders, node.title] : folders);
  };
  for (const root of await chrome.bookmarks.getTree()) visit(root, []);
  render();
}
async function run(action) { try { await action(); } catch (error) { byId("status").textContent = error.message; } }
byId("tabs").onclick = () => run(async () => download((await chrome.tabs.query({ windowType: "normal" })).filter(tab => !tab.incognito && tab.url)
  .map(tab => ({ title: tab.title || "", url: cleanUrl(tab.url), folder: "" })), "open-tabs"));
byId("groups").onclick = () => run(async () => {
  const groups = new Map((await chrome.tabGroups.query({})).map(group => [group.id, group.title || "Unnamed group"]));
  const tabs = (await chrome.tabs.query({ windowType: "normal" })).filter(tab => !tab.incognito && tab.url && tab.groupId !== chrome.tabGroups.TAB_GROUP_ID_NONE);
  tabs.sort((a, b) => a.windowId - b.windowId || a.index - b.index);
  download(tabs.map(tab => ({ title: tab.title || "", url: cleanUrl(tab.url), folder: groups.get(tab.groupId) || "Unnamed group", groupKey: `${tab.windowId}:${tab.groupId}` })), "grouped-tabs", true);
});
byId("enable").onclick = () => run(async () => { if (await chrome.permissions.request({ permissions: ["bookmarks"] })) await loadBookmarks(); });
byId("all").onchange = () => { selected.clear(); if (byId("all").checked) bookmarks.forEach(row => selected.add(row.id)); render(); };
byId("filter").oninput = render;
byId("download").onclick = () => run(() => download(bookmarks.filter(row => selected.has(row.id)), "bookmarks"));
byId("download-all").onclick = () => run(() => download(bookmarks, "all-bookmarks"));
run(loadBookmarks);
