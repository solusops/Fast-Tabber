# Fast-Tabber

Chrome tab management inside OpenCode or Codex: an authenticated local MCP server, portable Skill, and minimal Chrome extension. **V5 preview for user testing.** No separate chat application.

## Install in OpenCode (Windows)

Prerequisites: Chrome 120+, OpenCode, [uv](https://docs.astral.sh/uv/getting-started/installation/), and [Ollama](https://ollama.com/download).

Ollama must bind to loopback with `OLLAMA_CONTEXT_LENGTH=32768`; restart an existing Ollama service after changing these settings. The installer uses them when starting Ollama. Model verification during chat onboarding checks the actual OpenCode-compatible tool/result round trip. No model substitution is used.

Give the release ZIP to OpenCode's **Build** agent and ask it to extract it, read this README, and run:

```powershell
<extracted-folder>\install.cmd -Workspace <your-project-folder>
```

The installer creates the Python environment, installs pinned dependencies and starts the loopback MCP service. **It never downloads models.** It adds a project plugin without replacing existing agent configurations. A shared runtime registry at `~/.fast-tabber/runtime.json` lets other OpenCode installations reuse the same service, data, and Chrome pairing.

In your already working agent chat, ask `/tabs set up Fast-Tabber`. The agent explains model roles and asks which local Gemma tag to use, including installed options. Each missing model needs its own yes before a download starts; installed models are reused. The embedding model is `embeddinggemma-2:440m`. Download progress and actual inference verification are checked through MCP. After model selection/verification, restart OpenCode to use that model in Tabber. Initial onboarding uses your existing working chat model; it cannot bootstrap a local agent that has no usable inference model.

Require a successful installer exit, then restart OpenCode in that project and use `/tabs list my tabs`, select **Tabber**, or run `opencode --agent tabber`. Selecting the Skill alone does not install MCP or switch agents. Prompt normally: “Organize my browser”, “Find tabs about robotics”, “Save these tabs”, or “Revert last change”. The Tabber agent allows MCP tools and questions; shell, file editing, and other tools are denied. Use Build for installation or upgrades.

## Chrome setup

First installation only: open `chrome://extensions`, enable Developer mode, and load the runtime's `extension` folder unpacked. Approve Chrome's permissions. The installer prints the exact path. Normal Chrome installations cannot silently install or approve extensions.

Detection uses an authenticated extension connection, not a process-name guess. Onboarding checks it live. If disconnected, the agent shows the exact folder and browser setup steps, waits for your chat reply, then checks again. A reply alone never marks pairing successful. Another agent installation on the same machine/user can reuse it; a different Chrome profile needs its own extension approval. Background alarms reconnect after service restarts.

## Scope and local data

For natural-language organization, OpenCode asks **Simple** or **Rigorous** before processing. Simple uses rendered text and cache with no screenshots; insufficient evidence remains Uncategorized. Rigorous adds screenshots for unresolved pages and warns that Chrome may activate. Unselected requests return a choice instead of starting a job; direct screenshot organization sweeps are disabled.

Cataloguing supports up to 2,000 tabs. Sleeping pages without cached text are loaded individually in the background, read and returned to sleep without changing focus. Text is bounded to 12,000 characters per page and chunked locally. Screenshot capture masks fields/forms/frames, restores Chrome window geometry/state and detaches. Discard/restoration failures stop the job. This preview does not claim fully headless capture.

Recovery collections and progress checkpoints use batches of 50; cached index artifacts remain durable between checkpoints. Interrupted jobs can resume. The agent applies the saved classification after completion, without recapturing. Optional archive-and-close requires explicit confirmation, saves batches before closure and journals verified close operations. Unsaved forms/session state cannot be restored by reopening URLs. Sequential work is paused.

`.state/browser.sqlite3` stores local URL mappings, chunks, masked images, Gemma descriptions, and direct text/image embeddings. Thinking remains enabled; the server processes small validated batches. Models and browser data are excluded from the ZIP and Git.

Embedding images currently use Ollama's runtime preprocessing default; no visual token budget is explicitly requested or verified. Native screenshots cover a top-of-page rectangle capped at 1280×900 before scaling the longest side to 1024 pixels. This pixel limit is separate from the model's visual token budget. Switching embedding model digests requires regenerating vectors; cached text and screenshots remain reusable.

“Revert last change” uses `undo_last`; “revert all” uses `undo_last(all_changes=true)`. Journal snapshots precede mutations. Undo requires compatible running-browser windows; restarts or external changes may need reconciliation. Recovery stops on uncertainty; unsaved forms and arbitrary browser-internal state cannot be recovered.

“Delete local cache” previews counts and requires confirmation. Cache removal preserves saved collections and recovery history; deleting **all** local browser data also removes those. Pairing and model downloads are preserved.

Browser operations, screenshots, indexing, and model processing stay local. MCP results are bounded. Codex or a cloud-configured agent may process returned snippets remotely. The supplied local launcher uses only Ollama:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <runtime>\tabber.ps1 local
powershell -NoProfile -ExecutionPolicy Bypass -File <runtime>\tabber.ps1 codex
```

Fresh package installation and broader acceptance testing are left to the user for this preview. Benchmarking is deferred.

Interface references: [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools), [Agent Skills](https://agentskills.io/specification), [OpenCode plugins](https://opencode.ai/docs/plugins/). The portable Skill supplies workflow instructions; MCP exposes executable tools. The OpenCode adapter registers both without embedding the entire Skill in every system prompt.
