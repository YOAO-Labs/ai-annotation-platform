from __future__ import annotations

import os
import re
import stat
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable


class ArchiveSafetyError(ValueError):
    def __init__(self, code: str, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


@dataclass(frozen=True)
class ArchiveLimits:
    max_files: int = 200_000
    max_entry_bytes: int = 512 * 1024 * 1024
    max_total_bytes: int = 8 * 1024 * 1024 * 1024
    max_compression_ratio: float = 100.0
    max_central_directory_entries: int = 400_000


@dataclass(frozen=True)
class SafeArchiveEntry:
    source_name: str
    normalized_path: str
    size_bytes: int
    compressed_bytes: int


_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_CENTRAL_DIRECTORY_HEADER_SIZE = 46
_CENTRAL_DIRECTORY_LENGTHS_OFFSET = 28


def normalize_archive_path(raw_name: str) -> str:
    if not raw_name or "\x00" in raw_name:
        raise ArchiveSafetyError(
            "archive_path_invalid", "archive entry path is empty or contains NUL"
        )
    if "\\" in raw_name:
        raise ArchiveSafetyError(
            "archive_path_invalid", "archive entry path must use '/' separators"
        )
    if raw_name.startswith("/") or _DRIVE_RE.match(raw_name):
        raise ArchiveSafetyError(
            "archive_path_absolute", "absolute archive paths are forbidden"
        )
    path = PurePosixPath(raw_name)
    parts = [part for part in path.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ArchiveSafetyError(
            "archive_path_traversal", "archive path traversal is forbidden"
        )
    normalized = "/".join(parts)
    if len(normalized) > 1024:
        raise ArchiveSafetyError(
            "archive_path_too_long", "archive entry path is too long"
        )
    return normalized


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _scan_central_directory(source: BinaryIO, max_entries: int) -> None:
    """Count central-directory records before ZipFile builds its ZipInfo list."""
    try:
        end_record = zipfile._EndRecData(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArchiveSafetyError(
            "archive_invalid", "archive is not a valid ZIP"
        ) from exc
    if end_record is None:
        return

    # Match ZipFile._RealGetContents' offset calculation after its ZIP64 normalization.
    offset_cd = end_record[zipfile._ECD_OFFSET]
    directory_size = end_record[zipfile._ECD_SIZE]
    concat = end_record[zipfile._ECD_LOCATION] - directory_size - offset_cd
    if end_record[zipfile._ECD_SIGNATURE] == zipfile.stringEndArchive64:
        concat -= zipfile.sizeEndCentDir64 + zipfile.sizeEndCentDir64Locator
    directory_offset = offset_cd + concat
    if directory_size < 0 or directory_offset < 0:
        raise ArchiveSafetyError("archive_invalid", "archive is not a valid ZIP")

    source.seek(directory_offset)
    consumed = 0
    count = 0
    while consumed < directory_size:
        if count >= max_entries:
            raise ArchiveSafetyError(
                "resource_budget_exceeded",
                "archive central directory entry count exceeds the limit",
                detail={
                    "budget": "max_central_directory_entries",
                    "limit": max_entries,
                    "observed": max_entries + 1,
                },
            )
        header = source.read(_CENTRAL_DIRECTORY_HEADER_SIZE)
        if len(header) != _CENTRAL_DIRECTORY_HEADER_SIZE or not header.startswith(
            _CENTRAL_DIRECTORY_SIGNATURE
        ):
            raise ArchiveSafetyError("archive_invalid", "archive is not a valid ZIP")
        filename_size, extra_size, comment_size = struct.unpack_from(
            "<HHH", header, _CENTRAL_DIRECTORY_LENGTHS_OFFSET
        )
        record_size = (
            _CENTRAL_DIRECTORY_HEADER_SIZE + filename_size + extra_size + comment_size
        )
        consumed += record_size
        if consumed > directory_size:
            raise ArchiveSafetyError("archive_invalid", "archive is not a valid ZIP")
        source.seek(filename_size + extra_size + comment_size, os.SEEK_CUR)
        count += 1


def _preflight_central_directory(
    source: str | os.PathLike[str] | BinaryIO, max_entries: int
) -> None:
    if isinstance(source, (str, os.PathLike)):
        try:
            with open(source, "rb") as stream:
                _scan_central_directory(stream, max_entries)
        except OSError as exc:
            raise ArchiveSafetyError(
                "archive_invalid", "archive is not a valid ZIP"
            ) from exc
        return

    position = source.tell()
    try:
        _scan_central_directory(source, max_entries)
    finally:
        source.seek(position)


class SafeZipArchive:
    def __init__(
        self,
        source: str | os.PathLike[str] | BinaryIO,
        limits: ArchiveLimits,
        *,
        skip_unsafe_paths: bool = False,
    ) -> None:
        self.limits = limits
        self.skip_unsafe_paths = skip_unsafe_paths
        self.skipped_paths: list[str] = []
        try:
            _preflight_central_directory(
                source, self.limits.max_central_directory_entries
            )
            self._zip = zipfile.ZipFile(source)
        except ArchiveSafetyError:
            raise
        except (zipfile.BadZipFile, OSError) as exc:
            raise ArchiveSafetyError(
                "archive_invalid", "archive is not a valid ZIP"
            ) from exc
        try:
            self.entries = self._inspect()
        except Exception:
            self._zip.close()
            raise
        self._by_path = {entry.normalized_path: entry for entry in self.entries}

    def _inspect(self) -> list[SafeArchiveEntry]:
        entries: list[SafeArchiveEntry] = []
        exact: set[str] = set()
        folded: dict[str, str] = {}
        total = 0
        for info in self._zip.infolist():
            try:
                normalized = normalize_archive_path(info.filename)
            except ArchiveSafetyError:
                if not self.skip_unsafe_paths:
                    raise
                self.skipped_paths.append(info.filename)
                continue
            if info.is_dir():
                continue
            if _is_symlink(info):
                raise ArchiveSafetyError(
                    "archive_symlink",
                    "archive symlinks are forbidden",
                    detail={"path": normalized},
                )
            if normalized in exact:
                raise ArchiveSafetyError(
                    "archive_duplicate_path",
                    "archive contains duplicate normalized paths",
                    detail={"path": normalized},
                )
            folded_name = normalized.casefold()
            previous = folded.get(folded_name)
            if previous is not None and previous != normalized:
                raise ArchiveSafetyError(
                    "archive_casefold_collision",
                    "archive contains case-folding path collisions",
                    detail={"path": normalized, "conflicts_with": previous},
                )
            if info.file_size > self.limits.max_entry_bytes:
                raise ArchiveSafetyError(
                    "resource_budget_exceeded",
                    "archive entry exceeds the uncompressed byte limit",
                    detail={
                        "budget": "max_entry_bytes",
                        "limit": self.limits.max_entry_bytes,
                        "observed": info.file_size,
                        "path": normalized,
                    },
                )
            ratio = info.file_size / max(1, info.compress_size)
            if ratio > self.limits.max_compression_ratio:
                raise ArchiveSafetyError(
                    "archive_compression_ratio_exceeded",
                    "archive entry compression ratio exceeds the limit",
                    detail={
                        "path": normalized,
                        "limit": self.limits.max_compression_ratio,
                        "observed": ratio,
                    },
                )
            total += info.file_size
            if total > self.limits.max_total_bytes:
                raise ArchiveSafetyError(
                    "resource_budget_exceeded",
                    "archive expanded bytes exceed the limit",
                    detail={
                        "budget": "max_total_bytes",
                        "limit": self.limits.max_total_bytes,
                        "observed": total,
                    },
                )
            exact.add(normalized)
            folded[folded_name] = normalized
            entries.append(
                SafeArchiveEntry(
                    source_name=info.filename,
                    normalized_path=normalized,
                    size_bytes=info.file_size,
                    compressed_bytes=info.compress_size,
                )
            )
            if len(entries) > self.limits.max_files:
                raise ArchiveSafetyError(
                    "resource_budget_exceeded",
                    "archive file count exceeds the limit",
                    detail={
                        "budget": "max_files",
                        "limit": self.limits.max_files,
                        "observed": len(entries),
                    },
                )
        return entries

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> SafeZipArchive:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def require_paths(self, paths: Iterable[str]) -> None:
        missing = sorted(
            normalized
            for normalized in (normalize_archive_path(path) for path in paths)
            if normalized not in self._by_path
        )
        if missing:
            raise ArchiveSafetyError(
                "manifest_reference_missing",
                "manifest references files that are not present in the archive",
                detail={"paths": missing[:100], "missing_count": len(missing)},
            )

    def open(self, normalized_path: str) -> BinaryIO:
        normalized = normalize_archive_path(normalized_path)
        entry = self._by_path.get(normalized)
        if entry is None:
            raise ArchiveSafetyError(
                "archive_entry_missing",
                "archive entry does not exist",
                detail={"path": normalized},
            )
        return self._zip.open(entry.source_name)

    def copy_to(self, normalized_path: str, destination: str | os.PathLike[str]) -> int:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        try:
            with self.open(normalized_path) as source, target.open("wb") as sink:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > self.limits.max_entry_bytes:
                        raise ArchiveSafetyError(
                            "resource_budget_exceeded",
                            "archive entry exceeded the streaming byte limit",
                        )
                    sink.write(chunk)
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        return copied
