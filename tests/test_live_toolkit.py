"""Opt-in actual Chrome/MCP checks. Mutations use disposable resources only."""
import base64
import asyncio
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
    await asyncio.sleep(0.7)
    result = await client.call_tool(name, arguments or {})
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)




@pytest.mark.asyncio
async def test_agent_neutral_stdio_launcher_keeps_structured_browser_results():
    from pathlib import Path
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = Path(__file__).resolve().parent.parent
    async with stdio_client(StdioServerParameters(command="node", args=[str(root / "cli.js"), "mcp"], cwd=str(root))) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=180) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert any(tool.name == "group_selections" for tool in tools.tools)
            result = await session.call_tool("browser_status")
            assert not result.is_error
            assert result.structured_content == json.loads(result.content[0].text)
            assert result.structured_content["connected"]




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
            selection = await call(client, "selection_action", {"action": "create", "resource": "bookmarks", "ids": ids})
            grouped = await call(client, "group_selections", {"resource": "bookmarks", "assignments": [{"selection_id": selection["selection_id"], "title": "Python"}], "parent_id": folder}); history += 1
            assert grouped["grouped"] == 2 and grouped["verified"]
            await call(client,"undo_last"); history -= 1
            deleted = await call(client,"bookmark_action",{"action":"delete","ids":[nested],"confirmed":True}); history += 1
            assert deleted["deleted"] == 1
            await call(client,"undo_last"); history -= 1
            recovered = await call(client,"list_resources",{"resource":"bookmarks","query":marker,"limit":100})
            assert len([row for row in recovered["items"] if row["type"] == "bookmark"]) == 2
        finally:
            for _ in range(history): await call(client,"undo_last")
        assert not (await call(client,"list_resources",{"resource":"bookmarks","query":marker}))["items"]
