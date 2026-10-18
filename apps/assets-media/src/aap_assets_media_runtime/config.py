"""Explicit configuration for the standalone media execution process."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError

from aap_assets_media.contract import BATCH_SIZE, ClaimRequest, ClaimGarbageRequest

_BUCKET_ADAPTER = TypeAdapter(str)
_BUCKET_PATTERN = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)


class ConfigurationError(ValueError):
    """Required media worker configuration is absent or unsafe."""


@dataclass(frozen=True, slots=True)
class MediaExecutionSettings:
    """Execution values frozen by the media protocol and existing API defaults."""

    lease_seconds: int = ClaimRequest.model_fields["lease_seconds"].default
    frame_ttl_days: int = 14
    chunk_ttl_days: int = 30
    timetable_page_size: int = BATCH_SIZE
    garbage_lease_seconds: int = ClaimGarbageRequest.model_fields[
        "lease_seconds"
    ].default
    garbage_batch_size: int = ClaimGarbageRequest.model_fields["batch_size"].default

    def __post_init__(self) -> None:
        if not 30 <= self.lease_seconds <= 3600:
            raise ConfigurationError("media lease must be between 30 and 3600 seconds")
        if not 1 <= self.frame_ttl_days <= 3650:
            raise ConfigurationError("frame TTL must be between 1 and 3650 days")
        if not 1 <= self.chunk_ttl_days <= 3650:
            raise ConfigurationError("chunk TTL must be between 1 and 3650 days")
        if not 1 <= self.timetable_page_size <= BATCH_SIZE:
            raise ConfigurationError(
                "timetable page size is outside the protocol limit"
            )
        if not 30 <= self.garbage_lease_seconds <= 3600:
            raise ConfigurationError(
                "garbage lease must be between 30 and 3600 seconds"
            )
        if not 1 <= self.garbage_batch_size <= 100:
            raise ConfigurationError("garbage batch size is outside the protocol limit")

    @property
    def heartbeat_interval_seconds(self) -> float:
        return self.lease_seconds / 3


@dataclass(frozen=True, slots=True)
class MediaStorageConfig:
    endpoint: str
    public_endpoint: str
    region: str
    datasets_bucket: str
    cache_bucket: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    connect_timeout: float = 10.0
    read_timeout: float = 120.0
    max_attempts: int = 2


@dataclass(frozen=True, slots=True)
class MediaWorkerConfig:
    database_url: str = field(repr=False)
    storage: MediaStorageConfig
    poll_seconds: float = 5.0
    execution: MediaExecutionSettings = field(default_factory=MediaExecutionSettings)


def _secret(environment: Mapping[str, str], name: str) -> str:
    direct = environment.get(name)
    file_path = environment.get(f"{name}_FILE")
    if direct is not None and file_path is not None:
        raise ConfigurationError(f"Set only one of {name} and {name}_FILE")
    if direct is not None:
        value = direct
    elif file_path is not None:
        if not file_path.strip():
            raise ConfigurationError(f"Configured secret file {name}_FILE is empty")
        try:
            value = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(
                f"Cannot read configured secret {name}_FILE"
            ) from exc
    else:
        raise ConfigurationError(f"Missing required setting {name}")
    if not value or not value.strip():
        raise ConfigurationError(f"Configured secret {name} is empty")
    return value


def _database_url(environment: Mapping[str, str]) -> str:
    value = _secret(environment, "ASSETS_MEDIA_WORKER_DATABASE_URL")
    try:
        parsed = urlsplit(value)
        parsed.port
        valid = (
            parsed.scheme in {"postgres", "postgresql"}
            and bool(parsed.hostname)
            and parsed.username is not None
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError(
            "ASSETS_MEDIA_WORKER_DATABASE_URL must be a PostgreSQL URL"
        )
    return value


def _origin(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
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
        raise ConfigurationError(f"{name} must be an HTTP(S) origin")
    return value.rstrip("/")


def _bucket(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    try:
        _BUCKET_ADAPTER.validate_python(value, strict=True)
    except ValidationError as exc:
        raise ConfigurationError(f"{name} is invalid") from exc
    if (
        not 3 <= len(value) <= 63
        or not _BUCKET_PATTERN.fullmatch(value)
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", value)
    ):
        raise ConfigurationError(f"{name} is invalid")
    return value


def _positive_float(
    environment: Mapping[str, str], name: str, default: str, *, maximum: float
) -> float:
    try:
        value = float(environment.get(name, default))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(value) or not 0.1 <= value <= maximum:
        raise ConfigurationError(f"{name} must be between 0.1 and {maximum:g}")
    return value


def _positive_int(
    environment: Mapping[str, str],
    name: str,
    default: str,
    *,
    maximum: int,
    minimum: int = 1,
) -> int:
    try:
        value = int(environment.get(name, default))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def load_storage_config(
    environment: Mapping[str, str] | None = None,
) -> MediaStorageConfig:
    """Load fixed-bucket credentials without requiring a worker DSN or token."""
    values = os.environ if environment is None else environment
    region = values.get("ASSETS_MEDIA_S3_REGION", "us-east-1").strip()
    if not region:
        raise ConfigurationError("ASSETS_MEDIA_S3_REGION must not be empty")
    return MediaStorageConfig(
        endpoint=_origin(values, "ASSETS_MEDIA_S3_ENDPOINT"),
        public_endpoint=_origin(values, "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT"),
        region=region,
        datasets_bucket=_bucket(values, "ASSETS_MEDIA_DATASETS_BUCKET"),
        cache_bucket=_bucket(values, "ASSETS_MEDIA_CACHE_BUCKET"),
        access_key=_secret(values, "ASSETS_MEDIA_S3_ACCESS_KEY"),
        secret_key=_secret(values, "ASSETS_MEDIA_S3_SECRET_KEY"),
        connect_timeout=_positive_float(
            values, "ASSETS_MEDIA_S3_CONNECT_TIMEOUT_SECONDS", "10", maximum=3600
        ),
        read_timeout=_positive_float(
            values, "ASSETS_MEDIA_S3_READ_TIMEOUT_SECONDS", "120", maximum=3600
        ),
        max_attempts=_positive_int(
            values, "ASSETS_MEDIA_S3_MAX_ATTEMPTS", "2", maximum=10
        ),
    )


def load_config(environment: Mapping[str, str] | None = None) -> MediaWorkerConfig:
    """Load the worker login and explicit storage configuration."""
    values = os.environ if environment is None else environment
    return MediaWorkerConfig(
        database_url=_database_url(values),
        storage=load_storage_config(values),
        poll_seconds=_positive_float(
            values, "ASSETS_MEDIA_POLL_SECONDS", "5", maximum=3600
        ),
        execution=MediaExecutionSettings(
            lease_seconds=_positive_int(
                values,
                "ASSETS_MEDIA_LEASE_SECONDS",
                "1200",
                minimum=30,
                maximum=3600,
            ),
            frame_ttl_days=_positive_int(
                values, "ASSETS_MEDIA_FRAME_TTL_DAYS", "14", maximum=3650
            ),
            chunk_ttl_days=_positive_int(
                values, "ASSETS_MEDIA_CHUNK_TTL_DAYS", "30", maximum=3650
            ),
        ),
    )
