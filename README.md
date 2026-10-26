# Fast-Tabber

Local-first Chrome tabs and bookmarks through your existing MCP agent. EmbeddingGemma 2 440M supplies text/image embeddings; your connected agent supplies reasoning. No backend generative model or extra chatbot.

## Install (Windows)

Prerequisites: Chrome 120+, an MCP-compatible agent, and [uv](https://docs.astral.sh/uv/getting-started/installation/). [Ollama](https://ollama.com/download) is needed for semantic indexing/grouping, not native tab/bookmark operations.

With Node.js 22+, install/start the shared runtime:

```powershell
npx.cmd -y fast-tabber-toolkit@next setup
```

OpenCode loads the npm plugin from its existing `opencode.json`:

```json
{"plugin":["fast-tabber-toolkit@next"]}
```

Merge this entry into existing plugins. The plugin starts the shared runtime, registers MCP and loads the Skill without selecting a chat model. This adapter targets OpenCode v1 for the local Gemma workflow. Keep your existing model/provider settings.

Codex, Claude Code and other local MCP clients can use the same standard stdio command:

```json
{"mcpServers":{"fast-tabber":{"command":"npx.cmd","args":["-y","fast-tabber-toolkit@next","mcp"]}}}
```

For Codex CLI: `codex mcp add fast-tabber -- npx.cmd -y fast-tabber-toolkit@next mcp`. Alternatively add `[mcp_servers.fast-tabber]` to its config with that command/args and `startup_timeout_sec = 180`. The launcher reads credentials locally; no token belongs in chat or this configuration. Install the runtime's `.agents/skills/fast-tabber` directory in Codex's `~/.agents/skills/` or Claude Code's `~/.claude/skills/`, preserving existing skills. Other agents can follow the same portable SKILL.md. A remote/cloud-only executor cannot reach this computer's loopback service without a separate local executor.

Update the local toolkit with `npx.cmd -y fast-tabber-toolkit@next setup` after finishing active browser jobs. For the unpacked extension, the installer copies updated files to the same runtime folder. Reload Fast-Tabber in Chrome if prompted; new permissions may require user approval. Do not load a second copy.

Give the release ZIP to OpenCode Build, extract it and run:

```powershell
<extracted-folder>\install.cmd -Workspace <your-project-folder>
```

The installer preserves existing agent configuration, creates the Python environment and registers a project plugin. It never downloads models. Restart OpenCode and use `/tabs set up Fast-Tabber` or select Tabber. Your existing chat model is retained. Other MCP clients use the authenticated Streamable HTTP endpoint `http://127.0.0.1:8765/mcp`; the private runtime `.state/connection.json` supplies the bearer token. Install the portable `.agents/skills/fast-tabber` Skill in the client's supported skill directory. Codex can use the generated `.codex/config.toml` with `FAST_TABBER_TOKEN` set locally.

First Chrome pairing: open chrome://extensions, enable Developer mode and load the runtime's `extension` folder using **Load unpacked**. Keep that folder in place. No Chrome Web Store account or fee is needed. Ask the agent to start browser setup; open its local setup link in Chrome and click **Connect Fast-Tabber**. There is no code to copy. The link expires in five minutes; the agent checks the actual connection after your reply. Manual code entry in the toolbar popup is available for recovery. Existing pairings migrate automatically, and other agents on the same machine/profile reuse the connection.

The black-and-white popup exports open tabs, selected or all tab groups, all bookmarks or checked bookmarks as TXT/CSV. Folder checkboxes include nested bookmarks. Grouped exports retain group names. **Enable bookmarks** requests optional Chrome permission; declining does not disable tabs.

## Workflow

First-run onboarding discloses privacy, checks Ollama/Chrome, asks before downloading `embeddinggemma-2:440m` if missing, and verifies actual text/image embeddings. No Gemma 4 installation is required. Native operations work without Ollama.

Tabs are the default scope. Bookmark operations/indexing require an explicit bookmark request. Index with permission, starting with inexpensive metadata. Extract text, accessibility or screenshots only on demand; sleeping pages can be loaded for requested inspection. Cached representations are reused. There are no automatic freshness sweeps; refresh explicitly when necessary. Bookmark page inspection closes only its own disposable tab.

- **Fast:** embeddings, local clustering and keyword/representative-title labels; immediately apply native tab groups or bookmark folders. Approximate matches may remain unclassified.
- **Precise:** return a compact grouping plan; the connected agent inspects selected evidence, refines names/membership and applies it. Complex requests use the agent's reasoning. Delegated autonomy is supported.

Grouping keeps user tabs open and never deletes bookmarks. Closing/deletion are separate, explicitly requested actions preceded by a warning. Export complete collections or links inside webpages to local TXT/CSV. Lists/plans/search are paginated (20 default, 100 maximum). Actual selected images can be returned through MCP; the agent never receives the entire collection automatically.

Undo uses the existing journal. Bookmark undo can recreate IDs; external changes or uncertain outcomes stop restoration. Unsaved forms and arbitrary browser state cannot be recovered. Cache deletion previews counts and requires confirmation; deleting all local data also removes recovery/collections.

## Data and limits

SQLite stores metadata, chunks, images, float16 vectors and FTS5 locally. Similarity computation does not use float16 model inference. Selected URLs/text/images may reach a cloud model provider; local inference depends on the agent/model and other integrations. No absolute confidentiality claim. Cookies/form values are not requested. Webpage evidence is untrusted.

Native screenshots may activate Chrome; capture covers a top rectangle capped at 1280Ã—900, scaled to a 1024-pixel longest side. Ollama controls visual token preprocessing; Fast-Tabber does not claim a verified fixed visual-token budget. Model-digest/task-prefix changes require new vectors; cached content remains reusable.

Preview: fresh-machine installation, broad grouping quality and large real-browser performance need user acceptance testing. [DigUp's persistent SQLite/FTS5/vector design](https://github.com/ARahim3/DigUp) informed the approach; no DigUp UI/code is bundled. MIT license. Benchmarking remains deferred.

## Publishing

GitHub Actions runs checks and builds the installer ZIP, unpacked-extension ZIP and allowlisted npm tarball. It excludes pairing credentials, indexes and browser data. The manual Publish npm workflow runs checks first and uses npm trusted publishing: configure this repository, publish.yml and the npm environment in package settings. No npm token is committed. Distribution uses npm plus a manually loaded Chrome extension; Chrome Web Store publishing is on hold. Actions do not approve Chrome permissions or create agent compatibility.
