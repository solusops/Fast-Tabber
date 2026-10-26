"""Content-addressed local text, screenshots, descriptions, and multimodal vectors."""

import base64
import hashlib
import json
from urllib.parse import urlsplit, urlunsplit



def chunks(text, size=12000, overlap=0):
    """Keep one bounded first-page representation; no crawler-style passage overlap."""
    return [text[:size]]


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


    def cached_page(self, metadata):
        with self.lock:
            row = self.db.execute("SELECT id,title,headings,content FROM documents WHERE url=? ORDER BY (title=?) DESC,rowid DESC LIMIT 1",
                                  (metadata["url"], metadata.get("title", ""))).fetchone()
        if not row:
            return None
        with self.lock:
            image = self.db.execute("SELECT a.id FROM document_artifacts d JOIN artifacts a ON a.id=d.artifact_id WHERE d.document_id=? AND a.kind='image' LIMIT 1", (row[0],)).fetchone()
        return {"id": metadata["id"], "windowId": metadata["windowId"], "url": metadata["url"],
                "title": row[1], "headings": json.loads(row[2]), "text": row[3], "cached": True, "visual": image is not None,
                **({"image_id": image[0]} if image else {})}


    def artifacts(self, document):
        with self.lock:
            return [row[0] for row in self.db.execute("SELECT artifact_id FROM document_artifacts WHERE document_id=? ORDER BY ordinal", (document,))]
