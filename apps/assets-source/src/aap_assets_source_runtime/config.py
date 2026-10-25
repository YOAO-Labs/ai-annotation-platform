"""Explicit configuration for the independent Assets source runtime."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import ipaddress
import json
import math
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Mapping
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet
from aap_assets_images.policy import ImagePolicy, image_policy_from_environment

from aap_assets_source.connectors.host_guard import (
    ConnectorAllowlistInvalid,
    normalize_allowlist,
)

if TYPE_CHECKING:
    from .guide_config import GuideStorageConfig


class ConfigurationError(ValueError):
    """A required Assets source setting is absent or unsafe."""


@dataclass(frozen=True, slots=True)
class GuideOnlyConfig:
    """Settings for the Guide-only byte owner, without source DB credentials."""

    service_token: str = field(repr=False)
    host: str
    port: int
    max_request_bytes: int
    max_response_bytes: int
    guide: GuideStorageConfig = field(repr=False)
    guide_max_in_flight: int = 4


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


def _optional_secret(environment: Mapping[str, str], name: str) -> str:
    file_name = f"{name}_FILE"
    direct = environment.get(name)
    secret_file = environment.get(file_name)
    direct_value = direct.strip() if direct is not None else ""
    file_value = secret_file.strip() if secret_file is not None else ""
    if direct_value and file_value:
        raise ConfigurationError(f"Set only one of {name} and {file_name}")
    if direct_value:
        return direct_value
    if not file_value:
        return ""
    try:
        value = Path(file_value).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError(
            f"Cannot read configured secret file {file_name}"
        ) from exc
    return value


def _connector_encryption_key(environment: Mapping[str, str]) -> str:
    value = _optional_secret(environment, "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY")
    if not value:
        return ""
    try:
        Fernet(value.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise ConfigurationError(
            "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY is invalid"
        ) from exc
    return value


def _connector_host_allowlist(environment: Mapping[str, str]) -> tuple[str, ...]:
    raw = environment.get("ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST", "[]")
    try:
        values = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ConfigurationError(
            "ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST must be a JSON string array"
        ) from exc
    if not isinstance(values, list) or any(type(value) is not str for value in values):
        raise ConfigurationError(
            "ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST must be a JSON string array"
        )
    try:
        return tuple(normalize_allowlist(values))
    except ConnectorAllowlistInvalid as exc:
        raise ConfigurationError(
            "ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST contains an invalid entry"
        ) from exc


def _known_hosts_file(environment: Mapping[str, str]) -> str | None:
    value = environment.get("ASSETS_SOURCE_SFTP_KNOWN_HOSTS_FILE", "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_file() or not os.access(path, os.R_OK):
        raise ConfigurationError(
            "ASSETS_SOURCE_SFTP_KNOWN_HOSTS_FILE must name a readable file"
        )
    return str(path)


def _postgres_dsn(environment: Mapping[str, str]) -> tuple[str, str]:
    raw = _required_secret(environment, "ASSETS_SOURCE_DATABASE_URL")
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
        raise ConfigurationError("ASSETS_SOURCE_DATABASE_URL must be a PostgreSQL URL")
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


def _common_runtime_settings(
    environment: Mapping[str, str],
) -> tuple[str, str, int, int, int]:
    service_token = _required_secret(environment, "ASSETS_SOURCE_TOKEN")
    try:
        token_bytes = service_token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ConfigurationError("ASSETS_SOURCE_TOKEN must be ASCII") from exc
    if len(token_bytes) < 32 or any(chr(byte).isspace() for byte in token_bytes):
        raise ConfigurationError(
            "ASSETS_SOURCE_TOKEN must be 32+ ASCII non-whitespace characters"
        )

    host = environment.get("ASSETS_SOURCE_HOST", "0.0.0.0").strip()
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise ConfigurationError("ASSETS_SOURCE_HOST must be an IP address") from exc
    port = _positive_int(environment, "ASSETS_SOURCE_PORT", "8767", maximum=65535)
    max_request_bytes = _positive_int(
        environment,
        "ASSETS_SOURCE_MAX_REQUEST_BYTES",
        "1048576",
        maximum=1_048_576,
    )
    max_response_bytes = _positive_int(
        environment,
        "ASSETS_SOURCE_MAX_RESPONSE_BYTES",
        "16777216",
        maximum=16_777_216,
    )
    return service_token, host, port, max_request_bytes, max_response_bytes


@dataclass(frozen=True, slots=True)
class SourceConfig:
    database_url: str
    driver_database_url: str
    service_token: str
    s3_endpoint: str
    s3_public_endpoint: str
    s3_region: str
    s3_access_key: str
    s3_secret_key: str
    connector_encryption_key: str = field(repr=False)
    connector_host_allowlist: tuple[str, ...]
    sftp_known_hosts_file: str | None
    datasets_bucket: str
    import_bucket: str
    media_cache_bucket: str
    host: str
    port: int
    max_request_bytes: int
    max_response_bytes: int
    pool_min_size: int
    pool_max_size: int
    pool_timeout_seconds: float
    sql_timeout_seconds: float
    s3_connect_timeout_seconds: float
    s3_read_timeout_seconds: float
    image_policy: ImagePolicy = field(default_factory=ImagePolicy)
    upload_url_expires_seconds: int = 900
    image_header_bytes: int = 256 * 1024
    guide: GuideStorageConfig | None = field(default=None, repr=False)
    guide_max_in_flight: int = 4

    @property
    def cleanup_bucket(self) -> str:
        """Bucket recorded for deferred pyramid cleanup; no S3 write is granted."""
        return self.media_cache_bucket


def _load_source_config(source: Mapping[str, str]) -> SourceConfig:
    database_url, driver_database_url = _postgres_dsn(source)
    service_token, host, port, max_request_bytes, max_response_bytes = (
        _common_runtime_settings(source)
    )

    s3_endpoint = _http_origin(source, "ASSETS_SOURCE_S3_ENDPOINT")
    s3_public_endpoint = _http_origin(source, "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT")
    region = source.get("ASSETS_SOURCE_S3_REGION", "us-east-1").strip()
    if not region:
        raise ConfigurationError("ASSETS_SOURCE_S3_REGION must not be empty")

    pool_min_size = _positive_int(
        source, "ASSETS_SOURCE_POOL_MIN_SIZE", "1", maximum=32
    )
    pool_max_size = _positive_int(
        source, "ASSETS_SOURCE_POOL_MAX_SIZE", "8", maximum=64
    )
    if pool_min_size > pool_max_size:
        raise ConfigurationError(
            "ASSETS_SOURCE_POOL_MIN_SIZE must not exceed POOL_MAX_SIZE"
        )

    try:
        image_policy = image_policy_from_environment(source)
    except ValueError:
        raise ConfigurationError("IMAGE_PYRAMID_* settings are invalid") from None

    return SourceConfig(
        database_url=database_url,
        driver_database_url=driver_database_url,
        service_token=service_token,
        s3_endpoint=s3_endpoint,
        s3_public_endpoint=s3_public_endpoint,
        s3_region=region,
        s3_access_key=_required_secret(source, "ASSETS_SOURCE_S3_ACCESS_KEY"),
        s3_secret_key=_required_secret(source, "ASSETS_SOURCE_S3_SECRET_KEY"),
        connector_encryption_key=_connector_encryption_key(source),
        connector_host_allowlist=_connector_host_allowlist(source),
        sftp_known_hosts_file=_known_hosts_file(source),
        datasets_bucket=_bucket(source, "ASSETS_SOURCE_DATASETS_BUCKET"),
        import_bucket=_bucket(source, "ASSETS_SOURCE_IMPORT_BUCKET"),
        media_cache_bucket=_bucket(source, "ASSETS_SOURCE_MEDIA_CACHE_BUCKET"),
        host=host,
        port=port,
        max_request_bytes=max_request_bytes,
        max_response_bytes=max_response_bytes,
        pool_min_size=pool_min_size,
        pool_max_size=pool_max_size,
        pool_timeout_seconds=_positive_float(
            source, "ASSETS_SOURCE_POOL_TIMEOUT_SECONDS", "2", maximum=30
        ),
        sql_timeout_seconds=_positive_float(
            source, "ASSETS_SOURCE_SQL_TIMEOUT_SECONDS", "5", maximum=60
        ),
        s3_connect_timeout_seconds=_positive_float(
            source, "ASSETS_SOURCE_S3_CONNECT_TIMEOUT_SECONDS", "2", maximum=30
        ),
        s3_read_timeout_seconds=_positive_float(
            source, "ASSETS_SOURCE_S3_READ_TIMEOUT_SECONDS", "10", maximum=120
        ),
        image_policy=image_policy,
    )


def load_config(
    environment: Mapping[str, str] | None = None,
) -> SourceConfig | GuideOnlyConfig:
    """Load only the selected Assets runtime capabilities."""
    source = os.environ if environment is None else environment
    mode = source.get("ASSETS_SOURCE_MODE", "source")
    mode = mode.strip() if isinstance(mode, str) else ""
    if mode not in {"source", "guide-only", "source-and-guide"}:
        raise ConfigurationError("ASSETS_SOURCE_MODE is invalid")

    if mode == "guide-only":
        from .guide_config import load_guide_storage_config

        service_token, host, port, max_request_bytes, max_response_bytes = (
            _common_runtime_settings(source)
        )
        return GuideOnlyConfig(
            service_token=service_token,
            host=host,
            port=port,
            max_request_bytes=max_request_bytes,
            max_response_bytes=max_response_bytes,
            guide=load_guide_storage_config(source),
            guide_max_in_flight=_positive_int(
                source,
                "ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT",
                "4",
                maximum=32,
            ),
        )

    config = _load_source_config(source)
    if mode == "source-and-guide":
        from .guide_config import load_guide_storage_config

        config = replace(
            config,
            guide=load_guide_storage_config(source),
            guide_max_in_flight=_positive_int(
                source,
                "ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT",
                "4",
                maximum=32,
            ),
        )
    return config
