"""Explicit, fail-closed configuration for the independent catalog process."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import math
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


class ConfigurationError(ValueError):
    """A required Assets catalog setting is absent or unsafe."""


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
            raise ConfigurationError(
                f"Cannot read configured secret file {file_name}"
            ) from exc
    else:
        raise ConfigurationError(f"Missing required setting {name}")
    if not value or not value.strip():
        raise ConfigurationError(f"Configured secret {name} is empty")
    return value


def _postgres_dsn(environment: Mapping[str, str]) -> tuple[str, str]:
    raw = _required_secret(environment, "ASSETS_CATALOG_DATABASE_URL")
    try:
        parsed = urlsplit(raw)
        parsed.port
        valid = (
            parsed.scheme in {"postgres", "postgresql", "postgresql+asyncpg"}
            and bool(parsed.hostname)
            and parsed.username is not None
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError("ASSETS_CATALOG_DATABASE_URL must be a PostgreSQL URL")
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


def _http_origin(environment: Mapping[str, str], name: str) -> str:
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
    if (
        not 3 <= len(value) <= 63
        or not _BUCKET_RE.fullmatch(value)
        or _IP_BUCKET_RE.fullmatch(value)
    ):
        raise ConfigurationError(f"{name} is invalid")
    return value


def _positive_int(
    environment: Mapping[str, str], name: str, default: str, *, maximum: int
) -> int:
    raw = environment.get(name, default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise ConfigurationError(f"{name} must be between 1 and {maximum}")
    return value


def _positive_float(
    environment: Mapping[str, str], name: str, default: str, *, maximum: float
) -> float:
    raw = environment.get(name, default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or not 0.1 <= value <= maximum:
        raise ConfigurationError(f"{name} must be between 0.1 and {maximum:g}")
    return value


@dataclass(frozen=True, slots=True)
class CatalogConfig:
    database_url: str
    driver_database_url: str
    gateway_token: str
    s3_public_endpoint: str
    s3_region: str
    s3_access_key: str
    s3_secret_key: str
    datasets_bucket: str
    media_cache_bucket: str
    host: str
    port: int
    max_request_bytes: int
    max_response_bytes: int
    pool_min_size: int
    pool_max_size: int
    pool_timeout_seconds: float
    sql_timeout_seconds: float


def load_config(environment: Mapping[str, str] | None = None) -> CatalogConfig:
    """Read only Assets catalog settings; never fall back to API/AWS defaults."""
    source = os.environ if environment is None else environment
    database_url, driver_database_url = _postgres_dsn(source)
    gateway_token = _required_secret(source, "ASSETS_CATALOG_TOKEN")
    try:
        token_bytes = gateway_token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ConfigurationError("ASSETS_CATALOG_TOKEN must be ASCII") from exc
    if len(token_bytes) < 32 or any(chr(byte).isspace() for byte in token_bytes):
        raise ConfigurationError(
            "ASSETS_CATALOG_TOKEN must be 32+ ASCII non-whitespace characters"
        )

    public_endpoint = _http_origin(source, "ASSETS_CATALOG_S3_PUBLIC_ENDPOINT")
    region = source.get("ASSETS_CATALOG_S3_REGION", "us-east-1").strip()
    if not region:
        raise ConfigurationError("ASSETS_CATALOG_S3_REGION must not be empty")

    host = source.get("ASSETS_CATALOG_HOST", "0.0.0.0").strip()
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise ConfigurationError("ASSETS_CATALOG_HOST must be an IP address") from exc

    port = _positive_int(source, "ASSETS_CATALOG_PORT", "8766", maximum=65535)
    max_request_bytes = _positive_int(
        source, "ASSETS_CATALOG_MAX_REQUEST_BYTES", "1048576", maximum=4_194_304
    )
    max_response_bytes = _positive_int(
        source,
        "ASSETS_CATALOG_MAX_RESPONSE_BYTES",
        "16777216",
        maximum=67_108_864,
    )
    pool_min_size = _positive_int(
        source, "ASSETS_CATALOG_POOL_MIN_SIZE", "1", maximum=32
    )
    pool_max_size = _positive_int(
        source, "ASSETS_CATALOG_POOL_MAX_SIZE", "8", maximum=64
    )
    if pool_min_size > pool_max_size:
        raise ConfigurationError(
            "ASSETS_CATALOG_POOL_MIN_SIZE must not exceed POOL_MAX_SIZE"
        )

    return CatalogConfig(
        database_url=database_url,
        driver_database_url=driver_database_url,
        gateway_token=gateway_token,
        s3_public_endpoint=public_endpoint,
        s3_region=region,
        s3_access_key=_required_secret(source, "ASSETS_CATALOG_S3_ACCESS_KEY"),
        s3_secret_key=_required_secret(source, "ASSETS_CATALOG_S3_SECRET_KEY"),
        datasets_bucket=_bucket(source, "ASSETS_CATALOG_DATASETS_BUCKET"),
        media_cache_bucket=_bucket(source, "ASSETS_CATALOG_MEDIA_CACHE_BUCKET"),
        host=host,
        port=port,
        max_request_bytes=max_request_bytes,
        max_response_bytes=max_response_bytes,
        pool_min_size=pool_min_size,
        pool_max_size=pool_max_size,
        pool_timeout_seconds=_positive_float(
            source, "ASSETS_CATALOG_POOL_TIMEOUT_SECONDS", "2", maximum=30
        ),
        sql_timeout_seconds=_positive_float(
            source, "ASSETS_CATALOG_SQL_TIMEOUT_SECONDS", "5", maximum=60
        ),
    )
