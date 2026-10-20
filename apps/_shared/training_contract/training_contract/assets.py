"""Immutable public model assets and node-local, read-only consumer resolution.

Only the administrator preparation process writes the cache. Runtime consumers
resolve verified files; they never download while holding a GPU admission.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

MANIFEST_NAME = "assets.json"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 2 * 1024**3


class AssetUnavailable(ValueError):
    """A required immutable asset is absent or differs from its identity."""


def asset_object_key(sha256: str) -> str:
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise ValueError("Asset SHA256 must be 64 lowercase hexadecimal characters")
    return f"model-assets/sha256/{sha256}/weights.pt"


@dataclass(frozen=True)
class AssetReference:
    sha256: str
    filename: str
    size_bytes: int | None = None
    object_key: str | None = None

    def __post_init__(self):
        key = asset_object_key(self.sha256)
        if (
            not isinstance(self.filename, str)
            or not self.filename.endswith(".pt")
            or self.filename in {".pt", "..pt"}
            or len(self.filename) > 200
            or any(char in self.filename for char in "/\\\x00")
            or Path(self.filename).name != self.filename
        ):
            raise ValueError("Asset filename must be a local .pt filename")
        if self.size_bytes is not None and (
            type(self.size_bytes) is not int
            or not 0 < self.size_bytes <= MAX_ASSET_BYTES
        ):
            raise ValueError("Asset byte count is outside the preparation limit")
        if self.object_key is not None and self.object_key != key:
            raise ValueError("Asset object key must match its immutable SHA256")

    @classmethod
    def from_mapping(cls, value: Mapping) -> AssetReference:
        return cls(
            sha256=value["sha256"],
            filename=value["filename"],
            size_bytes=value.get("size_bytes"),
            object_key=value.get("object_key"),
        )

    def metadata(self, size_bytes: int | None) -> dict:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "size_bytes": size_bytes,
            "object_key": asset_object_key(self.sha256),
        }


def _reference(value: AssetReference | Mapping) -> AssetReference:
    return (
        value
        if isinstance(value, AssetReference)
        else AssetReference.from_mapping(value)
    )


def _verify(path: Path, reference: AssetReference) -> Path:
    try:
        size = path.stat().st_size
        if (
            not path.is_file()
            or not 0 < size <= MAX_ASSET_BYTES
            or (reference.size_bytes is not None and size != reference.size_bytes)
        ):
            raise AssetUnavailable("Prepared model asset byte count differs")
        checksum = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                checksum.update(chunk)
        if checksum.hexdigest() != reference.sha256:
            raise AssetUnavailable("Prepared model asset SHA256 differs")
    except OSError as exc:
        raise AssetUnavailable(
            "Prepared model asset is unavailable on this node"
        ) from exc
    return path


def resolve_asset(cache_dir: Path | str, reference: AssetReference | Mapping) -> Path:
    """Resolve a digest first, preserving verified legacy filenames if unmigrated.

    A corrupt digest entry cannot fall back to an alias or trigger a download.
    """
    reference = _reference(reference)
    root = Path(cache_dir)
    blob = root / f"{reference.sha256}.pt"
    if blob.exists() or blob.is_symlink():
        return _verify(blob, reference)
    return _verify(root / reference.filename, reference)


def read_manifest(path: Path | str) -> list[AssetReference]:
    path = Path(path)
    with path.open("rb") as source:
        content = source.read(MAX_MANIFEST_BYTES + 1)
    if len(content) > MAX_MANIFEST_BYTES:
        raise ValueError("Asset manifest exceeds its size limit")
    body = json.loads(content)
    if (
        not isinstance(body, dict)
        or body.get("version") != 1
        or not isinstance(body.get("assets"), list)
    ):
        raise ValueError("Unsupported model asset manifest")
    references = [AssetReference.from_mapping(item) for item in body["assets"]]
    names = [reference.filename for reference in references]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate model asset filenames")
    return references


def load_manifest(cache_dir: Path | str) -> list[AssetReference]:
    path = Path(cache_dir) / MANIFEST_NAME
    return read_manifest(path) if path.exists() else []


def resolve_prepared_asset(cache_dir: Path | str, filename: str) -> Path | None:
    for reference in load_manifest(cache_dir):
        if reference.filename == filename:
            return resolve_asset(cache_dir, reference)
    return None


def available_asset_digests(cache_dir: Path | str) -> list[str]:
    """Snapshot prepared identities at startup, never infer another node's state."""
    available = set()
    for reference in load_manifest(cache_dir):
        try:
            resolve_asset(cache_dir, reference)
        except AssetUnavailable:
            continue
        available.add(reference.sha256)
    return sorted(available)


@contextmanager
def _lock(path: Path):
    with path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _sync_directory(path: Path):
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _record(root: Path, reference: AssetReference, size: int):
    with _lock(root / ".assets.lock"):
        assets = {
            item.filename: item.metadata(item.size_bytes)
            for item in load_manifest(root)
        }
        existing = assets.get(reference.filename)
        if existing and existing["sha256"] != reference.sha256:
            raise ValueError(
                "Asset filename already refers to different immutable bytes"
            )
        assets[reference.filename] = reference.metadata(size)
        descriptor, temporary = tempfile.mkstemp(prefix=".assets-", dir=root)
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(
                    {"version": 1, "assets": [assets[key] for key in sorted(assets)]},
                    output,
                    sort_keys=True,
                )
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o444)
            os.replace(temporary, root / MANIFEST_NAME)
            _sync_directory(root)
        finally:
            Path(temporary).unlink(missing_ok=True)


def prepare_asset(
    cache_dir: Path | str,
    reference: AssetReference | Mapping,
    source: Callable[[], Iterable[bytes]],
) -> Path:
    """Atomically prepare one verified blob; source is opened only on a cold node.

    The per-digest flock also works across co-located preparation processes. A
    separate manifest lock prevents lost mappings when different assets arrive.
    The caller owns bounded transfer/retry and authorization to the source.
    """
    reference = _reference(reference)
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{reference.sha256}.pt"
    with _lock(root / f".{reference.sha256}.lock"):
        if target.exists() or target.is_symlink():
            _verify(target, reference)
        else:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{reference.sha256}-", dir=root
            )
            try:
                size = 0
                checksum = hashlib.sha256()
                with os.fdopen(descriptor, "wb") as output:
                    for chunk in source():
                        size += len(chunk)
                        if size > (reference.size_bytes or MAX_ASSET_BYTES):
                            raise AssetUnavailable(
                                "Asset transfer exceeds its byte count limit"
                            )
                        output.write(chunk)
                        checksum.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if (
                    not size
                    or checksum.hexdigest() != reference.sha256
                    or (
                        reference.size_bytes is not None
                        and size != reference.size_bytes
                    )
                ):
                    raise AssetUnavailable(
                        "Asset transfer differs from its immutable identity"
                    )
                os.chmod(temporary, 0o444)
                os.replace(temporary, target)
                _sync_directory(root)
            finally:
                Path(temporary).unlink(missing_ok=True)
        _record(root, reference, target.stat().st_size)
    return target
