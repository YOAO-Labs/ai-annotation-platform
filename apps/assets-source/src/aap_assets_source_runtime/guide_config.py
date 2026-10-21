"""Explicit, isolated object-storage settings for project guide files."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Mapping
from urllib.parse import unquote, urlsplit, urlunsplit

from .config import (
    ConfigurationError,
    _bucket,
    _http_origin,
    _positive_float,
    _required_secret,
)

_PUBLIC_URL = "ASSETS_SOURCE_GUIDE_S3_PUBLIC_URL"


@dataclass(frozen=True, slots=True)
class GuideStorageConfig:
    endpoint: str
    public_url: str
    region: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    bucket: str
    connect_timeout_seconds: float = 2.0
    read_timeout_seconds: float = 5.0


def _safe_path_prefix(path: str, name: str) -> str:
    decoded = unquote(path)
    if (
        any(ord(char) < 32 or ord(char) == 127 for char in decoded)
        or "\\" in decoded
        or decoded.startswith("//")
        or "//" in decoded[1:]
        or any(part in {".", ".."} for part in decoded.split("/"))
    ):
        raise ConfigurationError(f"{name} contains an unsafe path prefix")
    return path.rstrip("/")


def _public_url(environment: Mapping[str, str]) -> str:
    raw = environment.get(_PUBLIC_URL, "/minio")
    if not isinstance(raw, str):
        raise ConfigurationError(
            f"{_PUBLIC_URL} must be a same-origin path or HTTP(S) URL"
        )
    value = raw.strip()
    if (
        not value
        or "?" in value
        or "#" in value
        or "\\" in value
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ConfigurationError(
            f"{_PUBLIC_URL} must be a same-origin path or HTTP(S) URL"
        )

    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise ConfigurationError(
            f"{_PUBLIC_URL} must be a same-origin path or HTTP(S) URL"
        ) from exc

    if not parsed.scheme and not parsed.netloc and value.startswith("/"):
        if value.startswith("//") or parsed.query or parsed.fragment:
            raise ConfigurationError(
                f"{_PUBLIC_URL} must be a same-origin path or HTTP(S) URL"
            )
        return _safe_path_prefix(parsed.path, _PUBLIC_URL)

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127
            for char in parsed.hostname
        )
        or any(char in parsed.hostname for char in ("/", "\\", "%"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            f"{_PUBLIC_URL} must be a same-origin path or HTTP(S) URL"
        )
    path = _safe_path_prefix(parsed.path, _PUBLIC_URL)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def load_guide_storage_config(
    environment: Mapping[str, str] | None = None,
) -> GuideStorageConfig:
    """Load only explicitly scoped guide S3 settings; never use API/AWS defaults."""
    source = os.environ if environment is None else environment
    endpoint = _http_origin(source, "ASSETS_SOURCE_GUIDE_S3_ENDPOINT")
    region = source.get("ASSETS_SOURCE_GUIDE_S3_REGION", "us-east-1").strip()
    if not region:
        raise ConfigurationError("ASSETS_SOURCE_GUIDE_S3_REGION must not be empty")

    bucket = _bucket(source, "ASSETS_SOURCE_GUIDE_S3_BUCKET")

    return GuideStorageConfig(
        endpoint=endpoint,
        public_url=_public_url(source),
        region=region,
        access_key=_required_secret(source, "ASSETS_SOURCE_GUIDE_S3_ACCESS_KEY"),
        secret_key=_required_secret(source, "ASSETS_SOURCE_GUIDE_S3_SECRET_KEY"),
        bucket=bucket,
        connect_timeout_seconds=_positive_float(
            source,
            "ASSETS_SOURCE_GUIDE_S3_CONNECT_TIMEOUT_SECONDS",
            "2",
            maximum=30,
        ),
        read_timeout_seconds=_positive_float(
            source,
            "ASSETS_SOURCE_GUIDE_S3_READ_TIMEOUT_SECONDS",
            "5",
            maximum=120,
        ),
    )
