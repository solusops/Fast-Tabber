import asyncio

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fast_tabber.server import Bridge, create_app

TOKEN = "a" * 64
ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def test_mcp_rejects_missing_authentication():
    with TestClient(create_app(TOKEN, ORIGIN)) as client:
        assert client.post("/mcp", json={}).status_code == 401


def test_authenticated_mcp_requests_are_rate_limited(tmp_path):
    with TestClient(create_app(TOKEN, ORIGIN, tmp_path / "browser.sqlite3")) as client:
        headers = {"authorization": f"Bearer {TOKEN}"}
        for _ in range(120):
            client.post("/mcp", headers=headers, json={})
        response = client.post("/mcp", headers=headers, json={})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"


def test_status_tool_advertises_safe_read_only_behavior():
    with TestClient(create_app(TOKEN, ORIGIN), base_url="http://127.0.0.1:8765") as client:
        response = client.post("/mcp", headers={
            "authorization": f"Bearer {TOKEN}",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-11-25",
        }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tool = response.json()["result"]["tools"][0]
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False


def test_tools_advertise_output_schemas_and_return_compatible_structured_results(tmp_path):
    app = create_app(TOKEN, ORIGIN, tmp_path / "browser.sqlite3")
    async def reply(*args, **kwargs):
        return {"total": 1, "offset": 0, "groups": [], "tabs": [{"id": 7, "title": "Notes"}]}
    app.state.bridge.call = reply
    headers = {"authorization": f"Bearer {TOKEN}", "accept": "application/json, text/event-stream",
               "mcp-protocol-version": "2025-11-25"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        tools = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
        assert all(tool["outputSchema"]["type"] == "object" for tool in tools)
        result = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "list_tabs", "arguments": {}}}).json()["result"]
        import json
        assert result["structuredContent"] == json.loads(result["content"][0]["text"])
        assert result["structuredContent"]["tabs"][0]["id"] == 7


def test_tab_tools_are_bounded_and_task_oriented():
    with TestClient(create_app(TOKEN, ORIGIN), base_url="http://127.0.0.1:8765") as client:
        response = client.post("/mcp", headers={
            "authorization": f"Bearer {TOKEN}", "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-11-25",
        }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
            "browser_status", "browser_setup", "list_tabs", "tab_action", "organize_tabs",
            "collection_action", "remove_duplicates", "undo_last", "index_tabs",
            "search_tabs", "organize_by_text", "local_data", "catalogue_tabs",
        }
        tool = next(tool for tool in response.json()["result"]["tools"] if tool["name"] == "list_tabs")
        assert tool["inputSchema"]["properties"]["limit"]["maximum"] == 100
        tools = {item["name"]: item for item in response.json()["result"]["tools"]}
        assert tools["index_tabs"]["annotations"]["readOnlyHint"] is False
        assert tools["search_tabs"]["inputSchema"]["properties"]["mode"]["default"] == "keyword"
        assert tools["organize_by_text"]["inputSchema"]["properties"]["visual"]["default"] is False


def test_list_tabs_rejects_invalid_pagination():
    with TestClient(create_app(TOKEN, ORIGIN), base_url="http://127.0.0.1:8765") as client:
        response = client.post("/mcp", headers={
            "authorization": f"Bearer {TOKEN}", "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-11-25",
        }, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "list_tabs", "arguments": {"limit": 101}
        }})
        result = response.json()["result"]
        assert result["isError"] is True
        assert "limit" in result["content"][0]["text"] and "100" in result["content"][0]["text"]


@pytest.mark.parametrize("url", ["https://", "https://user:secret@example.com/"])
def test_open_rejects_invalid_or_credential_bearing_urls(url):
    with TestClient(create_app(TOKEN, ORIGIN), base_url="http://127.0.0.1:8765") as client:
        response = client.post("/mcp", headers={
            "authorization": f"Bearer {TOKEN}", "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-11-25",
        }, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "tab_action", "arguments": {"action": "open", "url": url}
        }})
        result = response.json()["result"]
        assert result["isError"] is True
        assert "requires an http or https URL" in result["content"][0]["text"]


def test_agent_receives_actionable_disconnection_error():
    with TestClient(create_app(TOKEN, ORIGIN), base_url="http://127.0.0.1:8765") as client:
        response = client.post("/mcp", headers={
            "authorization": f"Bearer {TOKEN}",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-11-25",
        }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "browser_status", "arguments": {}}})
        result = response.json()["result"]
        assert result["isError"] is True
        assert "Chrome extension is disconnected" in result["content"][0]["text"]


def test_websocket_rejects_untrusted_origin():
    with TestClient(create_app(TOKEN, ORIGIN)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/extension", headers={"origin": "https://example.com"}):
                pass


def test_websocket_rejects_invalid_token():
    with TestClient(create_app(TOKEN, ORIGIN)) as client:
        with client.websocket_connect("/extension", headers={"origin": ORIGIN}) as socket:
            socket.send_json({"token": "wrong"})
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()


def test_authenticated_extension_heartbeat():
    with TestClient(create_app(TOKEN, ORIGIN)) as client:
        with client.websocket_connect("/extension", headers={"origin": ORIGIN}) as socket:
            socket.send_json({"token": TOKEN})
            assert socket.receive_json() == {"type": "paired"}
            socket.send_json({"type": "ping"})
            assert socket.receive_json() == {"type": "pong"}


async def test_disconnected_browser_fails_immediately():
    with pytest.raises(RuntimeError, match="Chrome extension is disconnected"):
        await Bridge().call("probe")


async def test_timeout_does_not_retry():
    class Peer:
        sent = []

        async def send_json(self, value):
            self.sent.append(value)

    bridge = Bridge()
    bridge.peer = Peer()
    with pytest.raises(RuntimeError, match="outcome unknown"):
        await bridge.call("probe", timeout=0.01)
    assert len(bridge.peer.sent) == 1
    assert bridge.pending == {}


async def test_text_extraction_is_enabled_for_local_processing():
    with pytest.raises(RuntimeError, match="Chrome extension is disconnected"):
        await Bridge().call("extract_tabs")
