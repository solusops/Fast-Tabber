"""Authenticated local MCP and Chrome extension bridge."""

import asyncio
import hmac
import hashlib
import json
import math
import re
import secrets
import sqlite3
import struct
import threading
from typing import Annotated
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import JSONResponse
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from fast_tabber.text import classify_pages
from fast_tabber.text import label_clusters
from fast_tabber.semantic import cluster_vectors, cosine, embed_texts, embedding_digest

STATE = Path(__file__).resolve().parent.parent / ".state"


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS collections (name TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS undo (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ready')")
            self.db.execute("CREATE TABLE IF NOT EXISTS pages (tab_id INTEGER PRIMARY KEY, window_id INTEGER NOT NULL, url_hash TEXT NOT NULL, title TEXT NOT NULL, origin TEXT NOT NULL, headings TEXT NOT NULL, content TEXT NOT NULL)")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(pages)")}
            if "embedding" not in columns:
                self.db.execute("ALTER TABLE pages ADD COLUMN embedding BLOB")
            if "embedding_digest" not in columns:
                self.db.execute("ALTER TABLE pages ADD COLUMN embedding_digest TEXT")
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(title, origin, headings, content, tokenize='unicode61')")
            if "status" not in {row[1] for row in self.db.execute("PRAGMA table_info(undo)")}:
                self.db.execute("ALTER TABLE undo ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'")

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

    def push_undo(self, snapshot: dict):
        with self.lock, self.db:
            self.db.execute("INSERT INTO undo (payload) VALUES (?)", (json.dumps(snapshot),))
            self.db.execute("DELETE FROM undo WHERE status='ready' AND id NOT IN (SELECT id FROM undo WHERE status='ready' ORDER BY id DESC LIMIT 50)")

    def latest_undo(self):
        with self.lock:
            row = self.db.execute("SELECT id, payload, status FROM undo ORDER BY id DESC LIMIT 1").fetchone()
        return (row[0], json.loads(row[1]), row[2]) if row else None

    def claim_undo(self, entry_id: int):
        with self.lock, self.db:
            return self.db.execute("UPDATE undo SET status='applying' WHERE id=? AND status='ready'", (entry_id,)).rowcount == 1

    def reset_undo(self, entry_id: int):
        with self.lock, self.db:
            self.db.execute("UPDATE undo SET status='ready' WHERE id=? AND status='applying'", (entry_id,))

    def fail_undo(self, entry_id: int):
        with self.lock, self.db:
            self.db.execute("UPDATE undo SET status='failed' WHERE id=? AND status='applying'", (entry_id,))

    def remove_undo(self, entry_id: int):
        with self.lock, self.db:
            self.db.execute("DELETE FROM undo WHERE id=?", (entry_id,))

    def save_pages(self, pages: list[dict], model_digest: str | None = None):
        changed = []
        with self.lock, self.db:
            for page in pages:
                tab_id = page["id"]
                self.db.execute("DELETE FROM pages_fts WHERE rowid=?", (tab_id,))
                parsed = urlsplit(page["url"])
                content = page["text"][:3000]
                headings = " ".join(page["headings"])
                host = f"[{parsed.hostname}]" if ":" in (parsed.hostname or "") else parsed.hostname
                origin = f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")
                digest = hashlib.sha256(json.dumps([page["title"], headings, content], ensure_ascii=False).encode()).hexdigest()
                current = self.db.execute("SELECT url_hash, embedding, embedding_digest FROM pages WHERE tab_id=?", (tab_id,)).fetchone()
                changed_text = current is None or current[0] != digest
                stale_embedding = model_digest is not None and (current is None or current[1] is None or current[2] != model_digest)
                embedding, embedding_digest = (None, None) if changed_text or stale_embedding else (current[1], current[2])
                self.db.execute("INSERT INTO pages (tab_id, window_id, url_hash, title, origin, headings, content, embedding, embedding_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(tab_id) DO UPDATE SET window_id=excluded.window_id, url_hash=excluded.url_hash, title=excluded.title, origin=excluded.origin, headings=excluded.headings, content=excluded.content, embedding=excluded.embedding, embedding_digest=excluded.embedding_digest",
                    (tab_id, page["windowId"], digest, page["title"][:200], origin, headings, content, embedding, embedding_digest))
                if model_digest is not None and (changed_text or stale_embedding):
                    changed.append({"id": tab_id, "digest": digest, "text": f"{page['title']}\n{headings}\n{content}"})
                self.db.execute("INSERT INTO pages_fts(rowid, title, origin, headings, content) VALUES (?, ?, ?, ?, ?)",
                    (tab_id, page["title"][:200], origin, headings, content))
        return changed

    def save_embeddings(self, pages: list[dict], vectors: list[list[float]], model_digest: str):
        if len(pages) != len(vectors) or not pages:
            raise ValueError("Embedding output count did not match indexed pages")
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1 or not dimensions.pop() or any(not all(map(math.isfinite, vector)) or not any(vector) for vector in vectors):
            raise ValueError("Embedding output contains invalid vectors")
        with self.lock, self.db:
            for page, vector in zip(pages, vectors):
                row = self.db.execute("SELECT url_hash FROM pages WHERE tab_id=?", (page["id"],)).fetchone()
                if not row or row[0] != page["digest"]:
                    raise RuntimeError("Page text changed during embedding; re-index before semantic operations")
                blob = struct.pack(f"<{len(vector)}f", *vector)
                self.db.execute("UPDATE pages SET embedding=?, embedding_digest=? WHERE tab_id=?",
                                (blob, model_digest, page["id"]))

    def get_page_vectors(self, tab_ids: list[int], model_digest: str):
        if not tab_ids:
            return {}
        marks = ",".join("?" for _ in tab_ids)
        with self.lock:
            rows = self.db.execute(f"SELECT tab_id, embedding FROM pages WHERE embedding_digest=? AND tab_id IN ({marks})",
                                   (model_digest, *tab_ids)).fetchall()
        return {row[0]: struct.unpack(f"<{len(row[1]) // 4}f", row[1]) for row in rows}

    def pending_embeddings(self, model_digest: str):
        with self.lock:
            rows = self.db.execute("SELECT tab_id, url_hash, title, headings, content FROM pages WHERE embedding IS NULL OR embedding_digest!=?", (model_digest,)).fetchall()
        return [{"id": row[0], "digest": row[1], "text": f"{row[2]}\n{row[3]}\n{row[4]}"} for row in rows]

    def semantic_search(self, query_vector: list[float], offset: int, limit: int, model_digest: str):
        if not query_vector or not all(map(math.isfinite, query_vector)) or not any(query_vector):
            raise ValueError("Semantic query returned an invalid embedding")
        with self.lock:
            rows = self.db.execute("SELECT tab_id, window_id, title, origin, content, embedding FROM pages WHERE embedding_digest=? AND embedding IS NOT NULL",
                                   (model_digest,)).fetchall()
        q_norm = math.sqrt(sum(value * value for value in query_vector))
        scored = []
        for tab_id, window_id, title, origin, content, blob in rows:
            vector = struct.unpack(f"<{len(blob) // 4}f", blob)
            if len(vector) != len(query_vector):
                raise RuntimeError("Stored embedding dimensions differ from the current model; re-index tabs")
            score = sum(a * b for a, b in zip(query_vector, vector)) / (q_norm * math.sqrt(sum(value * value for value in vector)))
            scored.append((score, tab_id, window_id, title, origin, content))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [{"id": row[1], "windowId": row[2], "title": row[3], "origin": row[4],
                 "snippet": row[5][:240], "score": round(row[0], 4)} for row in scored[offset:offset + limit]], len(scored)

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
            rows = self.db.execute("SELECT p.tab_id, p.window_id, p.title, p.origin, snippet(pages_fts, 3, '', '', ' … ', 24) FROM pages_fts JOIN pages p ON p.tab_id=pages_fts.rowid WHERE pages_fts MATCH ? ORDER BY bm25(pages_fts) LIMIT ? OFFSET ?",
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
        self.pending: dict[str, asyncio.Future] = {}
        self.lock = asyncio.Lock()

    async def call(self, operation: str, arguments: dict | None = None, timeout: float = 15):
        if operation not in {"probe", "list_tabs", "tab_action", "group_action", "move_tabs", "close_tabs",
                             "close_saved_collection", "restore_snapshot", "capture_tabs", "find_duplicates",
                             "restore_collection", "extract_tabs", "filter_tabs", "group_categories"}:
            raise ValueError("Browser operation is not enabled")
        if operation == "extract_tabs":
            timeout = max(timeout, 180)
        async with self.lock:
            if self.peer is None:
                raise RuntimeError("Chrome extension is disconnected. Load/reload extension/ in Chrome.")
            request_id = secrets.token_hex(12)
            future = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            try:
                await self.peer.send_json({"id": request_id, "operation": operation, "arguments": arguments or {}})
                return await asyncio.wait_for(future, timeout)
            except (TimeoutError, WebSocketDisconnect, OSError) as error:
                raise RuntimeError("Chrome request failed; outcome unknown. Reconcile browser state before retrying.") from error
            finally:
                self.pending.pop(request_id, None)

    async def connect(self, socket: WebSocket, token: str, origin: str):
        if socket.headers.get("origin") != origin or self.peer is not None:
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            auth = await asyncio.wait_for(socket.receive_json(), 5)
            supplied = auth.get("token", "")
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, token):
                await socket.close(code=1008)
                return
            self.peer = socket
            await socket.send_json({"type": "paired"})
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
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(RuntimeError("Chrome disconnected; operation outcome unknown"))


class LocalAuthentication:
    def __init__(self, app, token):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            provided = headers.get(b"authorization", b"")
            expected = f"Bearer {self.token}".encode()
            origin = headers.get(b"origin")
            if not hmac.compare_digest(provided, expected):
                await JSONResponse({"error": "Authentication required"}, status_code=401)(scope, receive, send)
                return
            if origin and origin not in {b"http://127.0.0.1:8765", b"http://localhost:8765"}:
                await JSONResponse({"error": "Untrusted origin"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(token: str, origin: str, database: Path | None = None):
    bridge = Bridge()
    store = StateStore(database or (STATE / "browser.sqlite3"))
    operation_lock = asyncio.Lock()
    index_lock = asyncio.Lock()
    inference_lock = asyncio.Lock()
    mcp = MCPServer("fast-tabber", version="0.4.0", instructions="Manage, search, and organize Chrome tabs with local Gemma and semantic embeddings.")

    async def mutate(operation: str, arguments: dict, collection_name: str | None = None):
        async with operation_lock:
            result = await bridge.call(operation, arguments)
            before = result.pop("before", None)
            close_ids = result.pop("closeIds", None)
            if close_ids or (before and (before.get("tabs") or before.get("closeIds"))):
                snapshot = before or {"tabs": [], "groups": {}}
                if close_ids:
                    snapshot["closeIds"] = close_ids
                try:
                    store.push_undo({"operation": operation, "snapshot": snapshot, "collection": collection_name})
                except sqlite3.Error as error:
                    try:
                        await bridge.call("restore_snapshot", snapshot)
                    except Exception as rollback_error:
                        raise RuntimeError(f"Undo history could not be saved ({error}); browser rollback incomplete ({rollback_error})") from error
                    raise RuntimeError(f"Undo history could not be saved; browser change rolled back ({error})") from error
            return result

    async def index_pages(pages: list[dict]):
        if not pages:
            return 0, None
        async with index_lock:
            digest = await embedding_digest(STATE)
            store.save_pages(pages, digest)
            pending = store.pending_embeddings(digest)
            if pending:
                async with inference_lock:
                    vectors, actual_digest = await embed_texts([page["text"] for page in pending], STATE, digest)
                if actual_digest != digest:
                    raise RuntimeError("EmbeddingGemma changed during indexing; run tabber.ps1 check")
                store.save_embeddings(pending, vectors, digest)
            return len(pending), digest

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def browser_status() -> dict:
        """Verify the real Chrome connection and permissions without returning page content."""
        try:
            return await bridge.call("probe")
        except (RuntimeError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def list_tabs(window_id: int | None = None, offset: Annotated[int, Field(ge=0)] = 0,
                        limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict:
        """List compact tab metadata. Returns at most 100 tabs; use offset to page."""
        if offset < 0 or not 1 <= limit <= 100:
            raise ToolError("offset must be nonnegative and limit must be between 1 and 100")
        try:
            return await bridge.call("list_tabs", {"windowId": window_id, "offset": offset, "limit": limit})
        except (RuntimeError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def tab_action(action: str, tab_id: int | None = None, url: str | None = None,
                         window_id: int | None = None, index: int | None = None) -> dict:
        """Open, close, activate, or move one tab. Actions: open(url), close/activate(tab_id), move(tab_id, window_id, index)."""
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
            return await mutate("tab_action", {"action": action, "tabId": tab_id, "url": url,
                                                "windowId": window_id, "index": index})
        except (RuntimeError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def organize_tabs(action: str, tab_ids: list[int] | None = None, group_id: int | None = None,
                            title: str | None = None, color: str | None = None, collapsed: bool | None = None,
                            window_id: int | None = None, index: int | None = None, mode: str = "batch") -> dict:
        """Create, add/remove tabs from, update, or delete native groups; or move tabs. Modes are batch or sequential."""
        try:
            if mode not in {"batch", "sequential"}:
                raise ValueError("mode must be batch or sequential")
            ids = tab_ids or []
            if len(ids) > 500 or len(set(ids)) != len(ids):
                raise ValueError("tab_ids must be unique and contain at most 500 IDs")
            if action == "move":
                if window_id is None:
                    raise ValueError("move requires window_id")
                result = await mutate("move_tabs", {"tabIds": ids, "windowId": window_id, "index": index, "mode": mode})
            else:
                arguments = {"action": action, "tabIds": ids, "groupId": group_id,
                    "title": title, "color": color, "collapsed": collapsed, "mode": mode}
                result = await mutate("group_action", {key: value for key, value in arguments.items() if value is not None})
            return {key: value for key, value in result.items() if key != "before"}
        except (RuntimeError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def collection_action(action: str, name: str = "", tab_ids: list[int] | None = None,
                                mode: str = "batch") -> dict:
        """Save, list, close, restore, or delete a named tab collection."""
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

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def remove_duplicates(window_id: int | None = None, remove: bool = False, mode: str = "batch") -> dict:
        """Find exact-URL duplicate tabs, or close duplicates after explicit confirmation."""
        try:
            if mode not in {"batch", "sequential"}:
                raise ValueError("mode must be batch or sequential")
            result = await mutate("find_duplicates", {"windowId": window_id, "remove": remove, "mode": mode})
            return {key: value for key, value in result.items() if key != "before"}
        except (RuntimeError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def index_tabs(tab_ids: list[int] | None = None, window_id: int | None = None) -> dict:
        """Extract rendered page text locally and refresh the local search index; returns counts only."""
        try:
            if tab_ids is not None and (not tab_ids or len(tab_ids) > 500 or len(set(tab_ids)) != len(tab_ids)):
                raise ValueError("tab_ids must be unique and contain at most 500 IDs")
            if tab_ids is not None and window_id is not None:
                raise ValueError("choose tab_ids or window_id")
            result = await bridge.call("extract_tabs", {"tabIds": tab_ids, "windowId": window_id})
            embedded, _ = await index_pages(result["items"])
            return {"indexed": len(result["items"]), "embedded": embedded, "skipped": len(result["failures"]),
                    "failures": result["failures"][:20]}
        except (RuntimeError, ValueError, KeyError, sqlite3.Error) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def search_tabs(query: str, mode: str = "keyword", offset: Annotated[int, Field(ge=0)] = 0,
                          limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict:
        """Search local page text by keywords or EmbeddingGemma semantic similarity."""
        try:
            if len(query) > 500 or offset < 0 or not 1 <= limit <= 100 or mode not in {"keyword", "semantic"}:
                raise ValueError("query must be at most 500 characters; choose keyword or semantic mode, limit 1 to 100, and nonnegative offset")
            if not query.strip():
                raise ValueError("query must contain text")
            if mode == "semantic":
                async with inference_lock:
                    vectors, digest = await embed_texts([query], STATE)
                if store.pending_embeddings(digest):
                    raise RuntimeError("Some indexed tabs need semantic embeddings; run index_tabs first")
                matches, total = store.semantic_search(vectors[0], offset, limit, digest)
                if not total:
                    raise RuntimeError("No semantic index exists; run index_tabs first")
            else:
                total = store.count_pages(query)
                matches = store.search_pages(query, offset, limit)
            current = await bridge.call("filter_tabs", {"tabIds": [item["id"] for item in matches]})
            live = set(current["tabIds"])
            store.remove_pages([item["id"] for item in matches if item["id"] not in live])
            tabs = [item for item in matches if item["id"] in live]
            return {"mode": mode, "total": total, "offset": offset, "limit": limit, "tabs": tabs}
        except (RuntimeError, ValueError, KeyError, sqlite3.Error) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def organize_by_text(instruction: str, tab_ids: list[int] | None = None,
                               window_id: int | None = None, categories: list[str] | None = None,
                               mode: str = "batch") -> dict:
        """Classify extracted page content with local Gemma, then natively group verified matches."""
        try:
            if mode not in {"batch", "sequential"}:
                raise ValueError("mode must be batch or sequential")
            if tab_ids is not None and (not tab_ids or len(tab_ids) > 500 or len(set(tab_ids)) != len(tab_ids)):
                raise ValueError("tab_ids must be unique and contain at most 500 IDs")
            if tab_ids is not None and window_id is not None:
                raise ValueError("choose tab_ids or window_id")
            extracted = await bridge.call("extract_tabs", {"tabIds": tab_ids, "windowId": window_id})
            pages = extracted["items"]
            if not pages:
                raise ValueError(f"No readable page text; failed tabs: {json.dumps(extracted['failures'][:20])}")
            _, digest = await index_pages(pages)
            vectors = store.get_page_vectors([page["id"] for page in pages], digest)
            if len(vectors) != len(pages):
                raise RuntimeError("One or more tabs lack a verified semantic embedding; no tabs were changed")
            if categories:
                async with inference_lock:
                    category_vectors, _ = await embed_texts(categories, STATE, digest)
                    similarities = {page["id"]: {label: round(cosine(vectors[page["id"]], vector), 4)
                        for label, vector in zip(categories, category_vectors)} for page in pages}
                    labels = await classify_pages(pages, instruction, categories, STATE, similarities)
            else:
                cluster_ids = cluster_vectors([vectors[page["id"]] for page in pages])
                clusters = {}
                for page, cluster_id in zip(pages, cluster_ids):
                    if cluster_id >= 0:
                        clusters.setdefault(cluster_id, []).append(page)
                async with inference_lock:
                    names = await label_clusters(clusters, instruction, STATE) if clusters else {}
                labels = {page["id"]: names.get(cluster_id, "Uncategorized")
                          for page, cluster_id in zip(pages, cluster_ids)}
            grouped = {}
            for tab_id, label in labels.items():
                if label != "Uncategorized":
                    grouped.setdefault(label, []).append(tab_id)
            if not grouped:
                return {"grouped": 0, "uncategorized": len(labels), "skipped": len(extracted["failures"]),
                        "failures": extracted["failures"][:20], "verified": True}
            result = await mutate("group_categories", {"categories": [
                {"title": title, "tabIds": ids} for title, ids in grouped.items()], "mode": mode,
                "expectedUrls": {str(page["id"]): page["url"] for page in pages}})
            return {"grouped": result["grouped"], "groups": result["groups"], "categories": list(grouped)[:20],
                    "uncategorized": sum(label == "Uncategorized" for label in labels.values()),
                    "skipped": len(extracted["failures"]), "failures": extracted["failures"][:20], "verified": True}
        except (RuntimeError, ValueError, KeyError, sqlite3.Error) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def undo_last() -> dict:
        """Undo the most recent tab mutation or restore a closed tab collection."""
        async with operation_lock:
            entry = store.latest_undo()
            if entry is None:
                raise ToolError("No verified browser action is available to undo")
            entry_id, value, status = entry
            if status != "ready":
                raise ToolError("The previous undo outcome is uncertain; inspect Chrome before making another change")
            if not store.claim_undo(entry_id):
                raise ToolError("This undo is already being applied; inspect Chrome before retrying")
            try:
                result = await bridge.call("restore_snapshot", value["snapshot"])
                if value.get("collection"):
                    collection = store.get_collection(value["collection"])
                    restored_ids = result.get("ids", {})
                    if collection:
                        for tab in collection["tabs"]:
                            if str(tab["id"]) in restored_ids:
                                tab["id"] = restored_ids[str(tab["id"])]
                        store.update_collection(value["collection"], collection)
                store.remove_undo(entry_id)
                return {"undone": value["operation"], "restored": result.get("restored", 0),
                        "tabIds": list(result.get("ids", {}).values())[:20], "verified": True}
            except (RuntimeError, ValueError, sqlite3.Error) as error:
                if "Chrome extension is disconnected" in str(error):
                    store.reset_undo(entry_id)
                else:
                    store.fail_undo(entry_id)
                raise ToolError(str(error)) from error

    app = mcp.streamable_http_app(json_response=True, stateless_http=True)

    async def extension(socket):
        await bridge.connect(socket, token, origin)

    app.router.routes.append(WebSocketRoute("/extension", extension))
    app.state.bridge = bridge
    app.state.store = store
    app.add_middleware(LocalAuthentication, token=token)
    return app


def main():
    import uvicorn

    path = STATE / "connection.json"
    if not path.exists():
        raise SystemExit("Run python -m fast_tabber.setup first")
    connection = json.loads(path.read_text(encoding="utf-8"))
    uvicorn.run(create_app(connection["token"], connection["origin"]), host="127.0.0.1", port=8765, ws_max_size=8 * 1024 * 1024)


if __name__ == "__main__":
    main()
