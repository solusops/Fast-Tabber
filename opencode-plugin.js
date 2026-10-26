import { join } from "node:path";
import { readFile } from "node:fs/promises";
import { prepareRuntime } from "./runtime.js";

export const FastTabberPlugin = async ({ directory }) => {
  const { root, connection } = await prepareRuntime(directory);
  const workflow = (await readFile(join(root, ".agents", "skills", "fast-tabber", "SKILL.md"), "utf8")).replace(/^---[\s\S]*?---\s*/, "");
  return {
    config: async config => {
      config.mcp ??= {};
      if (config.mcp["fast-tabber"]?.url && config.mcp["fast-tabber"].url !== "http://127.0.0.1:8765/mcp")
        throw new Error("An unrelated fast-tabber MCP configuration already exists; resolve it before installation");
      config.mcp["fast-tabber"] = { type: "remote", url: "http://127.0.0.1:8765/mcp", oauth: false,
        headers: { Authorization: `Bearer ${connection.token}` }, timeout: 600000 };
      config.skills ??= {};
      config.skills.paths = [...new Set([...(config.skills.paths || []), join(root, ".agents", "skills")])];
      config.agent ??= {};
      config.agent.tabber = { description: "Manage Chrome through the Fast-Tabber MCP toolkit", mode: "primary",
        steps: 60,
        prompt: "Use Fast-Tabber MCP and the workflow below. You decide categories and exact assignments. Keep responses concise, announce long indexing work and continue polling until done. Ask only when intent is unclear or deletion needs confirmation. Never print private reasoning.\n\n" + workflow,
        permission: { "*": "deny", "fast-tabber_*": "allow", question: "allow", skill: { "*": "deny", "fast-tabber": "allow" } } };
      config.command ??= {};
      config.command["fast-tabber"] ??= { description: "Manage Chrome with Fast-Tabber", agent: "tabber",
        template: "Use Fast-Tabber MCP tools to fulfill this browser request, then summarize the verified result: $ARGUMENTS" };
    },
  };
};
