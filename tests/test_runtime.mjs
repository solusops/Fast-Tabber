import assert from "node:assert/strict";
import { compareVersions } from "../runtime.js";

for (const [older, newer] of [["0.7.0-preview.1","0.7.1-preview.1"], ["0.7.2-preview.2","0.7.2-preview.10"],
  ["0.7.2-preview.10","0.7.2"], ["0.9.0","0.10.0"], ["0.7.1","1.0.0"]]) {
  assert.ok(compareVersions(older,newer)<0);
  assert.ok(compareVersions(newer,older)>0);
}
assert.equal(compareVersions("0.7.2-preview.1","0.7.2-preview.1"),0);
assert.throws(()=>compareVersions("corrupt","0.7.2"),/Invalid/);
console.log("Runtime downgrade and numeric preview version ordering checks passed");
