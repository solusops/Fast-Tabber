"""Prepare pairing and agent configuration without changing global agent settings."""

import base64
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def register(root=ROOT):
    directory = Path.home() / ".fast-tabber"
    directory.mkdir(exist_ok=True)
    private_path(directory, directory=True)
    path = directory / "runtime.json"
    if path.exists() and Path(json.loads(path.read_text(encoding="utf-8-sig"))["root"]).resolve() != root.resolve():
        raise RuntimeError("Another Fast-Tabber runtime is registered; use its installer to preserve the existing Chrome pairing")
    path.write_text(json.dumps({"root": str(root.resolve())}), encoding="utf-8")
    private_path(path)


def private_path(path: Path, directory=False):
    if sys.platform != "win32":
        path.chmod(0o700 if directory else 0o600)
        return
    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    acl = win32security.ACL()
    flags = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE if directory else 0
    for sid in (owner, win32security.CreateWellKnownSid(win32security.WinLocalSystemSid), win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid)):
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, flags, ntsecuritycon.FILE_ALL_ACCESS, sid)
    win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None)


def prepare(root: Path = ROOT):
    state = root / ".state"
    state.mkdir(exist_ok=True)
    private_path(state, directory=True)
    manifest = json.loads((root / "extension" / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(base64.b64decode(manifest["key"], validate=True)).hexdigest()[:32]
    extension_id = "".join(chr(ord("a") + int(char, 16)) for char in digest)
    origin = f"chrome-extension://{extension_id}"
    path = state / "connection.json"
    if path.exists():
        connection = json.loads(path.read_text(encoding="utf-8"))
        if not re.fullmatch(r"[0-9a-f]{64}", connection.get("token", "")) or connection.get("origin") != origin:
            raise RuntimeError("Invalid existing pairing state; inspect .state/connection.json before changing it")
    else:
        connection = {"token": secrets.token_hex(32), "origin": origin}
        path.write_text(json.dumps(connection, indent=2), encoding="utf-8")

    config = {
        "$schema": "https://opencode.ai/config.json",
        "skills": {"paths": [str(root / ".agents" / "skills")]},
        "mcp": {"fast-tabber": {
            "type": "remote", "url": "http://127.0.0.1:8765/mcp", "oauth": False,
            "headers": {"Authorization": "Bearer {env:FAST_TABBER_TOKEN}"},
            "timeout": 600000,
        }},
        "permission": {"*": "deny", "fast-tabber_browser_status": "allow", "fast-tabber_list_tabs": "allow",
                       "fast-tabber_*": "ask", "skill": {"*": "deny", "fast-tabber": "allow"}},
    }
    (state / "opencode.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return connection


if __name__ == "__main__":
    result = prepare()
    register()
    print(f"Pairing prepared for {result['origin']}. Token is stored locally and is not printed.")
    print(f"Load unpacked extension in Chrome: {ROOT / 'extension'}")
