"""Portable connector primitives shared by API adapters and import workers."""

from .base import (
    SourceAdapter,
    SourceChangedError,
    SourceNotFoundError,
    SourceObject,
    SourcePathError,
)
from .crypto import (
    ConnectorCryptoError,
    ConnectorCryptoNotConfigured,
    decrypt_secret,
    encrypt_secret,
)
from .host_guard import (
    ConnectorAllowlistInvalid,
    ConnectorHostDenied,
    assert_host_allowed,
    extract_host,
    normalize_allowlist,
)
from .registry import build_source_adapter, target_host, validate_source_path

__all__ = [
    "ConnectorAllowlistInvalid",
    "ConnectorCryptoError",
    "ConnectorCryptoNotConfigured",
    "ConnectorHostDenied",
    "SourceAdapter",
    "SourceChangedError",
    "SourceNotFoundError",
    "SourceObject",
    "SourcePathError",
    "assert_host_allowed",
    "build_source_adapter",
    "extract_host",
    "decrypt_secret",
    "encrypt_secret",
    "normalize_allowlist",
    "target_host",
    "validate_source_path",
]
