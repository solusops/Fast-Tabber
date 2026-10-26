import pytest
from fast_tabber import preflight


@pytest.fixture(autouse=True)
def isolated_model_preferences(request, tmp_path, monkeypatch):
    if "test_live" not in request.module.__name__:
        monkeypatch.setattr(preflight, "SETTINGS", tmp_path / "settings.json")
