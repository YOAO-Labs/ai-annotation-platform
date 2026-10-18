"""Explicit connector registry, validation, and adapter construction."""

from __future__ import annotations

from typing import Any

from .host_guard import assert_host_allowed, normalize_allowlist
from .s3 import S3CompatibleSource, validate_s3_endpoint, validate_s3_source_path
from .sftp import SftpSource, validate_sftp_source_path


def target_host(kind: str, config: dict[str, Any]) -> str:
    """Return the endpoint validated immediately before connector I/O."""
    if kind == "s3":
        return str(config.get("endpoint", ""))
    if kind == "sftp":
        return str(config.get("host", ""))
    raise ValueError("unsupported storage connector")


def validate_source_path(kind: str, config: dict[str, Any], path: str | None) -> None:
    """Validate a user-selected source path under its configured connector root."""
    if kind == "s3":
        validate_s3_source_path(config, path)
    elif kind == "sftp":
        validate_sftp_source_path(config, path)
    else:
        raise ValueError("unsupported storage connector")


def validate_secret(kind: str, secret: dict[str, Any], config: dict[str, Any]) -> None:
    """Validate explicit connector credentials and configured authentication mode."""
    if not isinstance(secret, dict):
        raise ValueError("connector secret must be an object")
    if kind == "s3":
        validate_s3_endpoint(config)
        if not isinstance(config.get("bucket"), str) or not config.get("bucket"):
            raise ValueError("S3 bucket must be a non-empty string")
        if any(
            not isinstance(secret.get(key), str) or not secret.get(key)
            for key in ("access_key", "secret_key")
        ):
            raise ValueError("S3 access_key and secret_key are required")
        return
    if kind == "sftp":
        for key in ("private_key", "password", "passphrase"):
            value = secret.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError("SFTP credentials must be text")
        has_key = bool(secret.get("private_key"))
        has_password = bool(secret.get("password"))
        if has_key == has_password:
            raise ValueError("SFTP requires exactly one of password or private_key")
        auth_type = "key" if has_key else "password"
        if config.get("auth_type") not in {None, auth_type}:
            raise ValueError("SFTP auth_type does not match its credentials")
        return
    raise ValueError("unsupported storage connector")


def build_source_adapter(
    kind: str,
    config: dict[str, Any],
    secret: dict[str, Any],
    allowlist: list[str],
    *,
    source_path: str | None = None,
    known_hosts_file: str | None = None,
    sftp_timeout_seconds: float = 10,
    require_conditional_reads: bool = False,
    s3_client: Any | None = None,
    sftp_client: Any | None = None,
) -> S3CompatibleSource | SftpSource:
    """Validate host, path, and credentials immediately before opening a source."""
    normalized_allowlist = normalize_allowlist(allowlist)
    if source_path is not None:
        validate_source_path(kind, config, source_path)
    validate_secret(kind, secret, config)
    assert_host_allowed(target_host(kind, config), normalized_allowlist)
    if kind == "s3":
        return S3CompatibleSource(
            config,
            secret,
            client=s3_client,
            require_conditional_reads=require_conditional_reads,
        )
    if kind == "sftp":
        return SftpSource(
            config,
            secret,
            known_hosts_file=known_hosts_file,
            timeout_seconds=sftp_timeout_seconds,
            require_listing_identity=require_conditional_reads,
            client=sftp_client,
        )
    raise ValueError("unsupported storage connector")
