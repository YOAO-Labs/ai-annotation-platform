"""Portable path and deployment policy values for registered references."""

import hashlib
import json
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


class ReferenceError(ValueError):
    def __init__(
        self, code: str, status_code: int = 409, *, storage_code=None, unsettled=False
    ):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.storage_code = storage_code
        self.unsettled = unsettled


class SftpReferenceRoot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    host: str = Field(min_length=1, max_length=253, pattern=r"^[a-zA-Z0-9_.:-]+$")
    port: int = Field(default=22, strict=True, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    host_key: str = Field(min_length=20, max_length=4096)

    @field_validator("host_key")
    @classmethod
    def _validate_host_key(cls, value):
        from paramiko.hostkeys import HostKeyEntry

        try:
            entry = HostKeyEntry.from_line("qualified " + value)
            if entry is None or entry.key is None or len(value.split()) != 2:
                raise ValueError()
        except Exception:
            raise ValueError("Invalid pinned SFTP public host key") from None
        return value


def validate_sftp_roots(value):
    targets = [(root.host, root.port, root.username) for root in value.values()]
    if len(targets) != len(set(targets)) or any(
        not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", handle) for handle in value
    ):
        raise ValueError("Invalid or ambiguous SFTP reference roots")
    return value


def parse_sftp_roots(raw: str) -> dict[str, dict]:
    roots = validate_sftp_roots(
        TypeAdapter(dict[str, SftpReferenceRoot]).validate_json(raw)
    )
    return {handle: root.model_dump() for handle, root in roots.items()}


def qualified_sftp_root(config: dict, roots: dict[str, dict]) -> tuple[str, dict]:
    if config.get("base_path") == "/":
        target = (config.get("host"), config.get("port", 22), config.get("username"))
        for handle, root in roots.items():
            if target == (root["host"], root["port"], root["username"]):
                return handle, root
    raise ReferenceError("sftp_root_not_qualified")


def sftp_anchor(handle: str, root: dict) -> dict:
    return {
        "kind": "sftp",
        "root_handle": handle,
        "qualification": hashlib.sha256(
            json.dumps(root, sort_keys=True).encode()
        ).hexdigest(),
    }


def sftp_relative_path(value: str) -> str:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ReferenceError("unsupported_path_encoding") from None
    parts = value.split("/")
    if (
        len(value) > 1000
        or len(parts) > 32
        or "\\" in value
        or "\x00" in value
        or (value and any(part in {"", ".", ".."} for part in parts))
    ):
        raise ReferenceError("invalid_relative_path")
    return value


def s3_directory_prefix(base: str, relative: str = "") -> str:
    if any(not isinstance(part, str) or "\x00" in part for part in (base, relative)):
        raise ReferenceError("invalid_s3_prefix")
    return (base + ("/" if base and not base.endswith("/") else "")) + (
        relative + ("/" if relative and not relative.endswith("/") else "")
    )


def s3_endpoint(config: dict) -> str:
    endpoint = config["endpoint"]
    if "://" not in endpoint:
        endpoint = ("https://" if config.get("use_ssl") else "http://") + endpoint
    try:
        parts = urlsplit(endpoint)
        parts.port
        if (
            parts.scheme in {"http", "https"}
            and parts.hostname
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
            and parts.path in {"", "/"}
            and not any(ord(char) < 33 or ord(char) == 127 for char in endpoint)
        ):
            return endpoint.rstrip("/")
    except ValueError:
        pass
    raise ReferenceError("s3_endpoint_not_supported")


def s3_anchor(config: dict) -> dict:
    return {
        "kind": "s3",
        "endpoint": s3_endpoint(config),
        "bucket": config["bucket"],
        "prefix": s3_directory_prefix(config.get("base_prefix", "")),
    }
