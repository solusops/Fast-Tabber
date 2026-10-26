import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

let nodes = [
  { id: "0", title: "", index: 0 }, { id: "1", parentId: "0", title: "Bar", index: 0 },
  { id: "10", parentId: "1", title: "A", url: "https://a.test/", index: 0 },
  { id: "11", parentId: "1", title: "B", url: "https://b.test/", index: 1 },
  { id: "12", parentId: "1", title: "Folder", index: 2 },
];
let next = 100;
let failMove = false;
let concurrent = false;
const clone = value => JSON.parse(JSON.stringify(value));
const children = parent => nodes.filter(n => n.parentId === parent).sort((a,b) => a.index-b.index);
const get = id => { const node = nodes.find(n => n.id === id); if (!node) throw new Error("missing bookmark"); return node; };
const insert = (node, parentId, index) => {
  const siblings = children(parentId);
  const position = index == null ? siblings.length : Math.min(index, siblings.length);
  siblings.splice(position, 0, node);
  siblings.forEach((n,i) => { n.parentId = parentId; n.index = i; });
};
const bookmarks = {
  getTree: async () => { const visit = n => ({ ...n, ...(!n.url ? { children: children(n.id).map(visit) } : {}) }); return [visit(get("0"))]; },
  get: async id => [clone(get(id))],
  create: async values => { const node = { id: String(next++), title: values.title || "", ...(values.url ? {url:values.url} : {}) }; insert(node, values.parentId, values.index); nodes.push(node); return clone(node); },
  move: async (id, values) => {
    if (failMove) { failMove = false; throw new Error("injected move failure"); }
    const node = get(id), oldParent = node.parentId;
    nodes = nodes.filter(n => n.id !== id);
    children(oldParent).forEach((n,i) => n.index = i);
    insert(node, values.parentId, values.index); nodes.push(node);
    if (concurrent) { concurrent = false; nodes.push({ id: "user-edit", parentId: "1", index: children("1").length, title: "User addition", url: "https://user.test/" }); }
    return clone(node);
  },
  update: async (id, values) => Object.assign(get(id), values),
  remove: async id => bookmarks.removeTree(id),
  removeTree: async id => {
    const parent = get(id).parentId;
    const deleted = new Set([id]);
    let count;
    do { count = deleted.size; nodes.forEach(n => {if(deleted.has(n.parentId)) deleted.add(n.id);}); } while(count!==deleted.size);
    nodes = nodes.filter(n => !deleted.has(n.id));
    children(parent).forEach((n,i) => n.index=i);
  },
};
const context = vm.createContext({ chrome: { bookmarks, permissions: { contains: async () => true } }, URL });
vm.runInContext(readFileSync(new URL("../extension/bookmarks.js", import.meta.url), "utf8"), context);
const action = vm.runInContext("bookmarkAction", context);
const restore = vm.runInContext("restoreBookmarks", context);
const snapshot = async () => vm.runInContext("bookmarkTree()", context);
const fingerprint = vm.runInContext("bookmarkFingerprint", context);
const original = (await snapshot()).nodes;
await assert.rejects(action({action:"move",ids:["12"],parentId:"12"}), /inside itself/);
assert.equal(fingerprint((await snapshot()).nodes), fingerprint(original));
const grouped = await action({action:"group",parentId:"1",groups:[{title:"Topic",ids:["10","11"]}]});
assert.equal(grouped.grouped,2);
await restore(grouped.before);
assert.equal(fingerprint((await snapshot()).nodes),fingerprint(original));
const moved = await action({action:"move",ids:["10","11"],parentId:"12",index:0});
const deleted = await action({action:"delete",ids:["12"]});
const recovered = await restore(deleted.before);
assert.notEqual(recovered.bookmarkIds["12"],"12");
// Remap an earlier snapshot just as the server journal does.
for (const field of ["bookmarkBefore","bookmarkAfter"]) for (const node of moved.before[field]) {
  node.id = recovered.bookmarkIds[node.id] || node.id;
  node.parentId = recovered.bookmarkIds[node.parentId] || node.parentId;
}
await restore(moved.before);
assert.equal((await snapshot()).nodes.filter(n=>n.url).length,2);
const beforeFailure = (await snapshot()).nodes;
failMove = true;
await assert.rejects(action({action:"group",parentId:"1",groups:[{title:"Fail",ids:[recovered.bookmarkIds["10"]]}]}), /previous bookmarks restored/);
assert.equal(fingerprint((await snapshot()).nodes),fingerprint(beforeFailure));
concurrent = true;
await assert.rejects(action({action:"move",ids:[recovered.bookmarkIds["10"]],parentId:"1",index:0}), /changes preserved/);
assert.ok(nodes.some(n=>n.id==="user-edit"));
vm.runInContext(readFileSync(new URL("../extension/export.js",import.meta.url),"utf8"), context);
const exportRows = vm.runInContext("exportRows",context);
assert.equal(exportRows([{url:"https://a.test/"}],"txt"),"https://a.test/\n");
assert.match(exportRows([{title:'=SUM(1,2)',url:'https://a.test/?q="x"',folder:"Folder"}],"csv"), /"'=SUM\(1,2\)"/);
const groupRows = [{title:"A",url:"https://a.test/",folder:"Research",groupKey:"1:7"},
  {title:"B",url:"https://b.test/",folder:"Research",groupKey:"1:7"},
  {title:"C",url:"https://c.test/",folder:"Research",groupKey:"2:8"}];
assert.equal(exportRows(groupRows,"txt",true),"[Research]\nhttps://a.test/\nhttps://b.test/\n\n[Research]\nhttps://c.test/\n");
assert.match(exportRows(groupRows,"csv",true),/^title,url,group\r\n/);
console.log("Bookmark grouping, move/reorder, deletion/ID recovery, rollback, concurrency and TXT/CSV checks passed");
