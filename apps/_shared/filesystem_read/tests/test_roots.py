from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path

import pytest

from aap_filesystem_read import FilesystemStorageError, RootSpec
from aap_filesystem_read import roots as fs


@pytest.fixture
def controlled_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    return root, RootSpec("test", str(root))


def descriptors_for(root):
    targets = []
    for path in Path("/proc/self/fd").iterdir():
        try:
            target = os.readlink(path)
        except FileNotFoundError:
            continue
        if target == "/" or target.startswith(str(root)):
            targets.append(target)
    return sorted(targets)


def test_root_spec_is_immutable_and_explicit(controlled_root):
    _, spec = controlled_root
    assert spec.relative_base == "" and spec.binding_revision == 1
    with pytest.raises(FrozenInstanceError):
        spec.absolute_root = "/elsewhere"


def test_root_handle_is_an_opaque_uppercase_or_numeric_identifier():
    assert RootSpec("9DATA-Root_1", "/data").root_handle == "9DATA-Root_1"


@pytest.mark.parametrize(
    "changes",
    [
        {"root_handle": "../root"},
        {"absolute_root": "/"},
        {"absolute_root": "relative"},
        {"absolute_root": "/data/../other"},
        {"absolute_root": "/data/"},
        {"absolute_root": "/data\x00"},
        {"relative_base": "../other"},
        {"binding_revision": 0},
        {"binding_revision": True},
    ],
)
def test_root_spec_rejects_invalid_bindings(changes):
    with pytest.raises(FilesystemStorageError):
        RootSpec(**{"root_handle": "test", "absolute_root": "/data", **changes})


@pytest.mark.parametrize(
    "path",
    [
        "/etc",
        "../a",
        "a/../b",
        "a//b",
        "a/./b",
        "a/",
        "a\\b",
        "a\x00b",
        "a" * 1001,
        "/".join(["a"] * 33),
        None,
    ],
)
def test_unsafe_relative_paths_are_rejected(path):
    with pytest.raises(FilesystemStorageError, match="invalid_relative_path"):
        fs.relative_path(path)


def test_manifest_preserves_schema_and_observed_content_is_not_hashed(controlled_root):
    root, spec = controlled_root
    (root / "新增 数据集").mkdir()
    (root / "sample.bin").write_bytes(b"abcdefghijk")
    anchor = fs.capture_anchor(spec)
    assert set(anchor) == {"path_digest", "root", "base"}
    assert anchor["path_digest"] == hashlib.sha256(os.fsencode(root)).hexdigest()
    manifest = fs.scan_directory(spec, "", expected_anchor=anchor)
    assert set(manifest) == {"identity", "digest", "entries"}
    assert [item["name"] for item in manifest["entries"]] == [
        "sample.bin",
        "新增 数据集",
    ]
    assert set(manifest["entries"][0]) == {
        "name",
        "kind",
        "identity",
        "mode",
        "size",
        "mtime_ns",
        "ctime_ns",
    }
    assert (
        manifest["digest"]
        == hashlib.sha256(
            json.dumps(
                [manifest["identity"], manifest["entries"]],
                sort_keys=True,
                ensure_ascii=True,
            ).encode()
        ).hexdigest()
    )
    before = {path: path.stat().st_mtime_ns for path in root.rglob("*")}
    probe = fs.probe_directory(spec, "sample.bin", expected_anchor=anchor)
    assert probe["ok"]
    assert probe["capabilities"] == {
        "list": "verified",
        "read": "verified",
        "range": "verified",
        "write": "unsupported",
        "delete": "unsupported",
        "presign": "unsupported",
    }
    assert before == {path: path.stat().st_mtime_ns for path in root.rglob("*")}


def test_symlinks_and_root_replacement_fail_closed(controlled_root, tmp_path):
    root, spec = controlled_root
    anchor = fs.capture_anchor(spec)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"outside")
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "file-link").symlink_to(outside / "secret")
    with pytest.raises(FilesystemStorageError, match="symlink_rejected"):
        fs.browse_directory(spec, "link", expected_anchor=anchor, limit=1)
    with pytest.raises(FilesystemStorageError, match="symlink_rejected"):
        with fs.content_file(spec, "", "file-link", expected_anchor=anchor):
            pytest.fail("A symlink must never yield a file")
    root.rename(tmp_path / "original")
    root.mkdir()
    with pytest.raises(FilesystemStorageError, match="root_identity_changed"):
        fs.scan_directory(spec, "", expected_anchor=anchor)


@pytest.mark.parametrize("operation", ["scan", "browse", "read"])
def test_missing_root_never_uses_parent_or_leaks_descriptors(
    controlled_root, operation
):
    root, spec = controlled_root
    anchor = fs.capture_anchor(spec)
    root.rmdir()
    (root.parent / "secret.bin").write_bytes(b"outside the controlled root")
    before = descriptors_for(root)
    with pytest.raises(FilesystemStorageError, match="^path_unavailable$") as failure:
        if operation == "scan":
            fs.scan_directory(spec, "", expected_anchor=anchor)
        elif operation == "browse":
            fs.browse_directory(spec, "", expected_anchor=anchor, limit=1)
        else:
            with fs.content_file(spec, "", "secret.bin", expected_anchor=anchor):
                pytest.fail("A missing root must never yield its parent's file")
    assert failure.value.storage_fault_category == "root_unavailable"
    assert descriptors_for(root) == before


