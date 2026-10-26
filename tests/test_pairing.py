import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fast_tabber.pairing import Pairing
from fast_tabber.server import create_app

TOKEN = "a" * 64
ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def test_codes_expire_replace_and_are_single_use(monkeypatch):
    pairing = Pairing(TOKEN)
    assert pairing.token != TOKEN
    first = pairing.issue()["pairingCode"]
    second = pairing.issue()["pairingCode"]
    assert not pairing.consume(first)
    assert not pairing.consume("wrong")
    assert pairing.consume(second)
    assert not pairing.consume(second)
    third = pairing.issue()["pairingCode"]
    monkeypatch.setattr("fast_tabber.pairing.time.monotonic", lambda: pairing.expires + 1)
    assert not pairing.consume(third)


def test_store_pairing_and_browser_credentials_cannot_access_mcp(tmp_path):
    app = create_app(TOKEN, ORIGIN, tmp_path / "browser.sqlite3")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        result = client.post("/mcp", headers={"authorization": "Bearer " + TOKEN,
            "accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "browser_setup", "arguments": {"action": "pair"}}}).json()["result"]["structuredContent"]
        code = result["pairingCode"]
        with client.websocket_connect("/extension", headers={"origin": ORIGIN}) as ws:
            ws.send_json({"pairingCode": code, "version": "0.7.0"})
            browser_token = ws.receive_json()["token"]
            assert browser_token != TOKEN
            assert client.post("/mcp", headers={"authorization": "Bearer " + browser_token}, json={}).status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/extension", headers={"origin": ORIGIN}) as ws:
                ws.send_json({"pairingCode": code})
                ws.receive_json()
        with client.websocket_connect("/extension", headers={"origin": ORIGIN}) as ws:
            ws.send_json({"token": browser_token, "version": "0.7.0"})
            assert ws.receive_json()["type"] == "paired"
