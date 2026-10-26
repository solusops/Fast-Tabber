import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const data = {};
const sockets = [];
let listener;
let access;
class Socket {
  static OPEN = 1;
  constructor(url) { assert.equal(url, "ws://127.0.0.1:8765/extension"); this.readyState = 0; sockets.push(this); }
  send(message) { this.sent = JSON.parse(message); }
  close() { this.readyState = 3; }
}
const chrome = {
  storage: { local: { setAccessLevel: async options => { access = options.accessLevel; },
    get: async () => ({ ...data }), set: async values => Object.assign(data, values), remove: async key => { delete data[key]; } } },
  runtime: { id: "test", getManifest: () => ({ version: "0.7.0" }),
    onStartup: { addListener() {} }, onMessage: { addListener(fn) { listener = fn; } } },
  alarms: { create() {}, onAlarm: { addListener() {} } },
};
const context = vm.createContext({ chrome, WebSocket: Socket, URL, console,
  setTimeout() {}, setInterval() {}, clearInterval() {}, clearTimeout() {} });
context.importScripts = (...names) => names.forEach(name => vm.runInContext(readFileSync(new URL(`../extension/${name}`, import.meta.url), "utf8"), context));
const source = readFileSync(new URL("../extension/background.js", import.meta.url), "utf8");
vm.runInContext(source, context);
await new Promise(resolve => setImmediate(resolve));
assert.equal(sockets.length, 0, "Unpaired store builds do not connect or fetch a bundled secret");
assert.equal(access, "TRUSTED_CONTEXTS");
assert.equal(listener({type:"pair",code:"a".repeat(32)}, {id:"other"}, () => assert.fail()), undefined);
const reply = await new Promise(resolve => listener({type:"pair",code:"bad"}, {id:"test"}, resolve));
assert.match(reply.error, /32-character/);
await new Promise(resolve => listener({type:"pair",code:"a".repeat(32)}, {id:"test"}, resolve));
const ws = sockets[0];
ws.onopen();
assert.equal(ws.sent.pairingCode, "a".repeat(32));
assert.equal(ws.sent.token, undefined);
await ws.onmessage({data:JSON.stringify({type:"paired",token:"b".repeat(64)})});
assert.equal(data.pairing.token, "b".repeat(64));
assert.equal(data.pairing.pairingCode, undefined, "Do not retain the consumed one-time code");
await ws.onclose({code:1008});
assert.equal(data.pairing, undefined, "Stop reconnecting with rejected credentials; request a fresh code");
console.log("Store pairing, sender validation, restricted storage and credential replacement checks passed");
