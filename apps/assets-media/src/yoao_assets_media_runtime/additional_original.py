"""Explicit, read-only transport for one additional managed-original store."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit

_PREFIX = "AAP_ASSETS_ADDITIONAL_ORIGINAL_"
_BUCKET = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)


@dataclass(frozen=True, slots=True)
class AdditionalOriginalConfig:
    endpoint: str
    bucket: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)


def _secret(values: Mapping[str, str], name: str) -> str:
    direct, path = values.get(name), values.get(name + "_FILE")
    if direct is not None and path is not None:
        raise ValueError(f"Set only one of {name} and {name}_FILE")
    if path is not None:
        if not path.strip():
            raise ValueError(f"Configured secret file {name}_FILE is empty")
        try:
            direct = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"Cannot read configured secret {name}_FILE") from exc
    if not direct or not direct.strip() or any(char in direct for char in "\r\n\x00"):
        raise ValueError(f"Configured secret {name} is empty or invalid")
    return direct


def load_additional_original_config(
    values: Mapping[str, str] | None = None,
) -> AdditionalOriginalConfig | None:
    values = os.environ if values is None else values
    names = (
        _PREFIX + "ENDPOINT",
        _PREFIX + "BUCKET",
        _PREFIX + "ACCESS_KEY",
        _PREFIX + "SECRET_KEY",
        _PREFIX + "ACCESS_KEY_FILE",
        _PREFIX + "SECRET_KEY_FILE",
    )
    if not any(name in values for name in names):
        return None
    endpoint = values.get(_PREFIX + "ENDPOINT", "").strip()
    bucket = values.get(_PREFIX + "BUCKET", "").strip()
    try:
        parsed = urlsplit(endpoint)
        _ = parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and not any(
                char.isspace() or ord(char) < 32 or char == "\\" for char in endpoint
            )
        )
    except ValueError:
        valid = False
    if not valid or not _BUCKET.fullmatch(bucket) or len(bucket) > 63:
        raise ValueError("Additional original target endpoint or bucket is invalid")
    return AdditionalOriginalConfig(
        endpoint=endpoint.rstrip("/"),
        bucket=bucket,
        access_key=_secret(values, _PREFIX + "ACCESS_KEY"),
        secret_key=_secret(values, _PREFIX + "SECRET_KEY"),
    )


def physical_identity(endpoint: str, bucket: str) -> str:
    return hashlib.sha256(
        json.dumps([endpoint.rstrip("/"), bucket]).encode()
    ).hexdigest()


class AdditionalOriginalTarget:
    def __init__(
        self,
        config: AdditionalOriginalConfig,
        *,
        region: str,
        connect_timeout: float,
        read_timeout: float,
        max_attempts: int,
        client=None,
    ):
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=config.endpoint,
                aws_access_key_id=config.access_key,
                aws_secret_access_key=config.secret_key,
                region_name=region,
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    retries={"mode": "standard", "total_max_attempts": max_attempts},
                    s3={"addressing_style": "path"},
                ),
            )
        self.endpoint = config.endpoint
        self.bucket = config.bucket
        self.identity = physical_identity(config.endpoint, config.bucket)
        self.client = client

    def matches(self, permit) -> bool:
        actual = getattr(getattr(self.client, "meta", None), "endpoint_url", None)
        return (
            isinstance(actual, str)
            and actual.rstrip("/") == self.endpoint
            and permit.endpoint_url.rstrip("/") == self.endpoint
            and permit.bucket == self.bucket
            and permit.storage_identity == self.identity
        )

    def close(self) -> None:
        client, self.client = self.client, None
        close = getattr(client, "close", None)
        if callable(close):
            close()
