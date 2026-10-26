import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from fast_tabber.server import StateStore, create_app
from fast_tabber.library import Library


@pytest.fixture
def library(tmp_path, monkeypatch):
    (tmp_path / "settings.json").write_text(json.dumps({"privacy_acknowledged": True, "indexing_allowed": ["tabs", "bookmarks"]}))
    store = StateStore(tmp_path / "browser.sqlite3")
    rows = [{"id": str(i+10), "parentId": "1", "index": i, "url": f"https://example.test/{i}", "title": f"Subject {i%10} page {i}", "windowId": 1}
            for i in range(3000)]
    calls, inference, mutations = [], [], []
    async def call(operation, arguments=None):
        calls.append((operation, arguments))
        if operation == "bookmark_tree":
            return {"nodes": [{"id": "1", "parentId": "0", "title": "Bookmarks bar"}] + rows}
        if operation == "capture_browser":
            return {"tabs": rows[:20], "groups": {}}
        if operation == "inspect_page":
            identifier = str(arguments.get("tabId", arguments.get("bookmarkId")))
            row = next(row for row in rows if row["id"] == identifier)
            return {**row, "text": f"Full rendered content for {row['title']}", "headings": ["Overview"]}
        raise AssertionError(operation)
    async def digest(*args):
        return "embeddings-v1"
    async def embed(inputs, *args):
        inference.extend(inputs)
        vectors = []
        for value in inputs:
            match = re.search(r"Subject (\d+)", value)
            vector = [0.] * 10
            vector[int(match.group(1)) if match else 0] = 1.
            vectors.append(vector)
        return vectors, "embeddings-v1"
    async def mutate(operation, arguments):
        mutations.append((operation, arguments))
        return {"grouped": sum(len(group.get("ids", group.get("tabIds", []))) for group in arguments.get("groups", arguments.get("categories", []))), "verified": True}
    monkeypatch.setattr("fast_tabber.library.embedding_digest", digest)
    monkeypatch.setattr("fast_tabber.library.embed_texts", embed)
    engine = Library(store, SimpleNamespace(call=call), tmp_path, mutate, asyncio.Lock())
    return engine, rows, calls, inference, mutations


@pytest.mark.asyncio
async def test_three_thousand_bookmarks_cache_reuse_and_fast_grouping(library):
    engine, rows, calls, inference, mutations = library
    proposal = await engine.propose("bookmarks", None, None, None, "", False)
    assert proposal["groupCount"] == 10 and proposal["unclassified"] == 0
    count = len(inference)
    result = await engine.index("bookmarks")
    assert result["reused"] == 3000 and result["indexed"] == 0 and len(inference) == count
    assert all(operation != "inspect_page" for operation, _ in calls)
    applied = await engine.apply(engine.plan(proposal["planId"]))
    assert applied["grouped"] == 3000 and mutations[0][0] == "bookmark_action"
    assert mutations[0][1]["action"] == "group"
    assert len(engine.store.db.execute("SELECT vector FROM resources LIMIT 1").fetchone()[0]) == 20
    with pytest.raises(ValueError, match="already applied"):
        await engine.apply(engine.plan(proposal["planId"]))


@pytest.mark.asyncio
async def test_progressive_extraction_is_on_demand_and_model_versions_invalidate_vectors(library, monkeypatch):
    engine, _, calls, inference, _ = library
    await engine.index("tabs", ["10"])
    assert not any(operation == "inspect_page" for operation, _ in calls)
    await engine.index("tabs", ["10"], "text")
    assert sum(operation == "inspect_page" for operation, _ in calls) == 1
    count = len(inference)
    await engine.index("tabs", ["10"], "text")
    assert len(inference) == count
    await engine.index("tabs", ["10"], "text", refresh=True)
    assert sum(operation == "inspect_page" for operation, _ in calls) == 2
    # Changed metadata triggers new embedding but ordinary grouping/moving does not.
    library[1][0]["title"] = "Subject 2 changed"
    await engine.index("tabs", ["10"])
    assert len(inference) > count


