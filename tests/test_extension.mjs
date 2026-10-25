import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../extension/background.js", import.meta.url), "utf8");
const context = vm.createContext({
  chrome: { runtime: { onStartup: { addListener() {} }, getURL: () => "pairing.json", getManifest: () => ({ version: "0.2.0" }) } },
  fetch: async () => { throw new Error("test context has no extension pairing"); },
  console: { error() {} }, setTimeout, clearInterval, WebSocket: class {}, URL,
});
vm.runInContext(source, context);
const safeUrl = vm.runInContext("safeUrl", context);

assert.equal(safeUrl("https://example.com/private/path?token=secret#key"), "https://example.com");
assert.equal(safeUrl("chrome://extensions/"), "chrome://extensions");
assert.equal(safeUrl("file:///C:/private/document"), "file:");
console.log("3 URL privacy checks passed");
