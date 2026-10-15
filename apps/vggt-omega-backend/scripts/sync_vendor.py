"""Reproduce the small pinned source snapshot from an existing clean checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

COMMIT = "48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6"


def sync(source: Path) -> None:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(source), *args])

    if git("rev-parse", "HEAD").decode().strip() != COMMIT or git("status", "--porcelain").strip():
        raise ValueError("source checkout must match the pinned commit and be clean")
    destination = Path(__file__).resolve().parents[1] / "vendor"
    names = (
        git(
            "ls-tree",
            "-r",
            "--name-only",
            COMMIT,
            "vggt_omega",
            "LICENSE",
            "NOTICE",
            "USE_POLICY.md",
        )
        .decode()
        .splitlines()
    )
    expected_files = set(names) | {"SOURCE.json"}
    if destination.exists():
        extras = [
            path
            for path in destination.rglob("*")
            if path.is_file() and path.relative_to(destination).as_posix() not in expected_files
        ]
        if extras:
            raise ValueError("vendor contains files outside the pinned snapshot")
    hashes = {}
    for name in names:
        content = git("show", f"{COMMIT}:{name}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    (destination / "SOURCE.json").write_text(
        json.dumps(
            {
                "repository": "https://github.com/facebookresearch/vggt-omega",
                "commit": COMMIT,
                "files": hashes,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    sync(parser.parse_args().source)
