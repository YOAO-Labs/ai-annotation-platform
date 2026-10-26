"""Platform-only Ed25519 signer for managed GPU lifecycle admission tokens.

Moved verbatim from the legacy flat module ``gpu_admission_signer.py``; this is a
cycle-safe leaf (no ``ml_client`` / orchestration imports) consumed by the dispatch,
membership and rollout-control orchestration modules.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
from pathlib import Path
import re

from aap_protocol_v2.lifecycle import AdmissionTokenClaims, sign_admission_token
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aap_inference.gpu.settings import GPUAuthoritySettings as Settings


_KEY_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_RAW_ED25519_PRIVATE_KEY_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_MAX_SIGNING_KEY_FILE_BYTES = 64 * 1024
_MAX_SIGNING_KEYS = 64


class GPUAdmissionSignerConfigError(ValueError):
    """The platform signer secret is absent, unreadable, or malformed."""


def _json_without_duplicate_keys(raw: str) -> object:
    def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise GPUAdmissionSignerConfigError(
                    "GPU lifecycle signing key file contains duplicate key ids"
                )
            value[key] = item
        return value

    try:
        return json.loads(raw, object_pairs_hook=_object_pairs)
    except GPUAdmissionSignerConfigError:
        raise
    except (TypeError, json.JSONDecodeError):
        raise GPUAdmissionSignerConfigError(
            "GPU lifecycle signing key file must contain a JSON object"
        ) from None


def _decode_private_key(kid: str, encoded: object) -> Ed25519PrivateKey:
    if (
        not isinstance(encoded, str)
        or _RAW_ED25519_PRIVATE_KEY_RE.fullmatch(encoded) is None
    ):
        raise GPUAdmissionSignerConfigError(
            f"GPU lifecycle signing key {kid!r} must be an unpadded "
            "base64url Ed25519 private seed"
        )
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (TypeError, ValueError) as exc:
        raise GPUAdmissionSignerConfigError(
            f"GPU lifecycle signing key {kid!r} is invalid"
        ) from exc
    canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(raw) != 32 or canonical != encoded:
        raise GPUAdmissionSignerConfigError(
            f"GPU lifecycle signing key {kid!r} is invalid"
        )
    try:
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError as exc:
        raise GPUAdmissionSignerConfigError(
            f"GPU lifecycle signing key {kid!r} is invalid"
        ) from exc


@dataclass(frozen=True)
class GPUAdmissionTokenSigner:
    """One process-local active signing key loaded explicitly by GPU authority."""

    active_kid: str
    _private_key: Ed25519PrivateKey = field(repr=False)

    @classmethod
    def from_file(cls, path: str, active_kid: str) -> "GPUAdmissionTokenSigner":
        if not path or path != path.strip():
            raise GPUAdmissionSignerConfigError(
                "GPU lifecycle signing key file is not configured"
            )
        if not isinstance(active_kid, str) or _KEY_ID_RE.fullmatch(active_kid) is None:
            raise GPUAdmissionSignerConfigError(
                "GPU lifecycle active signing kid is missing or invalid"
            )

        try:
            with Path(path).open("rb") as stream:
                raw_bytes = stream.read(_MAX_SIGNING_KEY_FILE_BYTES + 1)
        except OSError as exc:
            raise GPUAdmissionSignerConfigError(
                "GPU lifecycle signing key file is unavailable"
            ) from exc
        if not raw_bytes:
            raise GPUAdmissionSignerConfigError(
                "GPU lifecycle signing key file is empty"
            )
        if len(raw_bytes) > _MAX_SIGNING_KEY_FILE_BYTES:
            raise GPUAdmissionSignerConfigError(
                "GPU lifecycle signing key file is too large"
            )
        try:
            raw_json = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise GPUAdmissionSignerConfigError(
                "GPU lifecycle signing key file must be UTF-8 JSON"
            ) from None

        payload = _json_without_duplicate_keys(raw_json)
        if not isinstance(payload, dict) or not payload:
            raise GPUAdmissionSignerConfigError(
                "GPU lifecycle signing key file must contain a non-empty JSON object"
            )
        if len(payload) > _MAX_SIGNING_KEYS:
            raise GPUAdmissionSignerConfigError(
                "GPU lifecycle signing key file contains too many keys"
            )

        keyring: dict[str, Ed25519PrivateKey] = {}
        for kid, encoded in payload.items():
            if not isinstance(kid, str) or _KEY_ID_RE.fullmatch(kid) is None:
                raise GPUAdmissionSignerConfigError(
                    "GPU lifecycle signing key ids must match [A-Za-z0-9._-]{1,64}"
                )
            keyring[kid] = _decode_private_key(kid, encoded)

        private_key = keyring.get(active_kid)
        if private_key is None:
            raise GPUAdmissionSignerConfigError(
                "GPU lifecycle active signing kid is not present in the key file"
            )
        return cls(active_kid=active_kid, _private_key=private_key)

    @classmethod
    def from_settings(cls, config: Settings) -> "GPUAdmissionTokenSigner":
        """Load lazily; constructing Settings never reads or decodes the secret."""

        return cls.from_file(
            config.gpu_lifecycle_signing_keys_file,
            config.gpu_lifecycle_active_signing_kid,
        )

    def sign(self, claims: AdmissionTokenClaims) -> str:
        return sign_admission_token(
            claims,
            private_key=self._private_key,
            kid=self.active_kid,
        )


__all__ = [
    "GPUAdmissionSignerConfigError",
    "GPUAdmissionTokenSigner",
]
