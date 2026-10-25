"""Disposable local-page V3 checks; run with FAST_TABBER_LIVE_CHROME=1 and paired Chrome."""

import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from fast_tabber.server import STATE


pytestmark = pytest.mark.skipif(os.getenv("FAST_TABBER_LIVE_CHROME") != "1", reason="live Chrome is opt-in")


@pytest.fixture
def local_pages():
    pages = {
        "/robotics": "<title>Reading page</title><main><h1>Learning control</h1><p>Diffusion policies learn robotic manipulation from demonstrations and visual feedback.</p></main>",
        "/admissions": "<title>Reading page</title><main><h1>Higher education</h1><p>University admissions and undergraduate applications require essays and recommendation letters.</p></main>",
        "/robotics-related": "<title>Reading page</title><main><h1>Robot systems</h1><p>Robot control through visual feedback and demonstrations.</p></main>",
        "/admissions-related": "<title>Reading page</title><main><h1>College entry</h1><p>College entry requires essays and applications.</p></main>",
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path, "").encode()
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    web = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{web.server_port}"
    finally:
        web.shutdown()
        web.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["batch", "sequential"])
async def test_v4_body_semantic_search_and_gemma_groups(local_pages, mode):
    connection = json.loads((STATE / "connection.json").read_text(encoding="utf-8-sig"))
    ids = []
    undo_db = sqlite3.connect(STATE / "browser.sqlite3")
    undo_before = undo_db.execute("SELECT coalesce(max(id),0) FROM undo").fetchone()[0]
    undo_db.close()
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {connection['token']}"}, trust_env=False, timeout=180) as http:
        async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()

                async def call(name, arguments=None):
                    result = await client.call_tool(name, arguments or {})
                    if result.is_error:
                        raise AssertionError(result.content[0].text)
                    return json.loads(result.content[0].text)

                try:
                    status = await call("browser_status")
                    assert status.get("stage") in {"v3", "v4", "v5"} and "extract_tabs" in status["operations"], \
                        "Reload Fast-Tabber in Chrome and grant ordinary-site access"
                    listing = await call("list_tabs", {"limit": 1})
                    window_id = listing["tabs"][0]["windowId"]
                    for path in ("/robotics", "/admissions", "/robotics-related", "/admissions-related"):
                        opened = await call("tab_action", {"action": "open",
                            "url": f"{local_pages}{path}", "window_id": window_id})
                        ids.append(opened["tab"]["id"])
                    indexed = await call("index_tabs", {"tab_ids": ids})
                    assert indexed["indexed"] == 4 and indexed["skipped"] == 0 and 0 <= indexed["embedded"] <= 4
                    found = await call("search_tabs", {"query": "diffusion manipulation demonstrations"})
                    assert len(found["tabs"]) == 1 and found["tabs"][0]["id"] == ids[0]
                    assert "Diffusion policies" in found["tabs"][0]["snippet"]
                    assert "127.0.0.1" in found["tabs"][0]["origin"]
                    keyword = await call("search_tabs", {"query": "college application"})
                    semantic = await call("search_tabs", {"query": "college application", "mode": "semantic"})
                    relevant = {ids[1], ids[3]}
                    assert not keyword["tabs"] and semantic["tabs"][0]["id"] in relevant
                    assert len({tab["id"] for tab in semantic["tabs"][:4]} & relevant) == 2
                    grouped = await call("organize_by_text", {
                        "instruction": "Separate robotics research from university admissions.",
                        "tab_ids": ids, "categories": ["Robotics", "Admissions"], "mode": mode})
                    assert grouped["grouped"] == 4 and grouped["groups"] == 2 and grouped["verified"]
                    after = await call("list_tabs", {"limit": 100})
                    by_id = {tab["id"]: tab for tab in after["tabs"]}
                    assert all(by_id[tab_id]["groupId"] >= 0 for tab_id in ids)
                    await call("undo_last")
                    after = await call("list_tabs", {"limit": 100})
                    by_id = {tab["id"]: tab for tab in after["tabs"]}
                    assert all(by_id[tab_id]["groupId"] == -1 for tab_id in ids)
                    automatic = await call("organize_by_text", {
                        "instruction": "Organize these pages by subject.", "tab_ids": ids})
                    assert automatic["grouped"] == 4 and automatic["groups"] == 2 and automatic["verified"]
                    await call("undo_last")
                finally:
                    for tab_id in ids:
                        await client.call_tool("tab_action", {"action": "close", "tab_id": tab_id})
                    store = sqlite3.connect(STATE / "browser.sqlite3")
                    store.execute("DELETE FROM pages_fts WHERE rowid IN (%s)" % ",".join("?" for _ in ids), ids) if ids else None
                    store.execute("DELETE FROM pages WHERE tab_id IN (%s)" % ",".join("?" for _ in ids), ids) if ids else None
                    rows = store.execute("SELECT id,payload FROM undo WHERE id>?", (undo_before,)).fetchall()
                    for entry_id, payload in rows:
                        snapshot = json.loads(payload)["snapshot"]
                        if set(ids) & (set(snapshot.get("closeIds", [])) | {tab["id"] for tab in snapshot.get("tabs", [])}):
                            store.execute("DELETE FROM undo WHERE id=?", (entry_id,))
                    store.commit()
                    store.close()
