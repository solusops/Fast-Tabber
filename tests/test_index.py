import json

import base64

def red_image():
    return base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
from fast_tabber.server import StateStore




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
    store.db.execute("INSERT INTO selections(id,kind,items) VALUES ('sel_cache','tabs','[]')")
    store.save_collection("keep", {"tabs": [], "groups": {}})
    store.push_undo({"snapshot": {"tabs": []}})
    backup = tmp_path / "browser-before-test.sqlite3"
    backup.write_bytes(b"test backup")
    preview = store.local_data("cache")
    assert preview["requiresConfirmation"] and not preview["deleted"]
    assert preview["records"]["artifacts"] == 1 and backup.exists()
    assert preview["records"]["selections"] == 1
    assert store.local_data("cache", True)["deleted"]
    assert store.db.execute("SELECT count(*) FROM selections").fetchone()[0] == 0
    assert not backup.exists()
    assert store.get_collection("keep") and store.latest_undo()
    assert store.local_data("all", True)["deleted"]
    assert not store.list_collections() and store.latest_undo() is None
