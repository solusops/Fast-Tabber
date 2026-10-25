# Fast-Tabber

Chrome tab management inside OpenCode or Codex: an authenticated local MCP server, portable Skill, and minimal Chrome extension. **V5 preview for user testing.** No separate chat application.

## Install in OpenCode (Windows)

Prerequisites: Chrome 120+, OpenCode, [uv](https://docs.astral.sh/uv/getting-started/installation/), and [Ollama](https://ollama.com/download).

Give the release ZIP to OpenCode's **Build** agent and ask it to extract it, read this README, and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <extracted-folder>\install.ps1 -Workspace <your-project-folder>
```

The installer creates the Python environment, installs pinned dependencies, downloads `gemma4:e2b` and `embeddinggemma-2:740m` if missing, verifies text/tool/vision/image-embedding execution, and starts the loopback MCP service. It adds a project plugin without replacing existing agent configurations. A shared runtime registry at `~/.fast-tabber/runtime.json` lets other OpenCode installations reuse the same service, data, and Chrome pairing.

Restart OpenCode in that project and select **Tabber**, or run `opencode --agent tabber`. Prompt there normally: “Organize my browser”, “Find tabs about robotics”, “Save these tabs”, or “Revert last change”. The Tabber agent allows MCP tools and questions; shell, file editing, and other tools are denied. Use Build for installation or upgrades.

## Chrome setup

First installation only: open `chrome://extensions`, enable Developer mode, and load the runtime's `extension` folder unpacked. Approve Chrome's permissions. The installer prints the exact path. Normal Chrome installations cannot silently install or approve extensions.

Detection uses an authenticated extension connection, not a process-name guess. If disconnected, MCP tells the agent the exact folder and required user step. Another agent installation on the same machine/user can reuse it; a different Chrome profile needs its own extension approval. Background alarms reconnect after service restarts.

## Scope and local data

Whole-browser screenshot cataloguing runs as a persistent background job. It automatically includes sleeping tabs, processes one page at a time, restores originally discarded tabs to sleep, and checkpoints completed pages. The agent checks job progress and then groups from the saved index without recapturing. Interrupted jobs can resume. Native DevTools capture masks fields/forms/frames and detaches after capture. It may briefly activate a tab; this preview does not claim fully headless capture. Sequential work is paused.

`.state/browser.sqlite3` stores local URL mappings, chunks, masked images, Gemma descriptions, and direct text/image embeddings. Thinking remains enabled; the server processes small validated batches. Models and browser data are excluded from the ZIP and Git.

“Revert last change” uses `undo_last`; “revert all” uses `undo_last(all_changes=true)`. Journal snapshots precede mutations. Undo requires compatible running-browser windows; restarts or external changes may need reconciliation. Recovery stops on uncertainty; unsaved forms and arbitrary browser-internal state cannot be recovered.

“Delete local cache” previews counts and requires confirmation. Cache removal preserves saved collections and recovery history; deleting **all** local browser data also removes those. Pairing and model downloads are preserved.

Browser operations, screenshots, indexing, and model processing stay local. MCP results are bounded. Codex or a cloud-configured agent may process returned snippets remotely. The supplied local launcher uses only Ollama:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <runtime>\tabber.ps1 local
powershell -NoProfile -ExecutionPolicy Bypass -File <runtime>\tabber.ps1 codex
```

Fresh package installation and broader acceptance testing are left to the user for this preview. Benchmarking is deferred.
