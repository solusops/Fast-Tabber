import pytest

from fast_tabber.server import StateStore


def test_collection_names_are_unique_and_list_is_compact(tmp_path):
    store = StateStore(tmp_path / "browser.sqlite3")
    store.save_collection("research", {"tabs": [{"id": 1}, {"id": 2}], "groups": {}})
    assert store.list_collections() == [{"name": "research", "tabCount": 2}]
    with pytest.raises(ValueError, match="already exists"):
        store.save_collection("research", {"tabs": [], "groups": {}})


def test_undo_history_keeps_all_changes_for_revert_all(tmp_path):
    store = StateStore(tmp_path / "browser.sqlite3")
    for index in range(55):
        store.push_undo({"index": index})
    _, latest, status = store.latest_undo()
    assert latest == {"index": 54}
    assert status == "ready"
    assert store.db.execute("SELECT count(*) FROM undo").fetchone()[0] == 55


def test_undo_claim_prevents_repeating_uncertain_mutation(tmp_path):
    store = StateStore(tmp_path / "browser.sqlite3")
    store.push_undo({"snapshot": {"tabs": []}})
    entry_id, _, status = store.latest_undo()
    assert status == "ready"
    assert store.claim_undo(entry_id) is True
    assert store.claim_undo(entry_id) is False
    store.fail_undo(entry_id)
    assert store.latest_undo()[2] == "failed"


def test_catalogue_checkpoint_survives_restart(tmp_path):
    path = tmp_path / "browser.sqlite3"
    store = StateStore(path)
    job = {"id": "job-1", "state": "running", "items": [{"id": 42, "url": "https://example.test/", "done": True}]}
    store.save_job(job)
    reopened = StateStore(path)
    assert reopened.get_job("job-1") == job
    assert reopened.get_job("") == job


def test_page_index_searches_rendered_content_with_bounded_snippet(tmp_path):
    store = StateStore(tmp_path / "browser.sqlite3")
    store.save_pages([{"id": 42, "windowId": 7, "url": "https://user:pass@example.test/private?q=secret",
                       "title": "Notes", "headings": ["Overview"], "text": "Diffusion policies train robots from demonstrations."}])
    assert store.search_pages("diffusion policies", 0, 20) == [{
        "id": 42, "windowId": 7, "title": "Notes", "origin": "https://example.test",
        "snippet": "Diffusion policies train robots from demonstrations.",
    }]
    assert store.count_pages("diffusion") == 1
    assert store.search_pages("secret", 0, 20) == []
    url_hash, origin, content = store.db.execute("SELECT url_hash, origin, content FROM pages").fetchone()
    assert len(url_hash) == 64 and origin == "https://example.test" and "secret" not in content


def test_page_index_handles_five_hundred_records(tmp_path):
    store = StateStore(tmp_path / "browser.sqlite3")
    store.save_pages([{"id": i + 1, "windowId": 1, "url": f"https://example.test/{i}",
        "title": "Notes", "headings": [], "text": f"shared research record {i}"} for i in range(500)])
    assert store.count_pages("shared research") == 500
    assert len(store.search_pages("shared research", 0, 100)) == 100


def test_embeddings_persist_cache_and_rank_semantically(tmp_path):
    store = StateStore(tmp_path / "browser.sqlite3")
    pages = [{"id": 1, "windowId": 1, "url": "https://robot.test/", "title": "Robot learning",
              "headings": [], "text": "Learn manipulation from visual demonstrations."},
             {"id": 2, "windowId": 1, "url": "https://school.test/", "title": "College",
              "headings": [], "text": "University admissions and applications."}]
    changed = store.save_pages(pages, "embedding-digest-a")
    store.save_embeddings(changed, [[1.0, 0.0], [0.0, 1.0]], "embedding-digest-a")
    assert store.save_pages(pages, "embedding-digest-a") == []
    assert store.semantic_search([0.9, 0.1], 0, 1, "embedding-digest-a")[0][0]["id"] == 1
    assert len(store.save_pages(pages, "embedding-digest-b")) == 2
