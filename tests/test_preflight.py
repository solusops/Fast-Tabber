import pytest
import json

from fast_tabber.preflight import require_models, save_report


def test_missing_model_fails_without_substitution():
    with pytest.raises(RuntimeError, match="embeddinggemma-2:740m"):
        require_models({"models": [{"name": "gemma4:e2b", "digest": "a"}]})


def test_records_exact_model_digests():
    assert require_models({"models": [
        {"name": "gemma4:e2b", "digest": "abc"},
        {"name": "embeddinggemma-2:740m", "digest": "def"},
    ]}) == {"gemma4:e2b": "abc", "embeddinggemma-2:740m": "def"}


def test_empty_digest_fails():
    with pytest.raises(RuntimeError, match="digest"):
        require_models({"models": [
            {"name": "gemma4:e2b", "digest": ""},
            {"name": "embeddinggemma-2:740m", "digest": "def"},
        ]})


def test_model_check_preserves_offline_proof_for_same_digests(tmp_path):
    models = {"gemma4:e2b": "a", "embeddinggemma-2:740m": "b"}
    proof = "passed: firewall blocks verified"
    (tmp_path / "models.json").write_text(json.dumps({"models": models, "external_inference_block_test": proof}))
    report = {"models": models, "external_inference_block_test": "not yet performed"}
    save_report(report, tmp_path)
    assert json.loads((tmp_path / "models.json").read_text())["external_inference_block_test"] == proof


def test_model_change_invalidates_offline_proof(tmp_path):
    (tmp_path / "models.json").write_text(json.dumps({"models": {"old": "digest"}, "external_inference_block_test": "passed: old models"}))
    report = {"models": {"new": "digest"}, "external_inference_block_test": "not yet performed"}
    save_report(report, tmp_path)
    assert json.loads((tmp_path / "models.json").read_text())["external_inference_block_test"] == "not yet performed"
