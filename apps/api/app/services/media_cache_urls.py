"""Short-lived exact-object capabilities for registered media-cache targets.

The caller has already authorized and verified the immutable output inventory.
These capabilities replace S3 presigning for the browser transport only.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import quote, urlencode
import uuid

from jose import JWTError, jwt

from app.config import settings

AUDIENCE = "aap-media-cache-read"
ISSUER = "aap-media-cache"
MAX_SECONDS = 3600
KINDS = frozenset({"thumbnail", "image-pyramid", "video-derivative"})
_SHA = re.compile(r"[0-9a-f]{64}")
_CLAIMS = frozenset(
    {
        "aud",
        "iss",
        "schema",
        "iat",
        "exp",
        "kind",
        "output_id",
        "relative",
        "reference",
        "sha256",
        "size_bytes",
    }
)


class MediaCacheCapabilityError(ValueError):
    def __init__(self):
        super().__init__("Media-cache capability is invalid or expired")


def _reject():
    raise MediaCacheCapabilityError()


def _identifier(value):
    if not isinstance(value, (str, uuid.UUID)):
        _reject()
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        _reject()
    if str(value) != str(parsed):
        _reject()
    return parsed


def _relative(value):
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or len(value) > 1024
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        or any(char in value for char in "\\?#%")
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        _reject()
    return value


def _instant(value):
    if value is None:
        value = datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        _reject()
    return int(value.timestamp())


def reference_digest(reference):
    """Bind an already validated reference without changing its serialized facts."""
    if not isinstance(reference, dict) or reference.get("schema") != 2:
        _reject()
    try:
        encoded = json.dumps(
            reference, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError, RecursionError):
        _reject()
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class ReadCapability:
    kind: str
    output_id: uuid.UUID
    relative: str
    reference: str
    sha256: str
    size_bytes: int
    expires_at: int


def sign(kind, output_id, key, storage_ref, receipt, expires, *, now=None):
    """Issue one browser URL only after the caller's owner/inventory checks."""
    identifier = _identifier(output_id)
    if kind not in KINDS or type(expires) is not int or not 0 < expires <= MAX_SECONDS:
        _reject()
    prefix = storage_ref.get("prefix") if isinstance(storage_ref, dict) else None
    if (
        not isinstance(prefix, str)
        or not prefix.endswith("/")
        or not isinstance(key, str)
        or not key.startswith(prefix)
        or not isinstance(receipt, dict)
        or not isinstance(receipt.get("sha256"), str)
        or _SHA.fullmatch(receipt["sha256"]) is None
        or type(receipt.get("size_bytes")) is not int
        or not 0 <= receipt["size_bytes"] <= 2**63 - 1
    ):
        _reject()
    relative = _relative(key[len(prefix) :])
    issued_at = _instant(now)
    token = jwt.encode(
        {
            "aud": AUDIENCE,
            "iss": ISSUER,
            "schema": 1,
            "iat": issued_at,
            "exp": issued_at + expires,
            "kind": kind,
            "output_id": str(identifier),
            "relative": relative,
            "reference": reference_digest(storage_ref),
            "sha256": receipt["sha256"],
            "size_bytes": receipt["size_bytes"],
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return (
        f"/api/v1/media-cache/{kind}/{identifier}/{quote(relative, safe='/')}?"
        + urlencode({"token": token})
    )


def decode(token, *, kind, output_id, relative, now=None):
    """Validate the exact route and first expiry, before looking up any output."""
    if not isinstance(token, str) or not 0 < len(token) <= 8192:
        _reject()
    try:
        claims = jwt.decode(
            token,
            settings.secret_key,
            algorithms=["HS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"verify_exp": False, "verify_iat": False},
        )
    except (JWTError, ValueError, TypeError):
        _reject()
    identifier = _identifier(output_id)
    if (
        not isinstance(claims, dict)
        or set(claims) != _CLAIMS
        or claims["aud"] != AUDIENCE
        or claims["iss"] != ISSUER
        or type(claims["schema"]) is not int
        or claims["schema"] != 1
        or kind not in KINDS
        or claims["kind"] != kind
        or claims["output_id"] != str(identifier)
        or claims["relative"] != _relative(relative)
        or type(claims["iat"]) is not int
        or type(claims["exp"]) is not int
        or not 0 < claims["exp"] - claims["iat"] <= MAX_SECONDS
        or claims["iat"] > _instant(now) + 5
        or claims["exp"] <= _instant(now)
        or not isinstance(claims["reference"], str)
        or _SHA.fullmatch(claims["reference"]) is None
        or not isinstance(claims["sha256"], str)
        or _SHA.fullmatch(claims["sha256"]) is None
        or type(claims["size_bytes"]) is not int
        or not 0 <= claims["size_bytes"] <= 2**63 - 1
    ):
        _reject()
    return ReadCapability(
        kind,
        identifier,
        relative,
        claims["reference"],
        claims["sha256"],
        claims["size_bytes"],
        claims["exp"],
    )
