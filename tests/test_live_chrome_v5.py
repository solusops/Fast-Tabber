"""Real screenshots disambiguate pages with identical extracted text."""

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


def visual_html(kind):
    tree = '<rect x="275" y="300" width="50" height="190" fill="saddlebrown"/><path d="M300 20 L470 340 H130 Z" fill="forestgreen"/><path d="M300 120 L490 410 H110 Z" fill="green"/>'
    chart = '<path d="M80 30 V470 H550" fill="none" stroke="black" stroke-width="6"/><rect x="120" y="300" width="70" height="170" fill="royalblue"/><rect x="230" y="200" width="70" height="270" fill="royalblue"/><rect x="340" y="100" width="70" height="370" fill="royalblue"/><rect x="450" y="40" width="70" height="430" fill="royalblue"/>'
    return ('<!doctype html><title>Visual sample</title><main><h1>Visual sample</h1>'
            '<svg width="600" height="500" viewBox="0 0 600 500">' + (tree if kind.startswith("tree") else chart) +
            '</svg><form><input value="PRIVATE_FORM_SENTINEL"></form></main>').encode()


@pytest.fixture
def visual_pages():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = visual_html(self.path.strip("/"))
            self.send_response(200)
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
async def test_native_screenshots_gemma_groups_and_search(visual_pages):
    connection = json.loads((STATE / "connection.json").read_text(encoding="utf-8-sig"))
    ids = []
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {connection['token']}"}, trust_env=False, timeout=600) as http:
        async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=600) as client:
                await client.initialize()

                async def call(name, args=None):
                    result = await client.call_tool(name, args or {})
                    assert not result.is_error, result.content[0].text
                    return json.loads(result.content[0].text)

                status = await call("browser_status")
                assert status["stage"] == "v5" and status["visualCapture"]
                before = await call("list_tabs", {"limit": 100})
                window = before["tabs"][0]["windowId"]
                try:
                    for kind in ("tree-a", "tree-b", "chart-a", "chart-b"):
                        opened = await call("tab_action", {"action": "open", "url": f"{visual_pages}/{kind}", "window_id": window})
                        ids.append(opened["tab"]["id"])
                    plain = await call("index_tabs", {"tab_ids": ids})
                    assert plain["indexed"] == 4 and plain["visual"] == 0
                    text_only = await call("search_tabs", {"query": "tree", "limit": 100})
                    assert not {tab["id"] for tab in text_only["tabs"]} & set(ids)
                    grouped = await call("organize_by_text", {"tab_ids": ids, "instruction": "Separate tree illustrations from data charts.",
                        "categories": ["Trees", "Data charts"], "visual": True})
                    assert grouped["verified"] and grouped["grouped"] == 4 and grouped["groups"] == 2
                    after = await call("list_tabs", {"window_id": window, "limit": 100})
                    tabs = {tab["id"]: tab for tab in after["tabs"]}
                    groups = {group["id"]: group["title"] for group in after["groups"]}
                    assert [groups[tabs[tab_id]["groupId"]] for tab_id in ids] == ["Trees", "Trees", "Data charts", "Data charts"]
                    assert not any(tabs[tab_id]["active"] for tab_id in ids), "Capture left a disposable screenshot tab active"
                    found = await call("search_tabs", {"query": "tree", "limit": 100})
                    assert set(ids[:2]) <= {tab["id"] for tab in found["tabs"]}
                    private = await call("search_tabs", {"query": "PRIVATE_FORM_SENTINEL", "limit": 100})
                    assert not set(ids) & {tab["id"] for tab in private["tabs"]}
                    await call("undo_last")
                    automatic = await call("organize_by_text", {"tab_ids": ids,
                        "instruction": "Organize these pages by their visible subject.", "visual": True, "mode": "sequential"})
                    with sqlite3.connect(STATE / "browser.sqlite3") as db:
                        evidence = [db.execute("SELECT content FROM pages WHERE tab_id=?", (tab_id,)).fetchone()[0] for tab_id in ids]
                    assert automatic["verified"] and automatic["grouped"] == 4 and automatic["groups"] == 2, evidence
                    await call("undo_last")
                finally:
                    for tab_id in ids:
                        await client.call_tool("tab_action", {"action": "close", "tab_id": tab_id})
                    with sqlite3.connect(STATE / "browser.sqlite3") as db:
                        for tab_id in ids:
                            db.execute("DELETE FROM pages_fts WHERE rowid=?", (tab_id,))
                            db.execute("DELETE FROM pages WHERE tab_id=?", (tab_id,))
                        for entry_id, payload in db.execute("SELECT id,payload FROM undo").fetchall():
                            snapshot = json.loads(payload)["snapshot"]
                            if set(ids) & (set(snapshot.get("closeIds", [])) | {tab["id"] for tab in snapshot.get("tabs", [])}):
                                db.execute("DELETE FROM undo WHERE id=?", (entry_id,))
