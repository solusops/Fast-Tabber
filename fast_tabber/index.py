"""Content-addressed local text, screenshots, descriptions, and multimodal vectors."""

import base64
import hashlib
import json
import math
import struct
from urllib.parse import urlsplit, urlunsplit

from fast_tabber.semantic import validate_embeddings


def chunks(text, size=1600, overlap=160):
    result = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind(" ", start + size // 2, end)
            if boundary > start:
                end = boundary
        result.append(text[start:end])
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return result or [""]


class ContentIndex:
    def __init__(self, db, lock):
        self.db, self.lock = db, lock
        with lock, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL, headings TEXT NOT NULL, content TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, kind TEXT NOT NULL, content BLOB NOT NULL, description TEXT, description_model TEXT, embedding BLOB, embedding_model TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS document_artifacts (document_id TEXT, ordinal INTEGER, artifact_id TEXT NOT NULL, PRIMARY KEY(document_id,ordinal))")
            if "encoding" not in {row[1] for row in db.execute("PRAGMA table_info(artifacts)")}:
                db.execute("ALTER TABLE artifacts ADD COLUMN encoding TEXT NOT NULL DEFAULT 'f32'")

    def document(self, page):
        parsed = urlsplit(page["url"])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Only ordinary web URLs can enter the local index")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        url = urlunsplit((parsed.scheme, host + (f":{parsed.port}" if parsed.port else ""), parsed.path, parsed.query, parsed.fragment))
        values = (url, page["title"], json.dumps(page["headings"], ensure_ascii=False), page["text"])
        key = hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()
        with self.lock, self.db:
            if not self.db.execute("SELECT 1 FROM documents WHERE id=?", (key,)).fetchone():
                self.db.execute("INSERT INTO documents VALUES (?,?,?,?,?)", (key, *values))
                text = f"{page['title']}\n{' '.join(page['headings'])}\n{page['text']}"
                for ordinal, part in enumerate(chunks(text)):
                    raw = part.encode()
                    artifact = hashlib.sha256(b"text\0" + raw).hexdigest()
                    self.db.execute("INSERT OR IGNORE INTO artifacts(id,kind,content) VALUES (?,'text',?)", (artifact, raw))
                    self.db.execute("INSERT INTO document_artifacts VALUES (?,?,?)", (key, ordinal, artifact))
            if page.get("image_id"):
                self.db.execute("INSERT OR REPLACE INTO document_artifacts VALUES (?,-1,?)", (key, page["image_id"]))
        return key, url

    def image(self, encoded):
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 6 * 1024 * 1024 or not raw.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
            raise ValueError("Screenshot is not a bounded PNG or JPEG")
        key = hashlib.sha256(b"image\0" + raw).hexdigest()
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO artifacts(id,kind,content) VALUES (?,'image',?)", (key, raw))
        return key

    def description(self, key, model):
        with self.lock:
            row = self.db.execute("SELECT description FROM artifacts WHERE id=? AND description_model=?", (key, model)).fetchone()
        return row[0] if row else None

    def cached_page(self, metadata):
        with self.lock:
            row = self.db.execute("SELECT id,title,headings,content FROM documents WHERE url=? AND title=? ORDER BY rowid DESC LIMIT 1",
                                  (metadata["url"], metadata.get("title", ""))).fetchone()
        if not row:
            return None
        with self.lock:
            image = self.db.execute("SELECT a.id FROM document_artifacts d JOIN artifacts a ON a.id=d.artifact_id WHERE d.document_id=? AND a.kind='image' LIMIT 1", (row[0],)).fetchone()
        return {"id": metadata["id"], "windowId": metadata["windowId"], "url": metadata["url"],
                "title": row[1], "headings": json.loads(row[2]), "text": row[3], "cached": True, "visual": image is not None,
                **({"image_id": image[0]} if image else {})}

    def save_description(self, key, model, description):
        with self.lock, self.db:
            self.db.execute("UPDATE artifacts SET description=?,description_model=? WHERE id=?", (description, model, key))

    def artifacts(self, document):
        with self.lock:
            return [row[0] for row in self.db.execute("SELECT artifact_id FROM document_artifacts WHERE document_id=? ORDER BY ordinal", (document,))]

    def pending(self, keys, model):
        result = []
        with self.lock:
            for key in dict.fromkeys(keys):
                row = self.db.execute("SELECT kind,content,embedding,embedding_model FROM artifacts WHERE id=?", (key,)).fetchone()
                if row is None:
                    raise RuntimeError("A local index artifact is missing")
                if row[2] is None or row[3] != model:
                    result.append((key, row[1].decode() if row[0] == "text" else {"image": base64.b64encode(row[1]).decode()}))
        return result

    def save_vectors(self, keys, vectors, model):
        validate_embeddings({"embeddings": vectors}, len(keys))
        with self.lock, self.db:
            for key, vector in zip(keys, vectors):
                norm = math.sqrt(sum(value * value for value in vector))
                self.db.execute("UPDATE artifacts SET embedding=?,embedding_model=?,encoding='f16' WHERE id=?",
                    (struct.pack(f"<{len(vector)}e", *(value / norm for value in vector)), model, key))

    def page_vector(self, keys, model):
        vectors = []
        with self.lock:
            for key in keys:
                row = self.db.execute("SELECT embedding,encoding FROM artifacts WHERE id=? AND embedding_model=?", (key, model)).fetchone()
                if not row or not row[0]:
                    raise RuntimeError("The local multimodal index is incomplete; resume index_tabs")
                size, code = (2, "e") if row[1] == "f16" else (4, "f")
                vectors.append(struct.unpack(f"<{len(row[0]) // size}{code}", row[0]))
        validate_embeddings({"embeddings": [list(vector) for vector in vectors]}, len(keys))
        mean = [sum(values) / len(vectors) for values in zip(*vectors)]
        norm = math.sqrt(sum(value * value for value in mean))
        if not norm:
            raise RuntimeError("Multimodal page vector has zero norm")
        return [value / norm for value in mean]
