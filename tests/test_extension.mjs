import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../extension/background.js", import.meta.url), "utf8");
const context = vm.createContext({
  chrome: { alarms: { create() {}, onAlarm: { addListener() {} } }, runtime: { onStartup: { addListener() {} }, getURL: () => "pairing.json", getManifest: () => ({ version: "0.2.0" }) } },
  fetch: async () => { throw new Error("test context has no extension pairing"); },
  console: { error() {} }, setTimeout, clearTimeout, clearInterval, WebSocket: class {}, URL,
});
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
