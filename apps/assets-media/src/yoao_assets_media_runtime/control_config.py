"""Configuration for the independent, function-only media control process."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import os
from typing import Mapping
from urllib.parse import urlsplit

from .config import (
    ConfigurationError,
    MediaStorageConfig,
    _positive_float,
    _positive_int,
    _secret,
    load_storage_config,
)


@dataclass(frozen=True, slots=True)
class MediaControlConfig:
    database_url: str = field(repr=False)
    token: str = field(repr=False)
    storage: MediaStorageConfig
    host: str = "0.0.0.0"
    port: int = 8768
    pool_min_size: int = 1
    pool_max_size: int = 8
    pool_timeout_seconds: float = 2
    sql_timeout_seconds: float = 5


def load_control_config(
    environment: Mapping[str, str] | None = None,
) -> MediaControlConfig:
    values = dict(os.environ if environment is None else environment)
    database_url = _secret(values, "ASSETS_MEDIA_CONTROL_DATABASE_URL")
    try:
        parsed = urlsplit(database_url)
        parsed.port
        valid = (
            parsed.scheme in {"postgres", "postgresql"}
            and bool(parsed.hostname)
            and parsed.username is not None
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError(
            "ASSETS_MEDIA_CONTROL_DATABASE_URL must be a PostgreSQL URL"
        )
    token = _secret(values, "ASSETS_MEDIA_TOKEN")
    if len(token) < 32 or not token.isascii() or any(char.isspace() for char in token):
        raise ConfigurationError(
            "ASSETS_MEDIA_TOKEN must be 32+ ASCII non-whitespace characters"
        )
    host = values.get("ASSETS_MEDIA_HOST", "0.0.0.0").strip()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        raise ConfigurationError("ASSETS_MEDIA_HOST must be an IP address") from None
    minimum = _positive_int(values, "ASSETS_MEDIA_POOL_MIN_SIZE", "1", maximum=32)
    maximum = _positive_int(values, "ASSETS_MEDIA_POOL_MAX_SIZE", "8", maximum=64)
    if minimum > maximum:
        raise ConfigurationError("ASSETS_MEDIA_POOL_MIN_SIZE exceeds POOL_MAX_SIZE")
    # HTTP control performs short HEAD/signing operations, never downloads.
    values.setdefault("ASSETS_MEDIA_S3_CONNECT_TIMEOUT_SECONDS", "2")
    values.setdefault("ASSETS_MEDIA_S3_READ_TIMEOUT_SECONDS", "10")
    return MediaControlConfig(
        database_url=database_url,
        token=token,
        storage=load_storage_config(values),
        host=host,
        port=_positive_int(values, "ASSETS_MEDIA_PORT", "8768", maximum=65535),
        pool_min_size=minimum,
        pool_max_size=maximum,
        pool_timeout_seconds=_positive_float(
            values, "ASSETS_MEDIA_POOL_TIMEOUT_SECONDS", "2", maximum=30
        ),
        sql_timeout_seconds=_positive_float(
            values, "ASSETS_MEDIA_SQL_TIMEOUT_SECONDS", "5", maximum=60
        ),
    )
