"""Read-only controlled Linux roots. No path resolution falls back to open()."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ctypes
import errno
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import stat
import time

MAX_ENTRIES = 5000
_BENEATH = 0x08
_NO_SYMLINKS = 0x04
_NO_XDEV = 0x01


class FilesystemStorageError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _close_descriptors(handles):
    failure = None
    for handle in handles:
        try:
            os.close(handle)
        except BaseException as exc:
            failure = mark_unsettled(exc)
    if failure is not None:
        raise failure


def relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise FilesystemStorageError("invalid_relative_path")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise FilesystemStorageError("unsupported_path_encoding") from None
    if (
        len(value) > 1000
        or len(value.split("/")) > 32
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or (value and any(part in {"", ".", ".."} for part in value.split("/")))
    ):
        raise FilesystemStorageError("invalid_relative_path")
    return value


def mark_unsettled(exc):
    """Keep physical close uncertainty visible to the caller's admission owner."""
    exc.storage_operation_unsettled = True
    return exc


def close_registered(resource):
    try:
        resource.close()
    except BaseException as exc:
        raise mark_unsettled(exc)


@contextmanager
def closing_registered(resource):
    try:
        yield resource
    finally:
        close_registered(resource)


@dataclass(frozen=True)
class RootSpec:
    """An explicit local root binding; no environment or settings lookup occurs."""

    root_handle: str
    absolute_root: str
    relative_base: str = ""
    binding_revision: int = 1

    def __post_init__(self):
        root = self.absolute_root
        if (
            not isinstance(self.root_handle, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.root_handle) is None
            or not isinstance(root, str)
            or not root.startswith("/")
            or root == "/"
            or "\x00" in root
            or any(part in {".", "..", ""} for part in root[1:].split("/"))
            or type(self.binding_revision) is not int
            or self.binding_revision < 1
        ):
            raise FilesystemStorageError("invalid_root_spec")
        try:
            root.encode("utf-8", errors="strict")
        except UnicodeError:
            raise FilesystemStorageError("unsupported_path_encoding") from None
        relative_path(self.relative_base)


