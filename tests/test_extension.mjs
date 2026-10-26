import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const popupCss = readFileSync(new URL("../extension/popup.css", import.meta.url), "utf8");
assert.match(popupCss, /font:18px\/1\.5/);
assert.match(popupCss, /small\{[^}]*font-size:16px/);
assert.match(popupCss, /#status\{font-size:17px/);
assert.doesNotMatch(popupCss, /—/);

const source = readFileSync(new URL("../extension/background.js", import.meta.url), "utf8");
const context = vm.createContext({
  chrome: { alarms: { create() {}, onAlarm: { addListener() {} } }, runtime: { onMessageExternal: { addListener() {} }, onMessage: { addListener() {} }, onStartup: { addListener() {} }, getURL: () => "pairing.json", getManifest: () => ({ version: "0.2.0" }) } },
  fetch: async () => { throw new Error("test context has no extension pairing"); },
  console: { error() {} }, setTimeout, clearTimeout, clearInterval, WebSocket: class {}, URL,
});
context.importScripts = (...names) => names.forEach(name => vm.runInContext(readFileSync(new URL(`../extension/${name}`, import.meta.url), "utf8"), context));
vm.runInContext(source, context);
const safeUrl = vm.runInContext("safeUrl", context);

assert.equal(safeUrl("https://example.com/private/path?token=secret#key"), "https://example.com");
assert.equal(safeUrl("chrome://extensions/"), "chrome://extensions");
assert.equal(safeUrl("file:///C:/private/document"), "file:");
console.log("3 URL privacy checks passed");

const capturePage = vm.runInContext("capturePage", context);
let active = 8;
let scripts = 0;
let allowed = false;
context.chrome.permissions = { contains: async () => allowed };
context.chrome.tabs = {
  get: async id => ({ id, windowId: 1, active: active === id, url: "https://example.com/", incognito: false }),
  query: async () => [{ id: active }],
  update: async id => { active = id; },
  captureVisibleTab: async () => { throw new Error("capture failed"); },
};
context.chrome.windows = { getLastFocused: async () => ({ id: 1 }), update: async () => {}, get: async () => ({ focused: true, state: "normal" }) };
context.chrome.scripting = { executeScript: async () => { scripts++; return [{ result: true }]; } };
let detached = false;
context.chrome.debugger = {
  attach: async () => {}, detach: async () => { detached = true; },
  sendCommand: async (_, method) => {
    if (method === "Page.bringToFront") return {};
    if (method === "Page.getLayoutMetrics") return { cssLayoutViewport: { clientWidth: 800 }, cssContentSize: { height: 600 } };
    throw new Error("capture failed");
  },
};
vm.runInContext("waitForLoad = async () => ({ status: 'complete' })", context);
await assert.rejects(capturePage({ tabId: 9 }), /permission/);
assert.equal(active, 8);
assert.equal(scripts, 0);
allowed = true;
await assert.rejects(capturePage({ tabId: 9 }), /capture failed/);
assert.equal(active, 8, "Capture failure must restore the previous active tab");
assert.equal(scripts, 2, "Capture failure must remove page masks and restore scroll");
assert.equal(detached, true, "Capture must release its debugger attachment on failure");
console.log("Capture prerequisite and failure-restoration checks passed");

let sleeping = true;
let reloads = 0;
context.chrome.tabs.get = async id => ({ id, windowId: 1, discarded: id === 9 && sleeping, status: "complete", url: "https://example.com/", incognito: false });
context.chrome.tabs.reload = async () => { reloads++; sleeping = false; };
context.chrome.tabs.discard = async () => { sleeping = true; };
const temporaryRemoved = [];
context.chrome.tabs.create = async options => { assert.equal(options.active, false); return { id: 19 }; };
context.chrome.tabs.remove = async id => { temporaryRemoved.push(id); };
context.chrome.tabs.query = async () => [{ id: 9 }];
context.chrome.tabs.update = async () => { throw new Error("Text-only extraction must not activate a tab"); };
context.chrome.windows.update = async () => { throw new Error("Text-only extraction must not change window state"); };
context.chrome.scripting.executeScript = async () => [{ result: { title: "Research", headings: ["Notes"], text: "Rendered page text" } }];
const extractTabs = vm.runInContext("extractTabs", context);
const textResult = await extractTabs({ tabIds: [9], wakeSleeping: true });
assert.equal(textResult.items.length, 1);
assert.equal(reloads, 0);
assert.equal(sleeping, true);
assert.equal(textResult.items[0].id, 9);
assert.deepEqual(temporaryRemoved, [19]);
console.log("Sleeping-page text extraction closes its background copy and leaves the original asleep");

sleeping = false;
const root = { querySelectorAll: () => [] };
let visitedTextNodes = 0;
const textNodes = Array.from({ length: 10000 }, () => ({ nodeValue: "robotics policy page content ".repeat(4),
  parentElement: { closest: () => null } }));
context.NodeFilter = { SHOW_TEXT: 4 };
context.document = {
  title: "Large page",
  querySelectorAll: () => [],
  querySelector: () => root,
  createTreeWalker: () => ({
    currentNode: null,
    nextNode() { this.currentNode = textNodes[visitedTextNodes++]; return Boolean(this.currentNode); },
  }),
};
context.chrome.scripting.executeScript = async ({ func }) => [{ result: func() }];
const bounded = await extractTabs({ tabIds: [9] });
assert.equal(bounded.items.length, 1);
assert.ok(bounded.items[0].text.length <= 12000);
assert.ok(visitedTextNodes < 5000, "Extraction must stop walking a large DOM at the text bound");
console.log("Large DOM extraction stops at the bounded first-page representation");

const articleHeading = { innerText: "Main article headline", textContent: "Main article headline" };
const articleRoot = { querySelectorAll: selector => selector === "h1,h2,h3" ? [articleHeading] : [] };
const bodyRoot = { querySelectorAll: () => [] };
let selectedRoot;
context.document = {
  title: "Page with large navigation",
  body: bodyRoot,
  querySelector: selector => selector === "article,[itemprop=articleBody]" ? articleRoot : null,
  createTreeWalker: node => {
    selectedRoot = node;
    const tree = [{ nodeValue: "The article explains embodied robotics and policy learning.", parentElement: { closest: () => null } }];
    return { currentNode: null, nextNode() { this.currentNode = tree.shift(); return Boolean(this.currentNode); } };
  },
};
context.chrome.scripting.executeScript = async ({ func }) => [{ result: func() }];
const representative = await extractTabs({ tabIds: [9] });
assert.equal(selectedRoot, articleRoot, "Article content must take priority over the body and its navigation");
assert.deepEqual(JSON.parse(JSON.stringify(representative.items[0].headings)), ["Main article headline"]);
assert.match(representative.items[0].text, /embodied robotics/);
console.log("Semantic article root and headings are prioritized ahead of page chrome");

context.chrome.tabs.get = async id => ({ id, windowId: 1, active: false, discarded: false,
  url: id === 10 ? "chrome://settings" : "https://example.com/", incognito: false });
context.document.body = {};
context.document.readyState = "interactive";
context.chrome.scripting.executeScript = async ({ target }) => {
  if (target.tabId === 9) throw new Error("Cannot access contents of the page");
  return [{ result: {} }];
};
const recoveredBatch = await extractTabs({ tabIds: [9, 10] });
assert.equal(recoveredBatch.items.length, 0);
assert.deepEqual(JSON.parse(JSON.stringify(recoveredBatch.failures.map(item => item.reason))).sort(),
  ["missing_site_permission", "unsupported_url"]);
console.log("Extraction classifies a restricted page and continues other tabs");

const inspectPage = vm.runInContext("inspectPage", context);
context.document = { body: {}, readyState: "interactive" };
context.chrome.tabs.get = async id => ({ id, windowId: 1, discarded: false, status: "loading", url: "https://example.com/", incognito: false });
vm.runInContext("waitForLoad = async () => { throw new Error('Must not wait for subresources during text inspection'); }", context);
context.chrome.scripting.executeScript = async injection => [{ result: injection.func.name === "extract"
  ? { title: "Research", headings: ["Notes"], text: "Rendered page text" }
  : await injection.func() }];
const inspected = await inspectPage({ tabId: 9, detail: "text" });
assert.equal(inspected.text, "Rendered page text", "A usable DOM is sufficient even when subresources keep the tab loading");
let removed = 0;
context.document = { body: null, readyState: "loading", addEventListener() {}, removeEventListener() { removed++; } };
context.setTimeout = (callback, duration) => setTimeout(callback, duration === 10000 ? 0 : duration);
await assert.rejects(inspectPage({ tabId: 9, detail: "text" }), /Page DOM was not ready/);
assert.equal(removed, 1, "DOM readiness timeouts must remove their listener");
console.log("Text inspection uses DOM readiness, reports load failures and removes timed-out listeners");

let unloaded = true;
let unloadedReloads = 0;
let unloadedDiscarded = false;
context.chrome.tabs.get = async id => ({ id, windowId: 1, discarded: unloadedDiscarded, status: id === 9 && unloaded ? "unloaded" : "complete",
  url: "https://example.com/", incognito: false });
context.chrome.tabs.reload = async () => { unloaded = false; unloadedReloads++; };
context.chrome.tabs.discard = async () => { unloadedDiscarded = true; };
context.document = { body: {}, readyState: "complete" };
context.chrome.scripting.executeScript = async injection => {
  if (injection.target.tabId === 9) throw new Error("Cannot access contents of the page. Extension manifest must request permission to access the respective host.");
  return [{ result: injection.func.name === "extract" ? { title: "Restored page", headings: ["Article"], text: "The main article text" } : await injection.func() }];
};
const restoredPage = await inspectPage({ tabId: 9, detail: "text" });
assert.equal(restoredPage.text, "The main article text");
assert.equal(restoredPage.id, 9);
assert.equal(unloadedReloads, 0);
assert.equal(unloadedDiscarded, false);
assert.equal(unloaded, true, "The original restored tab must stay unloaded with the same ID");
console.log("Chrome-restored unloaded tabs use background copies without changing their identity or focus");
assert.equal(vm.runInContext("inspectionTabs.size", context), 0);
vm.runInContext("inspectionTabs.add(19)", context);
context.chrome.tabs.query = async () => [{ id: 9, groupId: -1, windowId: 1 }, { id: 19, groupId: -1, windowId: 1 }];
const visibleTabs = await vm.runInContext("listTabs({})", context);
assert.equal(visibleTabs.total, 1, "Concurrent browser discovery must exclude temporary inspection copies");
assert.equal(visibleTabs.tabs[0].id, 9);
await assert.rejects(vm.runInContext("captureTabs([19])", context), /Temporary inspection/);
vm.runInContext("inspectionTabs.delete(19)", context);
console.log("Temporary inspection copies are excluded from listings and browser mutations");
