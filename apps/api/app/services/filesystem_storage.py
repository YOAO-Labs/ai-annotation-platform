"""Read-only controlled Linux roots. No path resolution falls back to open()."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import hashlib
import json
import os
import platform
import stat
import time

from app.config import settings

MAX_ENTRIES = 5000
_BENEATH = 0x08
_NO_SYMLINKS = 0x04
_NO_XDEV = 0x01


class FilesystemStorageError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def relative_path(value: str) -> str:
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


@contextmanager
def location_directory(config, path="", *, expected_anchor=None):
    path = relative_path(path)
    base = relative_path(config.get("relative_path", ""))
    root = settings.storage_filesystem_roots.get(config["root_handle"])
    if root is None:
        raise FilesystemStorageError("root_not_configured")
    handles = []
    try:
        handles.append(os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        root_fd = _open_beneath(
            handles[0], root.lstrip("/"), directory=True, no_xdev=False
        )
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
    except OSError:
        raise FilesystemStorageError("filesystem_unavailable") from None
    finally:
        for handle in reversed(handles):
            os.close(handle)


def capture_anchor(config):
    with location_directory(config) as (_, anchor):
        return anchor


def browse_directory(
    config, path, *, expected_anchor, limit, after=None, snapshot=None
):
    with location_directory(config, path, expected_anchor=expected_anchor) as (fd, _):
        entries, raw, checked = [], [], 0
        deadline = time.monotonic() + 2
        with os.scandir(fd) as iterator:
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


def probe_directory(config, sample_file=None, *, expected_anchor=None):
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
        with location_directory(config, expected_anchor=expected_anchor) as (
            fd,
            anchor,
        ):
            # Enumeration is independently bounded; reading is never inferred.
            browse_directory(config, "", expected_anchor=anchor, limit=1)
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
                    os.close(file_fd)
        return {
            "ok": True,
            "code": "ok",
            "capabilities": capabilities,
            "anchor": anchor,
        }
    except FilesystemStorageError as exc:
        return {
            "ok": False,
            "code": exc.code,
            "capabilities": capabilities,
            "anchor": anchor,
        }
