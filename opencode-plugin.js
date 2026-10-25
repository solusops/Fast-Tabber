import { readFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";
import { existsSync } from "node:fs";

const source = dirname(fileURLToPath(import.meta.url));

export const FastTabberPlugin = async ({ directory }) => {
  const registry = join(homedir(), ".fast-tabber", "runtime.json");
  let root = existsSync(registry) ? JSON.parse(await readFile(registry, "utf8")).root : source;
  const sourceVersion = JSON.parse(await readFile(join(source, "package.json"), "utf8")).version;
  const installedVersion = existsSync(join(root, "package.json")) ? JSON.parse(await readFile(join(root, "package.json"), "utf8")).version : null;
  const install = !existsSync(registry) || !existsSync(join(root, ".state", "connection.json")) || sourceVersion !== installedVersion;
  await new Promise((resolve, reject) => {
    const args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", join(install ? source : root, "install.ps1"),
      ...(install ? ["-Workspace", directory] : ["-StartOnly"])];
    const child = spawn("powershell.exe", args,
      { cwd: install ? source : root, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    let detail = "";
    child.stdout.on("data", data => { detail = (detail + data).slice(-4000); });
    child.stderr.on("data", data => { detail = (detail + data).slice(-4000); });
    child.on("error", reject);
    child.on("exit", code => code === 0 ? resolve() : reject(new Error(`Fast-Tabber startup failed: ${detail}`)));
  });
  root = JSON.parse(await readFile(registry, "utf8")).root;
  const connection = JSON.parse(await readFile(join(root, ".state", "connection.json"), "utf8"));
  const skill = await readFile(join(root, ".agents", "skills", "fast-tabber", "SKILL.md"), "utf8");
  return {
    config: async config => {
      config.mcp ??= {};
      if (config.mcp["fast-tabber"]?.url && config.mcp["fast-tabber"].url !== "http://127.0.0.1:8765/mcp")
        throw new Error("An unrelated fast-tabber MCP configuration already exists; resolve it before installation");
      config.mcp["fast-tabber"] = { type: "remote", url: "http://127.0.0.1:8765/mcp", oauth: false,
        headers: { Authorization: `Bearer ${connection.token}` }, timeout: 600000 };
      config.skills ??= {};
      config.skills.paths = [...new Set([...(config.skills.paths || []), join(root, ".agents", "skills")])];
      config.provider ??= {};
      config.provider.ollama ??= { npm: "@ai-sdk/openai-compatible", name: "Ollama (local)",
        options: { baseURL: "http://127.0.0.1:11434/v1" }, models: { "gemma4:e2b": {
          name: "Gemma 4 E2B", limit: { context: 16384, output: 4096 }, tool_call: true } } };
      config.model ??= "ollama/gemma4:e2b";
      config.small_model ??= "ollama/gemma4:e2b";
      config.agent ??= {};
      config.agent.tabber = { description: "Manage Chrome through the Fast-Tabber MCP toolkit", mode: "primary",
        model: "ollama/gemma4:e2b", prompt: skill,
        permission: { "*": "deny", "fast-tabber_*": "allow", question: "allow", skill: { "*": "deny", "fast-tabber": "allow" } } };
      config.default_agent ??= "tabber";
    },
  };
};
