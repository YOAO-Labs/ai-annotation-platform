from pathlib import Path
import os

import pytest

from app.config import settings
from app.services import filesystem_storage as fs


@pytest.fixture
def controlled_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(settings, "storage_filesystem_roots", {"test": str(root)})
    return root, {"root_handle": "test", "relative_path": ""}


def test_dynamic_subdirectories_and_read_capabilities_without_writes(controlled_root):
    root, config = controlled_root
    anchor = fs.capture_anchor(config)
    first = fs.probe_directory(config, expected_anchor=anchor)
    assert first["ok"]
    assert first["capabilities"]["list"] == "verified"
    assert first["capabilities"]["read"] == "unknown"
    child = root / "新增 数据集"
    child.mkdir()
    (child / "sample.bin").write_bytes(b"abcdefghijk")
    before = {p.relative_to(root): p.stat().st_mtime_ns for p in root.rglob("*")}
    names, more, _ = fs.browse_directory(config, "", expected_anchor=anchor, limit=100)
    assert names == ["新增 数据集"] and not more
    sample = fs.probe_directory(
        config, "新增 数据集/sample.bin", expected_anchor=anchor
    )
    assert (
        sample["capabilities"]["read"] == sample["capabilities"]["range"] == "verified"
    )
    assert all(
        sample["capabilities"][key] == "unsupported"
        for key in ["write", "delete", "presign"]
    )
    assert before == {
        p.relative_to(root): p.stat().st_mtime_ns for p in root.rglob("*")
    }


@pytest.mark.parametrize(
    "path", ["/etc", "../a", "a/../b", "a//b", "a/./b", "a/", "a\\b", "a\x00b"]
)
def test_unsafe_relative_paths_are_rejected(path):
    with pytest.raises(fs.FilesystemStorageError, match="invalid_relative_path"):
        fs.relative_path(path)


def test_no_symlinks_and_root_replacement(controlled_root, tmp_path):
    root, config = controlled_root
    anchor = fs.capture_anchor(config)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(fs.FilesystemStorageError, match="symlink_rejected"):
        fs.browse_directory(config, "link", expected_anchor=anchor, limit=1)
    root.rename(tmp_path / "original")
    root.mkdir()
    with pytest.raises(fs.FilesystemStorageError, match="root_identity_changed"):
        fs.browse_directory(config, "", expected_anchor=anchor, limit=1)


def test_subdirectory_anchor_and_special_files(controlled_root):
    root, config = controlled_root
    (root / "data").mkdir()
    config = {**config, "relative_path": "data"}
    anchor = fs.capture_anchor(config)
    os.mkfifo(root / "data" / "fifo")
    assert (
        fs.probe_directory(config, "fifo", expected_anchor=anchor)["code"]
        == "not_a_regular_file"
    )
    (root / "data").rename(root / "old-data")
    (root / "data").mkdir()
    assert (
        fs.probe_directory(config, expected_anchor=anchor)["code"]
        == "root_identity_changed"
    )


def test_directory_budget_counts_filtered_entries_and_snapshot_changes(
    controlled_root, monkeypatch
):
    root, config = controlled_root
    anchor = fs.capture_anchor(config)
    for name in ["a", "b", "c"]:
        (root / name).mkdir()
    names, more, snapshot = fs.browse_directory(
        config, "", expected_anchor=anchor, limit=1
    )
    assert names == ["a"] and more
    assert fs.browse_directory(
        config, "", expected_anchor=anchor, limit=1, after="a", snapshot=snapshot
    )[0] == ["b"]
    (root / "z").write_bytes(b"")
    with pytest.raises(fs.FilesystemStorageError, match="directory_changed"):
        fs.browse_directory(
            config, "", expected_anchor=anchor, limit=1, snapshot=snapshot
        )
    monkeypatch.setattr(fs, "MAX_ENTRIES", 3)
    with pytest.raises(fs.FilesystemStorageError, match="directory_too_large"):
        fs.browse_directory(config, "", expected_anchor=anchor, limit=1)


def test_repeated_opens_do_not_leak_descriptors(controlled_root):
    root, config = controlled_root

    def directory_handles():
        handles = []
        for path in Path("/proc/self/fd").iterdir():
            try:
                target = os.readlink(path)
            except FileNotFoundError:
                continue
            if target == "/" or target.startswith(str(root)):
                handles.append(target)
        return sorted(handles)

    before = directory_handles()
    for _ in range(30):
        fs.probe_directory(config, "missing")
    assert directory_handles() == before


def test_unsupported_kernel_fails_closed(controlled_root, monkeypatch):
    _, config = controlled_root
    monkeypatch.setattr(fs.platform, "system", lambda: "unsupported")
    assert fs.probe_directory(config)["code"] == "safe_open_unsupported"


def test_non_utf8_name_is_rejected_consistently(controlled_root):
    root, config = controlled_root
    os.mkdir(os.fsencode(root) + b"/invalid-\xff")
    with pytest.raises(fs.FilesystemStorageError, match="unsupported_path_encoding"):
        fs.browse_directory(config, "", expected_anchor=None, limit=1)
    result = fs.probe_directory(config)
    assert not result["ok"] and result["code"] == "unsupported_path_encoding"
    assert result["capabilities"]["list"] == "unknown"


@pytest.mark.parametrize("with_file", [False, True])
def test_scan_rejects_budget_exceeded_by_final_filesystem_call(
    controlled_root, monkeypatch, with_file
):
    root, config = controlled_root
    if with_file:
        (root / "a.jpg").write_bytes(b"a")
    times = iter([0, 0, 3] if with_file else [0, 3])
    monkeypatch.setattr(fs.time, "monotonic", lambda: next(times))
    with pytest.raises(fs.FilesystemStorageError, match="directory_budget_exceeded"):
        fs.scan_directory(config, "", expected_anchor=fs.capture_anchor(config))
