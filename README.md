# Fast-Tabber

An agent-native Chrome toolkit. Your existing agent decides; Fast-Tabber searches cached content and executes native browser operations.

## Install (Windows)

Requires Node.js 22+ and Chrome. The installer provisions Python through uv if needed. No Ollama or model downloads.

```powershell
npx -y fast-tabber-toolkit@latest setup
```

Choose Codex, OpenCode or Claude Code when prompted. Existing agent/model configuration is preserved. Restart the agent after installation. Load the displayed extension folder at `chrome://extensions` using **Load unpacked**, then follow the pairing link in agent chat. Enable optional bookmark permission in the extension only when needed.

For generic MCP clients, use command `npx.cmd` with arguments `-y fast-tabber-toolkit@latest mcp`, and install the Skill from `.agents/skills/fast-tabber` in this repository. Codex plugin installation is also available through `codex plugin marketplace add solusops/Fast-Tabber` then `codex plugin add fast-tabber@fast-tabber`.

## Use

Ask your agent to organize tabs by topic, find cached page text, export a group, save tabs as bookmarks, open bookmark folders, remove duplicates or undo the last change. It chooses categories and exact selection assignments. There are no Fast/Precise modes, clustering, embeddings or backend LLMs.

The catalogue returns 20 resources by default, maximum 100 per page. Local selection handles cover up to 5,000 resources without sending thousands of IDs to the agent. Page-text indexing captures a bounded first-page representation once, stores it in SQLite with FTS5/BM25 search, and reuses it across operations and reopened URLs. Explicit refresh recaptures content. No crawling linked pages. Screenshots, links and accessibility information are available for selected pages.

Grouping keeps tabs open. Native bulk operations verify Chrome state and retain undo. Closing/deleting requires confirmation. Undo cannot restore unsaved forms or all browser-internal state. Bookmark operations require explicit bookmark scope. Initial text extraction can take time; job status includes cached/completed/failed/pending counts and recoverable per-page failures.

Bulk data stays local. Requested evidence may be sent to the connected agent's model provider. See [PRIVACY.md](PRIVACY.md). The extension uses authenticated loopback communication and requires Chrome's one-time manual installation step.

## Development

```powershell
uv venv --python 3.13
uv pip install -r requirements.lock.txt pytest==9.1.1 pytest-asyncio==1.4.0
.venv\Scripts\python.exe -m pytest -q
npm test
```

Run optional real-Chrome tests only with a paired browser: set `FAST_TABBER_LIVE_CHROME=1`. They use disposable tabs/bookmarks and verify native mutations and undo. Version 1.0 removes the preview grouping/model APIs; cached content, saved collections and undo records are preserved.
