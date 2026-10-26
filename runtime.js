import { readFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";
import { existsSync } from "node:fs";

const source = dirname(fileURLToPath(import.meta.url));

export function compareVersions(left, right) {
  const parse = value => {
    const match = /^(\d+)\.(\d+)\.(\d+)(?:-(.+))?$/.exec(value);
    if (!match) throw new Error(`Invalid Fast-Tabber version: ${value}`);
    return { core: match.slice(1, 4).map(Number), pre: match[4] };
  };
  const a = parse(left), b = parse(right);
  for (let i = 0; i < 3; i++) if (a.core[i] !== b.core[i]) return a.core[i] - b.core[i];
  if (a.pre === b.pre) return 0;
  if (!a.pre) return 1;
  if (!b.pre) return -1;
  return a.pre.localeCompare(b.pre, "en", { numeric: true });
}

export async function prepareRuntime(directory = process.cwd()) {
  if (process.platform !== "win32") throw new Error("Fast-Tabber runtime installation currently supports Windows only");
  const registry = join(homedir(), ".fast-tabber", "runtime.json");
  let root = existsSync(registry) ? JSON.parse(await readFile(registry, "utf8")).root : source;
  const version = JSON.parse(await readFile(join(source, "package.json"), "utf8")).version;
  const installed = existsSync(join(root, "package.json")) ? JSON.parse(await readFile(join(root, "package.json"), "utf8")).version : null;
  const install = !existsSync(registry) || !existsSync(join(root, ".state", "connection.json")) || !installed || compareVersions(version, installed) > 0;
  await new Promise((resolve, reject) => {
    const child = spawn("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
      join(install ? source : root, "install.ps1"), "-NoOpenCode", ...(install ? ["-Workspace", directory] : ["-StartOnly"])],
      { cwd: install ? source : root, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    let detail = "";
    for (const stream of [child.stdout, child.stderr]) stream.on("data", data => { detail = (detail + data).slice(-4000); });
    child.on("error", reject);
    child.on("exit", code => code === 0 ? resolve() : reject(new Error(`Fast-Tabber startup failed: ${detail}`)));
  });
  root = JSON.parse(await readFile(registry, "utf8")).root;
  return { root, python: join(root, ".venv", "Scripts", "python.exe"),
    connection: JSON.parse(await readFile(join(root, ".state", "connection.json"), "utf8")) };
}
