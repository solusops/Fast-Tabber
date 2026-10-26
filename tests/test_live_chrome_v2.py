"""Disposable end-to-end checks; run with FAST_TABBER_LIVE_CHROME=1 and a paired Chrome."""

import json
import os
import sqlite3
import uuid

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from fast_tabber.server import STATE


pytestmark = pytest.mark.skipif(os.getenv("FAST_TABBER_LIVE_CHROME") != "1", reason="live Chrome is opt-in")


@pytest.mark.asyncio
async def test_v2_groups_collections_duplicates_and_undo():
    connection = json.loads((STATE / "connection.json").read_text(encoding="utf-8"))
    marker = uuid.uuid4().hex
    collection_name = f"v2-{marker[:10]}"
    test_ids = set()
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {connection['token']}"}, trust_env=False, timeout=45) as http:
        async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()

                async def call(name, arguments=None):
                    result = await client.call_tool(name, arguments or {})
                    if result.is_error:
                        raise AssertionError(result.content[0].text)
                    return json.loads(result.content[0].text)

                async def tabs(window_id):
                    result = await call("list_tabs", {"window_id": window_id, "limit": 100})
                    entries = list(result["tabs"])
                    groups = {group["id"]: group for group in result["groups"]}
                    for offset in range(100, result["total"], 100):
                        page = await call("list_tabs", {"window_id": window_id, "offset": offset, "limit": 100})
                        entries.extend(page["tabs"])
                        groups.update((group["id"], group) for group in page["groups"])
                    return {"total": result["total"], "tabs": entries, "groups": list(groups.values())}

                initial = await call("list_tabs", {"limit": 100})
                assert initial["tabs"], "Chrome has no normal window for disposable integration tabs"
                status = await call("browser_status")
                assert status.get("stage") in {"v2", "v3", "v4", "v5"} and "group_action" in status.get("operations", []), \
                    "Reload the current extension before running V2 browser tests"
                assert "tabGroups" in status.get("permissions", []), "Chrome tabGroups permission is missing"
                window_id = initial["tabs"][0]["windowId"]
                active_id = next((tab["id"] for tab in initial["tabs"] if tab["active"] and tab["windowId"] == window_id), None)
                undo_db = sqlite3.connect(STATE / "browser.sqlite3")
                undo_before = undo_db.execute("SELECT coalesce(max(id),0) FROM undo").fetchone()[0]
                undo_db.close()
                try:
                    opened = []
                    for suffix in ("a", "b"):
                        tab = await call("tab_action", {"action": "open", "url": f"https://example.com/?fast_tabber_test={marker}-{suffix}", "window_id": window_id})
                        opened.append(tab["tab"]["id"])
                        test_ids.add(tab["tab"]["id"])

                    group = await call("organize_tabs", {"action": "create", "tab_ids": opened,
                        "title": f"V2 {marker[:6]}", "color": "blue", "mode": "batch"})
                    group_id = group["groupId"]
                    await call("organize_tabs", {"action": "update", "group_id": group_id,
                        "title": f"V2 done {marker[:6]}", "color": "orange", "collapsed": True})
                    listing = await tabs(window_id)
                    group_meta = next(item for item in listing["groups"] if item["id"] == group_id)
                    assert group_meta["title"] == f"V2 done {marker[:6]}" and group_meta["collapsed"]
                    assert all(next(tab for tab in listing["tabs"] if tab["id"] == tab_id)["groupId"] == group_id for tab_id in opened)

                    await call("organize_tabs", {"action": "remove", "tab_ids": [opened[1]]})
                    assert next(tab for tab in (await tabs(window_id))["tabs"] if tab["id"] == opened[1])["groupId"] == -1
                    await call("undo_last")
                    assert next(tab for tab in (await tabs(window_id))["tabs"] if tab["id"] == opened[1])["groupId"] == group_id

                    await call("organize_tabs", {"action": "delete", "group_id": group_id})
                    listing = await tabs(window_id)
                    assert all(next(tab for tab in listing["tabs"] if tab["id"] == tab_id)["groupId"] == -1 for tab_id in opened)
                    await call("undo_last")
                    listing = await tabs(window_id)
                    group_id = next(group["id"] for group in listing["groups"] if group["title"] == f"V2 done {marker[:6]}")

                    moved = await call("organize_tabs", {"action": "move", "tab_ids": opened,
                        "window_id": window_id, "index": 0, "mode": "sequential"})
                    assert moved["verified"] and moved["moved"] == 2
                    listing = await tabs(window_id)
                    positions = sorted(next(tab["index"] for tab in listing["tabs"] if tab["id"] == tab_id) for tab_id in opened)
                    assert positions == [0, 1]

                    saved = await call("collection_action", {"action": "save", "name": collection_name, "tab_ids": opened})
                    assert saved["saved"] == 2
                    closed = await call("collection_action", {"action": "close", "name": collection_name, "mode": "batch", "confirmed": True})
                    assert closed["closed"] == 2 and closed["verified"]
                    assert not any(tab["id"] in opened for tab in (await tabs(window_id))["tabs"])
                    restored = await call("undo_last")
                    opened = restored.get("tabIds", [])
                    test_ids.update(opened)
                    assert restored["restored"] == 2
                    listing = await tabs(window_id)
                    assert all(next(tab for tab in listing["tabs"] if tab["id"] == tab_id)["groupId"] >= 0 for tab_id in opened), \
                        f"restored IDs/group IDs: {[(tab_id, next(tab for tab in listing['tabs'] if tab['id'] == tab_id)['groupId']) for tab_id in opened]}; groups: {listing['groups']}"

                    await call("collection_action", {"action": "close", "name": collection_name, "mode": "sequential", "confirmed": True})
                    restored = await call("collection_action", {"action": "restore", "name": collection_name, "mode": "sequential"})
                    opened = restored.get("tabIds", [])
                    test_ids.update(opened)
                    assert restored["restored"] == 2
                    listing = await tabs(window_id)
                    assert all(next(tab for tab in listing["tabs"] if tab["id"] == tab_id)["groupId"] >= 0 for tab_id in opened)
                    await call("undo_last")
                    assert not any(tab["id"] in opened for tab in (await tabs(window_id))["tabs"])
                    await call("collection_action", {"action": "delete", "name": collection_name, "confirmed": True})

                    duplicate_url = f"https://example.org/?fast_tabber_duplicate={marker}"
                    duplicate_ids = []
                    for _ in range(2):
                        result = await call("tab_action", {"action": "open", "url": duplicate_url, "window_id": window_id})
                        duplicate_ids.append(result["tab"]["id"])
                        test_ids.add(result["tab"]["id"])
                    preview = await call("remove_duplicates", {"window_id": window_id})
                    assert preview["count"] == 1 and duplicate_ids[1] in preview["samples"]
                    removed = await call("remove_duplicates", {"window_id": window_id, "remove": True, "confirmed": True})
                    assert removed["removed"] == 1 and not any(tab["id"] == duplicate_ids[1] for tab in (await tabs(window_id))["tabs"])
                    restored = await call("undo_last")
                    test_ids.update(restored.get("tabIds", []))
                    assert restored["restored"] == 1 and len(restored["tabIds"]) == 1
                    for tab_id in (duplicate_ids[0], restored["tabIds"][0]):
                        await call("tab_action", {"action": "close", "tab_id": tab_id, "confirmed": True})
                    assert not any(tab["id"] in test_ids for tab in (await tabs(window_id))["tabs"])
                    assert active_id is None or next(tab for tab in (await tabs(window_id))["tabs"] if tab["id"] == active_id)["active"]
                finally:
                    try:
                        listing = await tabs(window_id)
                        present = {tab["id"] for tab in listing["tabs"]}
                        for tab_id in sorted(test_ids & present):
                            await call("tab_action", {"action": "close", "tab_id": tab_id, "confirmed": True})
                    finally:
                        store = sqlite3.connect(STATE / "browser.sqlite3")
                        rows = store.execute("SELECT id,payload FROM undo WHERE id>?", (undo_before,)).fetchall()
                        for entry_id, payload in rows:
                            value = json.loads(payload)
                            snapshot = value["snapshot"]
                            test_url = any(marker in tab.get("url", "") for tab in snapshot.get("tabs", []))
                            test_tab = bool(test_ids.intersection(snapshot.get("closeIds", []))) or bool(
                                test_ids.intersection(tab.get("id") for tab in snapshot.get("tabs", [])))
                            if test_url or test_tab:
                                store.execute("DELETE FROM undo WHERE id=?", (entry_id,))
                        store.commit()
                        store.close()
                        await client.call_tool("collection_action", {"action": "delete", "name": collection_name, "confirmed": True})
