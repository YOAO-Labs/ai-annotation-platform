"""Public-asset identity, interrupted preparation and local process coordination."""

import hashlib
import errno
import json
import multiprocessing
from pathlib import Path
import time

import pytest

from training_contract.assets import (
    AssetReference,
    AssetUnavailable,
    asset_object_key,
    available_asset_digests,
    load_manifest,
    prepare_asset,
    resolve_asset,
    resolve_prepared_asset,
)


def reference(content=b"reviewed native weight", filename="yolo11n.pt"):
    sha256 = hashlib.sha256(content).hexdigest()
    return AssetReference(sha256, filename, len(content), asset_object_key(sha256))


def test_training_and_backend_resolve_one_blob_and_preserve_verified_legacy_alias(
    tmp_path,
):
    content = b"reviewed native weight"
    item = reference(content)
    legacy = tmp_path / item.filename
    legacy.write_bytes(content)
    assert resolve_asset(tmp_path, item) == legacy
    path = prepare_asset(tmp_path, item, lambda: [content])
    assert path.name == item.sha256 + ".pt"
    assert resolve_asset(tmp_path, item.metadata(len(content))) == path
    assert resolve_prepared_asset(tmp_path, item.filename) == path
    assert available_asset_digests(tmp_path) == [item.sha256]
    assert legacy.read_bytes() == content
    assert path.stat().st_mode & 0o222 == 0
    assert resolve_prepared_asset(tmp_path, "legacy-uncontrolled.pt") is None


def test_same_bytes_with_different_model_names_do_not_transfer_twice(tmp_path):
    content = b"reviewed native weight"
    first = prepare_asset(tmp_path, reference(content), lambda: [content])
    second = prepare_asset(
        tmp_path,
        reference(content, "legacy-alias.pt"),
        lambda: pytest.fail("A warm node must not open a network source"),
    )
    assert first == second
    assert len(load_manifest(tmp_path)) == 2
    assert len(list(tmp_path.glob("*.pt"))) == 1


def test_missing_and_corrupt_digest_never_fall_back_or_claim_readiness(tmp_path):
    item = reference()
    path = prepare_asset(tmp_path, item, lambda: [b"reviewed native weight"])
    (tmp_path / item.filename).write_bytes(b"reviewed native weight")
    path.chmod(0o644)
    path.write_bytes(b"corrupt")
    with pytest.raises(AssetUnavailable):
        resolve_prepared_asset(tmp_path, item.filename)
    with pytest.raises(AssetUnavailable):
        prepare_asset(tmp_path, item, lambda: pytest.fail("No automatic repair"))
    assert available_asset_digests(tmp_path) == []
    path.unlink()
    (tmp_path / item.filename).unlink()
    with pytest.raises(AssetUnavailable):
        resolve_prepared_asset(tmp_path, item.filename)


@pytest.mark.parametrize("kind", ["interrupted", "hash", "size", "overflow", "empty"])
def test_failed_transfer_keeps_partial_files_and_manifest_invisible(tmp_path, kind):
    item = reference()

    def source():
        if kind == "interrupted":
            yield b"reviewed"
            raise ConnectionError("interrupted signed transfer")
        yield {
            "hash": b"x" * item.size_bytes,
            "size": b"reviewed",
            "overflow": b"x" * (item.size_bytes + 1),
            "empty": b"",
        }[kind]

    with pytest.raises((ConnectionError, AssetUnavailable)):
        prepare_asset(tmp_path, item, source)
    assert list(tmp_path.glob("*.pt")) == []
    assert not (tmp_path / "assets.json").exists()
    assert not [
        path for path in tmp_path.iterdir() if path.name != f".{item.sha256}.lock"
    ]


def test_same_filename_cannot_silently_change_immutable_identity(tmp_path):
    item = reference()
    original = prepare_asset(tmp_path, item, lambda: [b"reviewed native weight"])
    with pytest.raises(ValueError, match="different immutable bytes"):
        prepare_asset(tmp_path, reference(b"new bytes"), lambda: [b"new bytes"])
    assert resolve_prepared_asset(tmp_path, item.filename) == original


def test_disk_failure_does_not_publish_partial_bytes_or_replace_manifest(
    tmp_path, monkeypatch
):
    import training_contract.assets as assets

    item = reference()
    original = prepare_asset(tmp_path, item, lambda: [b"reviewed native weight"])
    manifest = (tmp_path / "assets.json").read_bytes()
    other = reference(b"new model", "other.pt")

    def no_space(descriptor):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(assets.os, "fsync", no_space)
    with pytest.raises(OSError) as failure:
        prepare_asset(tmp_path, other, lambda: [b"new model"])
    assert failure.value.errno == errno.ENOSPC
    assert not (tmp_path / (other.sha256 + ".pt")).exists()
    assert not list(tmp_path.glob(f".{other.sha256}-*"))
    assert (tmp_path / "assets.json").read_bytes() == manifest
    assert resolve_prepared_asset(tmp_path, item.filename) == original


def _contending_preparation(root, filename, content, fetches):
    def source():
        with Path(fetches).open("a") as output:
            output.write("fetch\n")
        time.sleep(0.03)
        yield content

    prepare_asset(root, reference(content, filename), source)


@pytest.mark.parametrize("same_digest", [True, False])
def test_concurrent_processes_share_a_blob_without_losing_manifest_entries(
    tmp_path, same_digest
):
    context = multiprocessing.get_context("fork")
    fetches = tmp_path / "fetches"
    processes = [
        context.Process(
            target=_contending_preparation,
            args=(
                tmp_path,
                f"model-{index}.pt",
                b"shared" if same_digest else f"{index}".encode(),
                fetches,
            ),
        )
        for index in range(5)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(5)
    assert len(fetches.read_text().splitlines()) == (1 if same_digest else 5)
    assert len(load_manifest(tmp_path)) == 5


def test_two_nodes_have_independent_paths_and_readiness(tmp_path):
    first, second = tmp_path / "machine-a", tmp_path / "different" / "machine-b"
    item = reference()
    prepare_asset(first, item, lambda: [b"reviewed native weight"])
    assert available_asset_digests(first) == [item.sha256]
    assert available_asset_digests(second) == []
    second_path = prepare_asset(second, item, lambda: [b"reviewed native weight"])
    assert resolve_asset(first, item) != second_path
    assert available_asset_digests(second) == [item.sha256]


@pytest.mark.parametrize(
    "values",
    [
        {"sha256": "0" * 63},
        {"sha256": "G" * 64},
        {"filename": "../escape.pt"},
        {"filename": "sub\\escape.pt"},
        {"filename": "/escape.pt"},
        {"size_bytes": True},
        {"size_bytes": 0},
        {"object_key": "private/project/checkpoint.pt"},
    ],
)
def test_asset_identity_rejects_unsafe_or_conflicting_values(values):
    original = reference().metadata(22)
    original.update(values)
    with pytest.raises(ValueError):
        AssetReference.from_mapping(original)


def test_manifest_cannot_publish_duplicate_filenames(tmp_path):
    metadata = reference().metadata(22)
    (tmp_path / "assets.json").write_text(
        json.dumps({"version": 1, "assets": [metadata, metadata]})
    )
    with pytest.raises(ValueError, match="Duplicate"):
        available_asset_digests(tmp_path)
