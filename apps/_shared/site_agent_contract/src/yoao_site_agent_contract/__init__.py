"""Strict, dependency-free messages for read-only site agents."""

from .messages import (
    MAX_CONTENT_BYTES,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    canonical_json,
    digest,
    parse_json,
    validate_ack,
    validate_command,
    validate_result,
)

__all__ = [
    "MAX_CONTENT_BYTES",
    "MAX_MESSAGE_BYTES",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "canonical_json",
    "digest",
    "parse_json",
    "validate_ack",
    "validate_command",
    "validate_result",
]
