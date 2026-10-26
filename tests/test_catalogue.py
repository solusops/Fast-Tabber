"""Catalogue scheduling tests; synthetic records never represent a real Chrome demo."""
import json
import pytest
from starlette.testclient import TestClient

import fast_tabber.server as server
from fast_tabber.preflight import red_image

TOKEN = "a" * 64
HEADERS = {"authorization": f"Bearer {TOKEN}", "accept": "application/json, text/event-stream",
           "mcp-protocol-version": "2025-11-25"}


def call(client, arguments):
    result = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "catalogue_tabs", "arguments": arguments}}).json()["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("fail_capture,close_after", [(False, False), (True, False), (False, True)])
def test_six_hundred_sleeping_tabs_text_first_and_fifty_tab_backups(tmp_path, monkeypatch, fail_capture, close_after):
    tabs = [{"id": i, "url": f"https://example.test/{i}", "title": "Page", "windowId": 1, "discarded": True}
            for i in range(1, 601)]
    calls = []
    async def bridge(operation, arguments=None):
        calls.append((operation, arguments))
        if operation == "capture_browser":
            return {"tabs": tabs, "groups": {}}
        if operation == "capture_tabs":
            return {"tabs": [tabs[i - 1] for i in arguments["tabIds"]], "groups": {}}
        if operation == "extract_tabs":
            return {"items": [], "failures": [{"id": i, "reason": "discarded_tab_not_woken"} for i in arguments["tabIds"]]}
        if operation == "capture_page":
            if fail_capture:
                raise RuntimeError("capture failed")
            return {**tabs[arguments["tabId"] - 1], "headings": [], "text": "", "image": red_image()}
        if operation == "close_tabs":
            selected = [tabs[i - 1] for i in arguments["tabIds"]]
            assert len(selected) <= 50
            assert app.state.store.db.execute("SELECT count(*) FROM collections").fetchone()[0] == 12
            assert app.state.store.latest_undo()[2] == "executing"
            return {"closed": len(selected), "verified": True, "before": {"tabs": selected, "groups": {}}}
        raise AssertionError(operation)
    async def digest(*args, **kwargs):
        return "verified-test-model"
    async def embed(inputs, *args):
        return [[1., 0.] for _ in inputs], "verified-test-model"
    async def triage(pages, *args, **kwargs):
        return {page["id"]: {"category": "Research", "needs_visual": page["id"] % 100 == 0} for page in pages}
    async def describe(*args):
        return "Research diagram"
    async def classify(pages, *args):
        return {page["id"]: "Research" for page in pages}
    for name, value in {"embedding_digest": digest, "_gemma_digest": digest, "embed_texts": embed,
                        "triage_pages": triage, "describe_image": describe, "classify_pages": classify}.items():
        monkeypatch.setattr(server, name, value)
    app = server.create_app(TOKEN, "chrome-extension://test", tmp_path / "browser.sqlite3")
    monkeypatch.setattr(app.state.bridge, "call", bridge)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        job = call(client, {"action": "start", "close_after_index": close_after, "confirm_close": close_after})
        for _ in range(20):
            job = call(client, {"action": "status", "job_id": job["jobId"], "wait_seconds": 1})
            if job["state"] in {"complete", "failed"}:
                break
    assert job["textAssessed"] == 600
    assert job["backupBatches"] == 12
    assert job["screenshotsNeeded"] == 6
    assert job["state"] == ("failed" if fail_capture else "complete")
    assert sum(operation == "capture_page" for operation, _ in calls) == (1 if fail_capture else 6)
    first_capture = next(i for i, (operation, _) in enumerate(calls) if operation == "capture_page")
    assert sum(operation == "extract_tabs" for operation, _ in calls[:first_capture]) == 12
    assert sum(operation == "close_tabs" for operation, _ in calls) == (12 if close_after else 0)
    assert not any(operation == "group_categories" for operation, _ in calls)
    assert job["closed"] == (600 if close_after else 0)
    collections = app.state.store.db.execute("SELECT payload FROM collections").fetchall()
    assert all(len(json.loads(row[0])["tabs"]) == 50 for row in collections)
