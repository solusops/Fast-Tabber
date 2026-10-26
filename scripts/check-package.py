"""Verify the exact npm allowlist, including the portable Skill, before publishing."""
import glob
import json
from pathlib import Path
import sys
import tarfile

root = Path(__file__).resolve().parent.parent
package = json.loads((root / "package.json").read_text())
expected = {"package.json", "README.md", "LICENSE"}
for pattern in package["files"]:
    matches = list(root.glob(pattern))
    assert matches, f"Missing source: {pattern}"
    expected.update(path.relative_to(root).as_posix() for path in matches)
paths = [path for pattern in sys.argv[1:] for path in glob.glob(pattern)]
assert paths, "No npm tarball provided"
for path in paths:
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        assert all(member.isfile() and member.name.startswith("package/") for member in members)
        actual = {member.name.removeprefix("package/") for member in members}
        assert actual == expected, f"Unexpected: {actual - expected}; missing: {expected - actual}"
        connection = root / ".state/connection.json"
        if connection.exists():
            token = json.loads(connection.read_text())["token"].encode()
            assert all(token not in archive.extractfile(member).read() for member in members)
    print(f"Verified {Path(path).name}: {len(expected)} source files; no runtime state")
