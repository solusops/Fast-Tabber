import asyncio
from pathlib import Path

import pytest

from fast_tabber.text import classify_pages, validate_assignments


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
