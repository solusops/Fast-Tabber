import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from fast_tabber import preflight
from fast_tabber.onboarding import Onboarding
from fast_tabber.server import create_app


@pytest.mark.asyncio
async def test_e4b_selection_and_each_download_require_explicit_approval(tmp_path, monkeypatch):
    setup = Onboarding(tmp_path, SimpleNamespace(peer=None), lambda: False, asyncio.Lock())
    selected = await setup.call("select_model", "gemma4:e4b", False)
    assert selected["selectedModel"] == "gemma4:e4b" and not selected["downloaded"]
    assert preflight.model_name(tmp_path / "settings.json") == "gemma4:e4b"
    assert setup.task is None
    for model in ("gemma4:e4b", preflight.EMBEDDING):
        prompt = await setup.call("download", model, False)
        assert prompt["requiresConfirmation"] and prompt["downloadStarted"] is False
        assert setup.task is None
    with pytest.raises(ToolError, match="Select"):
        await setup.call("download", "gemma4:e2b", True)
    requested = []
    original = httpx.AsyncClient
    def handler(request):
        requested.append((request.method, request.url.path))
        if request.url.path == "/api/pull":
            assert json.loads(request.content)["name"] == "gemma4:e4b"
            return httpx.Response(200, text='{"status":"success"}\n')
        return httpx.Response(200, json={"models": [{"name": "gemma4:e4b"}]})
    monkeypatch.setattr("fast_tabber.onboarding.httpx.AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    started = await setup.call("download", "gemma4:e4b", True)
    assert started["started"]
    await setup.task
    assert setup.progress["state"] == "complete"
    assert requested == [("POST", "/api/pull"), ("GET", "/api/tags")]
    assert (await setup.call("download", preflight.EMBEDDING, False))["requiresConfirmation"]


def test_chat_reply_requires_a_fresh_live_extension_check(tmp_path):
    token = "a" * 64
    app = create_app(token, "chrome-extension://test", tmp_path / "browser.sqlite3")
    connected = False
    calls = []
    async def probe(operation):
        calls.append(operation)
        if not connected:
            raise RuntimeError("Chrome extension is disconnected")
        return {"extensionVersion": "test", "tabCount": 12}
    app.state.bridge.call = probe
    def check(client):
        result = client.post("/mcp", headers={"authorization": f"Bearer {token}", "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-11-25"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "onboarding", "arguments": {"action": "browser"}}}).json()["result"]
        return result["structuredContent"]
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        missing = check(client)
        assert missing["connected"] is False and missing["awaitingUserReply"]
        assert "extensionFolder" in missing
        connected = True
        actual = check(client)
        assert actual["connected"] and actual["verified"] and actual["tabCount"] == 12
    assert calls == ["probe", "probe"]
