"""Authenticated local MCP and Chrome extension bridge."""

import asyncio
import hmac
import json
import secrets
import sqlite3
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

STATE = Path(__file__).resolve().parent.parent / ".state"


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS collections (name TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS undo (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ready')")
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


class Bridge:
    def __init__(self):
        self.peer: WebSocket | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.lock = asyncio.Lock()

    async def call(self, operation: str, arguments: dict | None = None, timeout: float = 15):
        if operation not in {"probe", "list_tabs", "tab_action", "group_action", "move_tabs", "close_tabs",
                             "close_saved_collection", "restore_snapshot", "capture_tabs", "find_duplicates",
                             "restore_collection"}:
            raise ValueError("Browser operation is not enabled")
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
    mcp = MCPServer("fast-tabber", version="0.2.0", instructions="Manage Chrome tabs, groups, saved collections, and undo. List tabs to resolve IDs.")

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
