"""Physical identity of managed storage locations."""

from __future__ import annotations

import hashlib
import json
import uuid
from urllib.parse import urlsplit

LEGACY_LOCATION_ID = uuid.uuid5(uuid.NAMESPACE_URL, "aap:platform-storage:legacy")


def managed_storage_identity(endpoint_url, bucket):
    """Match the existing full-cache physical identity recipe."""
    return hashlib.sha256(
        json.dumps([endpoint_url.rstrip("/"), bucket]).encode()
    ).hexdigest()


def plain(value, maximum):
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and value.isascii()
        and not any(ord(char) <= 32 or ord(char) == 127 for char in value)
    )


def parse_endpoint(value):
    if not plain(value, 2048) or value.endswith("/") or "\\" in value:
        raise ValueError("Invalid storage endpoint")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid storage endpoint")
    parsed.port
    return value


def group_lock_identity(bucket, object_key):
    return json.dumps(
        ["managed-original-retention", bucket, object_key], separators=(",", ":")
    )
