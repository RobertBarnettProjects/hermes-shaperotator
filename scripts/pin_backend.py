"""Pin a backend's MCP server to a commit of its repository (backends/known.json): the commit users download.

    python scripts/pin_backend.py inventor ../inventor-forge      # its checked-out HEAD, which must be pushed

Reads the version from the checkout's package and refuses a checkout with uncommitted changes, so the pinned commit
is exactly what was tested.
"""
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
    if m:
        srv["version"] = m.group(1)
    KNOWN.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"{name}: {srv['package']} {srv.get('version')} pinned to {commit}")
    remote = subprocess.run(git + ["branch", "-r", "--contains", commit], capture_output=True, text=True).stdout.strip()
    if not remote:
        print("note: this commit is not on any remote branch yet; push it before releasing the plugin")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
