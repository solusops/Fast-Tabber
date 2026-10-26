"""Real Chrome first-release acceptance; disposable localhost pages only."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from fast_tabber.server import STATE

pytestmark = pytest.mark.skipif(os.getenv("FAST_TABBER_LIVE_CHROME") != "1", reason="live Chrome is opt-in")


@pytest.mark.asyncio
async def test_cached_text_exact_grouping_bulk_move_close_reopen_and_undo():
    topics = ["Robotic manipulation trajectories and demonstrations", "Robotic control and learning policies",
              "Architecture building facades and concrete structures", "Architecture floor plans and natural light",
              "Cooking recipes and kitchen ingredients", "Cooking bread fermentation and dough"]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            item = int(self.path.strip("/") or 0)
            body = (f"<title>Release note {item}</title><nav>{'Navigation shopping account ' * 500}</nav>"
                    f"<main><article><h1>{topics[item]}</h1><p>{topics[item]}.</p></article></main>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = json.loads((STATE / "connection.json").read_text())
    ids, history = [], 0
    base = f"http://127.0.0.1:{server.server_port}"
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False, timeout=180) as http:
        async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=180) as client:
                await client.initialize()

                async def call(name, **args):
                    result = await client.call_tool(name, args)
                    assert not result.is_error, result.content
                    return result.structured_content or json.loads(result.content[0].text)

                async def index(**args):
                    result = await call("index_resources", resource="tabs", ids=ids, confirm_index=True, **args)
                    while result.get("state") == "running":
                        result = await call("index_resources", resource="tabs", action="status", job_id=result["id"], wait_seconds=2)
                    assert result.get("state") == "complete", result
                    return result

                baseline = await call("list_resources", resource="tabs", limit=100)
                try:
                    for i in range(6):
                        made = await call("tab_action", action="open", url=f"{base}/{i}")
                        ids.append(made["tab"]["id"])
                        history += 1
                    first = await index()
                    assert first["completed"] == 6 and first["failed"] == 0
                    repeat = await index()
                    assert repeat["cached"] == 6 and repeat["completed"] == 0
                    assignments = []
                    for name in ("Robotic", "Architecture", "Cooking"):
                        found = await call("search_resources", resource="tabs", query=name)
                        matches = [row["id"] for row in found["items"] if row["url"].startswith(base)]
                        assert len(matches) == 2, found
                        assert all("Navigation" not in row["snippet"] for row in found["items"] if row["id"] in matches)
                        selected = await call("selection_action", action="create", resource="tabs", ids=matches)
                        assignments.append({"selection_id": selected["selection_id"], "title": name})
                    grouped = await call("group_selections", resource="tabs", assignments=assignments)
                    history += 1
                    assert grouped["verified"]
                    current = await call("list_resources", resource="tabs", ids=ids, limit=100)
                    assert len({row["groupId"] for row in current["items"]}) == 3
                    assert {row["groupTitle"] for row in current["items"]} == {"Robotic", "Architecture", "Cooking"}
                    architecture = next(row["groupId"] for row in current["items"] if row["groupTitle"] == "Architecture")
                    scoped = await call("index_resources", resource="tabs", group_id=architecture)
                    while scoped.get("state") == "running":
                        scoped = await call("index_resources", resource="tabs", action="status", job_id=scoped["id"], wait_seconds=1)
                    assert scoped["total"] == scoped["cached"] == 2
                    await call("undo_last")
                    history -= 1
                    undone = await call("list_resources", resource="tabs", ids=ids, limit=100)
                    assert all(row["groupId"] == -1 for row in undone["items"])
                    selected = await call("selection_action", action="create", resource="tabs", ids=ids)
                    window_id = undone["items"][0]["windowId"]
                    await call("tab_selection", action="move", selection_id=selected["selection_id"], window_id=window_id, index=0)
                    history += 1
                    moved = await call("list_resources", resource="tabs", ids=ids, limit=100)
                    assert sorted(row["index"] for row in moved["items"]) == list(range(6))
                    await call("undo_last")
                    history -= 1
                    await call("tab_selection", action="close", selection_id=selected["selection_id"], confirmed=True)
                    history += 1
                    await call("undo_last")
                    history -= 1
                    reopened = await call("list_resources", resource="tabs", query=base, limit=100)
                    ids[:] = [int(row["id"]) for row in reopened["items"]]
                    assert len(ids) == 6
                    assert (await index())["cached"] == 6
                    print("Chrome verified: 6 text pages; 6 repeat cache hits; 3 exact groups; scoped cache; bulk move/close and undo; reopened URL cache.")
                finally:
                    # Opening each test tab is also recorded, so unwind the remaining disposable history.
                    for _ in range(history):
                        await call("undo_last")
                    final = await call("list_resources", resource="tabs", limit=100)
                    assert final["total"] == baseline["total"]
                    assert not any(row.get("url", "").startswith(base) for row in final["items"])
    server.shutdown()
    server.server_close()
