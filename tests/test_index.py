import json

from fast_tabber.preflight import red_image
from fast_tabber.server import StateStore


def test_persistent_index_reuses_chunks_images_vectors_and_urls(tmp_path):
    path = tmp_path / "browser.sqlite3"
    store = StateStore(path)
    page = {"id": 1, "windowId": 1, "url": "https://example.test/article?id=42#section",
            "title": "Notes", "headings": [], "text": "Reusable content " * 220}
    image = store.index.image(red_image())
    document, url = store.index.document({**page, "image_id": image})
    keys = store.index.artifacts(document)
    pending = store.index.pending(keys, "model-a")
    assert any(isinstance(item[1], dict) and "image" in item[1] for item in pending)
    store.index.save_vectors([item[0] for item in pending], [[1.0, 0.0] for _ in pending], "model-a")
    store.index.save_description(image, "vision-v1", "A red image")
    store.db.close()
    reopened = StateStore(path)
    assert reopened.index.document(page)[0] == document
    assert reopened.index.pending(keys, "model-a") == []
    assert reopened.index.description(image, "vision-v1") == "A red image"
    assert reopened.index.description(image, "vision-v2") is None
    assert reopened.index.page_vector(keys, "model-a") == [1.0, 0.0]
    assert reopened.db.execute("SELECT url FROM documents WHERE id=?", (document,)).fetchone()[0] == url == page["url"]
    assert reopened.index.pending(keys, "model-b"), "Model digest changes must invalidate vectors"


def test_recreated_tab_ids_remap_older_undo_and_collections(tmp_path):
    store = StateStore(tmp_path / "browser.sqlite3")
    store.push_undo({"snapshot": {"tabs": [{"id": 1}], "closeIds": [1]}})
    store.save_collection("saved", {"tabs": [{"id": 1}], "groups": {}})
    store.remap_history({"1": 99})
    assert store.latest_undo()[1]["snapshot"] == {"tabs": [{"id": 99}], "closeIds": [99]}
    assert store.get_collection("saved")["tabs"][0]["id"] == 99
    store.mark_undone(store.latest_undo()[0])
    assert store.latest_undo() is None
    assert json.loads(store.db.execute("SELECT payload FROM undo").fetchone()[0])["snapshot"]["closeIds"] == [99]


def test_local_cache_preview_and_clear_preserve_recovery_until_all_is_selected(tmp_path):
    store = StateStore(tmp_path / "browser.sqlite3")
    store.index.image(red_image())
    store.save_collection("keep", {"tabs": [], "groups": {}})
    store.push_undo({"snapshot": {"tabs": []}})
    backup = tmp_path / "browser-before-test.sqlite3"
    backup.write_bytes(b"test backup")
    preview = store.local_data("cache")
    assert preview["requiresConfirmation"] and not preview["deleted"]
    assert preview["records"]["artifacts"] == 1 and backup.exists()
    assert store.local_data("cache", True)["deleted"]
    assert not backup.exists() and store.index.pending([], "model") == []
    assert store.get_collection("keep") and store.latest_undo()
    assert store.local_data("all", True)["deleted"]
    assert not store.list_collections() and store.latest_undo() is None
