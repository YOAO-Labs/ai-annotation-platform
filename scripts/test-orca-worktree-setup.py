"""Run with python3 scripts/test-orca-worktree-setup.py (no installs or services)."""

import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import tomllib


script = Path(__file__).with_name("orca-worktree-setup.sh").resolve()
lock = tomllib.loads((script.parent.parent / "apps/api/uv.lock").read_text())
matplotlib = next(
    package for package in lock["package"] if package["name"] == "matplotlib"
)
assert any(
    "cp312-cp312-macosx_" in wheel["url"]
    and wheel["url"].endswith(("_arm64.whl", "_universal2.whl"))
    for wheel in matplotlib.get("wheels", [])
), f"Matplotlib {matplotlib['version']} has no Python 3.12 macOS ARM64 wheel in uv.lock"


def test_setup(launcher):
    with tempfile.TemporaryDirectory(prefix="worktree setup ") as temporary:
        base = Path(temporary)
        root, worktree, binaries = (
            base / name for name in ("primary", "worktree", "bin")
        )
        for directory in (root, worktree, binaries):
            directory.mkdir()
        dependencies = (
            "node_modules",
            "apps/web/node_modules",
            "docs-site/node_modules",
        )
        manifests = (
            "pnpm-lock.yaml",
            "pnpm-workspace.yaml",
            "package.json",
            "apps/web/package.json",
            "docs-site/package.json",
        )
        for directory in (root, worktree):
            for name in manifests:
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("same\n")
            (directory / "apps/api").mkdir()
        for name in dependencies:
            (root / name).mkdir(exist_ok=True)
        (root / "node_modules/.pnpm").mkdir()
        installed_lock = root / "node_modules/.pnpm/lock.yaml"
        installed_lock.write_text("same\n")
        (root / "apps/api/.venv").mkdir()
        (worktree / ".env.example").write_text("EXAMPLE=preserve\n")
        (worktree / "scripts").mkdir()
        shutil.copy2(script, worktree / "scripts" / script.name)
        shutil.copy2(script.with_name("ensure-api-vendor.sh"), worktree / "scripts")
        trackeval = (
            worktree / "apps/api/app/vendor/TrackEval/trackeval_metrics/metrics.py"
        )
        trackeval.parent.mkdir(parents=True)
        trackeval.write_text("# initialized fixture\n")
        calls = base / "calls"
        for name in ("pnpm", "uv"):
            executable = binaries / name
            executable.write_text(
                '#!/bin/sh\nprintf "%s %s\\n" "${0##*/}" "$*" >> "$CALLS"\n'
            )
            executable.chmod(0o755)
        env = dict(
            os.environ,
            PATH=f"{binaries}:{os.environ['PATH']}",
            ORCA_ROOT_PATH=str(root),
            ORCA_WORKTREE_PATH=str(worktree),
            CALLS=str(calls),
            CODEX_SOURCE_TREE_PATH=str(root),
            CODEX_WORKTREE_PATH=str(worktree),
        )

        def run():
            return subprocess.run(
                (
                    ["bash", str(script)]
                    if launcher == "orca"
                    else ["bash", "-c", config["setup"]["script"]]
                ),
                env=env,
                capture_output=True,
                text=True,
            )

        result = run()
        assert result.returncode == 0, result.stderr
        for name in (".env", *dependencies):
            assert (worktree / name).is_symlink()
            assert (worktree / name).resolve() == (root / name).resolve()
        assert calls.read_text().splitlines() == [
            "uv sync --project apps/api --locked --extra test",
            "pnpm codegen",
        ]
        assert not (worktree / "apps/api/.venv").is_symlink()
        (worktree / ".env").write_text("LOCAL_VALUE=preserve\n")
        assert (worktree / ".env.example").read_text() == "EXAMPLE=preserve\n"
        assert run().returncode == 0
        assert (root / ".env").read_text() == "LOCAL_VALUE=preserve\n"

        (worktree / ".env").unlink()
        (worktree / ".env").write_text("WORKTREE_VALUE=preserve\n")
        assert run().returncode == 0
        assert (worktree / ".env").read_text() == "WORKTREE_VALUE=preserve\n"

        installed_lock.write_text("stale installation\n")
        previous_calls = calls.read_text()
        assert run().returncode != 0, (
            "Do not share an installation from a different lockfile"
        )
        assert calls.read_text() == previous_calls
        for name in dependencies:
            (worktree / name).unlink()
        assert run().returncode == 0
        assert "pnpm install --frozen-lockfile" in calls.read_text()
        assert all(not (worktree / name).is_symlink() for name in dependencies)
        installed_lock.write_text("same\n")
        assert run().returncode == 0

        (worktree / "pnpm-lock.yaml").write_text("different\n")
        previous_calls = calls.read_text()
        assert run().returncode != 0
        assert calls.read_text() == previous_calls
        for name in dependencies:
            (worktree / name).unlink()
        assert run().returncode == 0
        assert "pnpm install --frozen-lockfile" in calls.read_text()

        (worktree / "pnpm-lock.yaml").write_text("same\n")
        (worktree / "apps/api/.venv").symlink_to(root / "apps/api/.venv")
        previous_calls = calls.read_text()
        assert run().returncode != 0
        assert calls.read_text() == previous_calls
        (root / "apps/api/.venv").rmdir()
        assert run().returncode != 0
        assert calls.read_text() == previous_calls

        env["ORCA_WORKTREE_PATH"] = str(root)
        env["CODEX_WORKTREE_PATH"] = str(root)
        (root / "scripts").mkdir()
        shutil.copy2(script, root / "scripts" / script.name)
        assert run().returncode != 0
        assert calls.read_text() == previous_calls


def test_cleanup():
    with tempfile.TemporaryDirectory(prefix="codex cleanup ") as temporary:
        base = Path(temporary)
        worktree = base / "worktree"
        for directory in (base, worktree):
            cache = directory / "apps/api/.pytest_cache"
            cache.mkdir(parents=True)
            (cache / "sentinel").write_text("preserve outside worktree\n")
        env = dict(os.environ, CODEX_WORKTREE_PATH=str(worktree))
        result = subprocess.run(
            ["bash", "-c", config["cleanup"]["script"]],
            cwd=base,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert not (worktree / "apps/api/.pytest_cache").exists()
        assert (base / "apps/api/.pytest_cache/sentinel").is_file()
        env.pop("CODEX_WORKTREE_PATH")
        result = subprocess.run(
            ["bash", "-c", config["cleanup"]["script"]],
            cwd=base,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert (base / "apps/api/.pytest_cache/sentinel").is_file()


config = tomllib.loads(
    (script.parent.parent / ".codex/environments/environment.toml").read_text()
)
for launcher in ("orca", "codex"):
    test_setup(launcher)
    print(
        f"{launcher} setup: symlinks, repeat runs, local files and dependency isolation passed."
    )

test_cleanup()
print("Codex cleanup: explicit worktree path and API cache removal passed.")
