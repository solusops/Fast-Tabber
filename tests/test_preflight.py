import pytest
import json

from fast_tabber.preflight import require_models, save_report


def test_missing_model_fails_without_substitution():
    with pytest.raises(RuntimeError, match="embeddinggemma-2:440m"):
        require_models({"models": [{"name": "gemma4:e2b", "digest": "a"}]})


def test_old_embedding_model_is_not_substituted_for_selected_440m():
    with pytest.raises(RuntimeError, match="embeddinggemma-2:440m"):
        require_models({"models": [{"name": "gemma4:e2b", "digest": "a"},
                                    {"name": "embeddinggemma-2:740m", "digest": "old"}]})


def test_records_exact_model_digests():
    assert require_models({"models": [
        {"name": "gemma4:e2b", "digest": "abc"},
        {"name": "embeddinggemma-2:440m", "digest": "def"},
    ]}) == {"gemma4:e2b": "abc", "embeddinggemma-2:440m": "def"}


def test_empty_digest_fails():
    with pytest.raises(RuntimeError, match="digest"):
        require_models({"models": [
            {"name": "gemma4:e2b", "digest": ""},
            {"name": "embeddinggemma-2:440m", "digest": "def"},
        ]})


def test_model_check_preserves_offline_proof_for_same_digests(tmp_path):
    models = {"gemma4:e2b": "a", "embeddinggemma-2:440m": "b"}
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
