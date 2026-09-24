"""Write source identity into an image built by Make; no Git mutations."""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from runner.libs import ROOT_DIR


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arch", required=True)
    args = parser.parse_args()

    def git(*arguments: str) -> str:
        return subprocess.check_output(["git", *arguments], cwd=ROOT_DIR, text=True).strip()

    status = git("status", "--porcelain=v1", "--untracked-files=all")
    manifest = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "target_arch": args.arch,
        "repo_git_sha": git("rev-parse", "HEAD"),
        "repo_dirty": bool(status),
        "git_status": status.splitlines(),
        "submodule_status": git("submodule", "status", "--recursive").splitlines(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
