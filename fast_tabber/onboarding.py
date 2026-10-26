"""First-run disclosure and approved embedding setup. No generative-model dependency."""
import asyncio
import json
import httpx
from mcp.server.mcpserver.exceptions import ToolError
from fast_tabber import preflight

DISCLOSURE = ("Bulk indexing, clustering, embeddings and saved data stay local. Your connected agent may send requested titles, "
    "URLs, text, accessibility data and screenshots to its model provider. A local agent/model can keep inference on-device, "
    "subject to its other integrations. Fast-Tabber does not request cookies or form values.")


class Onboarding:
    def __init__(self, state, bridge, busy, inference_lock):
        self.state, self.bridge, self.busy, self.inference_lock = state, bridge, busy, inference_lock
        self.task, self.progress = None, {"state": "idle"}

    async def status(self):
        error, installed = None, {}
        try:
            async with httpx.AsyncClient(base_url=preflight.OLLAMA, timeout=5, trust_env=False) as client:
                reply = await client.get("/api/tags")
                reply.raise_for_status()
                installed = {item["name"]: item for item in reply.json()["models"]}
        except (httpx.HTTPError, ValueError, KeyError) as failure:
            error = f"Ollama unavailable on loopback ({type(failure).__name__}); install/start Ollama at 127.0.0.1:11434. Native browser operations remain available."
        path = self.state / "models.json"
        report = json.loads(path.read_text()).get("models", {}) if path.exists() else {}
        settings = self.state / "settings.json"
        privacy = json.loads(settings.read_text()).get("privacy_acknowledged", False) if settings.exists() else False
        name = preflight.EMBEDDING
        return {"models": [{"name": name, "installed": name in installed, "sizeBytes": installed.get(name, {}).get("size"),
                "verified": name in installed and report.get(name) == installed[name].get("digest")}],
                "runtimeError": error, "progress": self.progress, "privacyAcknowledged": privacy, "privacyDisclosure": DISCLOSURE,
                "browserConnected": self.bridge.peer is not None, "downloadConfirmationRequired": True,
                "nextStep": "Disclose privacy once; approve any missing embedding download, verify embeddings and check Chrome. Use the connected agent's existing model for reasoning."}

    async def run(self, action):
        try:
            async with self.inference_lock:
                async with httpx.AsyncClient(base_url=preflight.OLLAMA, timeout=3600, trust_env=False) as client:
                    if action == "download":
                        async with client.stream("POST", "/api/pull", json={"name": preflight.EMBEDDING, "stream": True}) as response:
                            response.raise_for_status()
                            async for line in response.aiter_lines():
                                if line:
                                    data = json.loads(line)
                                    if data.get("error"):
                                        raise RuntimeError(data["error"])
                                    self.progress.update(status=data.get("status"), completed=data.get("completed"), total=data.get("total"))
                        if self.progress.get("status") != "success":
                            raise RuntimeError("Embedding download did not complete")
                        tags = await client.get("/api/tags")
                        tags.raise_for_status()
                        preflight.require_models(tags.json())
                    else:
                        preflight.save_report(await preflight.check_models(client), self.state)
            self.progress["state"] = "complete"
        except Exception as error:
            self.progress.update(state="failed", error=str(error)[:500])

    async def call(self, action, model, confirm):
        if action == "status":
            return await self.status()
        if self.task and not self.task.done() or self.busy():
            raise ToolError("Finish the current operation first")
        if action == "acknowledge_privacy":
            if not confirm:
                return {"requiresDisclosure": True, "privacyDisclosure": DISCLOSURE}
            path = self.state / "settings.json"
            settings = json.loads(path.read_text()) if path.exists() else {}
            settings["privacy_acknowledged"] = True
            path.write_text(json.dumps(settings))
            return {"acknowledged": True}
        if action == "download":
            if model != preflight.EMBEDDING:
                raise ToolError(f"This server requires only {preflight.EMBEDDING}; select your chat model in the connected agent")
            if not confirm:
                return {"requiresConfirmation": True, "model": model, "downloadStarted": False}
        elif action != "verify":
            raise ToolError("Choose status, download, verify, browser or acknowledge_privacy")
        self.progress = {"state": "running", "action": action, "model": preflight.EMBEDDING}
        self.task = asyncio.create_task(self.run(action))
        return {"started": True, "progress": self.progress, "nextStep": "Poll status until complete or failed; do not merely announce polling."}