def _open_beneath(parent: int, path: str, *, directory: bool, no_xdev=True) -> int:
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise FilesystemStorageError("safe_open_unsupported")

    class OpenHow(ctypes.Structure):
        _fields_ = [
            ("flags", ctypes.c_uint64),
            ("mode", ctypes.c_uint64),
            ("resolve", ctypes.c_uint64),
        ]

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    how = OpenHow(flags, 0, _BENEATH | _NO_SYMLINKS | (_NO_XDEV if no_xdev else 0))
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(
        ctypes.c_long(437),
        ctypes.c_int(parent),
        ctypes.c_char_p(os.fsencode(path or ".")),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if result < 0:
        error = ctypes.get_errno()
        code = {
            errno.ENOSYS: "safe_open_unsupported",
            errno.EINVAL: "safe_open_unsupported",
            errno.ENOENT: "path_unavailable",
            errno.ENOTDIR: "not_a_directory",
            errno.EACCES: "permission_denied",
            errno.EPERM: "permission_denied",
            errno.ELOOP: "symlink_rejected",
            errno.EXDEV: "path_boundary_rejected",
            errno.EAGAIN: "path_changed",
        }.get(error, "filesystem_unavailable")
        raise FilesystemStorageError(code)
    return int(result)


def _identity(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino]


def host_identity():
    try:
        return hashlib.sha256(
            Path("/proc/sys/kernel/random/boot_id").read_bytes()
        ).hexdigest()
    except OSError:
        return None


@contextmanager
def location_directory(spec, path="", *, expected_anchor=None):
    path = relative_path(path)
    base = spec.relative_base
    root = spec.absolute_root
    handles = []
    try:
        handles.append(os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        try:
            root_fd = _open_beneath(
                handles[0], root.lstrip("/"), directory=True, no_xdev=False
            )
        except FilesystemStorageError as exc:
            if exc.code in {"path_unavailable", "not_a_directory"}:
                exc.storage_fault_category = "root_unavailable"
            raise
        handles.append(root_fd)
        base_fd = _open_beneath(root_fd, base, directory=True)
        handles.append(base_fd)
        anchor = {
            "path_digest": hashlib.sha256(os.fsencode(root)).hexdigest(),
            "root": _identity(root_fd),
            "base": _identity(base_fd),
        }
        if expected_anchor is not None and anchor != expected_anchor:
            raise FilesystemStorageError("root_identity_changed")
        target_fd = _open_beneath(base_fd, path, directory=True)
        handles.append(target_fd)
        yield target_fd, anchor
    except OSError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise
        raise FilesystemStorageError("filesystem_unavailable") from None
    finally:
        _close_descriptors(reversed(handles))


def capture_anchor(spec):
    with location_directory(spec) as (_, anchor):
        return anchor


def file_observation(info):
    return {
        "identity": [info.st_dev, info.st_ino],
        "size": info.st_size,
        "mode": info.st_mode,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


@contextmanager
def content_file(
    spec,
    source_path,
    path,
    *,
    expected_anchor,
    expected=None,
    expected_source_identity=None,
):
    """Keep the safe source directory and opened file pinned throughout copying."""
    path = relative_path(path)
    if not path:
        raise FilesystemStorageError("invalid_relative_path")
    with location_directory(spec, source_path, expected_anchor=expected_anchor) as (
        parent,
        _,
    ):
        source_identity = _identity(parent)
        if (
            expected_source_identity is not None
            and source_identity != expected_source_identity
        ):
            raise FilesystemStorageError("directory_identity_changed")
        fd = _open_beneath(parent, path, directory=False)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise FilesystemStorageError("not_a_regular_file")
            fingerprint = file_observation(before)
            observation = {**fingerprint, "source_identity": source_identity}
            if expected and any(observation.get(k) != v for k, v in expected.items()):
                raise FilesystemStorageError("source_changed")
            with closing_registered(os.fdopen(fd, "rb", closefd=False)) as body:
                yield body, observation
            if file_observation(os.fstat(fd)) != fingerprint:
                raise FilesystemStorageError("source_changed")
            current = _open_beneath(parent, path, directory=False)
            try:
                if file_observation(os.fstat(current)) != fingerprint:
                    raise FilesystemStorageError("source_changed")
            finally:
                _close_descriptors([current])
            # Detect replacement of the source directory itself after opening.
            with location_directory(
                spec, source_path, expected_anchor=expected_anchor
            ) as (again, _):
                if _identity(again) != _identity(parent):
                    raise FilesystemStorageError("source_changed")
        finally:
            _close_descriptors([fd])


def scan_directory(spec, path, *, expected_anchor, expected_identity=None):
    """Capture one bounded directory; callers commit the whole manifest atomically."""
    with location_directory(spec, path, expected_anchor=expected_anchor) as (fd, _):
        before = os.fstat(fd)
        identity = [before.st_dev, before.st_ino]
        if expected_identity is not None and identity != expected_identity:
            raise FilesystemStorageError("directory_identity_changed")
        entries = []
        deadline = time.monotonic() + 2
        with closing_registered(os.scandir(fd)) as iterator:
            for entry in iterator:
                if len(entries) >= MAX_ENTRIES:
                    raise FilesystemStorageError("directory_too_large")
                if time.monotonic() > deadline:
                    raise FilesystemStorageError("directory_budget_exceeded")
                relative_path(entry.name)
                info = entry.stat(follow_symlinks=False)
                kind = (
                    "directory"
                    if stat.S_ISDIR(info.st_mode)
                    else "file"
                    if stat.S_ISREG(info.st_mode)
                    else "symlink"
                    if stat.S_ISLNK(info.st_mode)
                    else "special"
                )
                entries.append(
                    {
                        "name": entry.name,
                        "kind": kind,
                        "identity": [info.st_dev, info.st_ino],
                        "mode": info.st_mode,
                        "size": info.st_size,
                        "mtime_ns": info.st_mtime_ns,
                        "ctime_ns": info.st_ctime_ns,
                    }
                )
        after = os.fstat(fd)
        if (before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise FilesystemStorageError("directory_changed")
        entries.sort(key=lambda item: item["name"])
        digest = hashlib.sha256(
            json.dumps([identity, entries], sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()
        if time.monotonic() > deadline:
            raise FilesystemStorageError("directory_budget_exceeded")
        return {"identity": identity, "digest": digest, "entries": entries}


def browse_directory(spec, path, *, expected_anchor, limit, after=None, snapshot=None):
    with location_directory(spec, path, expected_anchor=expected_anchor) as (fd, _):
        entries, raw, checked = [], [], 0
        deadline = time.monotonic() + 2
        with closing_registered(os.scandir(fd)) as iterator:
            for entry in iterator:
                try:
                    entry.name.encode("utf-8", errors="strict")
                except UnicodeError:
                    raise FilesystemStorageError("unsupported_path_encoding") from None
                checked += 1
                if checked > MAX_ENTRIES:
                    raise FilesystemStorageError("directory_too_large")
                if time.monotonic() > deadline:
                    raise FilesystemStorageError("directory_budget_exceeded")
                info = entry.stat(follow_symlinks=False)
                raw.append([entry.name, info.st_ino, info.st_mode, info.st_mtime_ns])
                if stat.S_ISDIR(info.st_mode):
                    entries.append(entry.name)
        digest = hashlib.sha256(
            json.dumps([_identity(fd), sorted(raw)], ensure_ascii=True).encode()
        ).hexdigest()
        if snapshot is not None and snapshot != digest:
            raise FilesystemStorageError("directory_changed")
        names = [name for name in sorted(entries) if after is None or name > after]
        return names[:limit], len(names) > limit, digest


def probe_directory(spec, sample_file=None, *, expected_anchor=None):
    capabilities = {
        "list": "unknown",
        "read": "unknown",
        "range": "unknown",
        "write": "unsupported",
        "delete": "unsupported",
        "presign": "unsupported",
    }
    anchor = None
    try:
        with location_directory(spec, expected_anchor=expected_anchor) as (
            fd,
            anchor,
        ):
            # Enumeration is independently bounded; reading is never inferred.
            browse_directory(spec, "", expected_anchor=anchor, limit=1)
            capabilities["list"] = "verified"
            if sample_file is not None:
                sample = relative_path(sample_file)
                if not sample:
                    raise FilesystemStorageError("sample_file_required")
                file_fd = _open_beneath(fd, sample, directory=False)
                try:
                    if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                        raise FilesystemStorageError("not_a_regular_file")
                    os.read(file_fd, 16)
                    capabilities["read"] = "verified"
                    os.pread(file_fd, 16, 1)
                    capabilities["range"] = "verified"
                finally:
                    _close_descriptors([file_fd])
        return {
            "ok": True,
            "code": "ok",
            "capabilities": capabilities,
            "anchor": anchor,
            "host_id": host_identity(),
        }
    except FilesystemStorageError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise
        return {
            "ok": False,
            "code": exc.code,
            "capabilities": capabilities,
            "anchor": anchor,
        }