@pytest.mark.asyncio
async def test_suspended_inspection_failure_is_not_replaced_by_metadata(library):
    engine, _, _, _, _ = library
    original = engine.bridge.call
    async def fail(operation, arguments=None):
        if operation == "inspect_page":
            raise RuntimeError("Page could not be loaded")
        return await original(operation, arguments)
    engine.bridge.call = fail
    with pytest.raises(RuntimeError, match="could not be loaded"):
        await engine.index("tabs", ["10"], "image")
    assert engine.cached("tabs", "10") is None


def test_explicit_resource_scope_privacy_and_index_consent(tmp_path):
    app = create_app("a"*64, "chrome-extension://test", tmp_path / "browser.sqlite3")
    async def forbidden(*args, **kwargs):
        raise AssertionError("No browser access before indexing consent/disclosure")
    app.state.bridge.call = forbidden
    headers = {"authorization": "Bearer " + "a"*64, "accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-11-25"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        def call(arguments):
            return client.post("/mcp", headers=headers, json={"jsonrpc":"2.0", "id":1,"method":"tools/call", "params":{"name":"index_resources","arguments":arguments}}).json()["result"]
        assert call({})["isError"]  # bookmarks are never an implicit scope
        result = call({"resource":"bookmarks"})
        assert result["structuredContent"]["requiresDisclosure"]
        (tmp_path / "settings.json").write_text('{"privacy_acknowledged":true}')
        assert call({"resource":"bookmarks"})["structuredContent"]["requiresIndexingConsent"]


def test_subset_consent_does_not_authorize_entire_browser(library):
    engine = library[0]
    path = engine.state / "settings.json"
    path.write_text('{"privacy_acknowledged":true}')
    assert engine.permission("tabs", True, ["10"]) is None
    assert engine.permission("tabs", False, ["10"]) is None
    assert engine.permission("tabs", False, None)["requiresIndexingConsent"]
    assert engine.permission("bookmarks", False, ["10"])["requiresIndexingConsent"]


def test_precise_plan_edits_and_image_evidence_use_cached_data(tmp_path):
    from fast_tabber.preflight import red_image
    store = StateStore(tmp_path / "browser.sqlite3")
    page = {"id":"1", "windowId":1, "url":"https://example.test/", "title":"Diagram", "headings":[], "text":"Chart"}
    page["image_id"] = store.index.image(red_image())
    document = store.index.document(page)[0]
    store.db.execute("INSERT INTO resources VALUES (?,?,?,?,?,?,?,?)", ("tabs","1",json.dumps(page),"hash",document,2,b"vector","model"))
    store.db.commit()
    app = create_app("a"*64,"chrome-extension://test",tmp_path/"browser.sqlite3")
    async def forbidden(*args, **kwargs):
        raise AssertionError("Cached evidence must not re-capture the browser")
    app.state.bridge.call = forbidden
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        result = client.post("/mcp",headers={"authorization":"Bearer "+"a"*64,"accept":"application/json, text/event-stream","mcp-protocol-version":"2025-11-25"},
            json={"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"inspect_page","arguments":{"resource":"tabs","id":"1","detail":"image"}}}).json()["result"]
        assert not result.get("isError"),result
        assert result["content"][1]["type"] == "image"
        assert result["content"][1]["mimeType"] == "image/png"


def test_apply_cannot_silently_ignore_a_rename_request(tmp_path):
    app = create_app("a"*64,"chrome-extension://test",tmp_path/"browser.sqlite3")
    plan = {"id":"test-plan","resource":"tabs","groups":[],"selection":[],"state":"ready","unclassified":0}
    app.state.store.db.execute("INSERT INTO grouping_plans VALUES (?,?)", (plan["id"],json.dumps(plan)))
    app.state.store.db.commit()
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        result = client.post("/mcp",headers={"authorization":"Bearer "+"a"*64,"accept":"application/json, text/event-stream","mcp-protocol-version":"2025-11-25"},
            json={"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"apply_grouping","arguments":{"plan_id":"test-plan","names":{"unknown-group":"Ignored title"}}}}).json()["result"]
        assert result["isError"]
        assert "No browser changes made" in result["content"][0]["text"]
        assert app.state.store.latest_undo() is None
