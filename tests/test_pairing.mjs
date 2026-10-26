import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const data = {};
const sockets = [];
let listener;
let external;
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
    onStartup: { addListener() {} }, onMessage: { addListener(fn) { listener = fn; } },
    onMessageExternal: { addListener(fn) { external = fn; } } },
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
const sender = {origin:"http://127.0.0.1:8765",url:"http://127.0.0.1:8765/setup",frameId:0};
for (const invalid of [{...sender,origin:"https://evil.test"}, {...sender,frameId:1},
  {...sender,url:"http://127.0.0.1:9000/setup"}, {...sender,id:"other"}, {...sender,url:"http://127.0.0.1:8765/other"}]) {
  assert.equal(external({type:"pair",code:"a".repeat(32)}, invalid, () => assert.fail()), undefined);
}
const reply = await new Promise(resolve => listener({type:"pair",code:"bad"}, {id:"test"}, resolve));
assert.match(reply.error, /32-character/);
await new Promise(resolve => external({type:"pair",code:"a".repeat(32)}, sender, resolve));
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

const elements = new Map(["folder", "connect", "status"].map(id => [id, {}]));
const navigation = [];
const sent = [];
const page = vm.createContext({ location:{hash:"#"+"c".repeat(32)}, history:{replaceState(...args){navigation.push(args);}},
  document:{getElementById:id=>elements.get(id)}, chrome:{runtime:{sendMessage:async (...args)=>{sent.push(args);return {started:true};}}} });
const script = readFileSync(new URL("../fast_tabber/setup-page.js",import.meta.url),"utf8")
  .replace("__CONFIG__",JSON.stringify({extensionId:"test",folder:"C:/runtime/extension"}));
vm.runInContext(script,page);
assert.equal(navigation[0][2],"/setup","Remove the expiring capability from the visible URL");
await elements.get("connect").onclick();
assert.equal(sent[0][0],"test");
assert.equal(sent[0][1].code,"c".repeat(32));
assert.match(elements.get("status").textContent,/Return to your agent/);
page.chrome.runtime.sendMessage=async()=>{throw new Error("Extension not loaded");};
await elements.get("connect").onclick();
assert.equal(elements.get("connect").disabled,false);
assert.match(elements.get("status").textContent,/First installation/);
console.log("One-click setup link, fragment removal, extension messaging and failure guidance checks passed");
