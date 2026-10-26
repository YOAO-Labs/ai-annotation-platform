"""Local operator configuration. Absolute roots never enter the wire contract."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from uuid import UUID

from aap_filesystem_read import RootSpec


class AgentError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def integer(value, low, high, code="invalid_config"):
    if type(value) is not int or not low <= value <= high:
        raise AgentError(code)
    return value


def private_directory(path):
    """Refuse shared or symlink state directories before creating local files."""
    path = Path(path)
    if not path.is_absolute():
        raise AgentError("absolute_state_directory_required")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or path.resolve() != path
    ):
        raise AgentError("private_state_directory_required")
    return path


def private_json(path, *, max_bytes=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > max_bytes
        ):
            raise AgentError("private_config_file_required")
        with os.fdopen(fd, "rb", closefd=False) as body:
            raw = body.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise AgentError("config_too_large")
        from aap_site_agent_contract import parse_json

        value = parse_json(raw, max_bytes=max_bytes)
        if not isinstance(value, dict):
            raise AgentError("invalid_config")
        return value
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Credentials:
    agent_id: str
    credential_revision: int
    token: str = field(repr=False)
    expires_at: int

    @classmethod
    def load(cls, path):
        value = private_json(path)
        if set(value) != {"agent_id", "credential_revision", "token", "expires_at"}:
            raise AgentError("invalid_credentials")
        try:
            identity = str(UUID(value["agent_id"]))
        except (ValueError, TypeError, AttributeError):
            raise AgentError("invalid_credentials") from None
        token = value["token"]
        if (
            not isinstance(token, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) is None
        ):
            raise AgentError("invalid_credentials")
        return cls(
            identity,
            integer(value["credential_revision"], 1, 2**31 - 1, "invalid_credentials"),
            token,
            integer(value["expires_at"], 1, 2**53 - 1, "invalid_credentials"),
        )


@dataclass(frozen=True)
class Config:
    center_url: str
    state_dir: Path
    credential_file: Path
    roots: dict
    max_pending: int = 16
    max_state_bytes: int = 256 * 1024 * 1024
    max_concurrency: int = 1
    bandwidth_bytes_per_second: int = 8 * 1024 * 1024
    ca_file: str | None = None

    @classmethod
    def load(cls, path):
        value = private_json(path)
        required = {"center_url", "state_dir", "credential_file", "roots"}
        allowed = required | {
            "max_pending",
            "max_state_bytes",
            "max_concurrency",
            "bandwidth_bytes_per_second",
            "ca_file",
        }
        if not required <= value.keys() or value.keys() - allowed:
            raise AgentError("invalid_config")
        origin = value["center_url"]
        try:
            url = urlsplit(origin)
            url.port
        except (ValueError, TypeError):
            raise AgentError("https_origin_required") from None
        if (
            not isinstance(origin, str)
            or url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or any(ord(char) <= 32 for char in origin)
        ):
            raise AgentError("https_origin_required")
        paths = [value["state_dir"], value["credential_file"]]
        if any(not isinstance(p, str) or not Path(p).is_absolute() for p in paths):
            raise AgentError("absolute_config_path_required")
        roots = value["roots"]
        if not isinstance(roots, dict) or not 1 <= len(roots) <= 32:
            raise AgentError("invalid_roots")
        specs = {}
        for handle, root in roots.items():
            if (
                not isinstance(handle, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", handle) is None
                or not isinstance(root, dict)
                or set(root) != {"path", "revision"}
                or not isinstance(root["path"], str)
                or not Path(root["path"]).is_absolute()
                or any(part in {".", ".."} for part in root["path"].split("/"))
            ):
                raise AgentError("invalid_roots")
            integer(root["revision"], 1, 2**31 - 1)
            specs[handle] = RootSpec(handle, root["path"], "", root["revision"])
            state = Path(value["state_dir"]).resolve()
            source = Path(root["path"]).resolve()
            if (
                state == source
                or state.is_relative_to(source)
                or source.is_relative_to(state)
            ):
                raise AgentError("state_overlaps_source")
        ca_file = value.get("ca_file")
        if ca_file is not None and (
            not isinstance(ca_file, str) or not Path(ca_file).is_absolute()
        ):
            raise AgentError("absolute_ca_path_required")
        return cls(
            origin.rstrip("/"),
            Path(value["state_dir"]),
            Path(value["credential_file"]),
            specs,
            integer(value.get("max_pending", 16), 1, 128),
            integer(
                value.get("max_state_bytes", 256 * 1024 * 1024),
                1024 * 1024,
                256 * 1024 * 1024,
            ),
            integer(value.get("max_concurrency", 1), 1, 4),
            integer(
                value.get("bandwidth_bytes_per_second", 8 * 1024 * 1024), 1024, 1024**3
            ),
            ca_file,
        )

    def public_limits(self):
        return {
            "max_concurrency": self.max_concurrency,
            "bandwidth_bytes_per_second": self.bandwidth_bytes_per_second,
            "max_bytes": min(64 * 1024 * 1024, self.max_state_bytes // 2),
        }
