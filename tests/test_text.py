import asyncio
from pathlib import Path

import pytest
import httpx
import json

from fast_tabber.text import _response_content, classify_pages, validate_assignments, validate_cluster_labels, structured_batches
from fast_tabber.text import validate_triage


PAGES = [{"id": 1}, {"id": 2}]


def test_triage_requires_explicit_sufficiency_and_valid_category():
    payload = {"assignments": [{"id": 1, "category": "Research", "needs_visual": False}]}
    assert validate_triage(payload, [{"id": 1, "visual": True}], ["Research"])[1]["needs_visual"] is False
    payload["assignments"][0]["needs_visual"] = "false"
    with pytest.raises(RuntimeError, match="sufficiency"):
        validate_triage(payload, [{"id": 1}])


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


@pytest.mark.asyncio
async def test_structured_harness_splits_truncation_without_changing_model():
    seen = []
    def handler(request):
        body = json.loads(request.content)
        batch = json.loads(body["messages"][0]["content"])
        seen.append(batch)
        assert body["think"] is True and body["model"] == "gemma4:e2b"
        assert body["options"]["num_predict"] >= 4096
        if len(batch) > 1:
            return httpx.Response(200, json={"done_reason": "length", "message": {"content": ""}})
        return httpx.Response(200, json={"done_reason": "stop", "message": {"content": json.dumps({str(batch[0]): "ok"})}})
    async with httpx.AsyncClient(base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(handler)) as client:
        result = await structured_batches(client, [1, 2], json.dumps, lambda _: {}, lambda payload, _: payload, "test")
    assert result == {"1": "ok", "2": "ok"} and seen == [[1, 2], [1], [2]]
