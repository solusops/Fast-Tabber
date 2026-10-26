#!/usr/bin/env node
import { spawn } from "node:child_process";
import { prepareRuntime } from "./runtime.js";

const command = process.argv[2] || "help";
if (!["setup", "mcp", "help", "--help"].includes(command)) {
  console.error("Use: fast-tabber setup | mcp"); process.exitCode = 1;
} else if (command === "help" || command === "--help") {
  console.log("fast-tabber setup: install/start the shared Windows runtime (requires uv).\nfast-tabber mcp: start the runtime and expose standard MCP over stdio.\nNo model downloads. Finish privacy, browser pairing and embedding setup in agent chat.");
} else {
  try {
    const { root, python } = await prepareRuntime();
    if (command === "setup") console.log(`Fast-Tabber runtime ready at ${root}. Add fast-tabber mcp to your agent, load the fast-tabber Skill and run onboarding. No models downloaded.`);
    else {
      const child = spawn(python, ["-m", "fast_tabber.client"], { cwd: root, windowsHide: true, stdio: "inherit" });
      child.on("error", error => { console.error(error.message); process.exitCode = 1; });
      child.on("exit", code => { process.exitCode = code ?? 1; });
      for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => child.kill());
    }
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
