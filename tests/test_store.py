import pytest

from fast_tabber.server import StateStore


def test_collection_names_are_unique_and_list_is_compact(tmp_path):
    store = StateStore(tmp_path / "browser.sqlite3")
    store.save_collection("research", {"tabs": [{"id": 1}, {"id": 2}], "groups": {}})
    assert store.list_collections() == [{"name": "research", "tabCount": 2}]
    with pytest.raises(ValueError, match="already exists"):
        store.save_collection("research", {"tabs": [], "groups": {}})


def test_undo_history_keeps_latest_fifty(tmp_path):
    store = StateStore(tmp_path / "browser.sqlite3")
    for index in range(55):
        store.push_undo({"index": index})
    _, latest, status = store.latest_undo()
    assert latest == {"index": 54}
    assert status == "ready"
    assert store.db.execute("SELECT count(*) FROM undo").fetchone()[0] == 50


def test_undo_claim_prevents_repeating_uncertain_mutation(tmp_path):
    store = StateStore(tmp_path / "browser.sqlite3")
    store.push_undo({"snapshot": {"tabs": []}})
    entry_id, _, status = store.latest_undo()
    assert status == "ready"
    assert store.claim_undo(entry_id) is True
    assert store.claim_undo(entry_id) is False
    store.fail_undo(entry_id)
    assert store.latest_undo()[2] == "failed"
