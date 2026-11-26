"""Run with python3 scripts/test-orca-worktree-setup.py (no installs or services)."""

import hashlib
import os
import shutil
from pathlib import Path
import subprocess
import sys
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
        base = Path(temporary).resolve()
        root, worktree, binaries = (
            base / name for name in ("primary", "worktree", "bin")
        )
        for directory in (root, worktree, binaries):
            directory.mkdir()
        dependencies = (
            "node_modules",
            "apps/web/node_modules",
        )
        manifests = (
            "pnpm-lock.yaml",
            "pnpm-workspace.yaml",
            "package.json",
            "apps/web/package.json",
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
        wheel_path = Path(
            ".artifacts/python/plugin-sdk/yoao_backend_runtime-0.2.0-py3-none-any.whl"
        )
        wheel_bytes = b"locked Plugin-SDK wheel\n"
        wheel_hash = hashlib.sha256(wheel_bytes).hexdigest()
        (worktree / "apps/api/uv.lock").write_text(
            f'[[package]]\nname = "yoao-backend-runtime"\nversion = "0.2.0"\n'
            f'source = {{ path = "../../{wheel_path.as_posix()}" }}\n'
            f'wheels = [{{ filename = "{wheel_path.name}", '
            f'hash = "sha256:{wheel_hash}" }}]\n'
        )
        primary_wheel = root / wheel_path
        local_wheel = worktree / wheel_path
        primary_wheel.parent.mkdir(parents=True)
        primary_wheel.write_bytes(wheel_bytes)
        calls = base / "calls"
        (binaries / "pnpm").write_text(
            '#!/bin/sh\nprintf "pnpm %s\\n" "$*" >> "$CALLS"\n'
        )
        (binaries / "uv").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = run ]; then\n'
            '    while [ "$1" != -- ]; do shift; done\n'
            "    shift 2\n"
            '    exec "$TEST_PYTHON" "$@"\n'
            "fi\n"
            'if [ ! -f "$LOCKED_WHEEL" ]; then\n'
            '    echo "uv sync cannot open locked wheel: $LOCKED_WHEEL" >&2\n'
            "    exit 1\n"
            "fi\n"
            'printf "uv %s\\n" "$*" >> "$CALLS"\n'
        )
        for name in ("pnpm", "uv"):
            (binaries / name).chmod(0o755)
        env = dict(
            os.environ,
            PATH=f"{binaries}:{os.environ['PATH']}",
            ORCA_ROOT_PATH=str(root),
            ORCA_WORKTREE_PATH=str(worktree),
            CALLS=str(calls),
            LOCKED_WHEEL=str(wheel_path),
            TEST_PYTHON=sys.executable,
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
        assert local_wheel.read_bytes() == wheel_bytes
        assert local_wheel.resolve() == local_wheel
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
        primary_wheel.unlink()
        assert run().returncode == 0
        assert (root / ".env").read_text() == "LOCAL_VALUE=preserve\n"

        local_wheel.unlink()
        previous_calls = calls.read_text()
        result = run()
        assert result.returncode != 0
        assert "scripts/fetch-python-artifacts.py" in result.stderr
        assert calls.read_text() == previous_calls

        primary_wheel.write_bytes(b"not the locked wheel\n")
        result = run()
        assert result.returncode != 0
        assert "does not match apps/api/uv.lock" in result.stderr
        assert not local_wheel.exists()
        assert calls.read_text() == previous_calls
        primary_wheel.write_bytes(wheel_bytes)
        local_wheel.parent.rmdir()
        local_wheel.parent.symlink_to(primary_wheel.parent, target_is_directory=True)
        result = run()
        assert result.returncode != 0
        assert "must be local to this worktree" in result.stderr
        assert calls.read_text() == previous_calls
        local_wheel.parent.unlink()

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
