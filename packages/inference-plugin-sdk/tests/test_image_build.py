import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from aap_inference_plugin.image_build import _locked_names, build_image


def make_wheel(directory, requires=None):
    wheel = directory / "author_plugin-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("author_plugin.py", "def main(): pass\n")
        archive.writestr("author_plugin-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: author-plugin\nVersion: 1.0\n" + (f"Requires-Dist: {requires}\n" if requires else ""))
        archive.writestr("author_plugin-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("author_plugin-1.0.dist-info/entry_points.txt", "[console_scripts]\nauthor-plugin = author_plugin:main\n")
        archive.writestr("author_plugin-1.0.dist-info/RECORD", "")
    return wheel


def test_pip_rejects_bad_hash_and_missing_dependency(tmp_path):
    wheel = make_wheel(tmp_path, requires="missing-package==1.0")
    lock = tmp_path / "requirements.lock"
    lock.write_text(f"author-plugin==1.0 --hash=sha256:{'0' * 64}\n")
    def install():
        return subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--find-links", str(tmp_path), "--only-binary=:all:", "--require-hashes", "--target", str(tmp_path / "target"), "-r", str(lock)], capture_output=True, text=True)
    bad_hash = install()
    assert bad_hash.returncode != 0
    assert "hash" in bad_hash.stderr.lower()
    lock.write_text(f"author-plugin==1.0 --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n")
    missing = install()
    assert missing.returncode != 0
    assert "missing-package" in missing.stderr.lower()


@pytest.mark.parametrize("line", ["author-plugin>=1 --hash=sha256:" + "a" * 64,
    "author-plugin==1", "-r other.lock", "author-plugin @ https://example.com/file.whl --hash=sha256:" + "a" * 64])
def test_lock_rejects_unpinned_or_unhashed_entries(tmp_path, line):
    lock = tmp_path / "lock"
    lock.write_text(line + "\n")
    with pytest.raises(ValueError):
        _locked_names(lock)


def test_builder_uses_exec_entrypoint_and_offline_lock(tmp_path, monkeypatch):
    wheel = make_wheel(tmp_path)
    lock = tmp_path / "lock"
    lock.write_text(f"author-plugin==1.0 --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n")
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "build":
            context = Path(argv[-1])
            recipe = (context / "Dockerfile").read_text()
            assert "--network=none" in recipe and "--no-index" in recipe and "--require-hashes" in recipe
            assert "--mount=type=bind,source=wheelhouse" in recipe
            assert "libgl1 libglib2.0-0" in recipe
            entry = next(line for line in recipe.splitlines() if line.startswith("ENTRYPOINT "))
            assert json.loads(entry.removeprefix("ENTRYPOINT ")) == ["author-plugin", "serve", "--host", "0.0.0.0", "--port", "8000"]
            assert (context / "requirements.lock").read_text() == lock.read_text()
    monkeypatch.setattr("aap_inference_plugin.image_build.subprocess.run", run)
    args = dict(base_image="python@sha256:" + "a" * 64, wheelhouse=tmp_path,
                requirements_lock=lock, entrypoint="author-plugin", tag="example:tag")
    build_image(**args, push=True)
    assert [call[1] for call in calls] == ["build", "push"]
    for script in ("/bin/sh", "author;touch", "author plugin", ""):
        with pytest.raises(ValueError):
            build_image(**(args | {"entrypoint": script}))
