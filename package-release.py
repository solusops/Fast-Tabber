"""Build a source-only installation ZIP; never include runtime/browser data."""
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import json

root = Path(__file__).resolve().parent
package = json.loads((root / "package.json").read_text())
files = {root / name for name in ("package.json", "README.md", "LICENSE")}
for pattern in package["files"]:
    matches = list(root.glob(pattern))
    assert matches, f"Missing packaged source: {pattern}"
    files.update(matches)
files = sorted(files)
target = root / "dist" / f"fast-tabber-{package['version']}.zip"
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
        token = json.loads(connection.read_text())["token"].encode()
        assert all(token not in archive.read(name) for name in archive.namelist())
target.with_suffix(".zip.sha256").write_text(f"{sha256(target.read_bytes()).hexdigest()}  {target.name}\n")
print(f"{target}: {len(files)} source files")
version = json.loads((root / "extension/manifest.json").read_text())["version"]
extension = root / "dist" / f"fast-tabber-extension-{version}.zip"
with ZipFile(extension, "w", ZIP_DEFLATED) as archive:
    for path in files:
        if path.parent == root / "extension":
            if path.name == "manifest.json":
                manifest = json.loads(path.read_text())
                manifest.pop("key", None)
                archive.writestr(path.name, json.dumps(manifest, indent=2))
            else:
                archive.write(path, path.name)
extension.with_suffix(".zip.sha256").write_text(f"{sha256(extension.read_bytes()).hexdigest()}  {extension.name}\n")
print(extension)
