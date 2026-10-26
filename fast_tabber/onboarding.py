"""Explicit model setup; no downloads on import, installation, or status checks."""
import asyncio
import json
import re

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from fast_tabber import preflight


class Onboarding:
    def __init__(self, state, bridge, busy, inference_lock):
        self.state, self.bridge, self.busy, self.inference_lock = state, bridge, busy, inference_lock
        self.task = None
        self.progress = {"state": "idle"}

    async def status(self):
        settings = self.state / "settings.json"
        selected = preflight.model_name(settings) if settings.exists() else None
        try:
            async with httpx.AsyncClient(base_url=preflight.OLLAMA, timeout=5, trust_env=False) as client:
                response = await client.get("/api/tags")
                response.raise_for_status()
                models = response.json()["models"]
            installed = {item["name"]: item for item in models}
            runtime_error = None
        except (httpx.HTTPError, KeyError, ValueError) as error:
            installed, runtime_error = {}, f"Ollama unavailable on loopback: {type(error).__name__}. Install/start Ollama at 127.0.0.1:11434."
        report = self.state / "models.json"
        verified = json.loads(report.read_text()).get("models", {}) if report.exists() else {}
        required = ([selected] if selected else []) + [preflight.EMBEDDING]
        return {"selectedModel": selected, "runtimeError": runtime_error,
                "requiresModelSelection": selected is None,
                "models": [{"name": name, "purpose": "instruction following and screenshots" if name == selected else "text/image semantic index",
                            "installed": name in installed, "sizeBytes": installed.get(name, {}).get("size"),
                            "verified": bool(name in installed and verified.get(name) == installed[name].get("digest"))} for name in required],
                "availableGemmaModels": [name for name in installed if name.startswith("gemma4:")][:20],
                "downloadConfirmationRequired": True, "progress": self.progress,
                "browserConnected": self.bridge.peer is not None,
                "nextStep": "Select the user's model; explain and confirm each missing download separately, then verify models and check the browser connection."}

    async def run(self, action, model):
        try:
            async with self.inference_lock:
                async with httpx.AsyncClient(base_url=preflight.OLLAMA, timeout=3600, trust_env=False) as client:
                    if action == "download":
                        async with client.stream("POST", "/api/pull", json={"name": model, "stream": True}) as response:
                            response.raise_for_status()
                            async for line in response.aiter_lines():
                                if not line:
                                    continue
                                data = json.loads(line)
                                if data.get("error"):
                                    raise RuntimeError(data["error"])
                                self.progress.update(status=data.get("status"), completed=data.get("completed"), total=data.get("total"))
                        if self.progress.get("status") != "success":
                            raise RuntimeError("Ollama did not report a completed download")
                        tags = await client.get("/api/tags")
                        tags.raise_for_status()
                        if model not in {item["name"] for item in tags.json()["models"]}:
                            raise RuntimeError("Downloaded model is not present in Ollama")
                    else:
                        report = await preflight.check_models(client)
                        preflight.save_report(report, self.state)
            self.progress["state"] = "complete"
        except Exception as error:
            self.progress.update(state="failed", error=str(error)[:500])

    async def call(self, action, model, confirm):
        if action == "status":
            return await self.status()
        if self.task and not self.task.done() or self.busy():
            raise ToolError("Finish the active setup/browser operation before changing models")
        selected = preflight.model_name(self.state / "settings.json")
        if action == "select_model":
            settings = self.state / "settings.json"
            # Validate before persisting; never substitute a smaller model automatically.
            if not re.fullmatch(r"gemma4:[a-zA-Z0-9_.-]+", model) or "cloud" in model.lower():
                raise ToolError("Choose an explicit local gemma4 tag, such as the user's installed model")
            settings.write_text(json.dumps({"model": model}))
            return {"selectedModel": model, "downloaded": False, "agentRestartRequired": True,
                    "nextStep": "Check status; ask before each missing download. Restart OpenCode to use the selected chat model."}
        if not (self.state / "settings.json").exists():
            raise ToolError("Ask the user which local Gemma model to use and select it before downloading or verifying")
        if action == "download":
            if model not in {selected, preflight.EMBEDDING}:
                raise ToolError("Select the requested Gemma model first; downloads are limited to that model and the required embedding model")
            if not confirm:
                return {"requiresConfirmation": True, "model": model, "downloadStarted": False,
                        "message": "Explain the model's role and potentially multi-GB download, then wait for the user's yes for this exact model."}
        elif action != "verify":
            raise ToolError("Choose status, select_model, download, verify, or browser")
        self.progress = {"state": "running", "action": action, "model": model if action == "download" else selected}
        self.task = asyncio.create_task(self.run(action, model))
        return {"started": True, "progress": self.progress, "nextStep": "Poll onboarding status; do not assume success until complete."}
