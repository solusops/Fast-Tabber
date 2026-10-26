import json
import subprocess
import sys

import pytest

from fast_tabber.setup import prepare


def root(tmp_path):
    extension = tmp_path / "extension"
    extension.mkdir()
    (extension / "manifest.json").write_text(json.dumps({"key": "aGVsbG8="}))
    return tmp_path


def test_setup_reuses_pairing_token(tmp_path):
    project = root(tmp_path)
    first = prepare(project)
    second = prepare(project)
    assert first == second
    assert len(first["token"]) == 64
    assert not (project / "extension" / "pairing.json").exists()


def test_setup_does_not_replace_existing_codex_config(tmp_path):
    project = root(tmp_path)
    (project / ".codex").mkdir()
    config = project / ".codex" / "config.toml"
    config.write_text("# user settings\n")
    prepare(project)
    assert config.read_text() == "# user settings\n"


def test_opencode_requires_confirmation_for_mutation_tools(tmp_path):
    project = root(tmp_path)
    prepare(project)
    config = json.loads((project / ".state" / "opencode.json").read_text())
    assert config["permission"]["fast-tabber_list_tabs"] == "allow"
    assert config["permission"]["fast-tabber_*"] == "ask"


def test_corrupt_existing_token_is_rejected(tmp_path):
    project = root(tmp_path)
    (project / ".state").mkdir()
    (project / ".state" / "connection.json").write_text('{"token":"short","origin":"wrong"}')
    with pytest.raises(RuntimeError, match="Invalid existing"):
        prepare(project)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL verification")
def test_pairing_secrets_do_not_inherit_shared_folder_access(tmp_path):
    import win32security

    project = root(tmp_path)
    subprocess.run(["icacls", str(project), "/grant", "*S-1-5-11:(OI)(CI)M"], check=True, capture_output=True)
    prepare(project)
    for path in (project / ".state" / "connection.json",):
        acl = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION).GetSecurityDescriptorDacl()
        identities = {win32security.ConvertSidToStringSid(acl.GetAce(index)[2]) for index in range(acl.GetAceCount())}
        assert "S-1-5-11" not in identities, "Authenticated Users can read the pairing secret"
        assert "S-1-5-32-545" not in identities, "Users can read the pairing secret"
