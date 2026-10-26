import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from fast_tabber.server import StateStore, create_app
from fast_tabber.library import Library


@pytest.mark.asyncio
async def test_text_cache_survives_reopened_resource_ids_without_extraction(library):
    engine, rows, calls, *_ = library
    first = await engine.index("tabs", ["10", "11"], "text")
    assert first["indexed"] == 2
    calls.clear()
    rows[0]["id"] = "90001"
    rows[0]["title"] = "Updated label for the same URL"
    second = await engine.index("tabs", ["90001", "11"], "text")
    assert second["cached"] == 2 and second["indexed"] == 0
    assert not any(operation == "inspect_page" for operation, _ in calls)
    result = await engine.search("tabs", "rendered content", 0, 20)
    assert {row["id"] for row in result["items"]} == {"90001", "11"}
    assert result["method"] == "fts5_bm25"
    assert engine.store.db.execute("SELECT count(*) FROM resources WHERE vector IS NOT NULL").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_individual_failure_preserves_committed_text_and_reports_partial_coverage(library):
    engine, rows, calls, *_ = library
    original = engine.bridge.call
    async def one_failure(operation, arguments=None):
        if operation == "inspect_page" and arguments.get("tabId") == 11:
            raise RuntimeError("Deliberately inaccessible page")
        return await original(operation, arguments)
    engine.bridge.call = one_failure
    result = await engine.index("tabs", ["10", "11", "12"], "text")
    assert (result["completed"], result["failed"], result["pending"]) == (2, 1, 0)
    assert result["failures"][0]["id"] == "11"
    repeat = await engine.index("tabs", ["10", "11", "12"], "text")
    assert repeat["cached"] == 2 and repeat["failed"] == 1


@pytest.mark.asyncio
async def test_three_thousand_bookmark_metadata_records_and_bounded_search_selection(library):
    engine, rows, calls, *_ = library
    result = await engine.index("bookmarks", level="metadata")
    assert result["indexed"] == 3000
    assert not any(operation == "inspect_page" for operation, _ in calls)
    page = await engine.search("bookmarks", "Subject", 0, 20)
    assert page["total"] == 3000 and len(page["items"]) == 20 and page["hasMore"]
    selection = await engine.selection("create", "bookmarks", query="Subject")
    assert selection["count"] == 3000 and len(selection["examples"]) == 5
    repeat = await engine.index("bookmarks", level="metadata")
    assert repeat["cached"] == 3000 and repeat["indexed"] == 0


@pytest.mark.asyncio
async def test_refresh_recaptures_text_but_group_changes_do_not(library):
    engine, rows, calls, *_ = library
    await engine.index("tabs", ["10"], "text")
    rows[0]["groupId"] = 90
    calls.clear()
    assert (await engine.index("tabs", ["10"], "text"))["cached"] == 1
    assert not any(operation == "inspect_page" for operation, _ in calls)
    assert (await engine.index("tabs", ["10"], "text", refresh=True))["indexed"] == 1
    assert sum(operation == "inspect_page" for operation, _ in calls) == 1


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
    async def mutate(operation, arguments):
        mutations.append((operation, arguments))
        return {"grouped": sum(len(group.get("ids", group.get("tabIds", []))) for group in arguments.get("groups", arguments.get("categories", []))), "verified": True}
    engine = Library(store, SimpleNamespace(call=call), tmp_path, mutate, asyncio.Lock())
    return engine, rows, calls, inference, mutations










@pytest.mark.asyncio
async def test_selections_combine_and_assign_exact_live_resources(library):
    engine, rows, _, _, mutations = library
    first = await engine.selection("create", "tabs", ids=["10", "11", "12"])
    second = await engine.selection("create", "tabs", ids=["12", "13", "14"])
    page = await engine.selection("show", "tabs", selection_id=first["selection_id"], offset=1, limit=1)
    assert page["total"] == 3 and page["returned"] == 1 and page["hasMore"]
    intersection = await engine.selection("combine", "tabs", selection_ids=[first["selection_id"], second["selection_id"]], operation="intersect")
    assert intersection["count"] == 1
    union = await engine.selection("combine", "tabs", selection_ids=[first["selection_id"], second["selection_id"]])
    assert union["count"] == 5
    third = await engine.selection("create", "tabs", ids=["15", "16", "17"])
    result = await engine.assign_selections("tabs", [
        {"selection_id": first["selection_id"], "title": "Research"},
        {"selection_id": third["selection_id"], "title": "Tools"},
    ])
    assert result["grouped"] == 6 and result["verified"]
    assert mutations[-1][0] == "group_categories"
    assert {group["title"] for group in mutations[-1][1]["categories"]} == {"Research", "Tools"}


