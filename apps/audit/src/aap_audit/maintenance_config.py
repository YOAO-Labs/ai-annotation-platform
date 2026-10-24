"""Small, secret-safe configuration for the one-shot Audit maintenance CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


class ConfigurationError(RuntimeError):
    """A safe configuration error that never includes secret values."""


@dataclass(frozen=True, slots=True)
class MaintenanceConfig:
    database_url: str = field(repr=False)
    archive_s3_endpoint_url: str | None = None
    archive_s3_region: str = "us-east-1"
    archive_s3_bucket: str = "audit-archive"
    archive_s3_access_key_id: str | None = field(default=None, repr=False)
    archive_s3_secret_access_key: str | None = field(default=None, repr=False)

    def archive_storage(self):
        if not all(
            (
                self.archive_s3_endpoint_url,
                self.archive_s3_access_key_id,
                self.archive_s3_secret_access_key,
            )
        ):
            raise ConfigurationError("missing dedicated Audit archive S3 settings")
        from .archive_storage import AuditArchiveStorage

        return AuditArchiveStorage(
            endpoint_url=self.archive_s3_endpoint_url,
            region=self.archive_s3_region,
            bucket=self.archive_s3_bucket,
            access_key_id=self.archive_s3_access_key_id,
            secret_access_key=self.archive_s3_secret_access_key,
        )


def _read_value(
    values: Mapping[str, str],
    name: str,
    *,
    required: bool = False,
    secret_file: bool = False,
) -> str | None:
    inline = values.get(name, "").strip()
    file_path = values.get(f"{name}_FILE", "").strip() if secret_file else ""
    if inline and file_path:
        raise ConfigurationError(f"set only one of {name} and {name}_FILE")
    if file_path:
        try:
            with Path(file_path).open("rb") as handle:
                raw = handle.read(4097)
        except OSError:
            raise ConfigurationError(f"unable to read {name} file") from None
        if len(raw) > 4096:
            raise ConfigurationError(f"{name} file exceeds its size limit")
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise ConfigurationError(f"{name} must be UTF-8") from None
    else:
        value = inline
    if not value:
        if required:
            raise ConfigurationError(f"missing {name}")
        return None
    if len(value.encode("utf-8")) > 4096:
        raise ConfigurationError(f"{name} exceeds its size limit")
    return value


def _valid_url(
    value: str, *, schemes: set[str], allow_path: bool, allow_query: bool = False
) -> bool:
    try:
        parsed = urlsplit(value)
        parsed.port
        return (
            parsed.scheme in schemes
            and bool(parsed.hostname)
            and (allow_path or parsed.path in ("", "/"))
            and (allow_query or not parsed.query)
            and not parsed.fragment
            and (allow_path or (parsed.username is None and parsed.password is None))
        )
    except ValueError:
        return False


def load_config(
    environment: Mapping[str, str] | None = None,
    *,
    require_archive_storage: bool = False,
) -> MaintenanceConfig:
    values = os.environ if environment is None else environment
    database_url = _read_value(
        values,
        "AUDIT_MAINTENANCE_DATABASE_URL",
        required=True,
        secret_file=True,
    )
    if not _valid_url(
        database_url,
        schemes={"postgres", "postgresql"},
        allow_path=True,
        allow_query=True,
    ):
        raise ConfigurationError(
            "AUDIT_MAINTENANCE_DATABASE_URL must be a PostgreSQL URL"
        )

    endpoint = None
    region = "us-east-1"
    bucket = "audit-archive"
    access_key_id = None
    secret_access_key = None
    if require_archive_storage:
        endpoint = _read_value(
            values, "AUDIT_MAINTENANCE_ARCHIVE_S3_ENDPOINT_URL", required=True
        )
        if not _valid_url(endpoint, schemes={"http", "https"}, allow_path=False):
            raise ConfigurationError(
                "AUDIT_MAINTENANCE_ARCHIVE_S3_ENDPOINT_URL must be an HTTP(S) origin"
            )
        region = _read_value(values, "AUDIT_MAINTENANCE_ARCHIVE_S3_REGION") or region
        bucket = _read_value(values, "AUDIT_MAINTENANCE_ARCHIVE_S3_BUCKET") or bucket
        access_key_id = _read_value(
            values,
            "AUDIT_MAINTENANCE_ARCHIVE_S3_ACCESS_KEY_ID",
            required=True,
            secret_file=True,
        )
        secret_access_key = _read_value(
            values,
            "AUDIT_MAINTENANCE_ARCHIVE_S3_SECRET_ACCESS_KEY",
            required=True,
            secret_file=True,
        )
        if (
            len(region) > 128
            or not region.isascii()
            or any(c.isspace() for c in region)
        ):
            raise ConfigurationError("invalid dedicated Audit archive S3 region")
        if (
            not 3 <= len(bucket) <= 63
            or bucket.lower() != bucket
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in bucket)
            or bucket[0] in ".-"
            or bucket[-1] in ".-"
            or ".." in bucket
        ):
            raise ConfigurationError("invalid dedicated Audit archive S3 bucket")

    return MaintenanceConfig(
        database_url=database_url,
        archive_s3_endpoint_url=endpoint,
        archive_s3_region=region,
        archive_s3_bucket=bucket,
        archive_s3_access_key_id=access_key_id,
        archive_s3_secret_access_key=secret_access_key,
    )
