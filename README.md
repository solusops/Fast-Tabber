# Fast-Tabber

Local Chrome tab management through MCP, Codex, and OpenCode + Gemma.

**V1 and V2 complete.**

## Setup (Windows)

```powershell
uv venv --python 3.13
uv pip install --python .venv\Scripts\python.exe -r requirements.lock.txt
ollama pull gemma4:e2b
ollama pull embeddinggemma-2:740m
powershell -NoProfile -ExecutionPolicy Bypass -File .\tabber.ps1 setup
powershell -NoProfile -ExecutionPolicy Bypass -File .\tabber.ps1 check
powershell -NoProfile -ExecutionPolicy Bypass -File .\tabber.ps1 serve
```

Install OpenCode 1.19.4 and Codex. In your existing Chrome, open `chrome://extensions`, enable Developer mode, and load this repo's `extension` directory unpacked. The extension has no interface. Keep the service running.

Launch either agent in another terminal:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tabber.ps1 local
powershell -NoProfile -ExecutionPolicy Bypass -File .\tabber.ps1 codex
```

The portable Skill is `.agents/skills/fast-tabber/SKILL.md`; both agents use the same loopback MCP endpoint. Existing global agent settings are not overwritten. Pairing secrets and runtime data are ignored by Git.

Tab lists expose titles and site origins only; URL paths, queries, and fragments are withheld. Saved collection URLs stay in the local SQLite database. V2 supports native groups, batch/sequential moves, named collections, exact-URL duplicate removal, and undo.

## Checks and privacy

Run `.venv\Scripts\python.exe -m pytest -q`, `node --check extension/background.js`, and `.venv\Scripts\python.exe -m fast_tabber.probe`. For the live disposable-tab test, set `$env:FAST_TABBER_LIVE_CHROME="1"` and run `.venv\Scripts\python.exe -m pytest -q tests\test_live_chrome_v2.py`.

Gemma performs local reasoning/vision; EmbeddingGemma provides local embeddings. No substitutes are used on failure. Downloaded model digests are recorded after successful preflight. Local inference and Chrome connection validation are separate gates; neither implies the other passed.

For Ollama's local-only mode, merge `"disable_ollama_cloud": true` into `%USERPROFILE%\.ollama\server.json` and restart Ollama after downloads finish. Offline inference was verified with external connections blocked for Ollama and OpenCode. Codex sends necessary tool outputs to its cloud service; never treat Codex mode as fully local.

To verify inference with external connections blocked, run `powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\offline.ps1` in Administrator PowerShell. It temporarily blocks only Ollama/OpenCode external connections, tests actual model/MCP execution, and removes its rules in `finally`. Normal operation does not require Administrator rights.

Chrome restoration cannot recover unsaved forms or every browser-internal state. Milestones are committed only after tests and actual-browser verification.
