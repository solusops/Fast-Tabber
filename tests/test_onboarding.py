import json
from types import SimpleNamespace
import pytest
from fast_tabber.onboarding import Onboarding

@pytest.mark.asyncio
async def test_onboarding_needs_no_models_and_preserves_existing_settings(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"model": "user-choice", "indexing_allowed": ["tabs"]}))
    setup = Onboarding(tmp_path, SimpleNamespace(peer=None), lambda: False, None)
    result = await setup.call("status")
    assert not result["browserConnected"] and not result["privacyAcknowledged"]
    assert "models" not in result and "runtimeError" not in result
    await setup.call("acknowledge_privacy", confirm=True)
    assert json.loads((tmp_path / "settings.json").read_text())["model"] == "user-choice"
    assert (await setup.call("status"))["privacyAcknowledged"]
