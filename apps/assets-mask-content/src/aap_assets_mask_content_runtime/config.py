"""Environment configuration with bounded Docker secret-file support."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

MAX_SECRET_FILE_BYTES = 4096


@dataclass(frozen=True)
class RuntimeConfig:
    role: str
    token: str = field(repr=False)
    endpoint_url: str = field(repr=False)
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    bucket: str
    region: str
    host: str
    port: int


def _secret(environment: Mapping[str, str], name: str) -> str:
    inline_name = f"{name}"
    file_name = f"{name}_FILE"
    inline = environment.get(inline_name)
    path = environment.get(file_name)
    inline_present = inline is not None and inline != ""
    file_present = path is not None and path != ""
    if inline_present and file_present:
        raise RuntimeError(f"set only one of {inline_name} and {file_name}")
    if file_present:
        try:
            with Path(path).open("rb") as handle:
                raw = handle.read(MAX_SECRET_FILE_BYTES + 1)
        except OSError:
            raise RuntimeError(f"unable to read secret file for {name}") from None
        if len(raw) > MAX_SECRET_FILE_BYTES:
            raise RuntimeError(f"secret file for {name} exceeds the size limit")
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise RuntimeError(f"secret file for {name} must be UTF-8") from None
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    else:
        value = inline or ""
    if not value:
        raise RuntimeError(f"missing required secret {name}")
    return value


def load_config(environment: Mapping[str, str] | None = None) -> RuntimeConfig:
    values = os.environ if environment is None else environment
    role = values.get("MASK_CONTENT_ROLE", "")
    if role not in {"content", "cleanup"}:
        raise RuntimeError("MASK_CONTENT_ROLE must be content or cleanup")
    endpoint_url = values.get("MASK_CONTENT_S3_ENDPOINT_URL", "")
    if not endpoint_url:
        raise RuntimeError(
            "missing required environment variable MASK_CONTENT_S3_ENDPOINT_URL"
        )
    try:
        endpoint = urlsplit(endpoint_url)
        endpoint.port
        endpoint_valid = (
            endpoint.scheme in {"http", "https"}
            and bool(endpoint.hostname)
            and endpoint.username is None
            and endpoint.password is None
            and not endpoint.query
            and not endpoint.fragment
        )
    except ValueError:
        endpoint_valid = False
    if not endpoint_valid:
        raise RuntimeError(
            "MASK_CONTENT_S3_ENDPOINT_URL must be an HTTP(S) endpoint without credentials"
        )
    bucket = values.get("MASK_CONTENT_BUCKET", "")
    if not bucket:
        raise RuntimeError("missing required environment variable MASK_CONTENT_BUCKET")
    region = values.get("MASK_CONTENT_REGION", "us-east-1")
    if not region:
        raise RuntimeError("MASK_CONTENT_REGION must not be empty")
    host = values.get("MASK_CONTENT_HOST", "127.0.0.1")
    try:
        port = int(values.get("MASK_CONTENT_PORT", "8770"))
    except ValueError as exc:
        raise RuntimeError("MASK_CONTENT_PORT must be a valid port") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("MASK_CONTENT_PORT must be between 1 and 65535")
    return RuntimeConfig(
        role=role,
        token=_secret(values, "MASK_CONTENT_TOKEN"),
        endpoint_url=endpoint_url,
        access_key=_secret(values, "MASK_CONTENT_S3_ACCESS_KEY"),
        secret_key=_secret(values, "MASK_CONTENT_S3_SECRET_KEY"),
        bucket=bucket,
        region=region,
        host=host,
        port=port,
    )
