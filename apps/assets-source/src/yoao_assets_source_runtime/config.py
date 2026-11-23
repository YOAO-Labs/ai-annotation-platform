"""Explicit configuration for the independent Assets source runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import math
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet
from yoao_assets_images.policy import ImagePolicy, image_policy_from_environment

from yoao_assets_source.connectors.host_guard import (
    ConnectorAllowlistInvalid,
    normalize_allowlist,
)
from yoao_assets_source.saved_io import (
    IMPORT_SCOPES,
    READ_WRITE_SCOPES,
    TOKEN_SETTINGS,
    valid_token,
)


class ConfigurationError(ValueError):
    """A required Assets source setting is absent or unsafe."""


_BUCKET_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_IP_BUCKET_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){3}$")

_NATIVE_SIGNER_SECRETS = (
    "ASSETS_SOURCE_NATIVE_PYRAMID_WRITER_TOKEN",
    "ASSETS_SOURCE_NATIVE_PYRAMID_READER_TOKEN",
    "ASSETS_SOURCE_NATIVE_PYRAMID_CLEANUP_TOKEN",
    "ASSETS_SOURCE_NATIVE_S3_ACCESS_KEY",
    "ASSETS_SOURCE_NATIVE_S3_SECRET_KEY",
)
_NATIVE_ROLE_SECRETS = _NATIVE_SIGNER_SECRETS[:3]


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


def _postgres_dsn(
    environment: Mapping[str, str], name: str = "ASSETS_SOURCE_DATABASE_URL"
) -> tuple[str, str]:
    raw = _required_secret(environment, name)
    try:
        parsed = urlsplit(raw)
        parsed.port
        valid = (
            parsed.scheme in {"postgres", "postgresql", "postgresql+asyncpg"}
            and bool(parsed.hostname)
            and bool(parsed.username)
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError(f"{name} must be a PostgreSQL URL")
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
class NativeSignerSettings:
    """Explicit Source-owned identity and role tokens for Native S3 signing."""

    writer_token: str = field(repr=False)
    reader_token: str = field(repr=False)
    cleanup_token: str = field(repr=False)
    s3_access_key: str = field(repr=False)
    s3_secret_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SavedIOSettings:
    database_url: str = field(repr=False)
    tokens: Mapping[str, str] = field(repr=False)
    spool_dir: str | None
    max_upload_bytes: int


@dataclass(frozen=True, slots=True)
class SourceConfig:
    database_url: str
    driver_database_url: str
    service_token: str
    import_verify_token: str = field(repr=False)
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
    import_verify_timeout_seconds: float
    image_policy: ImagePolicy = field(default_factory=ImagePolicy)
    upload_url_expires_seconds: int = 900
    image_header_bytes: int = 256 * 1024
    native: NativeSignerSettings | None = None
    connections_token: str = field(default="", repr=False)
    saved_io: SavedIOSettings | None = None
    annotations_bucket: str | None = None
    sftp_roots: dict[str, dict] = field(default_factory=dict)

    @property
    def cleanup_bucket(self) -> str:
        """Bucket recorded for deferred pyramid cleanup; no S3 write is granted."""
        return self.media_cache_bucket


def _native_signer_settings(
    environment: Mapping[str, str],
    *,
    service_token: str,
    import_verify_token: str,
    source_access_key: str,
) -> NativeSignerSettings | None:
    """Load the whole Native signer or leave the feature unconfigured."""
    values = {
        name: _optional_secret(environment, name) for name in _NATIVE_SIGNER_SECRETS
    }
    configured = [name for name, value in values.items() if value]
    if not configured:
        return None
    missing = [name for name in _NATIVE_SIGNER_SECRETS if not values[name]]
    if missing:
        raise ConfigurationError(
            "ASSETS_SOURCE_NATIVE_* settings must be configured together: "
            + ", ".join(missing)
        )
    roles = [values[name] for name in _NATIVE_ROLE_SECRETS]
    for name, token in zip(_NATIVE_ROLE_SECRETS, roles):
        try:
            token_bytes = token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ConfigurationError(f"{name} must be ASCII") from exc
        if len(token_bytes) < 32 or any(chr(byte).isspace() for byte in token_bytes):
            raise ConfigurationError(
                f"{name} must be 32+ ASCII non-whitespace characters"
            )
    if len({*roles, service_token, import_verify_token}) != 5:
        raise ConfigurationError(
            "ASSETS_SOURCE_NATIVE_PYRAMID_*_TOKEN values must be distinct from "
            "each other and from existing Source tokens"
        )
    if values["ASSETS_SOURCE_NATIVE_S3_ACCESS_KEY"] == source_access_key:
        raise ConfigurationError(
            "ASSETS_SOURCE_NATIVE_S3_ACCESS_KEY must differ from the Source identity"
        )
    return NativeSignerSettings(
        writer_token=values["ASSETS_SOURCE_NATIVE_PYRAMID_WRITER_TOKEN"],
        reader_token=values["ASSETS_SOURCE_NATIVE_PYRAMID_READER_TOKEN"],
        cleanup_token=values["ASSETS_SOURCE_NATIVE_PYRAMID_CLEANUP_TOKEN"],
        s3_access_key=values["ASSETS_SOURCE_NATIVE_S3_ACCESS_KEY"],
        s3_secret_key=values["ASSETS_SOURCE_NATIVE_S3_SECRET_KEY"],
    )


def _saved_io_settings(
    source: Mapping[str, str],
    existing_tokens: tuple[str, ...],
    connector_key: str,
    management_dsn: str,
) -> SavedIOSettings | None:
    token_values = {
        scope: _optional_secret(source, name) for scope, name in TOKEN_SETTINGS.items()
    }
    database_name = "ASSETS_SOURCE_SAVED_IO_DATABASE_URL"
    database = _optional_secret(source, database_name)
    if not database and not any(token_values.values()):
        return None
    if (
        not database
        or not connector_key
        or any(not token_values[scope] for scope in READ_WRITE_SCOPES)
    ):
        raise ConfigurationError(
            "Saved I/O requires the dedicated PostgreSQL URL, read/write/cleanup "
            "tokens and Source connector encryption key together"
        )
    if sum(bool(token_values[scope]) for scope in IMPORT_SCOPES) == 1:
        raise ConfigurationError(
            "Saved import I/O read and write tokens must be configured together"
        )
    tokens = {scope: token for scope, token in token_values.items() if token}
    if not all(valid_token(token) for token in tokens.values()) or len(
        {*tokens.values(), *existing_tokens}
    ) != len(tokens) + len(existing_tokens):
        raise ConfigurationError("Saved I/O bearer tokens must be valid and distinct")
    _, dsn = _postgres_dsn({database_name: database}, database_name)
    if dsn == management_dsn:
        raise ConfigurationError("Saved I/O must use a dedicated PostgreSQL login")
    spool = source.get("ASSETS_SOURCE_SAVED_IO_SPOOL_DIR", "").strip()
    if (
        not spool
        or not Path(spool).is_dir()
        or not os.access(spool, os.R_OK | os.W_OK | os.X_OK)
    ):
        raise ConfigurationError("ASSETS_SOURCE_SAVED_IO_SPOOL_DIR is not accessible")
    return SavedIOSettings(
        database_url=dsn,
        tokens=tokens,
        spool_dir=spool,
        max_upload_bytes=_positive_int(
            source,
            "ASSETS_SOURCE_SAVED_IO_MAX_UPLOAD_BYTES",
            "68719476736",
            maximum=5 * 1024**4,
        ),
    )


def _sftp_roots(source: Mapping[str, str]) -> dict[str, dict]:
    raw = source.get("STORAGE_SFTP_ROOTS", "").strip()
    if not raw or raw == "{}":
        return {}
    from yoao_assets_source.reference_protocol import parse_sftp_roots

    try:
        return parse_sftp_roots(raw)
    except ValueError:
        raise ConfigurationError("STORAGE_SFTP_ROOTS is invalid") from None


def _load_source_config(source: Mapping[str, str]) -> SourceConfig:
    database_url, driver_database_url = _postgres_dsn(source)
    service_token, host, port, max_request_bytes, max_response_bytes = (
        _common_runtime_settings(source)
    )
    import_verify_token = _required_secret(source, "ASSETS_SOURCE_IMPORT_VERIFY_TOKEN")
    try:
        token_bytes = import_verify_token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ConfigurationError(
            "ASSETS_SOURCE_IMPORT_VERIFY_TOKEN must be ASCII"
        ) from exc
    if (
        len(token_bytes) < 32
        or any(chr(byte).isspace() for byte in token_bytes)
        or import_verify_token == service_token
    ):
        raise ConfigurationError(
            "ASSETS_SOURCE_IMPORT_VERIFY_TOKEN must be distinct and contain 32+ ASCII non-whitespace characters"
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

    source_access_key = _required_secret(source, "ASSETS_SOURCE_S3_ACCESS_KEY")
    native = _native_signer_settings(
        source,
        service_token=service_token,
        import_verify_token=import_verify_token,
        source_access_key=source_access_key,
    )

    existing = (service_token, import_verify_token)
    if native is not None:
        existing += (native.writer_token, native.reader_token, native.cleanup_token)
    connections_token = _optional_secret(source, "ASSETS_SOURCE_CONNECTIONS_TOKEN")
    if connections_token:
        if not valid_token(connections_token) or connections_token in existing:
            raise ConfigurationError(
                "ASSETS_SOURCE_CONNECTIONS_TOKEN must be valid and distinct"
            )
        existing += (connections_token,)
    connector_key = _connector_encryption_key(source)
    saved_io = _saved_io_settings(source, existing, connector_key, driver_database_url)

    return SourceConfig(
        database_url=database_url,
        driver_database_url=driver_database_url,
        service_token=service_token,
        import_verify_token=import_verify_token,
        s3_endpoint=s3_endpoint,
        s3_public_endpoint=s3_public_endpoint,
        s3_region=region,
        s3_access_key=source_access_key,
        s3_secret_key=_required_secret(source, "ASSETS_SOURCE_S3_SECRET_KEY"),
        connector_encryption_key=connector_key,
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
        import_verify_timeout_seconds=_positive_float(
            source, "ASSETS_SOURCE_IMPORT_VERIFY_TIMEOUT_SECONDS", "120", maximum=600
        ),
        image_policy=image_policy,
        native=native,
        connections_token=connections_token,
        saved_io=saved_io,
        annotations_bucket=(
            _bucket(source, "ASSETS_SOURCE_ANNOTATIONS_BUCKET")
            if source.get("ASSETS_SOURCE_ANNOTATIONS_BUCKET", "").strip()
            else None
        ),
        sftp_roots=_sftp_roots(source),
    )


def load_config(
    environment: Mapping[str, str] | None = None,
) -> SourceConfig:
    """Load the Assets source runtime; retired guide-only modes are rejected."""
    source = os.environ if environment is None else environment
    mode = source.get("ASSETS_SOURCE_MODE", "source")
    mode = mode.strip() if isinstance(mode, str) else ""
    if mode != "source":
        raise ConfigurationError("ASSETS_SOURCE_MODE is invalid")
    return _load_source_config(source)
