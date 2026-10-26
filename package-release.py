"""Build a source-only installation ZIP; never include runtime/browser data."""
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parent
files = [root / name for name in (
    "install.ps1", "install.cmd", "opencode-plugin.js", "package.json", "pyproject.toml",
    "requirements.lock.txt", "tabber.ps1", "README.md", "LICENSE",
    "extension/manifest.json", "extension/background.js",
    ".agents/skills/fast-tabber/SKILL.md",
)] + sorted((root / "fast_tabber").glob("*.py"))
target = root / "dist" / "fast-tabber-0.5.2-preview.3.zip"
target.parent.mkdir(exist_ok=True)
with ZipFile(target, "w", ZIP_DEFLATED) as archive:
    for path in files:
        archive.write(path, "fast-tabber/" + path.relative_to(root).as_posix())
with ZipFile(target) as archive:
    assert len(archive.namelist()) == len(files)
    assert not any(part in {".state", ".venv", "pairing.json", "__pycache__"}
                   for name in archive.namelist() for part in name.split("/"))
    connection = root / ".state" / "connection.json"
    if connection.exists():
        import json
        token = json.loads(connection.read_text())["token"].encode()
        assert all(token not in archive.read(name) for name in archive.namelist())
target.with_suffix(".zip.sha256").write_text(f"{sha256(target.read_bytes()).hexdigest()}  {target.name}\n")
print(f"{target}: {len(files)} source files")
