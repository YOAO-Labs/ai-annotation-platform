"""Local, private credential persistence; never a network enrollment protocol."""

from __future__ import annotations

import os
import re
import secrets
import stat
from pathlib import Path

TOKEN_FILENAME = "token"


class CredentialFileError(ValueError):
    """The configured private store is missing, unsafe, or malformed."""


def _open_directory(directory: Path, *, create: bool) -> int:
    directory = directory.absolute()
    if directory.resolve() != directory:
        raise CredentialFileError("Credential directory must not use symlinks")
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if create and os.geteuid() not in {0, info.st_uid}:
            raise CredentialFileError("Credential directory must be private and owned")
        if create:
            # Tighten checkout-default permissions before publishing any secret.
            os.fchmod(fd, 0o700)
        elif info.st_mode & 0o077:
            raise CredentialFileError("Credential directory must be private and owned")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read(fd: int) -> str:
    token_fd = os.open(
        TOKEN_FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
    )
    with os.fdopen(token_fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.fstat(fd).st_uid
        ):
            raise CredentialFileError("Credential file must be private and regular")
        raw = stream.read(66)
    if re.fullmatch(rb"[0-9a-f]{64}\n?", raw) is None:
        raise CredentialFileError("Credential file is malformed")
    return raw.decode("ascii").strip()


def read_credential(directory: Path) -> str:
    """Read a private file, allowing a read-only container with a different UID."""
    fd = _open_directory(directory, create=False)
    try:
        return _read(fd)
    finally:
        os.close(fd)


def ensure_credential(directory: Path) -> str:
    """Publish once atomically. Concurrent initializers reuse the winning file."""
    fd = _open_directory(directory, create=True)
    temp = f".credential-{secrets.token_hex(16)}"
    try:
        try:
            return _read(fd)
        except FileNotFoundError:
            pass
        token_fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=fd,
        )
        with os.fdopen(token_fd, "wb") as stream:
            if os.geteuid() == 0:
                owner = os.fstat(fd)
                os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
            stream.write((secrets.token_hex(32) + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(
                temp,
                TOKEN_FILENAME,
                src_dir_fd=fd,
                dst_dir_fd=fd,
                follow_symlinks=False,
            )
            os.fsync(fd)
        except FileExistsError:
            pass
        return _read(fd)
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)
