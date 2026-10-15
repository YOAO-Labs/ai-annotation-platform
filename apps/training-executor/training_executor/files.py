"""Safe materialization and atomic, content-addressed local artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


def digest_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as target:
        json.dump(value, target, ensure_ascii=False, allow_nan=False)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def extract_dataset(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    total = 0
    with ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) > 50000:
            raise ValueError("Too many dataset archive entries")
        names = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            total += entry.file_size
            mode = entry.external_attr >> 16
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or entry.filename in names
                or stat.S_ISLNK(mode)
                or total > 24 * 1024**3
                or entry.flag_bits & 1
            ):
                raise ValueError("Unsafe dataset archive entry")
            names.add(entry.filename)
        for entry in entries:
            target = destination.joinpath(*PurePosixPath(entry.filename).parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    yaml_path = destination / "data.yaml"
    if not yaml_path.is_file():
        raise ValueError("Dataset is missing data.yaml")
    return yaml_path.resolve()


def immutable_copy(source: Path, destination: Path) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if digest_file(source) != digest_file(destination):
            raise ValueError("Immutable checkpoint already has different content")
    else:
        temporary = destination.with_suffix(".tmp")
        with source.open("rb") as original, temporary.open("wb") as copy:
            shutil.copyfileobj(original, copy, length=1024 * 1024)
            copy.flush()
            os.fsync(copy.fileno())
        os.replace(temporary, destination)
        sync_directory(destination.parent)
    return {
        "name": destination.name,
        "sha256": digest_file(destination),
        "size_bytes": destination.stat().st_size,
    }
