"""Persistent first-page text, FTS5 search and exact bulk selections."""
import asyncio
import base64
import csv
import hashlib
import io
import json
import re
import secrets
import sqlite3
import time
from collections import defaultdict
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.context import Context
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from fast_tabber.onboarding import DISCLOSURE


def clean_url(raw):
    parsed = urlsplit(raw)
    host = f"[{parsed.hostname}]" if ":" in (parsed.hostname or "") else parsed.hostname
    return urlunsplit((parsed.scheme, (host or "") + (f":{parsed.port}" if parsed.port else ""), parsed.path, parsed.query, parsed.fragment))


class Library:
    def __init__(self, store, bridge, state, mutate, lock):
        self.store, self.bridge, self.state, self.mutate, self.lock = store, bridge, state, mutate, lock
        self.tasks = {}
        with store.lock, store.db:
            store.db.execute("CREATE TABLE IF NOT EXISTS resources (kind TEXT,id TEXT,metadata TEXT,fingerprint TEXT,document TEXT,level INTEGER,vector BLOB,model TEXT,PRIMARY KEY(kind,id))")
            store.db.execute("CREATE TABLE IF NOT EXISTS selections (id TEXT PRIMARY KEY,kind TEXT NOT NULL,items TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            store.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS resources_fts USING fts5(kind UNINDEXED,id UNINDEXED,title,url,content)")

    def permission(self, kind, confirm=False, ids=None):
        path = self.state / "settings.json"
        settings = json.loads(path.read_text()) if path.exists() else {}
        if not settings.get("privacy_acknowledged"):
            return {"requiresDisclosure": True, "disclosure": DISCLOSURE, "started": False,
                    "nextAction": "ask_user", "waitingFor": "user",
                    "nextStep": "Show the disclosure once and acknowledge it through onboarding before indexing."}
        allowed = settings.get("indexing_allowed", [])
        if kind not in allowed:
            scopes = settings.get("indexing_scopes", {})
            if ids is not None and set(map(str, ids)) <= set(scopes.get(kind, [])):
                return None
            if not confirm:
                return {"requiresIndexingConsent": True, "resource": kind, "started": False,
                        "noChangesMade": True, "nextAction": "ask_user", "waitingFor": "user",
                        "message": f"May Fast-Tabber create/reuse a persistent local index of the selected {kind}? Page text is stored once and reused across operations.",
                        "nextStep": "Ask the user for indexing permission and wait. Set confirm_index=true only after explicit approval; a grouping request or unrelated ok is not indexing consent."}
            if ids is None:
                settings["indexing_allowed"] = [*allowed, kind]
            else:
                scopes[kind] = sorted(set(scopes.get(kind, [])) | set(map(str, ids)))
                settings["indexing_scopes"] = scopes
            path.write_text(json.dumps(settings))
        return None

    async def records(self, kind, ids=None, expand=False):
        if kind == "tabs":
            snapshot = await self.bridge.call("capture_browser")
            rows = [{**row, "groupTitle": snapshot.get("groups", {}).get(str(row.get("groupId")), {}).get("title", "")} for row in snapshot["tabs"]]
        else:
            rows = (await self.bridge.call("bookmark_tree"))["nodes"]
            parents = {row["id"]: row for row in rows}
            for row in rows:
                path, parent, seen = [], parents.get(row.get("parentId")), set()
                while parent and parent["id"] not in seen:
                    seen.add(parent["id"])
                    if parent.get("title"):
                        path.append(parent["title"])
                    parent = parents.get(parent.get("parentId"))
                row["folder"] = " / ".join(reversed(path))[:500]
        result = []
        wanted = {str(item) for item in ids} if ids is not None else None
        if wanted is not None and not wanted <= {str(row["id"]) for row in rows}:
            raise ValueError("Some selected resources no longer exist; resolve current IDs before continuing")
        if wanted is not None and expand and kind == "bookmarks":
            # Chrome supplies a parent-before-child tree; a selected folder scopes its descendants.
            for row in rows:
                if row.get("parentId") in wanted:
                    wanted.add(str(row["id"]))
        for row in rows:
            item = {**row, "id": str(row["id"]), "kind": kind}
            if wanted is not None and item["id"] not in wanted:
                continue
            if row.get("url"):
                parsed = urlsplit(row["url"])
                item["restricted"] = bool(parsed.username or parsed.password)
                item["url"] = clean_url(row["url"])
            result.append(item)
        if wanted is not None and wanted != {row["id"] for row in result}:
            raise ValueError("Some selected resources no longer exist; resolve current IDs before continuing")
        return result

    @staticmethod
    def web(rows):
        return [row for row in rows if not row.get("restricted") and row.get("url", "").startswith(("http://", "https://"))]

    def cached(self, kind, identifier):
        with self.store.lock:
            return self.store.db.execute("SELECT metadata,fingerprint,document,level,vector,model FROM resources WHERE kind=? AND id=?", (kind, str(identifier))).fetchone()

    @staticmethod
    def fingerprint(row):
        return hashlib.sha256(json.dumps([row.get("url"), row.get("title", "")], ensure_ascii=False).encode()).hexdigest()


    @staticmethod
    def has_page_text(row, page):
        text = page.get("text", "")
        if text.startswith("Website: "):
            text = text.split("\n", 2)[-1]
        return bool(text.strip() and text.strip() != row["url"])

    def progress(self, job, stage, **values):
        if job is None:
            return
        if self.store.get_job(job["id"])["state"] == "cancel_requested":
            raise asyncio.CancelledError
        job.update(stage=stage, updatedAt=time.time(), **values)
        self.store.save_job(job)

    async def index(self, kind, ids=None, level="text", confirm=False, refresh=False, job=None):
        permission = self.permission(kind, confirm, ids)
        if permission:
            return permission
        async with self.lock:
            rows = self.web(await self.records(kind, ids, expand=True))
            requested = {"metadata": 0, "text": 1}[level]
            changed = reused = 0
            failed = []
            total = len(rows)

            async def prepare(row):
                previous = self.cached(kind, row["id"])
                page = {"id": row["id"], "windowId": row.get("windowId", 0), "url": row["url"],
                        "title": row.get("title", ""), "headings": [], "text": ""}
                cached = self.store.index.cached_page(page) if not refresh else None
                if cached and (requested == 0 or self.has_page_text(row, cached)):
                    page.update(headings=cached["headings"], text=cached["text"])
                    if cached.get("image_id"):
                        page["image_id"] = cached["image_id"]
                    return row, page, max(requested, 1 if page["text"] else 0), True, None
                if not refresh and previous and previous[1] == self.fingerprint(row) and previous[3] >= requested:
                    document = self.store.db.execute("SELECT headings,content FROM documents WHERE id=?", (previous[2],)).fetchone()
                    if document:
                        page.update(headings=json.loads(document[0]), text=document[1])
                        return row, page, previous[3], True, None
                if requested:
                    arguments = {"tabId": int(row["id"])} if kind == "tabs" else {"bookmarkId": row["id"]}
                    try:
                        evidence = await self.bridge.call("inspect_page", {**arguments, "detail": "text"})
                        if kind == "tabs" and evidence["url"] != row["url"]:
                            raise ValueError("tab_navigated_during_extraction")
                        page.update(headings=evidence["headings"], text=evidence["text"])
                    except (RuntimeError, ValueError) as error:
                        return row, page, requested, False, str(error)[:200]
                return row, page, requested, False, None

            for offset in range(0, total, 8):
                tasks = [asyncio.create_task(prepare(row)) for row in rows[offset:offset + 8]]
                self.progress(job, "extracting_text" if requested else "indexing", inFlight=len(tasks))
                finished = 0
                try:
                    for task in asyncio.as_completed(tasks):
                        row, page, actual_level, cached, error = await task
                        finished += 1
                        if error:
                            failed.append({"id": row["id"], "stage": "extracting_text", "reason": error})
                        else:
                            document = self.store.index.document(page)[0]
                            with self.store.lock, self.store.db:
                                self.store.db.execute("INSERT OR REPLACE INTO resources VALUES (?,?,?,?,?,?,?,?)",
                                    (kind, row["id"], json.dumps(row), self.fingerprint(row), document, actual_level, None, None))
                                self.store.db.execute("DELETE FROM resources_fts WHERE kind=? AND id=?", (kind, row["id"]))
                                self.store.db.execute("INSERT INTO resources_fts VALUES (?,?,?,?,?)",
                                    (kind, row["id"], page["title"], row["url"], " ".join(page["headings"]) + "\n" + page["text"]))
                            reused += int(cached)
                            changed += int(not cached)
                        self.progress(job, "extracting_text" if requested else "indexing", currentResource=row["id"],
                            completed=changed, indexed=changed, cached=reused, reused=reused, failed=len(failed),
                            extracted=changed if requested else 0, extractionFailed=len(failed),
                            failedResources=failed[:20], pending=total - changed - reused - len(failed), inFlight=len(tasks) - finished)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            return {"resource": kind, "indexed": changed, "completed": changed, "reused": reused, "cached": reused,
                    "failed": len(failed), "failures": failed[:20], "pending": 0, "total": total, "level": level, "verified": True}

    async def search(self, kind, query, offset, limit):
        rows = self.web(await self.records(kind))
        live = {row["id"]: row for row in rows}
        words = re.findall(r"\w+", query, flags=re.UNICODE)[:20]
        if not words:
            raise ValueError("query must contain searchable words")
        match = " AND ".join('"' + word + '"' for word in words)
        with self.store.lock:
            hits = self.store.db.execute("SELECT f.id,snippet(resources_fts,4,'','',' ... ',32),r.fingerprint "
                "FROM resources_fts f JOIN resources r ON f.kind=r.kind AND f.id=r.id "
                "WHERE resources_fts MATCH ? AND f.kind=? ORDER BY bm25(resources_fts)", (match, kind)).fetchall()
        snippets = {identifier: snippet for identifier, snippet, fingerprint in hits
                    if identifier in live and fingerprint == self.fingerprint(live[identifier])}
        ordered = list(snippets)
        for row in rows:
            metadata = " ".join(str(row.get(key, "")) for key in ("title", "url", "folder", "groupTitle")).casefold()
            if row["id"] not in snippets and all(word.casefold() in metadata for word in words):
                ordered.append(row["id"])
        items = [{**self.compact(live[key], True), "snippet": snippets.get(key, "")[:300]} for key in ordered[offset:offset + limit]]
        return {"total": len(ordered), "offset": offset, "returned": len(items), "hasMore": offset + len(items) < len(ordered),
                "nextOffset": offset + len(items) if offset + len(items) < len(ordered) else None,
                "method": "fts5_bm25", "items": items}

    async def selection(self, action, kind, ids=None, query="", selection_ids=None, operation="union", selection_id="", offset=0, limit=20):
        if action == "create":
            if ids is not None and query:
                raise ValueError("Provide exact resource IDs or a search query, not both")
            if query:
                result = await self.search(kind, query, 0, 5001)
                selected = [item["id"] for item in result["items"]]
                if result["total"] > 5000:
                    raise ValueError("Search selection exceeds 5000 matches; refine the query")
            else:
                selected = list(dict.fromkeys(map(str, ids))) if ids is not None else [row["id"] for row in self.web(await self.records(kind))]
            if not selected or len(selected) > 5000:
                raise ValueError("Selection needs 1 to 5000 matching resources")
            rows = await self.records(kind, selected)
            items = [{"id": row["id"], "fingerprint": self.fingerprint(row)} for row in rows]
            identifier = "sel_" + secrets.token_hex(8)
            with self.store.lock, self.store.db:
                self.store.db.execute("INSERT INTO selections(id,kind,items) VALUES (?,?,?)", (identifier, kind, json.dumps(items)))
            return {"selection_id": identifier, "resource": kind, "count": len(items), "examples": [self.compact(row) for row in rows[:5]]}
        if action == "combine":
            if operation not in {"union", "intersect", "exclude"} or not selection_ids or len(selection_ids) < 2:
                raise ValueError("Combine needs at least two selections and union, intersect, or exclude")
            values = [self.get_selection(identifier, kind) for identifier in selection_ids]
            sets = [{item["id"]: item for item in value} for value in values]
            if operation == "union":
                combined = {key: item for group in sets for key, item in group.items()}
            elif operation == "intersect":
                combined = {key: item for key, item in sets[0].items() if all(key in group for group in sets[1:])}
            else:
                combined = {key: item for key, item in sets[0].items() if all(key not in group for group in sets[1:])}
            if not combined:
                raise ValueError("Combined selection is empty")
            if len(combined) > 5000:
                raise ValueError("Combined selection exceeds 5000 resources; refine it first")
            identifier = "sel_" + secrets.token_hex(8)
            with self.store.lock, self.store.db:
                self.store.db.execute("INSERT INTO selections(id,kind,items) VALUES (?,?,?)", (identifier, kind, json.dumps(list(combined.values()))))
            return {"selection_id": identifier, "resource": kind, "count": len(combined), "operation": operation}
        if action == "show":
            rows = await self.current_selection(kind, selection_id)
            return {"selection_id": selection_id, "resource": kind, "total": len(rows), "offset": offset,
                    "returned": len(rows[offset:offset+limit]), "hasMore": offset + limit < len(rows),
                    "nextOffset": offset + limit if offset + limit < len(rows) else None,
                    "items": [self.compact(row) for row in rows[offset:offset+limit]]}
        raise ValueError("Unsupported selection action")

    def get_selection(self, identifier, kind):
        with self.store.lock:
            row = self.store.db.execute("SELECT kind,items FROM selections WHERE id=?", (identifier,)).fetchone()
        if not row or row[0] != kind:
            raise ValueError("Selection not found for this resource type")
        return json.loads(row[1])

    async def current_selection(self, kind, identifier):
        items = self.get_selection(identifier, kind)
        rows = await self.records(kind, [item["id"] for item in items])
        current = {row["id"]: row for row in rows}
        if any(self.fingerprint(current[item["id"]]) != item["fingerprint"] for item in items):
            raise ValueError("A selected resource changed; refresh the selection before mutating")
        return rows

    async def assign_selections(self, kind, assignments, parent_id=None, target_group_id=None):
        if not assignments or len(assignments) > 100:
            raise ValueError("Provide 1 to 100 named selection assignments")
        if target_group_id is not None and kind != "tabs":
            raise ValueError("A Chrome group target is valid only for tabs")
        if parent_id is not None and kind != "bookmarks":
            raise ValueError("A bookmark parent folder is valid only for bookmarks")
        selected, categories = {}, []
        for assignment in assignments:
            if set(assignment) != {"selection_id", "title"}:
                raise ValueError("Each assignment must contain exactly selection_id and title")
            title = assignment.get("title", "").strip()
            if not title or len(title) > 80:
                raise ValueError("Every assignment needs a title of at most 80 characters")
            items = self.get_selection(assignment.get("selection_id", ""), kind)
            category_ids = []
            for item in items:
                if item["id"] in selected:
                    raise ValueError("Selections overlap; refine them before assigning to avoid ambiguity")
                selected[item["id"]] = item["fingerprint"]
                category_ids.append(item["id"])
            if not category_ids:
                raise ValueError("Cannot assign an empty selection")
            categories.append({"title": title, "ids": category_ids})
        if len(selected) > 5000:
            raise ValueError("Assignments exceed 5000 resources; narrow the selections")
        current = {row["id"]: row for row in await self.records(kind, list(selected))}
        if any(self.fingerprint(current[key]) != fingerprint for key, fingerprint in selected.items()):
            raise ValueError("A selected resource changed; refresh the selection before mutating")
        if len(self.web(list(current.values()))) != len(current):
            raise ValueError("Only ordinary HTTP/HTTPS resources can be grouped")
        if kind == "tabs":
            if target_group_id is not None:
                if len(assignments) != 1 or len({row["windowId"] for row in current.values()}) != 1:
                    raise ValueError("Moving to an existing Chrome group needs one selection from its single window")
                group_tabs = await self.bridge.call("capture_browser")
                group = next((row for row in group_tabs.get("tabs", []) if row.get("groupId") == target_group_id), None)
                if not group or group.get("windowId") not in {row["windowId"] for row in current.values()}:
                    raise ValueError("Target Chrome group is missing or belongs to another window")
                result = await self.mutate("group_action", {"action": "add", "groupId": target_group_id,
                    "tabIds": [int(key) for key in current]})
                return {**result, "resource": kind, "selectionCount": len(selected), "verified": True}
            expected = {key: current[key]["url"] for key in selected}
            groups = []
            for category in categories:
                windows = defaultdict(list)
                for key in category["ids"]:
                    windows[current[key]["windowId"]].append(int(key))
                groups.extend({"title": category["title"], "tabIds": values} for values in windows.values())
            if len(groups) > 100:
                raise ValueError("Assignments would create more than 100 native groups; narrow the selection")
            result = await self.mutate("group_categories", {"categories": groups, "mode": "batch", "expectedUrls": expected})
        else:
            if parent_id is None:
                roots = [row for row in (await self.bridge.call("bookmark_tree"))["nodes"] if row.get("parentId") == "0" and not row.get("managed")]
                if not roots:
                    raise ValueError("No writable bookmark root is available")
                parent_id = roots[0]["id"]
            result = await self.mutate("bookmark_action", {"action": "group", "parentId": parent_id, "groups": categories})
        return {**result, "resource": kind, "selectionCount": len(selected), "verified": True}

    @staticmethod
    def compact(row, urls=False):
        result = {key: row[key] for key in ("id", "title", "parentId", "folder", "windowId", "groupId", "groupTitle", "index", "discarded", "status") if key in row}
        if row.get("kind") == "bookmarks":
            result["type"] = "bookmark" if row.get("url") else "folder"
            result["managed"] = row.get("managed", False)
        if urls and row.get("url"):
            result["url"] = row["url"]
        return result


    async def pending_job(self):
        for identifier, task in self.tasks.items():
            if not task.done():
                job = self.store.get_job(identifier)
                return {**await self.job("status", job["resource"], None, job["level"], False, identifier),
                        "requestStarted": False, "message": "Another local job is still working. Follow its nextCall; do not start a duplicate."}


    async def job(self, action, resource, ids, level, confirm, job_id="", refresh=False):
        if action == "start":
            permission = self.permission(resource, confirm, ids)
            if permission:
                return permission
            pending = await self.pending_job()
            if pending:
                return pending
            records = self.web(await self.records(resource, ids, expand=True))
            pending = await self.pending_job()
            if pending:
                return pending
            job = {"id": secrets.token_hex(8), "resource": resource, "level": level, "items": [row["id"] for row in records],
                   "total": len(records), "completed": 0, "cached": 0, "reused": 0, "failed": 0,
                   "extracted": 0, "extractionFailed": 0, "inFlight": 0,
                   "pending": len(records), "failedResources": [], "state": "running", "stage": "validating", "refresh": refresh,
                   "noBrowserChanges": True, "startedAt": time.time(), "updatedAt": time.time()}
            self.store.save_job(job)
            self.tasks[job["id"]] = asyncio.create_task(self.run_job(job))
        else:
            job = self.store.get_job(job_id)
            if job.get("resource") not in {None, resource}:
                raise ValueError("This job belongs to another resource type")
            active = job["id"] in self.tasks and not self.tasks[job["id"]].done()
            if action == "cancel":
                if job.get("stage") == "grouping" and active:
                    raise ValueError("Chrome mutation is in progress; wait for its verified result before undoing")
                if active:
                    job["state"] = "cancel_requested"
                elif job["state"] in {"running", "cancel_requested"}:
                    job["state"] = "interrupted"
                self.store.save_job(job)
            elif action == "resume":
                if active or "resource" not in job or job.get("type") == "grouping":
                    raise ValueError("Job is active or predates this index format")
                # Revisit the original scope; committed representations are reused by index().
                job.update(total=len(job["items"]), completed=0, cached=0, reused=0, indexed=0, failed=0,
                           extracted=0, extractionFailed=0, inFlight=0, pending=len(job["items"]), failedResources=[])
                job["state"] = "running"
                job.update(stage="validating", startedAt=time.time(), updatedAt=time.time())
                job.pop("error", None)
                job.pop("errorStage", None)
                job.pop("result", None)
                self.store.save_job(job)
                self.tasks[job["id"]] = asyncio.create_task(self.run_job(job))
            elif not active and job["state"] in {"running", "cancel_requested"}:
                job["state"] = "interrupted"
                self.store.save_job(job)
        result = {key: job[key] for key in ("id", "type", "state", "resource", "mode", "level", "stage", "completed", "cached", "reused", "indexed", "failed", "failedResources", "pending", "extracted", "extractionFailed", "inFlight", "currentResource", "error", "errorStage", "noBrowserChanges", "result") if key in job} | {"total": job.get("total", len(job["items"]))}
        if "startedAt" in job:
            endpoint = time.time() if job["state"] in {"running", "cancel_requested"} else job.get("updatedAt", time.time())
            result.update(elapsedSeconds=max(0, int(endpoint - job["startedAt"])),
                          secondsSinceProgress=max(0, int(time.time() - job.get("updatedAt", job["startedAt"]))))
        if job["state"] in {"running", "cancel_requested"}:
            result.update(nextAction="poll", waitingFor=job.get("stage", "local processing"),
                          nextCall={"tool": "index_resources",
                                    "arguments": {"action": "status", "resource": job["resource"], "job_id": job["id"], "wait_seconds": 5}})
        else:
            result["nextAction"] = "continue" if job["state"] == "complete" else "stop"
        return result

    async def run_job(self, job):
        try:
            result = await self.index(job["resource"], job["items"], job["level"], confirm=False, refresh=job.get("refresh", False), job=job)
            if not result.get("verified"):
                raise RuntimeError("Indexing prerequisites changed; inspect setup/consent before resuming")
            job["result"] = {key: result[key] for key in ("indexed", "reused", "failed", "failures", "pending", "total", "level")}
            job.update(state="complete", stage="complete_with_failures" if job.get("failed") else "complete")
        except asyncio.CancelledError:
            job.update(state="cancelled", stage="cancelled")
        except Exception as error:
            job.update(state="failed", error=str(error)[:500], errorStage=job.get("stage"), noBrowserChanges=True,
                       pending=max(0, job.get("total", len(job["items"])) - job.get("completed", 0) - job.get("cached", 0) - job.get("failed", 0)))
        job["updatedAt"] = time.time()
        self.store.save_job(job)


