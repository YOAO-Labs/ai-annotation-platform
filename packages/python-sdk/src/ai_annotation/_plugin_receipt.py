"""Private, crash-safe local ownership receipt for an administrator installation.

The core Registry remains authoritative. This file holds only the local ownership
and recovery information needed to reconnect to that Registry entry.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ai_annotation.errors import AAPError


def _check_private(path: Path, *, directory: bool) -> None:
    st = path.lstat()
    expected = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
    if not expected or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise AAPError(f"Installation state must be private and owned by this user: {path}")


class ReceiptStore:
    """A single local receipt, serialized across concurrent operator commands."""

    def __init__(self, directory: Path):
        self.directory = directory.absolute()
        self.path = self.directory / "receipt.json"

    @contextmanager
    def locked(self, *, create: bool = False) -> Iterator[ReceiptStore]:
        if create:
            try:
                self.directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
        _check_private(self.directory, directory=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.directory / ".lock", flags, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise AAPError("Installation lock must be private and owned by this user")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield self
        finally:
            os.close(fd)

    def load(self) -> dict[str, Any] | None:
        try:
            _check_private(self.path, directory=False)
            with self.path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as exc:
            raise AAPError("Unable to read the private installation receipt") from exc
        if not isinstance(value, dict):
            raise AAPError("Invalid installation receipt")
        return value

    def save(self, value: dict[str, Any]) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            dir_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
