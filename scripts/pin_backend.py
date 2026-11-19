"""Pin a backend's MCP server to a commit of its repository (backends/known.json): the commit users download.

    python scripts/pin_backend.py inventor ../inventor-forge      # its checked-out HEAD, which must be pushed

Reads the version from the checkout's package and refuses a checkout with uncommitted changes, so the pinned commit
is exactly what was tested. Once the commit is on GitHub it also records the sha256 of GitHub's archive of that
commit, which pip checks when a machine without git installs from the archive instead.
"""
import hashlib
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
KNOWN = ROOT / "backends" / "known.json"


def main(name: str, checkout: str) -> None:
    repo = pathlib.Path(checkout).resolve()
    git = ["git", "-C", str(repo)]
    if subprocess.run(git + ["status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True,
                      check=True).stdout.strip():
        sys.exit(f"{repo} has uncommitted changes: commit them first")
    commit = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    data = json.loads(KNOWN.read_text(encoding="utf-8"))
    srv = data[name]["server"]
    init = (repo / srv["module"].split(".")[0] / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    srv["commit"] = commit
    srv.pop("archive_sha256", None)
    if m:
        srv["version"] = m.group(1)
    KNOWN.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"{name}: {srv['package']} {srv.get('version')} pinned to {commit}")
    remote = subprocess.run(git + ["branch", "-r", "--contains", commit], capture_output=True, text=True).stdout.strip()
    if not remote:
        print("note: this commit is not on any remote branch yet; push it, then run this again")
    else:
        import urllib.request
        url = f"{srv['repo']}/archive/{commit}.zip"
        with urllib.request.urlopen(url, timeout=120) as r:
            srv["archive_sha256"] = hashlib.sha256(r.read()).hexdigest()
        print(f"archive sha256 {srv['archive_sha256']}")
        KNOWN.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
