#!/usr/bin/env node
import { spawn } from "node:child_process";
import { createInterface } from "node:readline/promises";
import { mkdir, cp } from "node:fs/promises";
import { join } from "node:path";
import { homedir } from "node:os";
import { prepareRuntime } from "./runtime.js";

const command = process.argv[2] || "help";
if (!["setup", "mcp", "help", "--help"].includes(command)) {
  console.error("Use: fast-tabber setup | mcp"); process.exitCode = 1;
} else if (command === "help" || command === "--help") {
  console.log("fast-tabber setup [--agent codex|opencode|claude-code|generic]: install the MCP toolkit and Skill.\nfast-tabber mcp: expose standard MCP over stdio. No models required.");
} else {
  try {
    const { root, python } = await prepareRuntime();
    if (command === "setup") {
      let agent = process.argv[process.argv.indexOf("--agent") + 1];
      if (!process.argv.includes("--agent")) {
        if (!process.stdin.isTTY) throw new Error("Choose --agent codex, opencode, claude-code or generic for noninteractive setup");
        const prompt = createInterface({ input: process.stdin, output: process.stdout });
        try { agent = (await prompt.question("Install for codex, opencode, claude-code or generic? ")).trim().toLowerCase(); }
        finally { prompt.close(); }
      }
      if (!["codex", "opencode", "claude-code", "generic"].includes(agent)) throw new Error("Unknown agent; choose codex, opencode, claude-code or generic");
      const run = (exe, args) => new Promise((resolve, reject) => {
        const child = spawn(exe, args, { stdio: "inherit", windowsHide: true });
        child.on("error", reject);
        child.on("exit", code => code === 0 ? resolve() : reject(new Error(`${exe} exited with ${code}`)));
      });
      if (agent === "codex") {
        await run("codex.exe", ["plugin", "marketplace", "add", root]);
        await run("codex.exe", ["plugin", "add", "fast-tabber@fast-tabber"]);
      } else if (agent === "opencode") {
        await run("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", join(root, "install.ps1"), "-Workspace", process.cwd()]);
      } else if (agent === "claude-code") {
        await run("claude.exe", ["mcp", "add", "--transport", "stdio", "--scope", "user", "fast-tabber", "--", "node", join(root, "cli.js"), "mcp"]);
        const skills = join(homedir(), ".claude", "skills");
        await mkdir(skills, { recursive: true });
        await cp(join(root, ".agents", "skills", "fast-tabber"), join(skills, "fast-tabber"), { recursive: true });
      }
      console.log(`Fast-Tabber ready at ${root}. Load ${join(root, "extension")} unpacked in Chrome. Restart your agent and ask Fast-Tabber to check the browser connection.`);
      if (agent === "generic") console.log(`MCP command: node; args: ${JSON.stringify([join(root, "cli.js"), "mcp"])}. Skill: ${join(root, ".agents", "skills", "fast-tabber")}`);
    }
    else {
      const child = spawn(python, ["-m", "fast_tabber.client"], { cwd: root, windowsHide: true, stdio: "inherit" });
      child.on("error", error => { console.error(error.message); process.exitCode = 1; });
      child.on("exit", code => { process.exitCode = code ?? 1; });
      for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => child.kill());
    }
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
