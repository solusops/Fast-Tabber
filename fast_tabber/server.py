"""Authenticated local MCP and Chrome extension bridge."""

import asyncio
import hmac
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
import httpx
from functools import wraps
from typing import Annotated, Any, Literal
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import JSONResponse, Response
from starlette.routing import WebSocketRoute, Route
from starlette.websockets import WebSocket, WebSocketDisconnect
from fast_tabber.index import ContentIndex
from fast_tabber.library import Library, register_library_tools
from fast_tabber.onboarding import Onboarding
from fast_tabber.pairing import Pairing

STATE = Path(__file__).resolve().parent.parent / ".state"


class StateStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS collections (name TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS undo (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ready')")
            self.db.execute("CREATE TABLE IF NOT EXISTS catalogue_jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS pages (tab_id INTEGER PRIMARY KEY, window_id INTEGER NOT NULL, url_hash TEXT NOT NULL, title TEXT NOT NULL, origin TEXT NOT NULL, headings TEXT NOT NULL, content TEXT NOT NULL)")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(pages)")}
            if "embedding" not in columns:
                self.db.execute("ALTER TABLE pages ADD COLUMN embedding BLOB")
            if "embedding_digest" not in columns:
                self.db.execute("ALTER TABLE pages ADD COLUMN embedding_digest TEXT")
            for column in ("url", "document_id"):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE pages ADD COLUMN {column} TEXT")
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(title, origin, headings, content, tokenize='unicode61')")
            if "status" not in {row[1] for row in self.db.execute("PRAGMA table_info(undo)")}:
                self.db.execute("ALTER TABLE undo ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'")
        self.index = ContentIndex(self.db, self.lock)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS resources (kind TEXT,id TEXT,metadata TEXT,fingerprint TEXT,document TEXT,level INTEGER,vector BLOB,model TEXT,PRIMARY KEY(kind,id))")
            self.db.execute("CREATE TABLE IF NOT EXISTS grouping_plans (id TEXT PRIMARY KEY,payload TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS selections (id TEXT PRIMARY KEY,kind TEXT NOT NULL,items TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS resources_fts USING fts5(kind UNINDEXED,id UNINDEXED,title,url,content)")

    def local_data(self, scope, delete=False):
        tables = ["pages_fts", "pages", "document_artifacts", "documents", "artifacts", "catalogue_jobs", "resources_fts", "resources", "grouping_plans", "selections"]
        if scope == "all":
            tables += ["collections", "undo"]
        backups = list(self.path.parent.glob("browser-before-*.sqlite3"))
        with self.lock:
            counts = {table: self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
            report = {"scope": scope, "records": counts, "indexBackups": [path.name for path in backups[:20]], "backupCount": len(backups),
                      "preservesRecoveryHistory": scope == "cache", "deleted": False}
            if not delete:
                return {**report, "requiresConfirmation": True, "nextAction": "ask_user", "waitingFor": "user"}
            self.db.execute("PRAGMA secure_delete=ON")
            exports = self.path.parent / "exports"
            if exports.exists():
                for export in exports.iterdir():
                    if export.is_file() and export.suffix in {".txt", ".csv"}:
                        export.unlink()
            with self.db:
                for table in tables:
                    self.db.execute(f"DELETE FROM {table}")
            if self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise RuntimeError("Cache rows were removed, but another database reader prevented file compaction; retry cleanup")
            self.db.execute("VACUUM")
            for path in backups:
                if path.resolve().parent != self.path.parent or path.resolve() == self.path:
                    raise RuntimeError("A backup path is outside the managed local data directory")
                path.unlink()
            return {**report, "deleted": True, "requiresConfirmation": False}

    def save_job(self, job):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO catalogue_jobs VALUES (?,?)", (job["id"], json.dumps(job)))

    def get_job(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT payload FROM catalogue_jobs WHERE id=?", (job_id,)).fetchone() if job_id else self.db.execute("SELECT payload FROM catalogue_jobs ORDER BY rowid DESC LIMIT 1").fetchone()
        if row is None:
            raise ValueError("Catalogue job not found")
        return json.loads(row[0])

    def save_collection(self, name: str, payload: dict):
        try:
            with self.lock, self.db:
                self.db.execute("INSERT INTO collections VALUES (?, ?)", (name, json.dumps(payload)))
        except sqlite3.IntegrityError as error:
            raise ValueError(f"Collection already exists: {name}") from error

    def get_collection(self, name: str):
        with self.lock:
            row = self.db.execute("SELECT payload FROM collections WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def update_collection(self, name: str, payload: dict):
        with self.lock, self.db:
            self.db.execute("UPDATE collections SET payload=? WHERE name=?", (json.dumps(payload), name))

    def delete_collection(self, name: str):
        with self.lock, self.db:
            return self.db.execute("DELETE FROM collections WHERE name=?", (name,)).rowcount == 1

    def list_collections(self):
        with self.lock:
            rows = self.db.execute("SELECT name, payload FROM collections ORDER BY name").fetchall()
        return [{"name": name, "tabCount": len(json.loads(payload)["tabs"])} for name, payload in rows]

    def push_undo(self, snapshot: dict, status="ready"):
        with self.lock, self.db:
            return self.db.execute("INSERT INTO undo (payload,status) VALUES (?,?)", (json.dumps(snapshot), status)).lastrowid

    def finish_undo(self, entry_id, payload, status="ready"):
        with self.lock, self.db:
            self.db.execute("UPDATE undo SET payload=?,status=? WHERE id=?", (json.dumps(payload), status, entry_id))

    def mark_undone(self, entry_id):
        with self.lock, self.db:
            self.db.execute("UPDATE undo SET status='undone' WHERE id=?", (entry_id,))

    def remap_history(self, ids):
        if not ids:
            return
        def remap(snapshot):
            if snapshot.get("activation"):
                previous = snapshot["activation"]["tabId"]
                snapshot["activation"]["tabId"] = ids.get(str(previous), previous)
            for tab in snapshot.get("tabs", []):
                tab["id"] = ids.get(str(tab["id"]), tab["id"])
            if snapshot.get("closeIds"):
                snapshot["closeIds"] = [ids.get(str(tab), tab) for tab in snapshot["closeIds"]]
        with self.lock, self.db:
            for entry_id, payload in self.db.execute("SELECT id,payload FROM undo WHERE status!='undone'").fetchall():
                value = json.loads(payload)
                remap(value.get("snapshot", {}))
                self.db.execute("UPDATE undo SET payload=? WHERE id=?", (json.dumps(value), entry_id))
            for name, payload in self.db.execute("SELECT name,payload FROM collections").fetchall():
                value = json.loads(payload)
                remap(value)
                self.db.execute("UPDATE collections SET payload=? WHERE name=?", (json.dumps(value), name))
            for old, new in ids.items():
                if int(old) != new:
                    self.db.execute("UPDATE pages SET tab_id=? WHERE tab_id=?", (new, int(old)))
                    self.db.execute("UPDATE pages_fts SET rowid=? WHERE rowid=?", (new, int(old)))

    def latest_undo(self):
        with self.lock:
            row = self.db.execute("SELECT id, payload, status FROM undo WHERE status!='undone' ORDER BY id DESC LIMIT 1").fetchone()
        return (row[0], json.loads(row[1]), row[2]) if row else None

    def claim_undo(self, entry_id: int):
        with self.lock, self.db:
            return self.db.execute("UPDATE undo SET status='applying' WHERE id=? AND status IN ('ready','failed','executing')", (entry_id,)).rowcount == 1

    def reset_undo(self, entry_id: int):
        with self.lock, self.db:
            self.db.execute("UPDATE undo SET status='ready' WHERE id=? AND status='applying'", (entry_id,))

    def fail_undo(self, entry_id: int):
        with self.lock, self.db:
            self.db.execute("UPDATE undo SET status='failed' WHERE id=? AND status='applying'", (entry_id,))

    def remove_undo(self, entry_id: int):
        with self.lock, self.db:
            self.db.execute("DELETE FROM undo WHERE id=?", (entry_id,))

    def save_pages(self, pages: list[dict]):
        with self.lock, self.db:
            for page in pages:
                tab_id = page["id"]
                parsed = urlsplit(page["url"])
                host = f"[{parsed.hostname}]" if ":" in (parsed.hostname or "") else parsed.hostname
                origin = f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")
                headings, content = " ".join(page["headings"]), page["text"]
                digest = hashlib.sha256(json.dumps([page["title"], headings, content], ensure_ascii=False).encode()).hexdigest()
                self.db.execute("INSERT INTO pages (tab_id,window_id,url_hash,title,origin,headings,content) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(tab_id) DO UPDATE SET window_id=excluded.window_id,url_hash=excluded.url_hash,title=excluded.title,origin=excluded.origin,headings=excluded.headings,content=excluded.content",
                    (tab_id, page["windowId"], digest, page["title"][:200], origin, headings, content))
                self.db.execute("DELETE FROM pages_fts WHERE rowid=?", (tab_id,))
                self.db.execute("INSERT INTO pages_fts(rowid,title,origin,headings,content) VALUES (?,?,?,?,?)",
                    (tab_id, page["title"][:200], origin, headings, content))

    def remove_pages(self, tab_ids: list[int]):
        with self.lock, self.db:
            self.db.executemany("DELETE FROM pages_fts WHERE rowid=?", ((tab_id,) for tab_id in tab_ids))
            self.db.executemany("DELETE FROM pages WHERE tab_id=?", ((tab_id,) for tab_id in tab_ids))

    def search_pages(self, query: str, offset: int, limit: int):
        terms = re.findall(r"[\w]+", query, flags=re.UNICODE)
        if not terms:
            raise ValueError("query must contain searchable words")
        match = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms[:20])
        with self.lock:
            rows = self.db.execute("SELECT p.tab_id, p.window_id, p.title, p.origin, snippet(pages_fts, 3, '', '', ' â€¦ ', 24) FROM pages_fts JOIN pages p ON p.tab_id=pages_fts.rowid WHERE pages_fts MATCH ? ORDER BY bm25(pages_fts) LIMIT ? OFFSET ?",
                                   (match, limit, offset)).fetchall()
        return [{"id": row[0], "windowId": row[1], "title": row[2], "origin": row[3], "snippet": row[4][:240]} for row in rows]

    def count_pages(self, query: str):
        terms = re.findall(r"[\w]+", query, flags=re.UNICODE)
        if not terms:
            raise ValueError("query must contain searchable words")
        match = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms[:20])
        with self.lock:
            return self.db.execute("SELECT count(*) FROM pages_fts WHERE pages_fts MATCH ?", (match,)).fetchone()[0]


class Bridge:
    def __init__(self):
        self.peer: WebSocket | None = None
        self.extension_version = None
        self.pending: dict[str, asyncio.Future] = {}
        self.lock = asyncio.Lock()
        self.send_lock = asyncio.Lock()

    async def call(self, operation: str, arguments: dict | None = None, timeout: float = 15):
        if operation not in {"probe", "list_tabs", "tab_action", "group_action", "move_tabs", "close_tabs",
                             "close_saved_collection", "restore_snapshot", "capture_tabs", "find_duplicates",
                             "restore_collection", "extract_tabs", "filter_tabs", "group_categories", "capture_page", "capture_browser", "reload_extension", "bookmark_tree", "bookmark_action", "restore_bookmarks", "inspect_page"}:
            raise ValueError("Browser operation is not enabled")
        if operation == "extract_tabs":
            timeout = max(timeout, 180)
        if operation in {"bookmark_action", "restore_bookmarks", "group_categories", "group_action", "move_tabs"}:
            timeout = max(timeout, 300)
        if operation == "inspect_page":
            timeout = max(timeout, 120 if (arguments or {}).get("detail") == "image" else 35)
        if operation == "capture_page":
            timeout = max(timeout, 60)
        if operation in {"capture_browser", "restore_snapshot"}:
            timeout = max(timeout, 180)
        async def perform():
            if self.peer is None:
                raise RuntimeError("Chrome extension is disconnected. Open the paired Chrome profile if it is closed; "
                                   "allow up to 30 seconds for reconnection after a service restart. If already enabled, reload Fast-Tabber in chrome://extensions. "
                                   f"First installation only: load unpacked {STATE.parent / 'extension'} and approve Chrome permissions.")
            request_id = secrets.token_hex(12)
            future = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            try:
                async with self.send_lock:
                    if self.peer is None:
                        raise RuntimeError("Chrome disconnected before request dispatch")
                    await self.peer.send_json({"id": request_id, "operation": operation, "arguments": arguments or {}})
                return await asyncio.wait_for(future, timeout)
            except (TimeoutError, WebSocketDisconnect, OSError) as error:
                raise RuntimeError("Chrome request failed; outcome unknown. Reconcile browser state before retrying.") from error
            finally:
                self.pending.pop(request_id, None)
        if operation == "inspect_page" and (arguments or {}).get("detail") == "text":
            return await perform()
        async with self.lock:
            return await perform()

    async def connect(self, socket: WebSocket, token: str, origin: str, pairing=None, legacy_token=None):
        if socket.headers.get("origin") != origin or self.peer is not None:
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            auth = await asyncio.wait_for(socket.receive_json(), 5)
            supplied = auth.get("token", "")
            valid = isinstance(supplied, str) and (hmac.compare_digest(supplied, token) or
                    legacy_token is not None and hmac.compare_digest(supplied, legacy_token))
            if not valid and pairing is not None:
                valid = pairing.consume(auth.get("pairingCode"))
            if not valid:
                await socket.close(code=1008)
                return
            if self.peer is not None:
                await socket.close(code=1008)
                return
            self.peer = socket
            self.extension_version = auth.get("version") if isinstance(auth.get("version"), str) else None
            await socket.send_json({"type": "paired", **({"token": token} if pairing is not None else {})})
            while True:
                message = await socket.receive_json()
                if message.get("type") == "ping":
                    await socket.send_json({"type": "pong"})
                    continue
                future = self.pending.get(message.get("id"))
                if future is None or future.done():
                    continue
                if message.get("error"):
                    future.set_exception(RuntimeError(str(message["error"])[:500]))
                elif isinstance(message.get("result"), dict):
                    future.set_result(message["result"])
                else:
                    future.set_exception(RuntimeError("Invalid Chrome response; no success was verified"))
        except (WebSocketDisconnect, TimeoutError, ValueError, TypeError):
            pass
        finally:
            if self.peer is socket:
                self.peer = None
                self.extension_version = None
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(RuntimeError("Chrome disconnected; operation outcome unknown"))


class LocalAuthentication:
    def __init__(self, app, token):
        self.app = app
        self.token = token
        self.requests = deque()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            provided = headers.get(b"authorization", b"")
            expected = f"Bearer {self.token}".encode()
            origin = headers.get(b"origin")
            if scope["path"] in {"/setup", "/setup.js", "/setup.css"} and scope["method"] == "GET":
                if headers.get(b"host") != b"127.0.0.1:8765" or origin and origin != b"http://127.0.0.1:8765":
                    await JSONResponse({"error": "Untrusted setup origin"}, status_code=403)(scope, receive, send)
                    return
                await self.app(scope, receive, send)
                return
            if not hmac.compare_digest(provided, expected):
                await JSONResponse({"error": "Authentication required"}, status_code=401)(scope, receive, send)
                return
            if origin and origin not in {b"http://127.0.0.1:8765", b"http://localhost:8765"}:
                await JSONResponse({"error": "Untrusted origin"}, status_code=403)(scope, receive, send)
                return
            if scope["path"] == "/mcp" and scope["method"] == "POST":
                now = time.monotonic()
                while self.requests and now - self.requests[0] >= 60:
                    self.requests.popleft()
                if len(self.requests) >= 120:
                    await JSONResponse({"error": "MCP request rate exceeded; wait before retrying"}, status_code=429,
                                       headers={"Retry-After": "60"})(scope, receive, send)
                    return
                self.requests.append(now)
        await self.app(scope, receive, send)


def create_app(token: str, origin: str, database: Path | None = None):
    bridge = Bridge()
    pairing = Pairing(token)
    store = StateStore(database or (STATE / "browser.sqlite3"))
    operation_lock = asyncio.Lock()
    processing_lock = asyncio.Lock()
    catalogue_tasks = {}
    setup = Onboarding(store.path.parent, bridge, lambda: processing_lock.locked() or operation_lock.locked()
                       or any(not task.done() for task in catalogue_tasks.values()), processing_lock)
    def serialized(lock):
        def decorate(function):
            @wraps(function)
            async def run(*args, **kwargs):
                async with lock:
                    return await function(*args, **kwargs)
            return run
        return decorate
    serialized_processing = serialized(processing_lock)
    mcp = MCPServer("fast-tabber", version="1.0.0", instructions="Ask the user through the agent's native question tool for ANY unclear decision; reuse clear instructions and previous answers. Explain indexing, obtain missing consent and confirm closing/deletion. Follow nextAction/nextCall: pending work is not failure, ask_user waits for a reply, stop resolves an error. Announce waits and report actual progress. Never replay an uncertain mutation.")

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=False, open_world_hint=True))
    async def onboarding(action: Literal["status", "browser", "acknowledge_privacy"] = "status",
                         confirm: bool = False,
                         wait_seconds: Annotated[int, Field(ge=0, le=30)] = 0) -> dict[str, Any]:
        """First-run privacy disclosure and live Chrome pairing. No inference runtime or models are required. Recheck Chrome after the user's setup reply."""
        if action == "browser":
            try:
                status = await bridge.call("probe")
                return {"connected": True, "verified": True, "extensionVersion": status["extensionVersion"],
                        "tabCount": status["tabCount"], "nextStep": "Browser pairing confirmed; continue the user's request."}
            except RuntimeError as error:
                return {"connected": False, "verified": False, "error": str(error),
                        "extensionFolder": str(STATE.parent / "extension"),
                        "steps": ["Open your normal Chrome profile and install/enable Fast-Tabber",
                                  "Load extensionFolder unpacked at chrome://extensions",
                                  "Approve any requested permissions", "Call browser_setup(action='pair'), open setupUrl in Chrome and click Connect Fast-Tabber",
                                  "Reply in this agent chat when ready; the agent must call onboarding(action='browser') again"],
                        "awaitingUserReply": True, "nextAction": "ask_user", "waitingFor": "user",
                        "afterUserReply": {"tool": "onboarding", "arguments": {"action": "browser"}},
                        "nextStep": "Explain the connection/setup issue, ask the user to complete the steps, then wait for their reply before afterUserReply. The MCP service is responding; Chrome is not connected."}
        return await setup.call(action, confirm=confirm, wait_seconds=wait_seconds)

    async def mutate(operation: str, arguments: dict, collection_name: str | None = None):
        async with processing_lock, operation_lock:
            latest = store.latest_undo()
            if latest and latest[2] in {"executing", "failed", "applying"}:
                raise RuntimeError("A previous mutation needs recovery; use undo_last before further changes")
            if operation == "bookmark_action":
                await bridge.call("bookmark_action", {**arguments, "validateOnly": True})
            baseline = ({"bookmarkBefore": (await bridge.call("bookmark_tree"))["nodes"]}
                        if operation == "bookmark_action" and arguments.get("action") != "open" else await bridge.call("capture_browser"))
            provisional = {"operation": operation, "snapshot": baseline, "collection": collection_name,
                           "intent": arguments, "provisional": True}
            entry_id = store.push_undo(provisional, "executing")
            try:
                result = await bridge.call(operation, arguments)
            except Exception as error:
                provisional["error"] = str(error)
                store.finish_undo(entry_id, provisional, "undone" if str(error).endswith("previous bookmarks restored") else "failed")
                raise
            before = result.pop("before", None)
            close_ids = result.pop("closeIds", None)
            if close_ids or (before and (before.get("tabs") or before.get("closeIds") or before.get("activation") or before.get("bookmarkBefore"))):
                snapshot = before or {"tabs": [], "groups": {}}
                if close_ids:
                    snapshot["closeIds"] = close_ids
                try:
                    store.finish_undo(entry_id, {"operation": operation, "snapshot": snapshot, "collection": collection_name})
                except sqlite3.Error as error:
                    try:
                        await bridge.call("restore_snapshot", snapshot)
                    except Exception as rollback_error:
                        raise RuntimeError(f"Undo history could not be saved ({error}); browser rollback incomplete ({rollback_error})") from error
                    raise RuntimeError(f"Undo history could not be saved; browser change rolled back ({error})") from error
            else:
                store.mark_undone(entry_id)
            return result


    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def browser_status() -> dict[str, Any]:
        """Verify the real Chrome connection and permissions without returning page content."""
        try:
            return await bridge.call("probe")
        except (RuntimeError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=False, open_world_hint=False))
    async def browser_setup(action: Literal["reload_extension", "pair"] = "reload_extension") -> dict[str, Any]:
        """Create a five-minute Chrome setup link, or reload a paired extension. Announce reload/reconnection before calling; it may wait up to 30 seconds. Manual code is recovery only."""
        if action == "pair":
            return {**pairing.issue(), "extensionOrigin": origin, "nextAction": "ask_user", "waitingFor": "user",
                    "afterUserReply": {"tool": "onboarding", "arguments": {"action": "browser"}},
                    "nextStep": "Give the user the setup link, wait for their reply, then execute afterUserReply to verify the connection."}
        if action != "reload_extension":
            raise ToolError("action must be reload_extension")
        async with processing_lock, operation_lock:
            await bridge.call("reload_extension")
            for _ in range(30):
                await asyncio.sleep(1)
                try:
                    status = await bridge.call("probe", timeout=2)
                    expected = json.loads((STATE.parent / "extension" / "manifest.json").read_text())["version"]
                    if status["extensionVersion"] == expected:
                        return {"reloaded": True, "extensionVersion": expected, "verified": True}
                except RuntimeError:
                    pass
            raise ToolError("Extension reload did not reconnect with the installed version; inspect Chrome permissions")

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def list_tabs(window_id: int | None = None, offset: Annotated[int, Field(ge=0)] = 0,
                        limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict[str, Any]:
        """List compact tab metadata. Returns at most 100 tabs; use offset to page."""
        if offset < 0 or not 1 <= limit <= 100:
            raise ToolError("offset must be nonnegative and limit must be between 1 and 100")
        try:
            result = await bridge.call("list_tabs", {"windowId": window_id, "offset": offset, "limit": limit})
            end = offset + len(result["tabs"])
            return {**result, "returned": len(result["tabs"]), "hasMore": end < result["total"], "nextOffset": end if end < result["total"] else None}
        except (RuntimeError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def tab_action(action: Literal["open", "close", "activate", "move"], tab_id: int | None = None, url: str | None = None,
                         window_id: int | None = None, index: int | None = None, confirmed: bool = False) -> dict[str, Any]:
        """Open, close, activate, or move a tab. Before close, ask the user through the agent's native confirmation tool and set confirmed=true only after their answer."""
        try:
            if action not in {"open", "close", "activate", "move"}:
                raise ValueError("action must be open, close, activate, or move")
            if action == "open":
                parsed = urlsplit(url or "")
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("open requires an http or https URL with a host and no embedded credentials")
            if action != "open" and tab_id is None:
                raise ValueError(f"{action} requires tab_id")
            if action == "move" and window_id is None:
                raise ValueError("move requires window_id")
            if action == "close" and not confirmed:
                return {"requiresConfirmation": True, "question": "Close the selected tab? Unsaved page state may be lost.",
                        "executed": False, "noChangesMade": True, "tabId": tab_id,
                        "nextAction": "ask_user", "waitingFor": "user",
                        "nextStep": "Ask through the agent's native confirmation/question tool (or chat) and wait. Set confirmed=true only after the user's confirmation."}
            return await mutate("tab_action", {"action": action, "tabId": tab_id, "url": url,
                                                "windowId": window_id, "index": index})
        except (RuntimeError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=False, open_world_hint=False))
    async def organize_tabs(action: Literal["create", "add", "remove", "ungroup_all", "update", "delete", "move"], tab_ids: list[int] | None = None, group_id: int | None = None,
                            title: str | None = None, color: str | None = None, collapsed: bool | None = None,
                            window_id: int | None = None, index: int | None = None, mode: str = "batch") -> dict[str, Any]:
        """Native group edits. ungroup_all resolves all grouped tabs locally (optional window_id); no IDs needed. remove+tab_ids ungroups selected tabs; remove+group_id or delete+group_id ungroups one whole group. These reversible edits never close tabs, delete bookmarks, or require a second confirmation when explicitly requested. Indexing does not change groups. Use local selections for exact agent-decided assignments."""
        try:
            if mode not in {"batch", "sequential"}:
                raise ValueError("mode must be batch or sequential")
            ids = tab_ids or []
            ungroup_all = action == "ungroup_all"
            if ungroup_all:
                if tab_ids is not None or group_id is not None:
                    raise ValueError("ungroup_all accepts an optional window_id, not tab_ids or group_id")
                snapshot = await bridge.call("capture_browser")
                ids = [tab["id"] for tab in snapshot["tabs"] if tab.get("groupId", -1) >= 0 and not tab.get("incognito")
                       and (window_id is None or tab["windowId"] == window_id)]
                if not ids:
                    return {"ungrouped": 0, "verified": True, "noChangesMade": True}
                action = "remove"
            if action == "remove" and not ids and group_id is not None:
                action = "delete"
            limit = 5000 if ungroup_all else 500
            if len(ids) > limit or len(set(ids)) != len(ids):
                raise ValueError(f"tab_ids must be unique and contain at most {limit} IDs")
            if action in {"create", "add", "remove", "move"} and not ids:
                raise ValueError(f"No browser changes made: {action} requires tab_ids. To ungroup everything use action='ungroup_all'; for one group use action='remove' with group_id. Tabs stay open.")
            if action in {"add", "update", "delete"} and group_id is None:
                raise ValueError(f"{action} requires group_id")
            if action == "update" and title is None and color is None and collapsed is None:
                raise ValueError("update requires title, color, or collapsed")
            if action == "move":
                if window_id is None:
                    raise ValueError("move requires window_id")
                result = await mutate("move_tabs", {"tabIds": ids, "windowId": window_id, "index": index, "mode": mode})
            else:
                arguments = {"action": action, "tabIds": ids, "groupId": group_id,
                    "title": title, "color": color, "collapsed": collapsed, "mode": mode}
                result = await mutate("group_action", {key: value for key, value in arguments.items() if value is not None})
            return {key: value for key, value in result.items() if key != "before"} | ({"ungrouped": len(ids), "verified": True} if ungroup_all else {})
        except (RuntimeError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def collection_action(action: Literal["save", "list", "close", "restore", "delete"], name: str = "", tab_ids: list[int] | None = None,
                                mode: str = "batch", confirmed: bool = False) -> dict[str, Any]:
        """Save/list/restore collections. Closing collection tabs or deleting saved collection data requires the user's confirmation of the named scope before confirmed=true."""
        try:
            if action == "list":
                return {"collections": store.list_collections()}
            if not name or len(name) > 80:
                raise ValueError("name must contain 1 to 80 characters")
            if action == "save":
                ids = tab_ids or []
                if not ids or len(ids) > 500 or len(set(ids)) != len(ids):
                    raise ValueError("save requires 1 to 500 unique tab_ids")
                collection = await bridge.call("capture_tabs", {"tabIds": ids})
                if any(not tab["url"].startswith(("http://", "https://")) for tab in collection["tabs"]):
                    raise ValueError("Collections can save only ordinary web tabs")
                store.save_collection(name, collection)
                return {"saved": len(ids), "name": name}
            collection = store.get_collection(name)
            if collection is None:
                raise ValueError(f"Collection not found: {name}")
            if action in {"close", "delete"} and not confirmed:
                question = (f"Close the {len(collection['tabs'])} tabs in saved collection '{name}'?" if action == "close"
                            else f"Delete saved collection '{name}' from local storage?")
                return {"requiresConfirmation": True, "question": question, "executed": False, "noChangesMade": True,
                        "nextAction": "ask_user", "waitingFor": "user",
                        "nextStep": "Ask the user with the agent's native confirmation/question tool (or chat), then wait for confirmation of this exact scope before confirmed=true."}
            if action == "close":
                result = await mutate("close_saved_collection", {"tabIds": [tab["id"] for tab in collection["tabs"]], "mode": mode}, name)
                return {"closed": result["closed"], "missing": result["missing"], "name": name, "verified": result["verified"]}
            if action == "restore":
                result = await mutate("restore_collection", {"collection": collection, "mode": mode})
                restored_ids = result.pop("ids")
                for tab in collection["tabs"]:
                    tab["id"] = restored_ids[str(tab["id"])]
                store.update_collection(name, collection)
                return {"restored": result["restored"], "tabIds": list(restored_ids.values())[:20],
                        "name": name, "verified": True}
            if action == "delete":
                store.delete_collection(name)
                return {"deleted": True, "name": name}
            raise ValueError("action must be save, list, close, restore, or delete")
        except (RuntimeError, ValueError, KeyError, sqlite3.Error) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def remove_duplicates(window_id: int | None = None, remove: bool = False, mode: str = "batch", confirmed: bool = False) -> dict[str, Any]:
        """Find exact-URL duplicate tabs, or close duplicates after explicit confirmation."""
        try:
            if mode not in {"batch", "sequential"}:
                raise ValueError("mode must be batch or sequential")
            if remove and not confirmed:
                return {"requiresConfirmation": True, "question": "Close duplicate-URL tabs in the selected scope, keeping one copy of each?",
                        "executed": False, "noChangesMade": True,
                        "nextAction": "ask_user", "waitingFor": "user",
                        "nextStep": "Preview duplicates with remove=false, ask the user to confirm the scope through the agent's native question tool, then use remove=true, confirmed=true."}
            result = await mutate("find_duplicates", {"windowId": window_id, "remove": remove, "mode": mode})
            return {key: value for key, value in result.items() if key != "before"}
        except (RuntimeError, ValueError) as error:
            raise ToolError(str(error)) from error


    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True, open_world_hint=False))
    async def local_data(action: Literal["preview", "delete"] = "preview", scope: Literal["cache", "all"] = "cache", confirm: bool = False) -> dict[str, Any]:
        """Preview/delete local cache or all saved browser data. Ask the user to confirm the preview before confirm=true."""
        if action not in {"preview", "delete"} or scope not in {"cache", "all"}:
            raise ToolError("Choose preview/delete and cache/all. All also removes collections and undo history.")
        if action == "delete" and confirm and any(not task.done() for task in catalogue_tasks.values()):
            raise ToolError("Cancel the running catalogue and wait for it to stop before deleting local data")
        async with processing_lock, operation_lock:
            try:
                return store.local_data(scope, action == "delete" and confirm)
            except (OSError, RuntimeError, sqlite3.Error) as error:
                raise ToolError(str(error)) from error

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def undo_last(all_changes: bool = False) -> dict[str, Any]:
        """Revert the latest toolkit mutation, or all retained toolkit changes in reverse order. Keeps the local index."""
        async with processing_lock, operation_lock:
            completed = []
            restored = 0
            tab_ids = []
            while True:
                try:
                    result = await restore_one()
                except (RuntimeError, ValueError, sqlite3.Error, ToolError) as error:
                    raise ToolError(f"Revert stopped after {len(completed)} verified changes: {error}") from error
                completed.append(result["undone"])
                restored += result["restored"]
                tab_ids.extend(result["tabIds"])
                if not all_changes or store.latest_undo() is None:
                    return {"undone": result["undone"] if not all_changes else "all", "changes": len(completed),
                            "restored": restored, "tabIds": tab_ids[-20:], "verified": True}

    async def restore_one():
            entry = store.latest_undo()
            if entry is None:
                raise ToolError("No verified browser action is available to undo")
            entry_id, value, status = entry
            if status == "applying":
                raise ToolError("The previous undo outcome is uncertain; inspect Chrome before making another change")
            if not store.claim_undo(entry_id):
                raise ToolError("This undo is already being applied; inspect Chrome before retrying")
            try:
                if value["snapshot"].get("bookmarkBefore") is not None:
                    if value.get("provisional"):
                        raise RuntimeError("Bookmark outcome is uncertain; reconcile the journal before retrying undo")
                    result = await bridge.call("restore_bookmarks", value["snapshot"])
                    mapping = result.get("bookmarkIds", {})
                    with store.lock, store.db:
                        for prior_id, payload in store.db.execute("SELECT id,payload FROM undo WHERE status='ready'").fetchall():
                            prior = json.loads(payload)
                            for field in ("bookmarkBefore", "bookmarkAfter"):
                                for node in prior.get("snapshot", {}).get(field, []):
                                    node["id"] = mapping.get(node["id"], node["id"])
                                    node["parentId"] = mapping.get(node.get("parentId"), node.get("parentId"))
                            store.db.execute("UPDATE undo SET payload=? WHERE id=?", (json.dumps(prior), prior_id))
                    store.mark_undone(entry_id)
                    return {"undone": value["operation"], "restored": result["restored"], "tabIds": [], "verified": True}
                if value.get("provisional"):
                    current = await bridge.call("capture_browser")
                    original = {tab["id"] for tab in value["snapshot"]["tabs"]}
                    intent = value["intent"]
                    expected = []
                    if value["operation"] == "tab_action" and intent.get("action") == "open":
                        expected = [intent["url"]]
                    if value["operation"] == "restore_collection":
                        expected = [tab["url"] for tab in intent["collection"]["tabs"]]
                    created = [tab for tab in current["tabs"] if tab["id"] not in original and tab["url"] in expected]
                    if any(sum(tab["url"] == url for tab in created) > expected.count(url) for url in expected):
                        raise RuntimeError("New tabs cannot be identified unambiguously; reconcile them before recovery")
                    value["snapshot"]["closeIds"] = [tab["id"] for tab in created]
                result = await bridge.call("restore_snapshot", value["snapshot"])
                if value.get("collection"):
                    collection = store.get_collection(value["collection"])
                    restored_ids = result.get("ids", {})
                    if collection:
                        for tab in collection["tabs"]:
                            if str(tab["id"]) in restored_ids:
                                tab["id"] = restored_ids[str(tab["id"])]
                        store.update_collection(value["collection"], collection)
                store.remap_history(result.get("ids", {}))
                store.mark_undone(entry_id)
                return {"undone": value["operation"], "restored": result.get("restored", 0),
                        "tabIds": list(result.get("ids", {}).values())[:20], "verified": True}
            except (RuntimeError, ValueError, sqlite3.Error) as error:
                if "Chrome extension is disconnected" in str(error):
                    store.reset_undo(entry_id)
                else:
                    store.fail_undo(entry_id)
                raise ToolError(str(error)) from error

    library = Library(store, bridge, store.path.parent, mutate, processing_lock)
    catalogue_tasks = library.tasks
    register_library_tools(mcp, library)

    app = mcp.streamable_http_app(json_response=True, stateless_http=True)

    async def extension(socket):
        await bridge.connect(socket, pairing.token, origin, pairing, legacy_token=token)

    async def health(request):
        return JSONResponse({"name": "fast-tabber", "version": "1.0.0", "root": str(STATE.parent),
            "connected": bridge.peer is not None, "extensionVersion": bridge.extension_version, "pid": os.getpid(),
            "busy": operation_lock.locked() or processing_lock.locked() or inference_lock.locked() or any(not task.done() for task in catalogue_tasks.values())})

    async def shutdown(request):
        if operation_lock.locked() or processing_lock.locked() or inference_lock.locked() or any(not task.done() for task in catalogue_tasks.values()):
            return JSONResponse({"error": "The toolkit is busy; finish the current operation before restarting"}, status_code=409)
        runner = getattr(app.state, "server", None)
        if runner is None:
            return JSONResponse({"error": "This service cannot manage its own lifecycle"}, status_code=409)
        runner.should_exit = True
        return JSONResponse({"stopping": True})

    async def reload_extension(request):
        if operation_lock.locked() or processing_lock.locked() or any(not task.done() for task in catalogue_tasks.values()):
            return JSONResponse({"error": "Finish/cancel the current operation before updating the extension"}, status_code=409)
        try:
            async with operation_lock:
                return JSONResponse(await bridge.call("reload_extension"))
        except RuntimeError as error:
            return JSONResponse({"error": str(error)}, status_code=409)

    async def setup_page(request):
        base = Path(__file__).resolve().parent
        name, media = {"/setup": ("setup.html", "text/html"), "/setup.js": ("setup-page.js", "text/javascript"),
                       "/setup.css": ("setup-page.css", "text/css")}[request.url.path]
        content = (base / name).read_text(encoding="utf-8")
        if name.endswith(".js"):
            content = content.replace("__CONFIG__", json.dumps({"extensionId": origin.removeprefix("chrome-extension://"),
                                                               "folder": str(STATE.parent / "extension")}))
        return Response(content, media_type=media, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})

    app.router.routes.append(WebSocketRoute("/extension", extension))
    app.router.routes.extend(Route(path, setup_page) for path in ("/setup", "/setup.js", "/setup.css"))
    app.router.routes.extend([Route("/health", health), Route("/shutdown", shutdown, methods=["POST"]),
                              Route("/reload-extension", reload_extension, methods=["POST"])])
    app.state.bridge = bridge
    app.state.store = store
    app.state.library = library
    app.add_middleware(LocalAuthentication, token=token)
    return app


def main():
    import uvicorn

    path = STATE / "connection.json"
    if not path.exists():
        raise SystemExit("Run python -m fast_tabber.setup first")
    connection = json.loads(path.read_text(encoding="utf-8"))
    app = create_app(connection["token"], connection["origin"])
    runner = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, ws_max_size=8 * 1024 * 1024))
    app.state.server = runner
    runner.run()


if __name__ == "__main__":
    main()