@pytest.mark.asyncio
async def test_stale_or_overlapping_selections_fail_before_mutation(library):
    engine, rows, _, _, mutations = library
    one = await engine.selection("create", "tabs", ids=["10", "11"])
    overlap = await engine.selection("create", "tabs", ids=["11", "12"])
    with pytest.raises(ValueError, match="overlap"):
        await engine.assign_selections("tabs", [
            {"selection_id": one["selection_id"], "title": "A"},
            {"selection_id": overlap["selection_id"], "title": "B"},
        ])
    rows[0]["title"] = "Changed after selection"
    with pytest.raises(ValueError, match="changed"):
        await engine.assign_selections("tabs", [{"selection_id": one["selection_id"], "title": "A"}])
    assert not mutations




@pytest.mark.asyncio
async def test_union_cannot_bypass_selection_size_limit(library):
    engine = library[0]
    for identifier, start in (("sel_a", 0), ("sel_b", 3000)):
        items = [{"id": str(value), "fingerprint": "hash"} for value in range(start, start + 3000)]
        engine.store.db.execute("INSERT INTO selections(id,kind,items) VALUES (?,?,?)", (identifier, "tabs", json.dumps(items)))
    with pytest.raises(ValueError, match="exceeds 5000"):
        await engine.selection("combine", "tabs", selection_ids=["sel_a", "sel_b"])
    assert engine.store.db.execute("SELECT count(*) FROM selections").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_move_selection_targets_existing_group(library):
    engine, rows, _, _, mutations = library
    rows[0]["groupId"] = 42
    selection = await engine.selection("create", "tabs", ids=["10", "11"])
    result = await engine.assign_selections("tabs", [{"selection_id": selection["selection_id"], "title": "Existing"}], target_group_id=42)
    assert result["verified"]
    assert mutations[-1][0] == "group_action"
    assert mutations[-1][1] == {"action": "add", "groupId": 42, "tabIds": [10, 11]}




@pytest.mark.asyncio
async def test_resume_after_interruption_preserves_the_original_scope(library):
    engine, _, calls, *_ = library
    engine.store.save_job({"id": "interrupted", "resource": "tabs", "level": "text", "items": ["10"],
                           "total": 1, "state": "interrupted", "completed": 0, "cached": 0, "failed": 0})
    await engine.job("resume", "tabs", None, "text", False, "interrupted")
    await engine.tasks["interrupted"]
    result = await engine.job("status", "tabs", None, "text", False, "interrupted")
    assert [args["tabId"] for operation, args in calls if operation == "inspect_page"] == [10]
    assert result["total"] == result["completed"] == 1 and result["pending"] == 0


@pytest.mark.asyncio
async def test_resume_retries_all_failures_even_when_public_details_are_bounded(library):
    engine, rows, *_ = library
    original = engine.bridge.call
    failing = True
    async def browser(operation, arguments=None):
        if operation == "capture_browser":
            return {"tabs": rows[:32], "groups": {}}
        if operation == "inspect_page" and failing:
            raise RuntimeError("Page DOM was not ready")
        return await original(operation, arguments)
    engine.bridge.call = browser
    started = await engine.job("start", "tabs", [row["id"] for row in rows[:32]], "text", False)
    await engine.tasks[started["id"]]
    failed = await engine.job("status", "tabs", None, "text", False, started["id"])
    assert failed["failed"] == 32 and len(failed["failedResources"]) == 20
    failing = False
    await engine.job("resume", "tabs", None, "text", False, started["id"])
    await engine.tasks[started["id"]]
    done = await engine.job("status", "tabs", None, "text", False, started["id"])
    assert done["total"] == done["completed"] == 32 and done["pending"] == done["failed"] == 0








@pytest.mark.asyncio
async def test_text_load_error_keeps_its_actual_stage_and_resource(library):
    engine = library[0]
    original = engine.bridge.call
    async def fail(operation, arguments=None):
        if operation == "inspect_page":
            raise RuntimeError("Page DOM was not ready")
        return await original(operation, arguments)
    engine.bridge.call = fail
    started = await engine.job("start", "tabs", ["10"], "text", False)
    await engine.tasks[started["id"]]
    status = await engine.job("status", "tabs", None, "text", False, started["id"])
    assert status["state"] == "complete" and status["failed"] == 1 and status["pending"] == 0
    assert status["failedResources"][0]["id"] == "10" and status["noBrowserChanges"]
    assert "Page DOM was not ready" in status["failedResources"][0]["reason"] and not library[-1]


@pytest.mark.asyncio
async def test_index_job_progress_counts_all_committed_batches(library):
    engine = library[0]
    started = await engine.job("start", "bookmarks", [str(value) for value in range(10, 85)], "metadata", False)
    await engine.tasks[started["id"]]
    status = await engine.job("status", "bookmarks", None, "metadata", False, started["id"])
    assert status["state"] == "complete" and status["stage"] == "complete"
    assert status["completed"] == status["total"] == status["indexed"] == 75




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
