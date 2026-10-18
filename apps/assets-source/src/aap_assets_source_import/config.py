"""Explicit configuration for the dedicated Dataset import worker."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet

from aap_assets_source.connectors.host_guard import (
    ConnectorAllowlistInvalid,
    normalize_allowlist,
)


class ConfigurationError(ValueError):
    """An import-worker setting is missing, invalid, or unsafe."""


_BUCKET_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_IP_BUCKET_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){3}$")


def _required_secret(environment: Mapping[str, str], name: str) -> str:
    file_name = f"{name}_FILE"
    direct = environment.get(name)
    secret_file = environment.get(file_name)
    if direct is not None and secret_file is not None:
        raise ConfigurationError(f"Set only one of {name} and {file_name}")
    if direct is not None:
        value = direct
    elif secret_file is not None:
        try:
            value = Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(f"Cannot read {file_name}") from exc
    else:
        raise ConfigurationError(f"Missing required setting {name}")
    if not value or not value.strip():
        raise ConfigurationError(f"Configured secret {name} is empty")
    return value


def _postgres_dsn(environment: Mapping[str, str]) -> tuple[str, str]:
    raw = _required_secret(environment, "ASSETS_IMPORT_DATABASE_URL")
    try:
        parsed = urlsplit(raw)
        parsed.port
        valid = (
            parsed.scheme in {"postgres", "postgresql", "postgresql+asyncpg"}
            and bool(parsed.hostname)
            and parsed.username is not None
            and bool(parsed.path)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError("ASSETS_IMPORT_DATABASE_URL must be a PostgreSQL URL")
    driver_dsn = urlunsplit(
        (
            "postgresql" if parsed.scheme == "postgresql+asyncpg" else parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )
    return raw, driver_dsn


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
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError(f"{name} must be an HTTP(S) origin")
    return value.rstrip("/")


def _bucket(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if (
        not 3 <= len(value) <= 63
        or not _BUCKET_RE.fullmatch(value)
        or _IP_BUCKET_RE.fullmatch(value)
    ):
        raise ConfigurationError(f"{name} is invalid")
    return value


def _positive_int(
    environment: Mapping[str, str],
    name: str,
    default: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(environment.get(name, default))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _positive_float(
    environment: Mapping[str, str], name: str, default: str, *, maximum: float
) -> float:
    try:
        value = float(environment.get(name, default))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or not 0.1 <= value <= maximum:
        raise ConfigurationError(f"{name} must be between 0.1 and {maximum:g}")
    return value


def _host_allowlist(environment: Mapping[str, str]) -> list[str]:
    raw = environment.get("CONNECTOR_HOST_ALLOWLIST", "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                "CONNECTOR_HOST_ALLOWLIST is invalid JSON"
            ) from exc
        if not isinstance(entries, list) or any(
            not isinstance(item, str) for item in entries
        ):
            raise ConfigurationError("CONNECTOR_HOST_ALLOWLIST must be a string list")
    else:
        entries = [item.strip() for item in raw.split(",") if item.strip()]
    try:
        return normalize_allowlist(entries)
    except ConnectorAllowlistInvalid as exc:
        raise ConfigurationError("CONNECTOR_HOST_ALLOWLIST is invalid") from exc


@dataclass(frozen=True, slots=True)
class ImportConfig:
    database_url: str
    driver_database_url: str
    s3_endpoint: str
    s3_region: str
    s3_access_key: str
    s3_secret_key: str
    datasets_bucket: str
    connector_encryption_key: str
    connector_host_allowlist: list[str]
    sftp_known_hosts_file: str | None
    lease_seconds: int
    heartbeat_seconds: float
    poll_seconds: float
    pool_min_size: int
    pool_max_size: int
    pool_timeout_seconds: float
    sql_timeout_seconds: float
    s3_connect_timeout_seconds: float
    s3_read_timeout_seconds: float
    sftp_timeout_seconds: float
    part_bytes: int = 8 * 1024 * 1024
    image_header_bytes: int = 256 * 1024


def load_config(environment: Mapping[str, str] | None = None) -> ImportConfig:
    """Read only importer settings and never use ambient API/AWS credentials."""
    source = os.environ if environment is None else environment
    database_url, driver_database_url = _postgres_dsn(source)
    s3_endpoint = _origin(source, "ASSETS_IMPORT_S3_ENDPOINT")
    region = source.get("ASSETS_IMPORT_S3_REGION", "us-east-1").strip()
    if not region:
        raise ConfigurationError("ASSETS_IMPORT_S3_REGION must not be empty")

    lease_seconds = _positive_int(
        source, "ASSETS_IMPORT_LEASE_SECONDS", "120", minimum=30, maximum=3600
    )
    heartbeat_seconds = _positive_float(
        source,
        "ASSETS_IMPORT_HEARTBEAT_SECONDS",
        "20",
        maximum=float(lease_seconds - 1),
    )
    if heartbeat_seconds >= lease_seconds / 2:
        raise ConfigurationError(
            "ASSETS_IMPORT_HEARTBEAT_SECONDS must be less than half the lease"
        )

    known_hosts_file = source.get("ASSETS_IMPORT_SFTP_KNOWN_HOSTS", "").strip() or None
    if known_hosts_file is not None and not Path(known_hosts_file).is_file():
        raise ConfigurationError(
            "ASSETS_IMPORT_SFTP_KNOWN_HOSTS must name a readable file"
        )

    encryption_key = _required_secret(source, "CONNECTOR_ENCRYPTION_KEY")
    try:
        Fernet(encryption_key.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise ConfigurationError("CONNECTOR_ENCRYPTION_KEY is invalid") from exc
    pool_min_size = _positive_int(
        source, "ASSETS_IMPORT_POOL_MIN_SIZE", "1", minimum=1, maximum=8
    )
    pool_max_size = _positive_int(
        source, "ASSETS_IMPORT_POOL_MAX_SIZE", "4", minimum=2, maximum=32
    )
    if pool_min_size > pool_max_size:
        raise ConfigurationError(
            "ASSETS_IMPORT_POOL_MIN_SIZE must not exceed POOL_MAX_SIZE"
        )

    return ImportConfig(
        database_url=database_url,
        driver_database_url=driver_database_url,
        s3_endpoint=s3_endpoint,
        s3_region=region,
        s3_access_key=_required_secret(source, "ASSETS_IMPORT_S3_ACCESS_KEY"),
        s3_secret_key=_required_secret(source, "ASSETS_IMPORT_S3_SECRET_KEY"),
        datasets_bucket=_bucket(source, "ASSETS_IMPORT_DATASETS_BUCKET"),
        connector_encryption_key=encryption_key,
        connector_host_allowlist=_host_allowlist(source),
        sftp_known_hosts_file=known_hosts_file,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        poll_seconds=_positive_float(
            source, "ASSETS_IMPORT_POLL_SECONDS", "2", maximum=60
        ),
        pool_min_size=pool_min_size,
        pool_max_size=pool_max_size,
        pool_timeout_seconds=_positive_float(
            source, "ASSETS_IMPORT_POOL_TIMEOUT_SECONDS", "2", maximum=30
        ),
        sql_timeout_seconds=_positive_float(
            source, "ASSETS_IMPORT_SQL_TIMEOUT_SECONDS", "10", maximum=120
        ),
        s3_connect_timeout_seconds=_positive_float(
            source, "ASSETS_IMPORT_S3_CONNECT_TIMEOUT_SECONDS", "10", maximum=60
        ),
        s3_read_timeout_seconds=_positive_float(
            source, "ASSETS_IMPORT_S3_READ_TIMEOUT_SECONDS", "120", maximum=600
        ),
        sftp_timeout_seconds=_positive_float(
            source, "ASSETS_IMPORT_SFTP_TIMEOUT_SECONDS", "10", maximum=120
        ),
    )