def test_relative_base_replacement_and_special_file(controlled_root):
    root, spec = controlled_root
    (root / "data").mkdir()
    spec = RootSpec(spec.root_handle, spec.absolute_root, relative_base="data")
    anchor = fs.capture_anchor(spec)
    os.mkfifo(root / "data/fifo")
    assert fs.probe_directory(spec, "fifo", expected_anchor=anchor)["code"] == (
        "not_a_regular_file"
    )
    (root / "data").rename(root / "old-data")
    (root / "data").mkdir()
    with pytest.raises(FilesystemStorageError, match="root_identity_changed"):
        fs.scan_directory(spec, "", expected_anchor=anchor)


@pytest.mark.parametrize("replacement", ["file", "source_directory"])
def test_open_content_revalidates_replacements_before_exit(
    controlled_root, replacement
):
    root, spec = controlled_root
    source = root / "source"
    source.mkdir()
    path = source / "sample.bin"
    path.write_bytes(b"original")
    anchor = fs.capture_anchor(spec)
    with pytest.raises(FilesystemStorageError, match="source_changed"):
        with fs.content_file(spec, "source", "sample.bin", expected_anchor=anchor) as (
            body,
            observation,
        ):
            assert body.read() == b"original"
            assert set(observation) == {
                "identity",
                "size",
                "mode",
                "mtime_ns",
                "ctime_ns",
                "source_identity",
            }
            if replacement == "file":
                path.unlink()
                path.write_bytes(b"replaced")
            else:
                source.rename(root / "old-source")
                source.mkdir()
                os.link(root / "old-source/sample.bin", path)


def test_open_content_rejects_expected_observation_and_source_identity(controlled_root):
    root, spec = controlled_root
    (root / "sample.bin").write_bytes(b"bytes")
    anchor = fs.capture_anchor(spec)
    for expected in [{"size": 0}, {"mtime_ns": 0}]:
        with pytest.raises(FilesystemStorageError, match="source_changed"):
            with fs.content_file(
                spec, "", "sample.bin", expected_anchor=anchor, expected=expected
            ):
                pytest.fail("Mismatched observations must not yield")
    with pytest.raises(FilesystemStorageError, match="directory_identity_changed"):
        with fs.content_file(
            spec,
            "",
            "sample.bin",
            expected_anchor=anchor,
            expected_source_identity=[-1, -1],
        ):
            pytest.fail("Mismatched source identity must not yield")


def test_browse_snapshot_and_budget_include_filtered_entries(
    controlled_root, monkeypatch
):
    root, spec = controlled_root
    for name in ["a", "b", "c"]:
        (root / name).mkdir()
    anchor = fs.capture_anchor(spec)
    names, more, snapshot = fs.browse_directory(
        spec, "", expected_anchor=anchor, limit=1
    )
    assert names == ["a"] and more
    assert fs.browse_directory(
        spec, "", expected_anchor=anchor, limit=1, after="a", snapshot=snapshot
    )[0] == ["b"]
    (root / "z.bin").write_bytes(b"")
    with pytest.raises(FilesystemStorageError, match="directory_changed"):
        fs.browse_directory(
            spec, "", expected_anchor=anchor, limit=1, snapshot=snapshot
        )
    monkeypatch.setattr(fs, "MAX_ENTRIES", 3)
    with pytest.raises(FilesystemStorageError, match="directory_too_large"):
        fs.browse_directory(spec, "", expected_anchor=anchor, limit=1)
    with pytest.raises(FilesystemStorageError, match="directory_too_large"):
        fs.scan_directory(spec, "", expected_anchor=anchor)


@pytest.mark.parametrize("with_file", [False, True])
def test_scan_checks_time_budget_after_final_filesystem_call(
    controlled_root, monkeypatch, with_file
):
    root, spec = controlled_root
    if with_file:
        (root / "a.jpg").write_bytes(b"a")
    times = iter([0, 0, 3] if with_file else [0, 3])
    monkeypatch.setattr(fs.time, "monotonic", lambda: next(times))
    with pytest.raises(FilesystemStorageError, match="directory_budget_exceeded"):
        fs.scan_directory(spec, "", expected_anchor=fs.capture_anchor(spec))


def test_non_utf8_names_and_unsupported_kernel_fail_closed(
    controlled_root, monkeypatch
):
    root, spec = controlled_root
    os.mkdir(os.fsencode(root) + b"/invalid-\xff")
    with pytest.raises(FilesystemStorageError, match="unsupported_path_encoding"):
        fs.scan_directory(spec, "", expected_anchor=fs.capture_anchor(spec))
    assert fs.probe_directory(spec)["code"] == "unsupported_path_encoding"
    monkeypatch.setattr(fs.platform, "system", lambda: "unsupported")
    assert fs.probe_directory(spec)["code"] == "safe_open_unsupported"


def test_failure_paths_close_all_root_descriptors(controlled_root):
    root, spec = controlled_root
    before = descriptors_for(root)
    for _ in range(30):
        fs.probe_directory(spec, "missing")
    assert descriptors_for(root) == before


def test_descriptor_close_uncertainty_survives_root_cleanup(
    controlled_root, monkeypatch
):
    _, spec = controlled_root
    actual_close = fs.os.close

    def uncertain_close(fd):
        actual_close(fd)
        raise OSError("close outcome is uncertain")

    monkeypatch.setattr(fs.os, "close", uncertain_close)
    with pytest.raises(OSError) as caught:
        fs.capture_anchor(spec)
    assert caught.value.storage_operation_unsettled is True
