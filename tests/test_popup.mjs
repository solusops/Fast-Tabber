import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

class Element {
  constructor() { this.children = []; this.listeners = {}; this.value = ""; this.checked = false; }
  append(...nodes) { this.children.push(...nodes.flatMap(node => node.fragment ? node.children : [node])); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  addEventListener(name, action) { this.listeners[name] = action; }
}
const elements = new Map();
const document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  createElement: () => new Element(),
  createTextNode: text => ({ text }),
  createDocumentFragment: () => Object.assign(new Element(), { fragment: true }),
};
const tabs = [
  { windowId: 1, index: 0, groupId: 7, title: "A", url: "https://a.test/" },
  { windowId: 1, index: 1, groupId: 7, title: "B", url: "https://b.test/" },
  { windowId: 2, index: 0, groupId: 8, title: "C", url: "https://c.test/" },
  { windowId: 1, index: 2, groupId: -1, url: "https://ungrouped.test/" },
  { windowId: 3, index: 0, groupId: 9, incognito: true, url: "https://private.test/" },
];
const tree = [{ id: "0", children: [{ id: "1", title: "Bookmarks", children: [
  { id: "10", title: "Folder", children: [
    { id: "11", title: "A", url: "https://a.test/" },
    { id: "12", title: "Nested", children: [{ id: "13", title: "B", url: "https://b.test/" }] },
  ] },
  { id: "20", title: "Other", children: [{ id: "21", title: "C", url: "https://c.test/" }] },
] }] }];
const downloads = [];
const context = vm.createContext({ document, URL, downloads, chrome: {
  permissions: { contains: async () => true },
  tabs: { query: async () => tabs },
  tabGroups: { TAB_GROUP_ID_NONE: -1, query: async () => [{ id: 7, title: "Same name" }, { id: 8, title: "Same name" }] },
  bookmarks: { getTree: async () => tree },
} });
vm.runInContext(readFileSync(new URL("../extension/popup.js", import.meta.url), "utf8"), context);
vm.runInContext("download = (rows, name, grouped) => downloads.push({rows, name, grouped})", context);
await new Promise(resolve => setImmediate(resolve));
const change = (check, checked) => { check.checked = checked; check.listeners.change(); };
const groups = document.getElementById("tab-groups").children;
assert.equal(groups.length, 2, "Exclude ungrouped and incognito tabs; distinguish same-named groups");
change(groups[1].children[0], true);
await document.getElementById("selected-groups").onclick();
assert.deepEqual(Array.from(downloads.at(-1).rows, row => row.url), ["https://c.test/"]);
tabs.push({ windowId: 2, index: 1, groupId: 8, title: "New", url: "https://new.test/" });
await document.getElementById("selected-groups").onclick();
assert.equal(downloads.at(-1).rows.length, 2, "Refresh selected group contents at export time");
const folders = document.getElementById("folders").children;
change(folders[1].children[0], true);
await document.getElementById("download").onclick();
assert.deepEqual(Array.from(downloads.at(-1).rows, row => row.id), ["11", "13"], "Include nested folder contents once");
assert.equal(folders[0].children[0].indeterminate, true);
change(document.getElementById("bookmarks").children[0].children[0], false);
assert.equal(folders[1].children[0].indeterminate, true);
await document.getElementById("download-all").onclick();
assert.equal(downloads.at(-1).rows.length, 3, "Download all ignores partial selection");
assert.equal(document.getElementById("status").textContent || "", "");
console.log("Popup group selection, fresh exports, nested folder selection and partial checkbox checks passed");
