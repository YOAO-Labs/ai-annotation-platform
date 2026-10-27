from __future__ import annotations

from datetime import datetime, timezone
import io
import posixpath
import stat
from typing import BinaryIO, Iterable, Iterator

from app.services.storage_access import close_registered
from app.services.sources.base import (
    SourceAdapter,
    SourceObject,
    SourcePathError,
    matches_include_globs,
)


def _has_traversal(path: str | None) -> bool:
    return any(part == ".." for part in (path or "").replace("\\", "/").split("/"))


def _clean_remote(path: str | None) -> str:
    raw = (path or ".").replace("\\", "/").strip() or "."
    return posixpath.normpath(raw)


def _under_base(base_path: str, path: str) -> bool:
    base = _clean_remote(base_path)
    candidate = _clean_remote(path)
    if base in {"", "."}:
        # base 未设具体值（默认家目录）时，绝对路径会逃出预期子树，等同未限定——
        # 一并拒绝绝对路径与 '..'，迫使使用者配置具体 base_path 后才能用绝对 source_path。
        return (
            not candidate.startswith("/")
            and not candidate.startswith("../")
            and candidate != ".."
        )
    return candidate == base or candidate.startswith(f"{base.rstrip('/')}/")


def _join_under_base(base_path: str, path: str | None) -> str:
    if _has_traversal(path):
        raise SourcePathError("source_path 不能包含 '..'")

    base = _clean_remote(base_path)
    raw = (path or "").replace("\\", "/").strip()
    if raw in {"", "."}:
        return base

    requested = _clean_remote(raw)
    if requested.startswith("/"):
        candidate = requested
    elif _under_base(base, requested):
        candidate = requested
    else:
        candidate = posixpath.normpath(posixpath.join(base, requested))

    if not _under_base(base, candidate):
        raise SourcePathError("source_path 必须位于连接器 base_path 下")
    return candidate


def validate_sftp_source_path(config: dict, path: str | None) -> None:
    _join_under_base(str((config or {}).get("base_path") or "."), path)


def load_sftp_private_key(secret: dict):
    private_key = secret.get("private_key")
    if not private_key:
        return None

    import paramiko

    password = secret.get("passphrase")
    for key_cls in (
        paramiko.RSAKey,
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
    ):
        try:
            return key_cls.from_private_key(io.StringIO(private_key), password=password)
        except Exception:
            continue
    raise ValueError("无法解析 SFTP private_key")


class SftpSource(SourceAdapter):
    def __init__(self, config: dict, secret: dict) -> None:
        import paramiko

        self.base_path = _clean_remote(str(config.get("base_path") or "."))
        self._client = paramiko.SSHClient()
        try:
            self._client.load_system_host_keys()
            self._client.set_missing_host_key_policy(paramiko.RejectPolicy())
            pkey = load_sftp_private_key(secret)
            self._client.connect(
                hostname=str(config["host"]),
                port=int(config.get("port", 22)),
                username=str(config["username"]),
                password=secret.get("password") if pkey is None else None,
                pkey=pkey,
                timeout=10,
                allow_agent=False,
                look_for_keys=False,
            )
            self._sftp = self._client.open_sftp()
        except BaseException:
            close_registered(self._client)
            raise

    def _rel_from_path(self, path: str) -> str:
        if self.base_path in {"", "."}:
            return _clean_remote(path)
        return posixpath.relpath(_clean_remote(path), self.base_path)

    def _path_for_relpath(self, relpath: str) -> str:
        return _join_under_base(self.base_path, relpath)

    def list(
        self,
        path: str,
        recursive: bool,
        include_globs: Iterable[str] | None = None,
    ) -> Iterator[SourceObject]:
        root = _join_under_base(self.base_path, path)

        def walk(dir_path: str) -> Iterator[SourceObject]:
            for entry in self._sftp.listdir_attr(dir_path):
                name = entry.filename
                if name in {".", ".."}:
                    continue
                full_path = posixpath.normpath(posixpath.join(dir_path, name))
                mode = entry.st_mode or 0
                if stat.S_ISDIR(mode):
                    if recursive:
                        yield from walk(full_path)
                    continue
                relpath = self._rel_from_path(full_path)
                if not relpath or not matches_include_globs(relpath, include_globs):
                    continue
                mtime = (
                    datetime.fromtimestamp(entry.st_mtime, tz=timezone.utc)
                    if entry.st_mtime
                    else None
                )
                yield SourceObject(
                    relpath=relpath,
                    size=int(entry.st_size or 0),
                    mtime=mtime,
                    etag=None,
                )

        yield from walk(root)

    def open(self, relpath: str) -> BinaryIO:
        return self._sftp.open(self._path_for_relpath(relpath), "rb")

    def close(self) -> None:
        try:
            close_registered(self._sftp)
        finally:
            close_registered(self._client)
