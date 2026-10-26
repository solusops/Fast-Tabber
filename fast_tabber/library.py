"""Local tab/bookmark indexing and clustering. No generative inference or remote page fetching."""
import asyncio
import base64
import csv
import hashlib
import io
import json
import re
import secrets
import sqlite3
import struct
from collections import defaultdict
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field
from sklearn.cluster import AgglomerativeClustering
from sklearn.feature_extraction.text import TfidfVectorizer

from fast_tabber.semantic import cluster_vectors, cosine, embed_texts, embedding_digest
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
            store.db.execute("CREATE TABLE IF NOT EXISTS grouping_plans (id TEXT PRIMARY KEY,payload TEXT)")
            store.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS resources_fts USING fts5(kind UNINDEXED,id UNINDEXED,title,url,content)")

    def permission(self, kind, confirm=False, ids=None):
        path = self.state / "settings.json"
        settings = json.loads(path.read_text()) if path.exists() else {}
        if not settings.get("privacy_acknowledged"):
            return {"requiresDisclosure": True, "disclosure": DISCLOSURE, "started": False,
                    "nextStep": "Show the disclosure once and acknowledge it through onboarding before indexing."}
        allowed = settings.get("indexing_allowed", [])
        if kind not in allowed:
            scopes = settings.get("indexing_scopes", {})
            if ids is not None and set(map(str, ids)) <= set(scopes.get(kind, [])):
                return None
            if not confirm:
                return {"requiresIndexingConsent": True, "resource": kind, "started": False,
                        "message": "Allow a persistent local index of this resource? Deeper text/image inspection occurs only on demand."}
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

    async def index(self, kind, ids=None, level="metadata", confirm=False, refresh=False):
        permission = self.permission(kind, confirm, ids)
        if permission:
            return permission
        async with self.lock:
            all_rows = await self.records(kind, ids, expand=True)
            rows = self.web(all_rows)
            digest = await embedding_digest(self.state)
            model = digest + ":clustering-v1"
            requested = {"metadata": 0, "text": 1, "image": 2}[level]
            reused, changed, embedded, embedding_inputs = 0, 0, 0, 0
            for offset in range(0, len(rows), 50):
                prepared = []
                for row in rows[offset:offset + 50]:
                    previous = self.cached(kind, row["id"])
                    fingerprint = self.fingerprint(row)
                    if not refresh and previous and previous[1] == fingerprint and previous[3] >= requested and previous[5] == model:
                        with self.store.lock, self.store.db:
                            self.store.db.execute("UPDATE resources SET metadata=? WHERE kind=? AND id=?", (json.dumps(row), kind, row["id"]))
                        reused += 1
                        continue
                    page = {"id": row["id"], "windowId": row.get("windowId", 0), "url": row["url"],
                            "title": row.get("title", ""), "headings": [], "text": row["url"]}
                    cached = self.store.index.cached_page(page) if not refresh else None
                    if cached and requested >= 1 and not (previous and previous[3] >= 1) and cached["text"] == row["url"]:
                        cached = None
                    actual_level = requested
                    if cached:
                        page.update(headings=cached["headings"], text=cached["text"].split("\nVisual evidence:")[0])
                        actual_level = max(requested, previous[3] if previous else 1 if cached["text"] != row["url"] else 0)
                        if cached.get("image_id"):
                            page["image_id"] = cached["image_id"]
                        document = self.store.index.document(page)[0]
                    if requested == 1 and (refresh or not cached):
                        arguments = {"tabId": int(row["id"])} if kind == "tabs" else {"bookmarkId": row["id"]}
                        evidence = await self.bridge.call("inspect_page", {**arguments, "detail": "text"})
                        if kind == "tabs" and evidence["url"] != row["url"]:
                            raise RuntimeError("A tab navigated during extraction; no grouping was performed")
                        page.update(title=evidence["title"], headings=evidence["headings"], text=evidence["text"])
                    document = self.store.index.document(page)[0]
                    if requested == 2:
                        image_keys = [key for key in self.store.index.artifacts(document) if self.store.db.execute("SELECT kind FROM artifacts WHERE id=?", (key,)).fetchone()[0] == "image"]
                        if refresh or not image_keys:
                            arguments = {"tabId": int(row["id"])} if kind == "tabs" else {"bookmarkId": row["id"]}
                            capture = await self.bridge.call("inspect_page", {**arguments, "detail": "image"})
                            if kind == "tabs" and capture["url"] != row["url"]:
                                raise RuntimeError("A tab navigated during screenshot capture")
                            page["image_id"] = self.store.index.image(capture["image"])
                            page.update(title=capture["title"], headings=capture.get("headings", []), text=capture.get("text", ""))
                            document = self.store.index.document(page)[0]
                    prepared.append((row, fingerprint, page, document, actual_level))
                keys = {document: self.store.index.artifacts(document) for _, _, _, document, _ in prepared}
                pending = self.store.index.pending([key for values in keys.values() for key in values], model)
                computed = {key for key, _ in pending}
                embedding_inputs += len(pending)
                embedded += sum(any(key in computed for key in keys[document]) for _, _, _, document, _ in prepared)
                for start in range(0, len(pending), 16):
                    batch = pending[start:start + 16]
                    inputs = ["task: clustering | query: " + value if isinstance(value, str) else value for _, value in batch]
                    vectors, actual = await embed_texts(inputs, self.state, digest)
                    if actual != digest:
                        raise RuntimeError("Embedding model changed during indexing")
                    self.store.index.save_vectors([key for key, _ in batch], vectors, model)
                with self.store.lock, self.store.db:
                    for row, fingerprint, page, document, actual_level in prepared:
                        vector = self.store.index.page_vector(keys[document], model)
                        packed = struct.pack(f"<{len(vector)}e", *vector)
                        self.store.db.execute("INSERT OR REPLACE INTO resources VALUES (?,?,?,?,?,?,?,?)",
                            (kind, row["id"], json.dumps(row), fingerprint, document, actual_level, packed, model))
                        self.store.db.execute("DELETE FROM resources_fts WHERE kind=? AND id=?", (kind, row["id"]))
                        self.store.db.execute("INSERT INTO resources_fts VALUES (?,?,?,?,?)", (kind, row["id"], page["title"], row["url"], page["text"]))
                changed += len(prepared)
            return {"resource": kind, "indexed": changed, "reused": reused, "total": len(rows), "level": level,
                    "embedded": embedded, "embeddingInputs": embedding_inputs,
                    "skipped": sum(bool(row.get("url")) and row not in rows for row in all_rows),
                    "model": model, "verified": True}

    async def search(self, kind, query, offset, limit, mode="hybrid"):
        rows = self.web(await self.records(kind))
        live = {row["id"]: row for row in rows}
        valid = {identifier for identifier, fingerprint in self.store.db.execute("SELECT id,fingerprint FROM resources WHERE kind=?", (kind,))
                 if identifier in live and fingerprint == self.fingerprint(live[identifier])}
        scores = []
        if mode != "keyword":
            digest = await embedding_digest(self.state)
            vectors, _ = await embed_texts(["task: clustering | query: " + query], self.state, digest)
            for identifier, raw in self.store.db.execute("SELECT id,vector FROM resources WHERE kind=? AND model=?", (kind, digest + ":clustering-v1")):
                if identifier in valid:
                    scores.append((identifier, cosine(vectors[0], struct.unpack(f"<{len(raw)//2}e", raw))))
        ranks = defaultdict(float)
        for rank, (identifier, _) in enumerate(sorted(scores, key=lambda item: -item[1])):
            ranks[identifier] += 1 / (60 + rank)
        words = re.findall(r"\w+", query, flags=re.UNICODE)[:20]
        if words and mode != "semantic":
            match = (" AND " if mode == "keyword" else " OR ").join('"' + word + '"' for word in words)
            for rank, (identifier,) in enumerate(self.store.db.execute("SELECT id FROM resources_fts WHERE resources_fts MATCH ? AND kind=? ORDER BY rank", (match, kind))):
                if identifier in valid:
                    ranks[identifier] += 1 / (60 + rank)
        if not ranks and mode != "keyword":
            raise ValueError("No indexed resources are available; index the explicitly requested resource first")
        ordered = sorted(ranks, key=ranks.get, reverse=True)
        items = []
        for key in ordered[offset:offset+limit]:
            item = self.compact(live[key])
            row = self.store.db.execute("SELECT d.content FROM documents d JOIN resources r ON d.id=r.document WHERE r.kind=? AND r.id=?", (kind, key)).fetchone()
            text = row[0] if row else ""
            start = next((max(0, text.casefold().find(word.casefold()) - 60) for word in words if word.casefold() in text.casefold()), 0)
            item.update(snippet=text[start:start+300], origin=urlsplit(live[key]["url"]).netloc)
            items.append(item)
        return {"total": len(ordered), "offset": offset, "method": mode, "ranking": "Candidates, not guaranteed semantic matches",
                "items": items}

    @staticmethod
    def compact(row, urls=False):
        result = {key: row[key] for key in ("id", "title", "parentId", "folder", "windowId", "groupId", "groupTitle", "index") if key in row}
        if row.get("kind") == "bookmarks":
            result["type"] = "bookmark" if row.get("url") else "folder"
            result["managed"] = row.get("managed", False)
        if urls and row.get("url"):
            result["url"] = row["url"]
        return result

    def label(self, members):
        titles = [row.get("title", "") for row in members]
        if len(members) == 1 and titles[0].strip():
            return titles[0].strip()[:80]
        try:
            vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), max_features=1000,
                                       token_pattern=r"(?u)\b[^\W\d_][\w-]+\b")
            matrix = vectorizer.fit_transform(titles)
            weights = matrix.mean(axis=0).A1
            return str(vectorizer.get_feature_names_out()[int(weights.argmax())]).title()[:80]
        except ValueError:
            return (urlsplit(members[0]["url"]).hostname or "Related pages")[:80]

    async def propose(self, kind, ids, categories, max_groups, instruction, confirm):
        result = await self.index(kind, ids, confirm=confirm)
        if result.get("started") is False:
            return result
        rows = self.web(await self.records(kind, ids, expand=True))
        if not rows:
            raise ValueError("No ordinary web resources selected")
        if len(rows) > 5000:
            raise ValueError("Group at most 5000 resources per operation")
        vectors = []
        for row in rows:
            cached = self.cached(kind, row["id"])
            vectors.append(list(struct.unpack(f"<{len(cached[4])//2}e", cached[4])))
        grouped = defaultdict(list)
        if categories:
            category_vectors, _ = await embed_texts(["task: clustering | query: " + value for value in categories], self.state)
            for row, vector in zip(rows, vectors):
                similarities = sorted([(cosine(vector, other), title) for title, other in zip(categories, category_vectors)], reverse=True)
                if similarities[0][0] < 0.4 or len(similarities) > 1 and similarities[0][0] - similarities[1][0] < 0.025:
                    continue
                grouped[similarities[0][1]].append(row)
        else:
            labels = (AgglomerativeClustering(n_clusters=min(max_groups, len(rows)), metric="cosine", linkage="average").fit_predict(vectors).tolist()
                      if max_groups and len(rows) > 1 else cluster_vectors(vectors))
            for row, cluster in zip(rows, labels):
                if cluster >= 0:
                    grouped[str(cluster)].append(row)
        groups = []
        used = set()
        for title, members in grouped.items():
            label = title if categories else self.label(members)
            if label in used:
                label = f"{label} {len(groups)+1}"
            used.add(label)
            groups.append({"id": str(len(groups)+1), "title": label, "ids": [row["id"] for row in members]})
        plan = {"id": secrets.token_hex(8), "resource": kind, "instruction": instruction, "groups": groups,
                "selection": rows, "state": "ready", "unclassified": len(rows) - sum(len(group["ids"]) for group in groups)}
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO grouping_plans VALUES (?,?)", (plan["id"], json.dumps(plan)))
        return self.show_plan(plan)

    def plan(self, identifier):
        row = self.store.db.execute("SELECT payload FROM grouping_plans WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("Grouping plan not found")
        return json.loads(row[0])

    def show_plan(self, plan, offset=0, limit=20):
        rows = {row["id"]: row for row in plan["selection"]}
        return {"planId": plan["id"], "resource": plan["resource"], "state": plan["state"], "applied": plan["state"] == "applied",
                "proposalOnly": plan["state"] == "ready", "nextStep": "Agent reviews selected evidence, then apply_grouping(plan_id, names) to make actual browser changes.", "groupCount": len(plan["groups"]),
                "unclassified": plan["unclassified"], "offset": offset, "groups": [{"planGroupId": group["id"], "title": group["title"],
                "count": len(group["ids"]), "examples": [self.compact(rows[key]) for key in group["ids"][:3]]} for group in plan["groups"][offset:offset+limit]]}

    async def apply(self, plan, parent_id=None):
        if plan["state"] != "ready":
            raise ValueError("This plan was already applied or needs reconciliation; do not retry it blindly")
        current = {row["id"]: row for row in await self.records(plan["resource"], [row["id"] for row in plan["selection"]])}
        if any(self.fingerprint(row) != self.fingerprint(current[row["id"]]) for row in plan["selection"]):
            raise ValueError("A selected title or URL changed; generate a new plan on demand")
        if not plan["groups"]:
            return {"grouped": 0, "unclassified": plan["unclassified"], "verified": True}
        # Persist uncertain state before any browser mutation.
        plan["state"] = "applying"
        self.save_plan(plan)
        if plan["resource"] == "tabs":
            groups = []
            for group in plan["groups"]:
                windows = defaultdict(list)
                for key in group["ids"]:
                    windows[current[key]["windowId"]].append(int(key))
                groups.extend({"title": group["title"], "tabIds": values} for values in windows.values())
            result = await self.mutate("group_categories", {"categories": groups, "mode": "batch",
                "expectedUrls": {key: row["url"] for key, row in current.items()}})
        else:
            if parent_id is None:
                roots = [row for row in (await self.bridge.call("bookmark_tree"))["nodes"] if row.get("parentId") == "0" and not row.get("managed")]
                if not roots:
                    raise ValueError("No writable bookmark root is available")
                parent_id = roots[0]["id"]
            result = await self.mutate("bookmark_action", {"action": "group", "parentId": parent_id,
                "groups": [{"title": group["title"], "ids": group["ids"]} for group in plan["groups"]]})
        plan["state"] = "applied"
        self.save_plan(plan)
        return {**result, "resource": plan["resource"], "unclassified": plan["unclassified"],
                "names": [group["title"] for group in plan["groups"][:20]], "verified": True}

    def save_plan(self, plan):
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE grouping_plans SET payload=? WHERE id=?", (json.dumps(plan), plan["id"]))

    async def job(self, action, resource, ids, level, confirm, job_id="", refresh=False):
        if action == "start":
            permission = self.permission(resource, confirm, ids)
            if permission:
                return permission
            if any(not task.done() for task in self.tasks.values()):
                raise ValueError("An indexing job is already running")
            records = self.web(await self.records(resource, ids, expand=True))
            job = {"id": secrets.token_hex(8), "resource": resource, "level": level, "items": [row["id"] for row in records], "completed": 0, "state": "running", "refresh": refresh}
            self.store.save_job(job)
            self.tasks[job["id"]] = asyncio.create_task(self.run_job(job))
        else:
            job = self.store.get_job(job_id)
            active = job["id"] in self.tasks and not self.tasks[job["id"]].done()
            if action == "cancel":
                job["state"] = "cancel_requested" if active else "cancelled"
                self.store.save_job(job)
            elif action == "resume":
                if active or "resource" not in job:
                    raise ValueError("Job is active or predates this index format")
                job["state"] = "running"
                self.store.save_job(job)
                self.tasks[job["id"]] = asyncio.create_task(self.run_job(job))
            elif not active and job["state"] in {"running", "cancel_requested"}:
                job["state"] = "interrupted"
                self.store.save_job(job)
        return {key: job.get(key) for key in ("id", "state", "resource", "level", "completed", "error")} | {"total": len(job["items"])}

    async def run_job(self, job):
        try:
            for offset in range(job["completed"], len(job["items"]), 50):
                if self.store.get_job(job["id"])["state"] == "cancel_requested":
                    job["state"] = "cancelled"
                    break
                result = await self.index(job["resource"], job["items"][offset:offset+50], job["level"], confirm=True, refresh=job.get("refresh", False))
                if not result.get("verified"):
                    raise RuntimeError("Indexing prerequisites changed; inspect setup/consent before resuming")
                job["completed"] = min(offset + 50, len(job["items"]))
                self.store.save_job(job)
                await asyncio.sleep(0)
            else:
                job["state"] = "complete"
        except Exception as error:
            job.update(state="failed", error=str(error)[:500])
        self.store.save_job(job)


def register_library_tools(mcp, library):
    Kind = Literal["tabs", "bookmarks"]
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
    async def list_resources(resource: Kind, query: str = "", ids: list[str] | None = None,
                             offset: Offset = 0, limit: Limit = 20, include_urls: bool = False) -> dict[str, Any]:
        """List/filter tabs OR explicitly requested bookmarks. Bounded metadata, URLs only when requested. Folders are included for bookmarks."""
        rows = await library.records(resource, ids)
        words = query.casefold().split()
        rows = [row for row in rows if all(word in " ".join(str(row.get(key, "")) for key in ("title", "url", "folder", "groupTitle")).casefold() for word in words)]
        return {"resource": resource, "total": len(rows), "offset": offset,
                "items": [library.compact(row, include_urls) for row in rows[offset:offset+limit]]}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    @errors
    async def index_resources(resource: Kind, action: Literal["start", "status", "resume", "cancel"] = "start",
                              ids: list[str] | None = None, level: Literal["metadata", "text", "image"] = "metadata",
                              confirm_index: bool = False, job_id: str = "", wait_seconds: Annotated[int, Field(ge=0, le=30)] = 0,
                              refresh: bool = False) -> dict[str, Any]:
        """On-demand persistent indexing. Start with metadata; request text/images only as needed. Bookmarks require explicit scope and indexing permission. Checkpoints every 50."""
        result = await library.job(action, resource, ids, level, confirm_index, job_id, refresh)
        if action == "status" and result.get("state") in {"running", "cancel_requested"}:
            for _ in range(wait_seconds * 2):
                await asyncio.sleep(.5)
                result = await library.job("status", resource, None, level, False, result["id"])
                if result["state"] not in {"running", "cancel_requested"}:
                    break
        return result

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    @errors
    async def search_resources(resource: Kind, query: Annotated[str, Field(min_length=1, max_length=500)],
                               offset: Offset = 0, limit: Limit = 20, mode: Literal["keyword", "semantic", "hybrid"] = "hybrid") -> dict[str, Any]:
        """Local hybrid keyword/semantic search across explicitly chosen tabs or bookmarks; only matching compact results reach the agent."""
        return await library.search(resource, query, offset, limit, mode)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True))
    @errors
    async def group_resources(resource: Kind, mode: Literal["fast", "precise"] = "fast", ids: list[str] | None = None,
                              instruction: Annotated[str, Field(max_length=1000)] = "", categories: list[str] | None = None,
                              max_groups: Annotated[int, Field(ge=1, le=100)] | None = None,
                              parent_id: str | None = None, confirm_index: bool = False) -> dict[str, Any]:
        """Fast: local embeddings/clusters/keyword names then native grouping. Precise: return a plan for the CONNECTED AGENT to refine using selected evidence. Neither closes tabs nor deletes bookmarks. Agent translates prompts into explicit categories, counts, or plan refinements."""
        if categories is not None and (not categories or len(categories) > 100 or any(not value.strip() or len(value) > 80 for value in categories)):
            raise ValueError("Provide 1 to 100 nonempty category descriptions of at most 80 characters")
        proposal = await library.propose(resource, ids, categories, max_groups, instruction, confirm_index)
        if "planId" not in proposal or mode == "precise":
            return proposal
        return await library.apply(library.plan(proposal["planId"]), parent_id)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True))
    @errors
    async def group_plan(action: Literal["show", "members", "rename", "merge", "assign"], plan_id: str,
                         group_ids: list[str] | None = None, ids: list[str] | None = None, title: str = "",
                         parent_id: str | None = None, offset: Offset = 0, limit: Limit = 20) -> dict[str, Any]:
        """Review or edit a cached proposal: show examples/members, rename/merge groups, assign selected resource IDs. This tool NEVER applies browser changes. Use apply_grouping for final names and application."""
        plan = library.plan(plan_id)
        if action == "members":
            if not group_ids or len(group_ids) != 1:
                raise ValueError("Select one group ID, or use unclassified")
            assigned = {key for group in plan["groups"] for key in group["ids"]}
            if group_ids[0] == "unclassified":
                members = [row for row in plan["selection"] if row["id"] not in assigned]
            else:
                group = next((group for group in plan["groups"] if group["id"] == group_ids[0]), None)
                if group is None:
                    raise ValueError("Group not found")
                members = [row for row in plan["selection"] if row["id"] in group["ids"]]
            return {"total": len(members), "offset": offset, "items": [library.compact(row) for row in members[offset:offset+limit]]}
        if action == "show":
            return library.show_plan(plan, offset, limit)
        if plan["state"] != "ready":
            raise ValueError("Generate a new plan after an already applied or uncertain operation")
        selected = [group for group in plan["groups"] if group["id"] in (group_ids or [])]
        if len(selected) != len(set(group_ids or [])):
            raise ValueError("Unknown group IDs")
        if title and len(title) > 80:
            raise ValueError("Group title exceeds 80 characters")
        if action == "rename":
            if len(selected) != 1 or not title.strip():
                raise ValueError("Rename needs one group and a title")
            selected[0]["title"] = title
        elif action == "merge":
            if len(selected) < 2:
                raise ValueError("Merge needs at least two group IDs")
            selected[0]["ids"] = [key for group in selected for key in group["ids"]]
            selected[0]["title"] = title or selected[0]["title"]
            plan["groups"] = [group for group in plan["groups"] if group not in selected[1:]]
        else:
            allowed = {row["id"] for row in plan["selection"]}
            if not ids or not set(ids) <= allowed or len(selected) > 1 or not selected and not title.strip():
                raise ValueError("Assign selected resource IDs to one group, or supply a new group title")
            for group in plan["groups"]:
                group["ids"] = [key for key in group["ids"] if key not in ids]
            if selected:
                selected[0]["ids"].extend(dict.fromkeys(ids))
            else:
                plan["groups"].append({"id": secrets.token_hex(4), "title": title, "ids": list(dict.fromkeys(ids))})
            plan["groups"] = [group for group in plan["groups"] if group["ids"]]
        plan["unclassified"] = len(plan["selection"]) - len({key for group in plan["groups"] for key in group["ids"]})
        library.save_plan(plan)
        return library.show_plan(plan)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True))
    @errors
    async def apply_grouping(plan_id: str, names: dict[str, str] | None = None, parent_id: str | None = None) -> dict[str, Any]:
        """Apply a reviewed proposal to Chrome. Optional names maps planGroupId values (not tab/bookmark IDs or native Chrome groupId) to meaningful titles. Renames and applies in one call; no reindexing or generative inference. Keeps user tabs open and bookmarks intact."""
        plan = library.plan(plan_id)
        allowed = {group["id"] for group in plan["groups"]}
        if names:
            if not set(names) <= allowed or any(not title.strip() or len(title) > 80 for title in names.values()):
                raise ValueError("No browser changes made: names must map proposal group IDs to nonempty titles of at most 80 characters")
            if plan["state"] != "ready":
                raise ValueError("No browser changes made: plan is no longer ready")
            for group in plan["groups"]:
                group["title"] = names.get(group["id"], group["title"])
            library.save_plan(plan)
        result = await library.apply(plan, parent_id)
        return {**result, "planId": plan_id, "applied": True}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True))
    @errors
    async def bookmark_action(action: Literal["create", "rename", "move", "delete", "save_tabs", "open"],
                              ids: list[str] | None = None, parent_id: str | None = None, title: str | None = None,
                              url: str | None = None, index: int | None = None, window_id: int | None = None,
                              warned: bool = False) -> dict[str, Any]:
        """Explicit bookmark operations: create URL/folder, rename, reorder/move, delete, save selected tabs, or open bookmarks (optionally as a named tab group). Before deletion show the warning; existing user authorization is sufficient."""
        if ids is not None and len(ids) > 5000:
            raise ValueError("Operate on at most 5000 resources")
        if action == "delete" and not warned:
            return {"warning": "This deletes selected bookmarks/folders, including folder descendants. Undo can recreate them with new IDs.",
                    "executed": False, "nextStep": "Warn the user, then use warned=true if their request already authorizes this exact deletion."}
        if action == "open" and ids:
            ids = [row["id"] for row in library.web(await library.records("bookmarks", ids, expand=True))]
        return await library.mutate("bookmark_action", {key: value for key, value in {
            "action": action, "ids": ids or [], "parentId": parent_id, "title": title, "url": url,
            "index": index, "windowId": window_id}.items() if value is not None})

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    @errors
    async def find_duplicates(resource: Kind, offset: Offset = 0, limit: Limit = 20) -> dict[str, Any]:
        """Find exact-URL duplicates in explicitly selected tabs or bookmarks. Preview only; deletion/closing uses separate warned operations."""
        grouped = defaultdict(list)
        for row in library.web(await library.records(resource)):
            grouped[row["url"]].append(row)
        duplicates = [rows for rows in grouped.values() if len(rows) > 1]
        return {"resource": resource, "total": len(duplicates), "offset": offset,
                "duplicates": [{"url": rows[0]["url"], "count": len(rows), "samples": [library.compact(row) for row in rows[:20]]}
                               for rows in duplicates[offset:offset+limit]]}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=False))
    @errors
    async def export_urls(resource: Kind, format: Literal["txt", "csv"] = "txt", ids: list[str] | None = None,
                          query: str = "", tab_id_for_links: int | None = None) -> dict[str, Any]:
        """Export complete selected URL collections to a local TXT/CSV file without flooding agent context. Optional tab_id_for_links exports links inside a selected webpage."""
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
            result = await library.bridge.call("inspect_page", {**arguments, "detail": detail, "offset": offset, "limit": limit})
        image = result.pop("image", None)
        content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        if image:
            mime = "image/png" if base64.b64decode(image).startswith(b"\x89PNG") else "image/jpeg"
            content.append(ImageContent(type="image", data=image, mime_type=mime))
        return CallToolResult(content=content)

    # Compatibility aliases keep existing clients working without the removed generative backend.
    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    @errors
    async def index_tabs(tab_ids: list[int] | None = None, visual: bool = False, confirm_index: bool = False) -> dict[str, Any]:
        """Index tabs only. Prefer index_resources for explicit metadata/text/image levels and job progress."""
        result = await library.index("tabs", [str(key) for key in tab_ids] if tab_ids else None,
                                     "image" if visual else "text", confirm_index)
        return {**result, "visual": result.get("total", 0) if visual else 0}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    @errors
    async def search_tabs(query: str, offset: Offset = 0, limit: Limit = 20,
                          mode: Literal["keyword", "semantic", "hybrid"] = "keyword") -> dict[str, Any]:
        """Search indexed tabs only; bookmark search must be requested explicitly through search_resources."""
        result = await library.search("tabs", query, offset, limit, mode)
        items = result.pop("items")
        for item in items:
            item["id"] = int(item["id"])
        return {**result, "tabs": items, "mode": mode}

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=True))
    @errors
    async def organize_by_text(instruction: str, tab_ids: list[int] | None = None, categories: list[str] | None = None,
                               processing: Literal["fast", "precise", "simple", "rigorous"] = "fast",
                               confirm_index: bool = False) -> dict[str, Any]:
        """Compatibility entry: local Fast grouping or a Precise plan for the connected agent. No backend LLM or automatic screenshot sweep."""
        mode = "precise" if processing in {"precise", "rigorous"} else "fast"
        return await group_resources("tabs", mode, [str(key) for key in tab_ids] if tab_ids else None, instruction,
                                     categories, confirm_index=confirm_index)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(destructive_hint=False))
    @errors
    async def catalogue_tabs(action: Literal["start", "status", "resume", "cancel"] = "start", job_id: str = "",
                             tab_ids: list[int] | None = None, confirm_index: bool = False) -> dict[str, Any]:
        """Compatibility indexing job for tabs only. Use index_resources for bookmarks or deeper extraction."""
        return await library.job(action, "tabs", [str(key) for key in tab_ids] if tab_ids else None, "metadata", confirm_index, job_id)
