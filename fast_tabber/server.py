"""Authenticated local MCP and Chrome extension bridge."""

import asyncio
import hmac
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import struct
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
from starlette.responses import JSONResponse
from starlette.routing import WebSocketRoute, Route
from starlette.websockets import WebSocket, WebSocketDisconnect
from fast_tabber.text import classify_pages
from fast_tabber.text import label_clusters
from fast_tabber.text import _gemma_digest
from fast_tabber.text import triage_pages
from fast_tabber.semantic import cluster_vectors, cosine, embed_texts, embedding_digest
from fast_tabber.vision import describe_image
from fast_tabber.index import ContentIndex
from fast_tabber.preflight import GEMMA, OLLAMA

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

    def local_data(self, scope, delete=False):
        tables = ["pages_fts", "pages", "document_artifacts", "documents", "artifacts", "catalogue_jobs"]
        if scope == "all":
            tables += ["collections", "undo"]
        backups = list(self.path.parent.glob("browser-before-*.sqlite3"))
        with self.lock:
            counts = {table: self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
            report = {"scope": scope, "records": counts, "indexBackups": [path.name for path in backups[:20]], "backupCount": len(backups),
                      "preservesRecoveryHistory": scope == "cache", "deleted": False}
            if not delete:
                return {**report, "requiresConfirmation": True}
            self.db.execute("PRAGMA secure_delete=ON")
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

    def save_pages(self, pages: list[dict], model_digest: str | None = None):
        changed = []
        with self.lock, self.db:
            for page in pages:
                tab_id = page["id"]
                self.db.execute("DELETE FROM pages_fts WHERE rowid=?", (tab_id,))
                parsed = urlsplit(page["url"])
                content = page["text"]
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
        self.extension_version = None
        self.pending: dict[str, asyncio.Future] = {}
        self.lock = asyncio.Lock()

    async def call(self, operation: str, arguments: dict | None = None, timeout: float = 15):
        if operation not in {"probe", "list_tabs", "tab_action", "group_action", "move_tabs", "close_tabs",
                             "close_saved_collection", "restore_snapshot", "capture_tabs", "find_duplicates",
                             "restore_collection", "extract_tabs", "filter_tabs", "group_categories", "capture_page", "capture_browser", "reload_extension"}:
            raise ValueError("Browser operation is not enabled")
        if operation == "extract_tabs":
            timeout = max(timeout, 180)
        if operation == "capture_page":
            timeout = max(timeout, 60)
        if operation in {"capture_browser", "restore_snapshot"}:
            timeout = max(timeout, 180)
        async with self.lock:
            if self.peer is None:
                raise RuntimeError("Chrome extension is disconnected. Open the paired Chrome profile if it is closed; "
                                   "allow up to 30 seconds for reconnection after a service restart. If already enabled, reload Fast-Tabber in chrome://extensions. "
                                   f"First installation only: load unpacked {STATE.parent / 'extension'} and approve Chrome permissions.")
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
            self.extension_version = auth.get("version") if isinstance(auth.get("version"), str) else None
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
    store = StateStore(database or (STATE / "browser.sqlite3"))
    operation_lock = asyncio.Lock()
    index_lock = asyncio.Lock()
    inference_lock = asyncio.Lock()
    processing_lock = asyncio.Lock()
    catalogue_control_lock = asyncio.Lock()
    catalogue_tasks = {}
    def serialized(lock):
        def decorate(function):
            @wraps(function)
            async def run(*args, **kwargs):
                async with lock:
                    return await function(*args, **kwargs)
            return run
        return decorate
    serialized_processing = serialized(processing_lock)
    serialized_catalogue = serialized(catalogue_control_lock)
    mcp = MCPServer("fast-tabber", version="0.5.1", instructions="Manage, search, and organize Chrome tabs with local Gemma text, vision, and semantic embeddings.")

    async def mutate(operation: str, arguments: dict, collection_name: str | None = None):
        async with operation_lock:
            latest = store.latest_undo()
            if latest and latest[2] in {"executing", "failed", "applying"}:
                raise RuntimeError("A previous mutation needs recovery; use undo_last before further changes")
            baseline = await bridge.call("capture_browser")
            provisional = {"operation": operation, "snapshot": baseline, "collection": collection_name,
                           "intent": arguments, "provisional": True}
            entry_id = store.push_undo(provisional, "executing")
            try:
                result = await bridge.call(operation, arguments)
            except Exception as error:
                provisional["error"] = str(error)
                store.finish_undo(entry_id, provisional, "failed")
                raise
            before = result.pop("before", None)
            close_ids = result.pop("closeIds", None)
            if close_ids or (before and (before.get("tabs") or before.get("closeIds") or before.get("activation"))):
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

    async def index_pages(pages: list[dict]):
        if not pages:
            return 0, None
        async with index_lock:
            digest = await embedding_digest(STATE)
            store.save_pages(pages, digest)
            documents = {page["id"]: store.index.document(page) for page in pages}
            keys = {tab: store.index.artifacts(document[0]) for tab, document in documents.items()}
            pending = store.index.pending([key for artifacts in keys.values() for key in artifacts], digest)
            for offset in range(0, len(pending), 16):
                batch = pending[offset:offset + 16]
                async with inference_lock:
                    vectors, actual_digest = await embed_texts([item[1] for item in batch], STATE, digest)
                if actual_digest != digest:
                    raise RuntimeError("EmbeddingGemma changed during indexing; run tabber.ps1 check")
                store.index.save_vectors([item[0] for item in batch], vectors, digest)
            page_vectors = [store.index.page_vector(keys[page["id"]], digest) for page in pages]
            rows = [{"id": page["id"], "digest": store.db.execute("SELECT url_hash FROM pages WHERE tab_id=?", (page["id"],)).fetchone()[0]} for page in pages]
            store.save_embeddings(rows, page_vectors, digest)
            with store.lock, store.db:
                for tab, (document, url) in documents.items():
                    store.db.execute("UPDATE pages SET document_id=?,url=? WHERE tab_id=?", (document, url, tab))
            computed = {item[0] for item in pending}
            return sum(any(key in computed for key in artifacts) for artifacts in keys.values()), digest

    async def add_visual_evidence(extracted: dict):
        async with httpx.AsyncClient(base_url=OLLAMA, timeout=180, trust_env=False) as client:
            vision_model = await _gemma_digest(client, STATE) + ":subject-v2"
        pages = {page["id"]: page for page in extracted["items"]}
        empty = [failure["id"] for failure in extracted["failures"] if failure["reason"] in {"no_rendered_text", "discarded_tab_not_woken"}]
        for tab_id in [*pages, *empty]:
            async with operation_lock:
                capture = await bridge.call("capture_page", {"tabId": tab_id})
            page = pages.get(tab_id, {"id": tab_id, "windowId": capture["windowId"], "url": capture["url"],
                                      "title": capture["title"], "headings": [], "text": ""})
            if page["url"] != capture["url"]:
                raise RuntimeError("A page navigated between text and visual capture; no tabs were grouped")
            page.update({"title": capture["title"], "headings": capture["headings"], "text": capture["text"]})
            store.index.document(page)
            image = capture.pop("image")
            image_id = store.index.image(image)
            description = store.index.description(image_id, vision_model)
            if description is None:
                async with inference_lock:
                    description = await describe_image(image, STATE)
                store.index.save_description(image_id, vision_model, description)
            pages[tab_id] = {**page, "text": page["text"] + "\nVisual evidence: " + description,
                            "visual": True, "image_id": image_id}
        return {"items": list(pages.values()), "failures": [failure for failure in extracted["failures"]
                                                            if failure["reason"] not in {"no_rendered_text", "discarded_tab_not_woken"}]}

    async def reuse_sleeping_pages(extracted):
        sleeping = [item["id"] for item in extracted["failures"] if item["reason"] == "discarded_tab_not_woken"]
        if not sleeping:
            return extracted
        metadata = await bridge.call("capture_tabs", {"tabIds": sleeping})
        cached = [page for item in metadata["tabs"] if (page := store.index.cached_page(item)) is not None]
        found = {page["id"] for page in cached}
        return {"items": extracted["items"] + cached,
                "failures": [item for item in extracted["failures"] if item["id"] not in found]}

    async def cached_selection(tab_ids, window_id, require_visual=False):
        metadata = await bridge.call("capture_tabs", {"tabIds": tab_ids}) if tab_ids else await bridge.call("capture_browser")
        selected = [tab for tab in metadata["tabs"] if tab["url"].startswith(("http://", "https://")) and
                    (window_id is None or tab["windowId"] == window_id)]
        pages = []
        for tab in selected:
            page = store.index.cached_page(tab)
            if page is None or require_visual and not page["visual"]:
                raise RuntimeError("Some selected tabs have no complete local index. Start/resume catalogue_tabs, then organize with use_index=true.")
            pages.append(page)
        return {"items": pages, "failures": []}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def browser_status() -> dict[str, Any]:
        """Verify the real Chrome connection and permissions without returning page content."""
        try:
            return await bridge.call("probe")
        except (RuntimeError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def browser_setup(action: str = "reload_extension") -> dict[str, Any]:
        """Reload the already paired Chrome extension after a local toolkit update; requires no Computer Use."""
        if action != "reload_extension":
            raise ToolError("action must be reload_extension")
        async with operation_lock:
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
            return await bridge.call("list_tabs", {"windowId": window_id, "offset": offset, "limit": limit})
        except (RuntimeError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def tab_action(action: Literal["open", "close", "activate", "move"], tab_id: int | None = None, url: str | None = None,
                         window_id: int | None = None, index: int | None = None) -> dict[str, Any]:
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

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def organize_tabs(action: str, tab_ids: list[int] | None = None, group_id: int | None = None,
                            title: str | None = None, color: str | None = None, collapsed: bool | None = None,
                            window_id: int | None = None, index: int | None = None, mode: str = "batch") -> dict[str, Any]:
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

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def collection_action(action: Literal["save", "list", "close", "restore", "delete"], name: str = "", tab_ids: list[int] | None = None,
                                mode: str = "batch") -> dict[str, Any]:
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

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    async def remove_duplicates(window_id: int | None = None, remove: bool = False, mode: str = "batch") -> dict[str, Any]:
        """Find exact-URL duplicate tabs, or close duplicates after explicit confirmation."""
        try:
            if mode not in {"batch", "sequential"}:
                raise ValueError("mode must be batch or sequential")
            result = await mutate("find_duplicates", {"windowId": window_id, "remove": remove, "mode": mode})
            return {key: value for key, value in result.items() if key != "before"}
        except (RuntimeError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    @serialized_processing
    async def index_tabs(tab_ids: list[int] | None = None, window_id: int | None = None, visual: bool = False) -> dict[str, Any]:
        """Refresh local text/semantic index; visual=True adds native screenshots interpreted by local Gemma. Returns counts only."""
        try:
            if visual and (not tab_ids or len(tab_ids) > 20):
                raise ValueError("Use catalogue_tabs for automatic whole-browser visual indexing; it checkpoints each page and limits memory use.")
            if tab_ids is not None and (not tab_ids or len(tab_ids) > 500 or len(set(tab_ids)) != len(tab_ids)):
                raise ValueError("tab_ids must be unique and contain at most 500 IDs")
            if tab_ids is not None and window_id is not None:
                raise ValueError("choose tab_ids or window_id")
            result = await bridge.call("extract_tabs", {"tabIds": tab_ids, "windowId": window_id})
            if visual:
                result = await add_visual_evidence(result)
            else:
                result = await reuse_sleeping_pages(result)
            embedded, _ = await index_pages(result["items"])
            return {"indexed": len(result["items"]), "embedded": embedded, "visual": len(result["items"]) if visual else 0, "skipped": len(result["failures"]),
                    "failures": result["failures"][:20]}
        except (RuntimeError, ValueError, KeyError, sqlite3.Error) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    @serialized_processing
    async def search_tabs(query: str, mode: Literal["keyword", "semantic"] = "keyword", offset: Annotated[int, Field(ge=0)] = 0,
                          limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict[str, Any]:
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

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    @serialized_processing
    async def organize_by_text(instruction: str, tab_ids: list[int] | None = None,
                               window_id: int | None = None, categories: list[str] | None = None,
                               mode: str = "batch", visual: bool = False, use_index: bool = False) -> dict[str, Any]:
        """Group text/semantic content; visual=True adds local Gemma screenshot interpretation before grouping."""
        try:
            if visual and not use_index and (not tab_ids or len(tab_ids) > 20):
                raise ValueError("Start catalogue_tabs, wait for completion, then organize with use_index=true and visual=true.")
            if mode not in {"batch", "sequential"}:
                raise ValueError("mode must be batch or sequential")
            if not instruction.strip() or len(instruction) > 1000:
                raise ValueError("instruction must contain 1 to 1000 characters")
            if tab_ids is not None and (not tab_ids or len(tab_ids) > 500 or len(set(tab_ids)) != len(tab_ids)):
                raise ValueError("tab_ids must be unique and contain at most 500 IDs")
            if tab_ids is not None and window_id is not None:
                raise ValueError("choose tab_ids or window_id")
            extracted = await cached_selection(tab_ids, window_id, visual) if use_index else await bridge.call("extract_tabs", {"tabIds": tab_ids, "windowId": window_id})
            if visual and not use_index:
                extracted = await add_visual_evidence(extracted)
            elif not use_index:
                extracted = await reuse_sleeping_pages(extracted)
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
                if visual:
                    ordered = [page for members in clusters.values() for page in members]
                    ordered.extend(page for page, cluster_id in zip(pages, cluster_ids) if cluster_id < 0)
                    async with inference_lock:
                        labels = await classify_pages(ordered, instruction, None, STATE)
                else:
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

    async def run_catalogue(job_id):
        try:
            job = store.get_job(job_id)
            async def checkpoint():
                # Persist checkpoints in batches; artifact caching remains durable independently.
                if store.get_job(job_id)["state"] == "cancel_requested":
                    job["state"] = "cancelled"
                store.save_job(job)
                return job["state"] == "cancelled"

            for phase in ("text", "visual", "close"):
                if phase == "close" and not job.get("close_after_index"):
                    continue
                job["phase"] = phase
                for offset in range(0, len(job["items"]), 50):
                    if await checkpoint():
                        return
                    batch = job["items"][offset:offset + 50]
                    pending = [item for item in batch if not item.get({"text": "text_done", "visual": "done", "close": "closed"}[phase])]
                    if not pending:
                        continue
                    async with processing_lock:
                        metadata = await bridge.call("capture_tabs", {"tabIds": [item["id"] for item in pending]})
                        current = {tab["id"]: tab for tab in metadata["tabs"]}
                        if any(current[item["id"]]["url"] != item["url"] for item in pending):
                            raise RuntimeError("A pending tab navigated; start a new catalogue before continuing")
                        if phase == "text":
                            name = f"catalogue-{job_id}-{offset // 50 + 1}"
                            if store.get_collection(name) is None:
                                store.save_collection(name, metadata)
                            if name not in job.setdefault("backups", []):
                                job["backups"].append(name)
                            extracted = await bridge.call("extract_tabs", {"tabIds": list(current)})
                            failures = [item for item in extracted["failures"] if item["reason"] not in {"discarded_tab_not_woken", "no_rendered_text"}]
                            if failures:
                                raise RuntimeError(f"Text extraction failed: {failures[:3]}")
                            readable = {page["id"]: page for page in extracted["items"]}
                            pages = [readable.get(tab["id"]) or store.index.cached_page(tab) or
                                     {**tab, "headings": [], "text": ""} for tab in current.values()]
                            async with inference_lock:
                                decisions = await triage_pages(pages, job["instruction"], job.get("categories"), STATE,
                                    known=sorted({item["category"] for item in job["items"] if item.get("category")}))
                            await index_pages(pages)
                            for item in pending:
                                item.update(decisions[item["id"]], text_done=True,
                                            done=not decisions[item["id"]]["needs_visual"])
                        elif phase == "visual":
                            for item in pending:
                                cached = store.index.cached_page(current[item["id"]])
                                evidence = {"items": [cached], "failures": []} if cached and cached["visual"] else await add_visual_evidence(
                                    {"items": [], "failures": [{"id": item["id"], "reason": "no_rendered_text"}]})
                                await index_pages(evidence["items"])
                                async with inference_lock:
                                    labels = await classify_pages(evidence["items"], job["instruction"], job.get("categories"), STATE)
                                item.update(category=labels[item["id"]], done=True)
                                if store.get_job(job_id)["state"] == "cancel_requested":
                                    break
                        else:
                            # Journal and saved batch precede irreversible tab closure.
                            await mutate("close_tabs", {"tabIds": list(current), "mode": "batch"})
                            for item in pending:
                                item["closed"] = True
                    if await checkpoint():
                        return
                    await asyncio.sleep(0)
            job["state"] = "complete"
            store.save_job(job)
        except Exception as error:
            job.update(state="failed", error=str(error)[:500])
            store.save_job(job)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
    @serialized_catalogue
    async def catalogue_tabs(action: Literal["start", "status", "resume", "cancel", "apply"] = "start", job_id: str = "", tab_ids: list[int] | None = None,
                             wait_seconds: Annotated[int, Field(ge=0, le=30)] = 20,
                             instruction: str = "Organize tabs by subject", categories: list[str] | None = None,
                             close_after_index: bool = False, confirm_close: bool = False) -> dict[str, Any]:
        """Text-first catalogue; screenshot unresolved pages only. Checkpoints/backups in batches of 50. start/status/resume/cancel/apply. Closing requires user confirmation."""
        try:
            if action not in {"start", "status", "resume", "cancel", "apply"}:
                raise ValueError("Choose start, status, resume, cancel, or apply")
            if action == "start":
                if not instruction.strip() or len(instruction) > 1000:
                    raise ValueError("instruction must contain 1 to 1000 characters")
                if categories is not None and (not categories or len(categories) > 20 or len(set(categories)) != len(categories)
                        or any(not label.strip() or len(label) > 80 or label == "Uncategorized" for label in categories)):
                    raise ValueError("Choose 1 to 20 unique category labels, excluding Uncategorized")
                if close_after_index and not confirm_close:
                    raise ValueError("Ask the user to confirm archive-and-close, then set confirm_close=true; unsaved page state cannot be restored")
                if any(not task.done() for task in catalogue_tasks.values()):
                    raise ValueError("A catalogue is already running; inspect or cancel its job before starting another")
                metadata = await bridge.call("capture_tabs", {"tabIds": tab_ids}) if tab_ids else await bridge.call("capture_browser")
                items = []
                for tab in metadata["tabs"]:
                    if tab["url"].startswith(("http://", "https://")):
                        items.append({"id": tab["id"], "url": tab["url"], "done": False})
                if not items or len(items) > 2000:
                    raise ValueError("Catalogue requires 1 to 2000 ordinary web tabs")
                await embedding_digest(STATE)
                async with httpx.AsyncClient(base_url=OLLAMA, timeout=180, trust_env=False) as client:
                    await _gemma_digest(client, STATE)
                job = {"id": secrets.token_hex(8), "state": "running", "items": items,
                       "instruction": instruction, "categories": categories, "close_after_index": close_after_index}
                store.save_job(job)
                catalogue_tasks[job["id"]] = asyncio.create_task(run_catalogue(job["id"]))
            else:
                job = store.get_job(job_id)
                job_id = job["id"]
                active = job_id in catalogue_tasks and not catalogue_tasks[job_id].done()
                if action == "status" and active:
                    for _ in range(wait_seconds * 2):
                        await asyncio.sleep(0.5)
                        if catalogue_tasks[job_id].done():
                            break
                    job = store.get_job(job_id)
                    active = not catalogue_tasks[job_id].done()
                if action == "apply":
                    if active or job["state"] != "complete" or job.get("close_after_index"):
                        raise ValueError("Only a completed catalogue of still-open tabs can be grouped")
                    grouped = {}
                    for item in job["items"]:
                        if item["category"] != "Uncategorized":
                            grouped.setdefault(item["category"], []).append(item["id"])
                    if not grouped:
                        return {"grouped": 0, "verified": True}
                    if len(grouped) > 20:
                        raise ValueError("More than 20 labels were inferred; rerun with explicit categories")
                    return await mutate("group_categories", {"categories": [{"title": label, "tabIds": ids} for label, ids in grouped.items()],
                        "mode": "batch", "expectedUrls": {str(item["id"]): item["url"] for item in job["items"]}})
                elif action == "cancel":
                    job["state"] = "cancel_requested" if active else "cancelled"
                    store.save_job(job)
                elif action == "resume":
                    if "instruction" not in job:
                        raise ValueError("This job predates text-first cataloguing; start a new catalogue to reuse its local index")
                    if active or any(not task.done() for task in catalogue_tasks.values()):
                        raise ValueError("A catalogue is already running")
                    current = (await bridge.call("capture_browser"))["tabs"]
                    for item in job["items"]:
                        if item.get("closed"):
                            continue
                        matches = [tab for tab in current if tab["id"] == item["id"] and tab["url"] == item["url"]]
                        matches = matches or [tab for tab in current if tab["url"] == item["url"]]
                        if len(matches) != 1:
                            raise ValueError("A pending tab is missing or ambiguous; select the current tabs for a new catalogue")
                        item["id"] = matches[0]["id"]
                    job.update(state="running", error=None)
                    store.save_job(job)
                    catalogue_tasks[job_id] = asyncio.create_task(run_catalogue(job_id))
                elif not active and job["state"] in {"running", "cancel_requested"}:
                    job["state"] = "interrupted"
                    store.save_job(job)
            return {"jobId": job["id"], "state": job["state"], "phase": job.get("phase", "text"),
                    "completed": sum(item["done"] for item in job["items"]), "textAssessed": sum(bool(item.get("text_done")) for item in job["items"]),
                    "screenshotsNeeded": sum(bool(item.get("needs_visual")) for item in job["items"]),
                    "backupBatches": len(job.get("backups", [])), "closed": sum(bool(item.get("closed")) for item in job["items"]),
                    "total": len(job["items"]), "error": job.get("error")}
        except (RuntimeError, ValueError, KeyError, OSError, sqlite3.Error) as error:
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
        async with operation_lock:
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

    app = mcp.streamable_http_app(json_response=True, stateless_http=True)

    async def extension(socket):
        await bridge.connect(socket, token, origin)

    async def health(request):
        return JSONResponse({"name": "fast-tabber", "version": "0.5.1", "root": str(STATE.parent),
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

    app.router.routes.append(WebSocketRoute("/extension", extension))
    app.router.routes.extend([Route("/health", health), Route("/shutdown", shutdown, methods=["POST"]),
                              Route("/reload-extension", reload_extension, methods=["POST"])])
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
    app = create_app(connection["token"], connection["origin"])
    runner = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, ws_max_size=8 * 1024 * 1024))
    app.state.server = runner
    runner.run()


if __name__ == "__main__":
    main()
