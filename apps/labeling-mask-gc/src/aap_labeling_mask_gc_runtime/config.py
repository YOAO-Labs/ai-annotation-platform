"""Bounded independent runtime configuration with secret-file support."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

MAX_SECRET_FILE_BYTES = 8192


class ConfigurationError(ValueError):
    """Required runtime settings are absent or malformed."""


def _secret(environment: Mapping[str, str], name: str) -> str:
    inline = environment.get(name)
    path = environment.get(f"{name}_FILE")
    inline_present = inline is not None and inline != ""
    file_present = path is not None and path != ""
    if inline_present and file_present:
        raise ConfigurationError(f"set only one of {name} and {name}_FILE")
    if file_present:
        try:
            with Path(path).open("rb") as handle:
                raw = handle.read(MAX_SECRET_FILE_BYTES + 1)
        except OSError:
            raise ConfigurationError(f"cannot read secret file for {name}") from None
        if len(raw) > MAX_SECRET_FILE_BYTES:
            raise ConfigurationError(f"secret file for {name} exceeds the limit")
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ConfigurationError(f"secret file for {name} must be UTF-8") from None
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    else:
        value = inline or ""
    if not value or not value.strip():
        raise ConfigurationError(f"missing or empty setting {name}")
    return value


def _service_token(value: str, name: str) -> str:
    if (
        len(value) < 32
        or not value.isascii()
        or any(character.isspace() for character in value)
    ):
        raise ConfigurationError(f"{name} must be a 32+ character ASCII token")
    return value


def _database_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        parsed.port
        valid = (
            parsed.scheme in {"postgres", "postgresql"}
            and bool(parsed.hostname)
            and parsed.username is not None
            and bool(parsed.path.strip("/"))
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError("MASK_GC_DATABASE_URL must be a PostgreSQL URL")
    return value


def _content_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError("MASK_GC_CONTENT_URL must be an HTTP(S) origin")
    return value.rstrip("/")


@dataclass(frozen=True)
class RuntimeConfig:
    database_url: str = field(repr=False)
    token: str = field(repr=False)
    content_url: str = field(repr=False)
    content_token: str = field(repr=False)
    host: str
    port: int
    poll_seconds: float


def load_config(environment: Mapping[str, str] | None = None) -> RuntimeConfig:
    values = os.environ if environment is None else environment
    database_url = _database_url(_secret(values, "MASK_GC_DATABASE_URL"))
    token = _service_token(_secret(values, "MASK_GC_TOKEN"), "MASK_GC_TOKEN")
    content_url = _content_origin(_secret(values, "MASK_GC_CONTENT_URL"))
    content_token = _service_token(
        _secret(values, "MASK_GC_CONTENT_TOKEN"), "MASK_GC_CONTENT_TOKEN"
    )

    host = values.get("MASK_GC_HOST", "0.0.0.0")
    if not host or any(character.isspace() for character in host):
        raise ConfigurationError("MASK_GC_HOST is invalid")
    raw_port = values.get("MASK_GC_PORT", "8771")
    try:
        port = int(raw_port)
    except ValueError:
        raise ConfigurationError("MASK_GC_PORT must be an integer") from None
    if not 1 <= port <= 65535:
        raise ConfigurationError("MASK_GC_PORT must be between 1 and 65535")

    raw_poll_seconds = values.get("MASK_GC_POLL_SECONDS", "5")
    try:
        poll_seconds = float(raw_poll_seconds)
    except ValueError:
        raise ConfigurationError("MASK_GC_POLL_SECONDS must be numeric") from None
    if not math.isfinite(poll_seconds) or not 0.1 <= poll_seconds <= 3600:
        raise ConfigurationError("MASK_GC_POLL_SECONDS must be 0.1-3600 seconds")

    return RuntimeConfig(
        database_url=database_url,
        token=token,
        content_url=content_url,
        content_token=content_token,
        host=host,
        port=port,
        poll_seconds=poll_seconds,
    )
