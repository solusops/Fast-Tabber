import asyncio
from pathlib import Path

import pytest

from fast_tabber.text import _response_content, classify_pages, validate_assignments, validate_cluster_labels


PAGES = [{"id": 1}, {"id": 2}]


def test_valid_classification_covers_each_page_once():
    assert validate_assignments({"assignments": [{"id": 1, "category": "Research"},
                                                  {"id": 2, "category": "Research"}]}, PAGES, ["Research"]) == {
        1: "Research", 2: "Research"}


@pytest.mark.parametrize("payload", [
    {"assignments": [{"id": 1, "category": "Research"}]},
    {"assignments": [{"id": 1, "category": "Research"}, {"id": 1, "category": "Research"}]},
    {"assignments": [{"id": 1, "category": "Other"}, {"id": 2, "category": "Research"}]},
])
def test_invalid_classification_fails_before_browser_mutation(payload):
    with pytest.raises(RuntimeError, match="no tabs were changed"):
        validate_assignments(payload, PAGES, ["Research"])


def test_uncategorized_is_reserved_for_unmatched_pages():
    with pytest.raises(ValueError, match="exclude Uncategorized"):
        asyncio.run(classify_pages(PAGES, "Sort", ["Uncategorized"], Path(".")))


def test_cluster_label_output_must_cover_ids_once():
    assert validate_cluster_labels({"labels": [{"cluster": 3, "category": "Research"}]}, [3]) == {3: "Research"}
    with pytest.raises(RuntimeError, match="no tabs were changed"):
        validate_cluster_labels({"labels": [{"cluster": 4, "category": "Research"}]}, [3])


def test_truncated_gemma_output_is_not_accepted():
    class Response:
        def json(self):
            return {"done_reason": "length", "message": {"content": ""}}

    with pytest.raises(RuntimeError, match="truncated or empty"):
        _response_content(Response(), "cluster labeling")
