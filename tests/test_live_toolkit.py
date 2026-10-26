"""Opt-in actual Chrome/MCP checks. Mutations use disposable resources only."""
import base64
import json
import os
import secrets
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from fast_tabber.server import STATE

pytestmark = pytest.mark.skipif(os.getenv("FAST_TABBER_LIVE_TOOLKIT") != "1", reason="actual Chrome tests require explicit opt-in")


@asynccontextmanager
async def connected():
    connection = json.loads((STATE / "connection.json").read_text(encoding="utf-8-sig"))
    async with httpx2.AsyncClient(headers={"Authorization":"Bearer "+connection["token"]}, trust_env=False, timeout=600) as http:
        async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=600) as client:
                await client.initialize()
                yield client


async def call(client, name, arguments=None):
    result = await client.call_tool(name, arguments or {})
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_actual_selected_image_cache_export_and_native_group_undo():
    async with connected() as client:
        baseline = await call(client,"browser_status")
        ids = []
        history = 0
        try:
            for url in ("https://docs.python.org/3/", "https://docs.python.org/3/library/asyncio.html"):
                opened = await call(client,"tab_action",{"action":"open","url":url})
                ids.append(str(opened["tab"]["id"])); history += 1
            await call(client,"onboarding",{"action":"acknowledge_privacy","confirm":True})
            indexed = await call(client,"index_tabs",{"tab_ids":list(map(int,ids)),"confirm_index":True})
            assert indexed["total"] == 2
            reused = await call(client,"index_tabs",{"tab_ids":list(map(int,ids))})
            assert reused["reused"] == 2 and reused["embeddingInputs"] == 0
            grouped = await call(client,"group_resources",{"resource":"tabs","ids":ids,"max_groups":1,"mode":"fast"})
            history += 1
            assert grouped["verified"] and grouped["grouped"] == 2
            assert (await call(client,"browser_status"))["tabCount"] == baseline["tabCount"] + 2
            await call(client,"undo_last"); history -= 1
            job = await call(client,"index_resources",{"resource":"tabs","ids":[ids[0]],"level":"image"})
            for _ in range(30):
                job = await call(client,"index_resources",{"resource":"tabs","action":"status","job_id":job["id"],"wait_seconds":3})
                assert job["state"] != "failed",job
                if job["state"] == "complete": break
            assert job["state"] == "complete",job
            evidence = await client.call_tool("inspect_page",{"resource":"tabs","id":ids[0],"detail":"image"})
            assert not evidence.is_error
            image = next(item for item in evidence.content if item.type == "image")
            assert base64.b64decode(image.data).startswith(b"\xff\xd8\xff")
            assert (await call(client,"export_urls",{"resource":"tabs","ids":ids,"format":"csv"}))["count"] == 2
            assert (await call(client,"export_urls",{"resource":"tabs","tab_id_for_links":int(ids[0])}))["count"] > 5
        finally:
            for _ in range(history): await call(client,"undo_last")
            assert (await call(client,"browser_status"))["tabCount"] == baseline["tabCount"]


@pytest.mark.asyncio
async def test_actual_disposable_bookmark_operations_and_undo():
    async with connected() as client:
        status = await call(client,"browser_status")
        assert "bookmarks" in status["permissions"], "Enable optional bookmarks access in the Fast-Tabber popup first"
        listing = await call(client,"list_resources",{"resource":"bookmarks","limit":100})
        parent = next(row["id"] for row in listing["items"] if row.get("parentId") == "0" and not row["managed"])
        history = 0
        marker = "Fast-Tabber disposable " + secrets.token_hex(4)
        try:
            folder = (await call(client,"bookmark_action",{"action":"create","parent_id":parent,"title":marker}))["ids"][0]; history += 1
            nested = (await call(client,"bookmark_action",{"action":"create","parent_id":folder,"title":marker+" nested"}))["ids"][0]; history += 1
            ids = []
            for target in (folder, nested):
                made = await call(client,"bookmark_action",{"action":"create","parent_id":target,"title":marker+" Python","url":"https://docs.python.org/3/"})
                ids.append(made["ids"][0]); history += 1
            assert (await call(client,"export_urls",{"resource":"bookmarks","ids":[folder]}))["count"] == 2
            moved = await call(client,"bookmark_action",{"action":"move","ids":[ids[0]],"parent_id":nested,"index":0}); history += 1
            assert moved["moved"] == 1
            await call(client,"bookmark_action",{"action":"rename","ids":[ids[0]],"title":marker+" renamed"}); history += 1
            grouped = await call(client,"group_resources",{"resource":"bookmarks","ids":ids,"max_groups":1,"parent_id":folder,"confirm_index":True}); history += 1
            assert grouped["grouped"] == 2 and grouped["verified"]
            await call(client,"undo_last"); history -= 1
            deleted = await call(client,"bookmark_action",{"action":"delete","ids":[nested],"warned":True}); history += 1
            assert deleted["deleted"] == 1
            await call(client,"undo_last"); history -= 1
            recovered = await call(client,"list_resources",{"resource":"bookmarks","query":marker,"limit":100})
            assert len([row for row in recovered["items"] if row["type"] == "bookmark"]) == 2
        finally:
            for _ in range(history): await call(client,"undo_last")
        assert not (await call(client,"list_resources",{"resource":"bookmarks","query":marker}))["items"]
