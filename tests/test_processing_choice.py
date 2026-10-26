import json
import pytest
from starlette.testclient import TestClient
from fast_tabber.server import create_app

TOKEN = "a" * 64
HEADERS = {"authorization": f"Bearer {TOKEN}", "accept": "application/json, text/event-stream",
           "mcp-protocol-version": "2025-11-25"}


@pytest.mark.parametrize("tool,args", [
    ("catalogue_tabs", {"action": "start", "instruction": "As few groups as possible"}),
    ("organize_by_text", {"instruction": "As few groups as possible", "visual": True}),
    ("index_tabs", {"visual": True, "tab_ids": [1]}),
])
def test_mode_choice_precedes_any_browser_or_model_work(tmp_path, tool, args):
    app = create_app(TOKEN, "chrome-extension://test", tmp_path / "browser.sqlite3")
    async def forbidden(*args, **kwargs):
        raise AssertionError("No browser calls before mode selection")
    app.state.bridge.call = forbidden
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        result = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}).json()["result"]
    assert not result.get("isError"), result
    assert json.loads(result["content"][0]["text"])["requiresChoice"] is True
    assert app.state.store.latest_undo() is None


def test_native_group_validation_does_not_poison_recovery_history(tmp_path):
    app = create_app(TOKEN, "chrome-extension://test", tmp_path / "browser.sqlite3")
    async def forbidden(*args, **kwargs):
        raise AssertionError("Invalid native group request must not reach Chrome")
    app.state.bridge.call = forbidden
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        result = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "organize_tabs", "arguments": {"action": "create"}}}).json()["result"]
    assert result["isError"] is True
    assert app.state.store.latest_undo() is None
