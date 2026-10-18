"""Portable storage-connection configuration, credential, and output helpers."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


_REQUIRED_CONFIG = {
    "s3": {"endpoint", "bucket"},
    "sftp": {"host", "username"},
}
_ALLOWED_CONFIG = {
    "s3": {"endpoint", "bucket", "region", "base_prefix", "use_ssl"},
    "sftp": {"host", "port", "username", "base_path", "auth_type"},
}


class ConnectorValidationError(Exception):
    """A connection config or credential does not match its connector kind."""


def validate_config(kind: str, config: dict | None) -> dict:
    """Return the allowlisted config fields used by the current API service."""
    if kind not in _ALLOWED_CONFIG:
        raise ConnectorValidationError(f"不支持的连接类型: {kind}")
    config = config or {}
    unknown = set(config) - _ALLOWED_CONFIG[kind]
    if unknown:
        raise ConnectorValidationError(
            f"{kind} 不支持配置项: {', '.join(sorted(unknown))}"
        )
    missing = _REQUIRED_CONFIG[kind] - set(config)
    if missing:
        raise ConnectorValidationError(
            f"{kind} 缺少必填配置: {', '.join(sorted(missing))}"
        )
    clean_config = {key: config[key] for key in _ALLOWED_CONFIG[kind] if key in config}
    if kind == "s3":
        _reject_endpoint_userinfo(clean_config["endpoint"])
    return clean_config


def _reject_endpoint_userinfo(endpoint: object) -> None:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ConnectorValidationError("s3 endpoint 必须为非空文本")
    candidate = endpoint if "://" in endpoint else f"//{endpoint}"
    try:
        parsed = urlsplit(candidate)
        has_userinfo = parsed.username is not None or parsed.password is not None
    except ValueError as exc:
        raise ConnectorValidationError("s3 endpoint URL 无效") from exc
    if has_userinfo:
        raise ConnectorValidationError("s3 endpoint URL 不能包含用户名或密码")


def validate_secret(
    kind: str, secret: dict | None, config: dict | None = None
) -> str | None:
    """Validate the existing S3/SFTP credential rules and infer SFTP auth mode."""
    secret = secret or {}
    if kind == "s3":
        if any(
            not isinstance(secret.get(key), str) or not secret.get(key)
            for key in ("access_key", "secret_key")
        ):
            raise ConnectorValidationError("s3 需提供 access_key 与 secret_key")
    elif kind == "sftp":
        for key in ("private_key", "password", "passphrase"):
            value = secret.get(key)
            if value is not None and (
                not isinstance(value, str)
                or (key in {"private_key", "password"} and not value)
            ):
                raise ConnectorValidationError("sftp 凭据必须为文本")
        has_key = bool(secret.get("private_key"))
        has_password = bool(secret.get("password"))
        if has_key == has_password:
            raise ConnectorValidationError(
                "sftp 必须且只能提供 password 或 private_key 其中之一"
            )
        auth = "key" if has_key else "password"
        configured_auth = (config or {}).get("auth_type")
        if configured_auth is not None and configured_auth != auth:
            raise ConnectorValidationError("sftp auth_type 必须与凭据类型一致")
        return auth
    elif kind not in _ALLOWED_CONFIG:
        raise ConnectorValidationError(f"不支持的连接类型: {kind}")
    return None


def public_config(kind: str, config: dict | None) -> dict:
    """Return only public config fields and redact legacy S3 endpoint userinfo."""
    source = config or {}
    allowed = _ALLOWED_CONFIG.get(kind, set())
    result = {key: source[key] for key in allowed if key in source}
    endpoint = result.get("endpoint")
    if kind == "s3" and isinstance(endpoint, str):
        result["endpoint"] = _redact_endpoint_userinfo(endpoint)
    return result


def _redact_endpoint_userinfo(endpoint: str) -> str:
    candidate = endpoint if "://" in endpoint else f"//{endpoint}"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return "[invalid endpoint]"
    if "@" not in parsed.netloc:
        return endpoint
    safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
    safe = urlunsplit(
        (parsed.scheme, safe_netloc, parsed.path, parsed.query, parsed.fragment)
    )
    return safe if parsed.scheme else safe.removeprefix("//")


__all__ = [
    "ConnectorValidationError",
    "public_config",
    "validate_config",
    "validate_secret",
]
