import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True)


def main(base, head):
    run("git", "diff", "--check", base, head, "--")
    run("node", "scripts/check-product-naming.mjs", "--base", base)
    changed = subprocess.check_output(
        [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "--diff-filter=AMT",
            "-z",
            base,
            head,
            "--",
        ]
    )
    paths = [Path(os.fsdecode(entry)) for entry in changed.split(b"\0") if entry]
    python = [p for p in paths if p.suffix == ".py"]
    web_root = Path("apps/web")
    web = [
        p
        for p in paths
        if p.is_relative_to(web_root) and p.suffix in {".ts", ".tsx", ".js", ".jsx"}
    ]
    prettier = [p for p in paths if p.suffix == ".md"] + web
    if prettier:
        if web:
            command = ["pnpm", "exec", "prettier"]
        else:
            version = json.loads(Path("package.json").read_text())["devDependencies"][
                "prettier"
            ]
            command = ["npx", "--yes", f"prettier@{version}"]
        run(*command, "--check", *(f"./{p}" for p in prettier))
    if python or web:
        run(
            "uv",
            "run",
            "--script",
            "scripts/check_comments.py",
            "--base",
            base,
            "--",
            *python,
            *web,
        )
    if python:
        pin = tomllib.loads(Path("ruff.toml").read_text())["required-version"]
        ruff = ["uvx", "--from", f"ruff{pin}", "ruff"]
        run(*ruff, "format", "--check", "--", *python)
        run(*ruff, "check", "--", *python)
    if web:
        run(
            "pnpm",
            "exec",
            "eslint",
            "--",
            *(p.relative_to(web_root) for p in web),
            cwd=web_root,
        )


if __name__ == "__main__":
    main(*sys.argv[1:])
