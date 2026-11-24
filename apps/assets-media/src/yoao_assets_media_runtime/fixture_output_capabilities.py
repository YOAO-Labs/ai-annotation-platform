"""Short-lived capabilities for exact fixture-native output reads."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlencode, urlsplit
import uuid

_DOMAIN = b"aap-fixture-native-output-read/v1\0"
_KEY_DOMAIN = b"aap-fixture-native-output-key/v1\0"
_READY_DOMAIN = b"aap-fixture-native-output-ready/v1\0"
_MAX_SECONDS = 604800


def _key(selector_name, *, exact_length=None):
    selector = os.environ.get(selector_name)
    if not selector or not Path(selector).is_absolute():
        raise RuntimeError("fixture native output capability is unavailable")
    try:
        descriptor = os.open(selector, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError("invalid capability key")
            value = stream.read(4097)
        if (
            not 32 <= len(value) <= 4096
            or exact_length is not None
            and len(value) != exact_length
        ):
            raise ValueError("invalid capability key")
        return value
    except (OSError, ValueError):
        raise RuntimeError("fixture native output capability is unavailable") from None


def _signature(key, run_id, write_id, expires):
    message = _DOMAIN + run_id.bytes + write_id.bytes + expires.to_bytes(8, "big")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _validate_issuer(run_id, expires_in, origin):
    if (
        not isinstance(run_id, uuid.UUID)
        or type(expires_in) is not int
        or not 0 < expires_in <= _MAX_SECONDS
    ):
        raise ValueError("invalid fixture native output capability")
    try:
        parsed = urlsplit(origin)
        parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid origin")
    except (TypeError, ValueError, AttributeError):
        raise RuntimeError("fixture native output proxy is unavailable") from None


def issue(run_id, write_id, expires_in, origin):
    _validate_issuer(run_id, expires_in, origin)
    if not isinstance(write_id, uuid.UUID):
        raise ValueError("invalid fixture native output capability")
    expires = int(time.time()) + expires_in
    master = _key("AAP_PRIVATE_FIXTURE_MEDIA_PROXY_SIGNING_KEY_FILE")
    run_key = hmac.new(master, _KEY_DOMAIN + run_id.bytes, hashlib.sha256).digest()
    query = urlencode(
        {
            "expires": expires,
            "signature": _signature(run_key, run_id, write_id, expires),
        }
    )
    return f"{origin}/v1/fixture-native-output/{run_id}/{write_id}?{query}"


def issue_ready(run_id, expires_in, origin, *, run_key):
    _validate_issuer(run_id, expires_in, origin)
    if not isinstance(run_key, bytes) or len(run_key) != 32:
        raise ValueError("invalid fixture native readiness key")
    expires = int(time.time()) + expires_in
    signature = hmac.new(
        run_key,
        _READY_DOMAIN + run_id.bytes + expires.to_bytes(8, "big"),
        hashlib.sha256,
    ).hexdigest()
    return f"{origin}/v1/fixture-native-output-ready/{run_id}?{urlencode({'expires': expires, 'signature': signature})}"


def verify_ready(run_id, expires, signature):
    if (
        not isinstance(run_id, uuid.UUID)
        or type(expires) is not int
        or not isinstance(signature, str)
        or len(signature) != 64
        or expires <= int(time.time())
        or expires > int(time.time()) + _MAX_SECONDS
    ):
        return False
    key = _key("AAP_PRIVATE_FIXTURE_MEDIA_PROXY_VERIFY_KEY_FILE", exact_length=32)
    expected = hmac.new(
        key, _READY_DOMAIN + run_id.bytes + expires.to_bytes(8, "big"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify(run_id, write_id, expires, signature):
    if (
        not isinstance(run_id, uuid.UUID)
        or not isinstance(write_id, uuid.UUID)
        or type(expires) is not int
        or not isinstance(signature, str)
        or len(signature) != 64
        or expires <= int(time.time())
        or expires > int(time.time()) + _MAX_SECONDS
    ):
        return False
    key = _key("AAP_PRIVATE_FIXTURE_MEDIA_PROXY_VERIFY_KEY_FILE", exact_length=32)
    return hmac.compare_digest(_signature(key, run_id, write_id, expires), signature)
