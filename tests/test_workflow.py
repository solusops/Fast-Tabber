import json
import pytest

from starlette.testclient import TestClient

from fast_tabber.server import create_app


def invoke(client, tool_name, **arguments):
    return client.post("/mcp", headers={"authorization": "Bearer " + "a" * 64,
        "accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-11-25"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}}).json()["result"]


def app_for(tmp_path, allowed=False):
    (tmp_path / "settings.json").write_text(json.dumps({"privacy_acknowledged": True, "indexing_allowed": ["tabs"] if allowed else []}))
    app = create_app("a" * 64, "chrome-extension://test", tmp_path / "browser.sqlite3")
    async def forbidden(*args, **kwargs):
        if args[0] == "capture_browser":
            return {"tabs": [{"id": value, "windowId": 1, "title": f"Page {value}", "url": f"https://example.test/{value}"} for value in range(1, 32)], "groups": {}}
        raise AssertionError("No page extraction or mutation before workflow choices")
    app.state.bridge.call = forbidden
    return app








@pytest.mark.parametrize("tool,arguments", [
    ("tab_action", {"action": "close", "tab_id": 1}),
    ("bookmark_action", {"action": "delete", "ids": ["11"], "warned": True}),
    ("collection_action", {"action": "close", "name": "Selected"}),
    ("collection_action", {"action": "delete", "name": "Selected"}),
    ("remove_duplicates", {"remove": True}),
])
def test_destructive_actions_wait_for_actual_confirmation(tmp_path, tool, arguments):
    app = app_for(tmp_path)
    app.state.store.save_collection("Selected", {"tabs": [{"id": 1}]})
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        result = invoke(client, tool, **arguments)["structuredContent"]
    assert result["requiresConfirmation"] and result["noChangesMade"] and not result["executed"]
    assert app.state.store.latest_undo() is None








def test_tab_selection_can_be_saved_as_bookmarks_and_exported(tmp_path):
    app = app_for(tmp_path)
    rows = [{"id": "11", "url": "https://example.test/selected", "title": "Selected"}]
    async def selection(kind, identifier):
        assert kind == "tabs" and identifier == "sel_test"
        return rows
    async def records(kind, ids, expand=False):
        assert kind == "tabs" and ids == ["11"]
        return rows
    received = []
    async def mutate(operation, arguments):
        received.append((operation, arguments))
        return {"verified": True}
    app.state.library.current_selection = selection
    app.state.library.records = records
    app.state.library.mutate = mutate
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        saved = invoke(client, "bookmark_action", action="save_tabs", selection_id="sel_test", parent_id="folder")
        assert not saved.get("isError"), saved
        exported = invoke(client, "export_urls", resource="tabs", selection_id="sel_test")["structuredContent"]
    assert received[0][1]["ids"] == ["11"] and exported["count"] == 1




def test_running_index_job_returns_the_exact_next_poll(tmp_path):
    app = app_for(tmp_path, allowed=True)
    async def job(*args):
        return {"id": "example-job", "state": "running", "resource": "tabs", "level": "metadata", "total": 30, "completed": 0}
    app.state.library.job = job
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        result = invoke(client, "index_resources", resource="tabs")["structuredContent"]
        assert result["nextCall"] == {"tool": "index_resources", "arguments": {"action": "status", "resource": "tabs", "job_id": "example-job", "wait_seconds": 5}}
        assert result["nextAction"] == "poll"


def test_mcp_progress_notifications_are_monotonic_and_describe_extraction(tmp_path, monkeypatch):
    from mcp.server.mcpserver.context import Context

    app = app_for(tmp_path, allowed=True)
    app.state.store.save_job({"id": "job-progress", "type": "index", "resource": "tabs", "items": ["1", "2"],
                              "total": 2, "state": "running", "stage": "extracting_text"})
    statuses = iter([
        {"id": "job-progress", "resource": "tabs", "state": "running", "stage": "extracting_text", "total": 2,
         "completed": 0, "cached": 0, "failed": 0, "pending": 2, "extracted": 1, "extractionFailed": 0, "inFlight": 1},
        {"id": "job-progress", "resource": "tabs", "state": "running", "stage": "embedding", "total": 2,
         "completed": 0, "cached": 0, "failed": 0, "pending": 2, "extracted": 2, "extractionFailed": 0, "inFlight": 0},
        {"id": "job-progress", "resource": "tabs", "state": "complete", "stage": "complete", "total": 2,
         "completed": 2, "cached": 0, "failed": 0, "pending": 0, "extracted": 2, "extractionFailed": 0, "inFlight": 0},
    ])

    async def status(*args, **kwargs):
        return next(statuses)

    reports = []

    async def report(self, progress, total=None, message=None):
        reports.append((progress, total, message))

    app.state.library.job = status
    monkeypatch.setattr(Context, "report_progress", report)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        result = invoke(client, "index_resources", resource="tabs", action="status", job_id="job-progress", wait_seconds=2)["structuredContent"]
    assert result["state"] == "complete"
    assert [value[0] for value in reports] == [1, 2]
    assert all(value[1] is None and "extracted" in value[2] for value in reports)




def test_remove_whole_group_is_ungrouping_not_a_catalogue_error(tmp_path):
    app = app_for(tmp_path)
    calls = []
    async def browser(operation, arguments=None):
        calls.append((operation, arguments))
        if operation == "capture_browser":
            return {"tabs": [], "groups": {}, "windows": []}
        assert operation == "group_action"
        return {"removed": 2, "verified": True, "before": {"tabs": []}}
    app.state.bridge.call = browser
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        result = invoke(client, "organize_tabs", action="remove", group_id=42)
        assert not result.get("isError"), result
        assert calls[-1][1]["action"] == "delete" and calls[-1][1]["groupId"] == 42


def test_ungroup_all_resolves_the_entire_window_locally(tmp_path):
    app = app_for(tmp_path)
    rows = [{"id": value, "windowId": 7, "groupId": 42} for value in range(1, 502)]
    rows += [{"id": 600, "windowId": 7, "groupId": -1}, {"id": 601, "windowId": 8, "groupId": 43}]
    calls = []
    async def browser(operation, arguments=None):
        if operation == "capture_browser":
            return {"tabs": rows, "groups": {}, "windows": []}
        calls.append((operation, arguments))
        return {"affected": len(arguments["tabIds"]), "before": {"tabs": []}}
    app.state.bridge.call = browser
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        result = invoke(client, "organize_tabs", action="ungroup_all", window_id=7)
    assert not result.get("isError"), result
    value = result["structuredContent"]
    assert value["ungrouped"] == 501 and value["verified"]
    assert calls[0][0] == "group_action" and calls[0][1]["action"] == "remove"
    assert calls[0][1]["tabIds"] == list(range(1, 502))
