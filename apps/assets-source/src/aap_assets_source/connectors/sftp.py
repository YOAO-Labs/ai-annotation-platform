"""Read-only SFTP connector with pinned host keys and rooted paths."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import posixpath
import stat
from typing import Any, BinaryIO, Iterable, Iterator

from .base import (
    SourceAdapter,
    SourceChangedError,
    SourceNotFoundError,
    SourceObject,
    SourcePathError,
    matches_include_globs,
    normalize_relpath,
)


def _has_traversal(path: str | None) -> bool:
    return any(part == ".." for part in (path or "").replace("\\", "/").split("/"))


def _has_control(path: str | None) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in (path or ""))


def _clean_remote(path: str | None) -> str:
    raw = (path or ".").replace("\\", "/").strip() or "."
    return posixpath.normpath(raw)


def _under_base(base_path: str, path: str) -> bool:
    base = _clean_remote(base_path)
    candidate = _clean_remote(path)
    if base in {"", "."}:
        return (
            not candidate.startswith("/")
            and not candidate.startswith("../")
            and candidate != ".."
        )
    return candidate == base or candidate.startswith(f"{base.rstrip('/')}/")


def _join_under_base(base_path: str, path: str | None) -> str:
    if _has_traversal(path) or _has_control(path):
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
    for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return key_cls.from_private_key(io.StringIO(private_key), password=password)
        except Exception:
            continue
    raise ValueError("无法解析 SFTP private_key")


def _identity(attrs: Any) -> tuple[int, int, int, int | None, int | None]:
    def optional_int(name: str) -> int | None:
        value = getattr(attrs, name, None)
        return int(value) if value is not None else None

    size = optional_int("st_size")
    mtime = optional_int("st_mtime")
    mode = optional_int("st_mode")
    return (
        size if size is not None else -1,
        mtime if mtime is not None else 0,
        mode if mode is not None else 0,
        optional_int("st_uid"),
        optional_int("st_gid"),
    )


def _encode_identity(identity: tuple[int, int, int, int | None, int | None]) -> str:
    return ":".join("" if value is None else str(value) for value in identity)


def _decode_identity(value: str) -> tuple[int, int, int, int | None, int | None]:
    parts = value.split(":")
    if len(parts) != 5:
        raise SourceChangedError("SFTP listing identity is malformed")
    try:
        parsed = tuple(None if item == "" else int(item) for item in parts)
    except ValueError as exc:
        raise SourceChangedError("SFTP listing identity is malformed") from exc
    if any(item is None for item in parsed[:3]):
        raise SourceChangedError("SFTP listing identity is malformed")
    return parsed  # type: ignore[return-value]


class _CheckedSftpFile:
    def __init__(self, stream: Any, sftp: Any, path: str, identity: tuple) -> None:
        self._stream = stream
        self._sftp = sftp
        self._path = path
        self._identity = identity
        self._checked = False

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        if not chunk and not self._checked:
            self.verify_unchanged()
        return chunk

    def verify_unchanged(self) -> None:
        try:
            current = self._sftp.stat(self._path)
        except Exception as exc:
            raise SourceChangedError("SFTP source changed during read") from exc
        if _identity(current) != self._identity:
            raise SourceChangedError("SFTP source changed during read")
        self._checked = True

    def close(self) -> None:
        self._stream.close()

    @property
    def closed(self) -> bool:
        return bool(getattr(self._stream, "closed", False))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class SftpSource(SourceAdapter):
    def __init__(
        self,
        config: dict,
        secret: dict,
        *,
        known_hosts_file: str | None = None,
        timeout_seconds: float = 10,
        require_listing_identity: bool = False,
        client: Any | None = None,
    ) -> None:
        import paramiko

        self.base_path = _clean_remote(str(config.get("base_path") or "."))
        self._client = client if client is not None else paramiko.SSHClient()
        self._timeout_seconds = timeout_seconds
        self._require_listing_identity = require_listing_identity
        self._sftp: Any | None = None
        try:
            if known_hosts_file:
                self._client.load_host_keys(known_hosts_file)
            else:
                self._client.load_system_host_keys()
            self._client.set_missing_host_key_policy(paramiko.RejectPolicy())
            pkey = load_sftp_private_key(secret)
            self._client.connect(
                hostname=str(config["host"]),
                port=int(config.get("port", 22)),
                username=str(config["username"]),
                password=secret.get("password") if pkey is None else None,
                pkey=pkey,
                timeout=timeout_seconds,
                banner_timeout=timeout_seconds,
                auth_timeout=timeout_seconds,
                allow_agent=False,
                look_for_keys=False,
            )
            self._sftp = self._client.open_sftp()
            channel = self._sftp.get_channel()
            if channel is not None:
                channel.settimeout(timeout_seconds)

            configured_root = self.base_path
            root_candidate = (
                configured_root
                if configured_root.startswith("/")
                else _clean_remote(self._sftp.normalize(configured_root))
            )
            self._root_realpath = self._canonical_path(root_candidate, root_init=True)
            root_attrs = self._sftp.lstat(self._root_realpath)
            if not stat.S_ISDIR(root_attrs.st_mode or 0):
                raise SourcePathError("SFTP base_path 必须是目录")
        except Exception:
            if self._sftp is not None:
                try:
                    self._sftp.close()
                except Exception:
                    pass
            try:
                self._client.close()
            except Exception:
                pass
            raise

    def _canonical_path(self, path: str, *, root_init: bool = False) -> str:
        candidate = _clean_remote(path)
        if not candidate.startswith("/"):
            if root_init:
                candidate = _clean_remote(self._sftp.normalize(candidate))
            else:
                candidate = posixpath.normpath(
                    posixpath.join(self._root_realpath, candidate)
                )
        try:
            real = _clean_remote(self._sftp.normalize(candidate))
        except Exception as exc:
            raise SourcePathError("无法确认 SFTP 路径真实位置") from exc
        if not root_init and not self._inside_root(real):
            raise SourcePathError("SFTP 路径逃出连接器 base_path")
        if not root_init and real != candidate:
            raise SourcePathError("SFTP 不允许导入符号链接路径")
        return real

    def _inside_root(self, path: str) -> bool:
        root = self._root_realpath.rstrip("/") or "/"
        candidate = _clean_remote(path)
        return candidate == root or candidate.startswith(f"{root.rstrip('/')}/")

    def _relative_path(self, path: str) -> str:
        if not self._inside_root(path):
            raise SourcePathError("SFTP 路径逃出连接器 base_path")
        relpath = posixpath.relpath(path, self._root_realpath)
        return "" if relpath == "." else relpath

    def _candidate_for_source_path(self, path: str | None) -> str:
        scoped = _join_under_base(self.base_path, path)
        if scoped.startswith("/"):
            candidate = scoped
        else:
            configured = _clean_remote(self.base_path)
            relative = (
                scoped[len(configured) :].lstrip("/")
                if configured not in {"", "."}
                and (scoped == configured or scoped.startswith(f"{configured}/"))
                else scoped
            )
            candidate = posixpath.normpath(
                posixpath.join(self._root_realpath, relative)
            )
        return self._canonical_path(candidate)

    def _safe_file_path(self, relpath: str) -> str:
        normalized = _clean_remote(relpath)
        if _has_traversal(relpath) or _has_control(relpath):
            raise SourcePathError("relpath contains an invalid component")
        candidate = posixpath.normpath(posixpath.join(self._root_realpath, normalized))
        return self._canonical_path(candidate)

    def list(
        self,
        path: str,
        recursive: bool,
        include_globs: Iterable[str] | None = None,
    ) -> Iterator[SourceObject]:
        root = self._candidate_for_source_path(path)
        root_attrs = self._sftp.lstat(root)
        if stat.S_ISLNK(root_attrs.st_mode or 0):
            raise SourcePathError("source_path 不能是符号链接")
        if not stat.S_ISDIR(root_attrs.st_mode or 0):
            raise SourcePathError("source_path 必须是目录")

        def walk(directory: str) -> Iterator[SourceObject]:
            safe_dir = self._canonical_path(directory)
            for entry in self._sftp.listdir_iter(safe_dir):
                name = str(entry.filename)
                if name in {".", ".."} or "/" in name or "\\" in name:
                    continue
                full_path = posixpath.normpath(posixpath.join(safe_dir, name))
                mode = int(entry.st_mode or 0)
                if stat.S_ISDIR(mode):
                    safe_child = self._canonical_path(full_path)
                    if recursive:
                        yield from walk(safe_child)
                    continue
                relpath = self._relative_path(full_path)
                relpath = normalize_relpath(relpath)
                if not relpath or not matches_include_globs(relpath, include_globs):
                    continue
                identity = _identity(entry)
                mtime = (
                    datetime.fromtimestamp(identity[1], tz=timezone.utc)
                    if identity[1]
                    else None
                )
                # Symlink and special-file entries stay visible but cannot be opened.
                yield SourceObject(
                    relpath=relpath,
                    size=max(0, identity[0]),
                    mtime=mtime,
                    etag=_encode_identity(identity),
                )

        yield from walk(root)

    def open(self, relpath: str, *, etag: str | None = None) -> BinaryIO:
        if self._require_listing_identity and etag is None:
            raise SourceChangedError("SFTP listing identity is missing")
        path = self._safe_file_path(relpath)
        try:
            attrs = self._sftp.lstat(path)
        except Exception as exc:
            if _remote_not_found(exc):
                raise SourceNotFoundError(
                    "SFTP source object no longer exists"
                ) from exc
            raise SourcePathError("SFTP source is unavailable") from exc
        mode = int(attrs.st_mode or 0)
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise SourcePathError("SFTP source must be a regular non-symlink file")
        before = _identity(attrs)
        if etag is not None and _decode_identity(etag) != before:
            raise SourceChangedError("SFTP source changed after enumeration")
        stream = self._sftp.open(path, "rb")
        opened = stream.stat()
        if _identity(opened) != before:
            stream.close()
            raise SourceChangedError("SFTP source changed while opening")
        return _CheckedSftpFile(stream, self._sftp, path, before)

    def close(self) -> None:
        try:
            if self._sftp is not None:
                self._sftp.close()
        finally:
            self._client.close()


def _remote_not_found(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    return getattr(exc, "errno", None) == 2