def register_library_tools(mcp, library):
    Kind = Literal["tabs", "bookmarks"]
    ResourceID = str | int
    Limit = Annotated[int, Field(ge=1, le=100)]
    Offset = Annotated[int, Field(ge=0)]

    def errors(function):
        @wraps(function)
        async def guarded(*args, **kwargs):
            try:
                return await function(*args, **kwargs)
            except (ValueError, RuntimeError, KeyError, OSError, sqlite3.Error, httpx.HTTPError) as error:
                raise ToolError(str(error)) from error
        return guarded

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    @errors
    async def list_resources(resource: Kind, query: str = "", ids: list[ResourceID] | None = None,
                             offset: Offset = 0, limit: Limit = 20, include_urls: bool = True, group_id: int | None = None) -> dict[str, Any]:
        """List/filter tabs OR explicitly requested bookmarks. Bounded metadata, URLs only when requested. Folders are included for bookmarks."""
        ids = list(dict.fromkeys(map(str, ids))) if ids is not None else None
        rows = await library.records(resource, ids)
        if group_id is not None:
            if resource != "tabs":
                raise ValueError("group_id is valid only for tabs")
            rows = [row for row in rows if row.get("groupId") == group_id]
        words = query.casefold().split()
        rows = [row for row in rows if all(word in " ".join(str(row.get(key, "")) for key in ("title", "url", "folder", "groupTitle")).casefold() for word in words)]
        return {"resource": resource, "total": len(rows), "offset": offset, "returned": len(rows[offset:offset+limit]),
                "hasMore": offset + limit < len(rows), "nextOffset": offset + limit if offset + limit < len(rows) else None,
                "items": [library.compact(row, include_urls) for row in rows[offset:offset+limit]]}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    @errors
    async def index_resources(ctx: Context, resource: Kind, action: Literal["start", "status", "resume", "cancel"] = "start",
                              ids: list[ResourceID] | None = None, level: Literal["metadata", "text"] = "text",
                              confirm_index: Annotated[bool, Field(description="True only after explicit user approval for this scope; do not infer from a grouping request or unrelated ok")] = False, job_id: str = "", wait_seconds: Annotated[int, Field(ge=0, le=30)] = 0,
                              refresh: bool = False, group_id: int | None = None) -> dict[str, Any]:
        """Create/reuse a LOCAL index only after explicit user permission. Never self-authorize confirm_index. Poll running jobs using nextCall until complete/failed; do not finish by promising to poll later. ids omitted means all eligible resources, not the first list page."""
        if action != "start" and library.store.get_job(job_id).get("type") == "grouping":
            raise ValueError("Legacy grouping jobs cannot be resumed; indexing never applies grouping")
        ids = list(dict.fromkeys(map(str, ids))) if ids is not None else None
        if group_id is not None:
            if resource != "tabs":
                raise ValueError("group_id is valid only for tabs")
            ids = [row["id"] for row in await library.records(resource, ids) if row.get("groupId") == group_id]
        result = await library.job(action, resource, ids, level, confirm_index, job_id, refresh)
        last_progress = None
        progress_step = 0
        if action == "status" and result.get("state") in {"running", "cancel_requested"}:
            for _ in range(wait_seconds * 2):
                await asyncio.sleep(.5)
                result = await library.job("status", resource, None, level, False, result["id"])
                marker = (result.get("stage"), result.get("completed"), result.get("cached"), result.get("failed"),
                          result.get("extracted"), result.get("extractionFailed"), result.get("currentResource"))
                if marker != last_progress:
                    progress_step += 1
                    await ctx.report_progress(progress_step, None,
                                              f"{result.get('stage')}: {result.get('extracted', 0)} extracted, {result.get('extractionFailed', 0)} extraction failures, {result.get('inFlight', 0)} in flight, {result.get('completed', 0)} indexed, {result.get('cached', 0)} cached, {result.get('failed', 0)} failed, {result.get('pending', 0)} pending")
                    last_progress = marker
                if result["state"] not in {"running", "cancel_requested"}:
                    break
        if result.get("state") in {"running", "cancel_requested"}:
            result.setdefault("nextAction", "poll")
            result.setdefault("nextCall", {"tool": "index_resources", "arguments": {"action": "status", "resource": result["resource"], "job_id": result["id"], "wait_seconds": 5}})
            result["nextStep"] = "Briefly report stage, extracted, inFlight, completed, cached, failed and pending counts, then execute nextCall. Running is not completion. On failure report the exact errorStage and error, not an invented inference failure."
        elif result.get("state") in {"failed", "interrupted"}:
            result["nextStep"] = "Report the exact errorStage/currentResource/error. No grouping was applied by indexing. Report browser extraction errors accurately or retry blindly."
        elif result.get("state") == "complete" and result.get("failed", 0):
            result["nextStep"] = "Indexing completed with partial coverage. Continue using successfully indexed resources, report failedResources, and retry only those pages if the user asks."
        return result

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    @errors
    async def search_resources(resource: Kind, query: Annotated[str, Field(min_length=1, max_length=500)],
                               offset: Offset = 0, limit: Limit = 20) -> dict[str, Any]:
        """Local FTS5 keyword search across explicitly chosen tabs or bookmarks; only matching compact results reach the agent."""
        return await library.search(resource, query, offset, limit)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    @errors
    async def selection_action(action: Literal["create", "combine", "show"], resource: Kind, ids: list[ResourceID] | None = None,
                               query: str = "",
                               selection_ids: list[str] | None = None,
                               operation: Literal["union", "intersect", "exclude"] = "union",
                               selection_id: str = "", offset: Offset = 0, limit: Limit = 20, group_id: int | None = None) -> dict[str, Any]:
        """Create a local bulk selection from exact IDs or a keyword query, combine selections, or show compact examples. A query selection contains all matching resources, not just the current page. Selections are revalidated before mutation."""
        ids = list(dict.fromkeys(map(str, ids))) if ids is not None else None
        if group_id is not None:
            if resource != "tabs" or action != "create" or query:
                raise ValueError("group_id scopes exact tab selection creation; combine with a search selection separately")
            ids = [row["id"] for row in await library.records(resource, ids) if row.get("groupId") == group_id]
        return await library.selection(action, resource, ids, query, selection_ids, operation, selection_id, offset, limit)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True))
    @errors
    async def group_selections(resource: Kind, assignments: list[dict[str, str]], parent_id: str | None = None) -> dict[str, Any]:
        """Apply exact agent-decided selection assignments directly. Each assignment is {selection_id,title}; tabs stay open. Bookmark use must be explicitly requested. Every target is checked against live metadata before native grouping/folder operations; undo_last restores changes."""
        return await library.assign_selections(resource, assignments, parent_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True))
    @errors
    async def move_selection(selection_id: str, group_id: int) -> dict[str, Any]:
        """Move one exact tab selection into an existing native Chrome group in the same window. Use selection_action first; verified and undoable."""
        return await library.assign_selections("tabs", [{"selection_id": selection_id, "title": "Existing group"}], target_group_id=group_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True))
    @errors
    async def tab_selection(action: Literal["move", "close"], selection_id: str, window_id: int | None = None,
                            index: int | None = None, confirmed: bool = False) -> dict[str, Any]:
        """Move/reorder or close an exact local selection using native bulk Chrome APIs. Closing requires user confirmation. Verified and undoable."""
        rows = await library.current_selection("tabs", selection_id)
        if action == "close" and not confirmed:
            return {"requiresConfirmation": True, "count": len(rows), "executed": False, "noChangesMade": True,
                    "question": f"Close these {len(rows)} tabs?", "nextAction": "ask_user", "waitingFor": "user"}
        if action == "move" and window_id is None:
            raise ValueError("move requires window_id")
        arguments = {"tabIds": [int(row["id"]) for row in rows], "mode": "batch"}
        if action == "move":
            arguments.update(windowId=window_id, index=index)
        return await library.mutate("move_tabs" if action == "move" else "close_tabs", arguments)


    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True))
    @errors
    async def bookmark_action(action: Literal["create", "rename", "move", "delete", "save_tabs", "open"],
                              ids: list[ResourceID] | None = None, parent_id: str | None = None, title: str | None = None,
                              url: str | None = None, index: int | None = None, window_id: int | None = None,
                              warned: bool = False, selection_id: str | None = None,
                              confirmed: Annotated[bool, Field(description="True only after the user confirms this exact deletion; a warning alone is insufficient")] = False) -> dict[str, Any]:
        """Explicit bookmark operations. Use selection_id for bulk selections. Deletion requires the agent to ask the user for confirmation of the exact scope before confirmed=true; warned is retained only for older clients."""
        if selection_id and ids:
            raise ValueError("Provide ids or a bookmark selection_id, not both")
        ids = list(dict.fromkeys(map(str, ids))) if ids is not None else None
        if selection_id:
            if action == "create":
                raise ValueError("Creating a bookmark does not consume a selection")
            kind = "tabs" if action == "save_tabs" else "bookmarks"
            ids = [row["id"] for row in await library.current_selection(kind, selection_id)]
        if ids is not None and len(ids) > 5000:
            raise ValueError("Operate on at most 5000 resources")
        if action == "delete" and not confirmed:
            return {"requiresConfirmation": True, "question": f"Delete the selected {len(ids or [])} bookmarks/folders, including folder descendants?",
                    "warning": "Undo may recreate bookmarks with new IDs.", "executed": False, "noChangesMade": True,
                    "nextAction": "ask_user", "waitingFor": "user",
                    "nextStep": "Use the agent's native question/confirmation tool (or ask in chat) and wait for the user's answer. Set confirmed=true only after confirmation of this exact selection."}
        if action == "open" and ids:
            ids = [row["id"] for row in library.web(await library.records("bookmarks", ids, expand=True))]
        return await library.mutate("bookmark_action", {key: value for key, value in {
            "action": action, "ids": ids or [], "parentId": parent_id, "title": title, "url": url,
            "index": index, "windowId": window_id}.items() if value is not None})

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    @errors
    async def find_duplicates(resource: Kind, offset: Offset = 0, limit: Limit = 20) -> dict[str, Any]:
        """Find exact-URL duplicates in explicitly selected tabs or bookmarks. Preview only; deletion/closing requires separate user-confirmed operations."""
        grouped = defaultdict(list)
        for row in library.web(await library.records(resource)):
            grouped[row["url"]].append(row)
        duplicates = [rows for rows in grouped.values() if len(rows) > 1]
        return {"resource": resource, "total": len(duplicates), "offset": offset,
                "duplicates": [{"url": rows[0]["url"], "count": len(rows), "samples": [library.compact(row) for row in rows[:20]]}
                               for rows in duplicates[offset:offset+limit]]}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=False))
    @errors
    async def export_urls(resource: Kind, format: Literal["txt", "csv"] = "txt", ids: list[ResourceID] | None = None,
                          query: str = "", tab_id_for_links: int | None = None, selection_id: str | None = None) -> dict[str, Any]:
        """Export complete selected URL collections to a local TXT/CSV file without flooding agent context. Optional tab_id_for_links exports links inside a selected webpage."""
        ids = list(dict.fromkeys(map(str, ids))) if ids is not None else None
        if selection_id:
            if ids is not None or tab_id_for_links is not None:
                raise ValueError("Use a selection, exact IDs, or a page-link target, not multiple scopes")
            ids = [row["id"] for row in await library.current_selection(resource, selection_id)]
        if tab_id_for_links is not None:
            result = await library.bridge.call("inspect_page", {"tabId": tab_id_for_links, "detail": "links", "limit": 5000})
            rows = result["links"]
        else:
            rows = [row for row in await library.records(resource, ids, expand=True) if row.get("url")]
            rows = [row for row in rows if query.casefold() in (row.get("title", "") + " " + row["url"]).casefold()]
        directory = library.state / "exports"
        directory.mkdir(exist_ok=True)
        path = directory / f"{resource}-{secrets.token_hex(6)}.{format}"
        if format == "txt":
            path.write_text("\n".join(row["url"] for row in rows) + "\n", encoding="utf-8")
        else:
            def safe(value):
                text = str(value or "")
                return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["title", "url", "parent_id"])
                writer.writerows([safe(row.get("title")), safe(row["url"]), safe(row.get("parentId"))] for row in rows)
        return {"path": str(path.resolve()), "count": len(rows), "format": format, "verified": path.exists()}

    @mcp.tool(structured_output=False, annotations=ToolAnnotations(destructive_hint=False))
    @errors
    async def inspect_page(resource: Kind, id: str, detail: Literal["text", "image", "links", "accessibility"] = "text",
                           offset: Offset = 0, limit: Limit = 20, refresh: bool = False) -> CallToolResult:
        """Explicit selected-page evidence for the connected agent, including actual masked MCP images. Suspended tabs are inspected on demand. Bookmark inspection uses a disposable tab and closes only that temporary tab."""
        arguments = {"tabId": int(id)} if resource == "tabs" else {"bookmarkId": id}
        cached = library.cached(resource, id) if not refresh else None
        if cached:
            try:
                current = (await library.records(resource, [id]))[0]
            except ValueError:
                current = None
            if current and cached[1] != library.fingerprint(current):
                cached = None
        result = None
        if cached:
            if detail == "text" and cached[3] >= 1:
                row = library.store.db.execute("SELECT title,headings,content FROM documents WHERE id=?", (cached[2],)).fetchone()
                result = {"id": id, "title": row[0], "headings": json.loads(row[1]), "text": row[2], "cached": True}
            elif detail == "image" and cached[3] >= 2:
                row = library.store.db.execute("SELECT content FROM artifacts a JOIN document_artifacts d ON a.id=d.artifact_id WHERE d.document_id=? AND a.kind='image'", (cached[2],)).fetchone()
                if row:
                    result = {"id": id, "image": base64.b64encode(row[0]).decode(), "cached": True}
        if result is None:
            async with library.lock:
                result = await library.bridge.call("inspect_page", {**arguments, "detail": detail, "offset": offset, "limit": limit})
                if detail in {"text", "image"}:
                    current = (await library.records(resource, [id]))[0]
                    page = {"url": current["url"], "title": result.get("title", current.get("title", "")),
                            "headings": result.get("headings", []), "text": result.get("text", "")}
                    if result.get("image"):
                        page["image_id"] = library.store.index.image(result["image"])
                    document = library.store.index.document(page)[0]
                    with library.store.lock, library.store.db:
                        library.store.db.execute("INSERT OR REPLACE INTO resources VALUES (?,?,?,?,?,?,?,?)",
                            (resource, id, json.dumps(current), library.fingerprint(current), document, 2 if result.get("image") else 1, None, None))
                        library.store.db.execute("DELETE FROM resources_fts WHERE kind=? AND id=?", (resource, id))
                        library.store.db.execute("INSERT INTO resources_fts VALUES (?,?,?,?,?)",
                            (resource, id, page["title"], page["url"], " ".join(page["headings"]) + "\n" + page["text"]))
        image = result.pop("image", None)
        content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        if image:
            mime = "image/png" if base64.b64decode(image).startswith(b"\x89PNG") else "image/jpeg"
            content.append(ImageContent(type="image", data=image, mime_type=mime))
        return CallToolResult(content=content)
