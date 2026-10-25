import json

from starlette.testclient import TestClient

from fast_tabber.server import create_app


def test_mutations_are_journaled_before_execution_and_revert_all_is_lifo(tmp_path):
    token = "a" * 64
    app = create_app(token, "chrome-extension://test", tmp_path / "browser.sqlite3")
    opened = []
    reverted = []

    async def browser(operation, args=None):
        if operation == "capture_browser":
            return {"tabs": [], "groups": {}}
        if operation == "tab_action":
            assert app.state.store.latest_undo()[2] == "executing"
            tab_id = len(opened) + 1
            opened.append(tab_id)
            return {"tab": {"id": tab_id}, "before": {"tabs": [], "groups": {}, "closeIds": [tab_id]}}
        assert operation == "restore_snapshot"
        reverted.extend(args["closeIds"])
        return {"restored": 0, "ids": {}}

    app.state.bridge.call = browser
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        def call(name, args):
            response = client.post("/mcp", headers={"authorization": f"Bearer {token}",
                "accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-11-25"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
            result = response.json()["result"]
            assert not result.get("isError"), result
            return json.loads(result["content"][0]["text"])
        for _ in range(2):
            call("tab_action", {"action": "open", "url": "https://example.com/"})
        result = call("undo_last", {"all_changes": True})
        assert result["verified"] and result["changes"] == 2
        assert reverted == [2, 1]
        assert app.state.store.latest_undo() is None
        assert app.state.store.db.execute("SELECT count(*) FROM undo WHERE status='undone'").fetchone()[0] == 2
