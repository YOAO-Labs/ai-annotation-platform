"""Explicit configuration for the cleanup-only process."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import math
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError

from .contract import CleanupBucket

_BUCKET_ADAPTER = TypeAdapter(CleanupBucket)


class ConfigurationError(ValueError):
    """Required cleanup configuration is absent or unsafe."""


def _secret(
    environment: Mapping[str, str], name: str, *, file_name: str | None = None
) -> str:
    file_key = file_name or f"{name}_FILE"
    direct = environment.get(name)
    secret_file = environment.get(file_key)
    if direct and secret_file:
        raise ConfigurationError(f"Set only one of {name} and {file_key}")
    if direct:
        value = direct
    elif secret_file:
        try:
            value = Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(
                f"Cannot read configured secret file {file_key}"
            ) from exc
    else:
        raise ConfigurationError(f"Missing required setting {name}")
    if not value or not value.strip():
        raise ConfigurationError(f"Configured secret {name} is empty")
    return value


def _positive_float(environment: Mapping[str, str], name: str, default: str) -> float:
    raw = environment.get(name, default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"{name} must be between 0.1 and 3600 seconds"
        ) from exc
    if not math.isfinite(value) or not 0.1 <= value <= 3600:
        raise ConfigurationError(f"{name} must be between 0.1 and 3600 seconds")
    return value


def _page_size(environment: Mapping[str, str]) -> int:
    raw = environment.get("ASSET_CLEANUP_PAGE_SIZE", "100")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError("ASSET_CLEANUP_PAGE_SIZE must be an integer") from exc
    if not 1 <= value <= 1000:
        raise ConfigurationError("ASSET_CLEANUP_PAGE_SIZE must be between 1 and 1000")
    return value


@dataclass(frozen=True)
class CleanupConfig:
    database_url: str
    bucket: str
    s3_endpoint: str
    s3_region: str
    s3_access_key: str
    s3_secret_key: str
    poll_seconds: float
    page_size: int
    health_host: str
    health_port: int


def load_config(environment: Mapping[str, str] | None = None) -> CleanupConfig:
    source = os.environ if environment is None else environment
    database_url = _secret(source, "ASSET_CLEANUP_DATABASE_URL")
    try:
        parsed_database_url = urlsplit(database_url)
        parsed_database_url.port
        valid_database_url = (
            parsed_database_url.scheme in {"postgres", "postgresql"}
            and bool(parsed_database_url.hostname)
            and parsed_database_url.username is not None
        )
    except ValueError:
        valid_database_url = False
    if not valid_database_url:
        raise ConfigurationError("ASSET_CLEANUP_DATABASE_URL must be a PostgreSQL URL")

    endpoint = source.get("ASSET_CLEANUP_S3_ENDPOINT", "").strip()
    try:
        parsed_endpoint = urlsplit(endpoint)
        parsed_endpoint.port
        endpoint_valid = (
            parsed_endpoint.scheme in {"http", "https"}
            and bool(parsed_endpoint.hostname)
            and parsed_endpoint.username is None
            and parsed_endpoint.password is None
            and parsed_endpoint.path in {"", "/"}
            and not parsed_endpoint.query
            and not parsed_endpoint.fragment
        )
    except ValueError:
        endpoint_valid = False
    if not endpoint_valid:
        raise ConfigurationError("ASSET_CLEANUP_S3_ENDPOINT must be an HTTP(S) origin")

    try:
        bucket = _BUCKET_ADAPTER.validate_python(source.get("ASSET_CLEANUP_BUCKET", ""))
    except ValidationError as exc:
        raise ConfigurationError("ASSET_CLEANUP_BUCKET is invalid") from exc

    region = source.get("ASSET_CLEANUP_S3_REGION", "us-east-1").strip()
    if not region:
        raise ConfigurationError("ASSET_CLEANUP_S3_REGION must not be empty")

    raw_health_port = source.get("ASSET_CLEANUP_HEALTH_PORT", "8765")
    try:
        health_port = int(raw_health_port)
    except ValueError as exc:
        raise ConfigurationError(
            "ASSET_CLEANUP_HEALTH_PORT must be an integer"
        ) from exc
    if not 1 <= health_port <= 65535:
        raise ConfigurationError("ASSET_CLEANUP_HEALTH_PORT is out of range")

    health_host = source.get("ASSET_CLEANUP_HEALTH_HOST", "127.0.0.1")
    try:
        address = ip_address(health_host)
    except ValueError as exc:
        raise ConfigurationError("Cleanup health host must be a loopback IP") from exc
    if not address.is_loopback or address.version != 4:
        raise ConfigurationError("Cleanup health endpoint must use IPv4 loopback")

    return CleanupConfig(
        database_url=database_url,
        bucket=bucket,
        s3_endpoint=endpoint.rstrip("/"),
        s3_region=region,
        s3_access_key=_secret(source, "ASSET_CLEANUP_S3_ACCESS_KEY"),
        s3_secret_key=_secret(source, "ASSET_CLEANUP_S3_SECRET_KEY"),
        poll_seconds=_positive_float(source, "ASSET_CLEANUP_POLL_SECONDS", "5"),
        page_size=_page_size(source),
        health_host=health_host,
        health_port=health_port,
    )
