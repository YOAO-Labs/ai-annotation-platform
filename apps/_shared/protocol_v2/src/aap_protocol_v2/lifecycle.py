"""Managed GPU lifecycle wire models and admission-token codec.

This module is deliberately framework-neutral.  It defines the wire contract shared by
the platform and GPU backends, but does not implement backend locks, model-pool state,
request accounting, or transition replay storage.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from enum import Enum
from typing import Annotated, Literal, Mapping, Sequence

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

GPU_GENERATION_HEADER = "X-AAP-GPU-Generation"
GPU_ADMISSION_TOKEN_HEADER = "X-AAP-GPU-Admission-Token"
GPU_HEALTH_CHALLENGE_HEADER = "X-AAP-GPU-Health-Challenge"
GPU_HEALTH_CHALLENGE_QUERY_PARAM = "aap_gpu_health_challenge"
GPU_LIFECYCLE_AUDIENCE = "aap-gpu-lifecycle"
GPU_ADMISSION_TOKEN_TYPE = "aap-gpu+jwt"
GPU_ADMISSION_TOKEN_ALGORITHM = "EdDSA"
MANAGED_LIFECYCLE_PROTOCOL_VERSION = "1"

MAX_POSITIVE_INT64 = 9_223_372_036_854_775_807
_CANONICAL_POSITIVE_INT64_RE = re.compile(r"[1-9][0-9]{0,18}\Z")
_GPU_HEALTH_CHALLENGE_RE = re.compile(r"[0-9a-f]{64}\Z")
_KEY_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_RAW_ED25519_PUBLIC_KEY_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def validate_canonical_positive_int64(value: str) -> str:
    """Validate the canonical JSON/header representation of a positive int64."""

    if not isinstance(value, str):
        raise ValueError("must be a decimal string")
    if _CANONICAL_POSITIVE_INT64_RE.fullmatch(value) is None:
        raise ValueError("must be a canonical positive int64 decimal string")
    if int(value) > MAX_POSITIVE_INT64:
        raise ValueError("must not exceed signed int64 max")
    return value


def validate_gpu_health_challenge(value: str) -> str:
    """Validate the canonical nonce used to bind one live GPU health response."""

    if not isinstance(value, str) or _GPU_HEALTH_CHALLENGE_RE.fullmatch(value) is None:
        raise ValueError("must be exactly 64 lowercase hexadecimal characters")
    return value


def match_gpu_health_challenge(
    header_values: Sequence[str],
    query_values: Sequence[str],
) -> str | None:
    """Return a challenge only when header and query contain one exact valid value."""

    if len(header_values) != 1 or len(query_values) != 1:
        return None
    challenge = header_values[0]
    if challenge != query_values[0]:
        return None
    try:
        return validate_gpu_health_challenge(challenge)
    except ValueError:
        return None


def parse_gpu_admission_header_values(
    generation_values: Sequence[str],
    token_values: Sequence[str],
) -> tuple[str, str] | None:
    """Require workload lifecycle headers to be absent together or unique together."""

    if not generation_values and not token_values:
        return None
    if len(generation_values) != 1 or len(token_values) != 1:
        raise ValueError("managed lifecycle headers must appear exactly once together")
    return generation_values[0], token_values[0]


def parse_gpu_control_token_header_values(
    generation_values: Sequence[str],
    token_values: Sequence[str],
) -> str:
    """Require one control token and forbid workload generation headers."""

    if generation_values or len(token_values) != 1:
        raise ValueError("control lifecycle requests require exactly one token only")
    return token_values[0]


CanonicalPositiveInt64String = Annotated[
    str,
    AfterValidator(validate_canonical_positive_int64),
]


class LifecycleState(str, Enum):
    UNLOADED = "unloaded"
    LOADING = "loading"
    RESIDENT = "resident"
    DRAINING = "draining"
    UNLOADING = "unloading"
    UNKNOWN = "unknown"


class LifecycleGate(str, Enum):
    LEGACY = "legacy"
    ENFORCE = "enforce"


class AdmissionScope(str, Enum):
    PREDICT = "predict"
    WARMUP = "warmup"
    RELOAD = "reload"
    DRAIN = "drain"
    UNLOAD = "unload"
    RESUME = "resume"
    MODE = "mode"
    RESET = "reset"


WORKLOAD_ADMISSION_SCOPES = frozenset(
    {
        AdmissionScope.PREDICT,
        AdmissionScope.WARMUP,
        AdmissionScope.RELOAD,
    }
)
CONTROL_ADMISSION_SCOPES = frozenset(
    {
        AdmissionScope.DRAIN,
        AdmissionScope.UNLOAD,
        AdmissionScope.RESUME,
        AdmissionScope.MODE,
        AdmissionScope.RESET,
    }
)


class TerminalDrainRequest(BaseModel):
    """Identity-bound request to permanently drain a CPU backend process."""

    model_config = ConfigDict(extra="forbid")

    registry_id: str = Field(min_length=1, max_length=128)
    boot_id: str = Field(min_length=1, max_length=128)
    operation_id: str = Field(min_length=1, max_length=256)


class ManagedLifecycleCapabilities(BaseModel):
    """Additive ``/setup.managed_lifecycle`` capability declaration."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["1"] = MANAGED_LIFECYCLE_PROTOCOL_VERSION
    generation_fencing: Literal[True] = True
    drain_endpoint: Literal["/drain"] = "/drain"
    drain_cancel_endpoint: Literal["/drain/cancel"] = "/drain/cancel"
    unload_endpoint: Literal["/unload"] = "/unload"
    mode_endpoint: Literal["/lifecycle/mode"] = "/lifecycle/mode"
    reset_endpoint: Literal["/lifecycle/reset"] = "/lifecycle/reset"
    generation_header: Literal["X-AAP-GPU-Generation"] = GPU_GENERATION_HEADER
    token_header: Literal["X-AAP-GPU-Admission-Token"] = GPU_ADMISSION_TOKEN_HEADER


def canonical_managed_lifecycle_capabilities(value: object) -> dict[str, object]:
    """Strictly validate a remote ``/setup.managed_lifecycle`` declaration.

    Model defaults are useful when a backend publishes its own declaration, but a
    remote peer must send every frozen field explicitly.  Otherwise an old or
    partial backend could be upgraded accidentally by platform-side defaults.
    """

    expected_fields = set(ManagedLifecycleCapabilities.model_fields)
    if type(value) is not dict or set(value) != expected_fields:
        raise ValueError(
            "managed lifecycle capability must contain the exact protocol fields"
        )
    if type(value["generation_fencing"]) is not bool or any(
        type(value[field]) is not str
        for field in expected_fields - {"generation_fencing"}
    ):
        raise ValueError("managed lifecycle capability field types are invalid")
    capability = ManagedLifecycleCapabilities.model_validate(value, strict=True)
    return capability.model_dump(mode="json")


def managed_lifecycle_capability_sha256(value: object) -> str:
    """Hash one strict canonical capability for challenge-bound health proof."""

    canonical = canonical_managed_lifecycle_capabilities(value)
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class PoolResidency(BaseModel):
    """One stable pool's GPU residency; CPU-only handles report ``resident=false``."""

    model_config = ConfigDict(extra="forbid")

    resident: bool | None
    device: str | None
    provider: str | None


class BoundLifecycleIdentity(BaseModel):
    """Platform identity atomically bound to one backend boot lifecycle domain."""

    model_config = ConfigDict(extra="forbid")

    audience: Literal["aap-gpu-lifecycle"] = GPU_LIFECYCLE_AUDIENCE
    backend_registry_id: str = Field(min_length=1, max_length=128)
    gpu_resource_id: str = Field(min_length=1, max_length=512)


class BackendResidency(BaseModel):
    """Managed GPU residency snapshot returned under ``/health.residency``."""

    model_config = ConfigDict(extra="forbid")

    state: LifecycleState
    gpu_loaded: bool | None
    active_requests: int = Field(ge=0)
    builders: int = Field(ge=0)
    borrowers: int = Field(ge=0)
    draining: bool
    evictable: bool
    generation: CanonicalPositiveInt64String | None
    pools: dict[str, PoolResidency]
    boot_id: str = Field(min_length=1, max_length=128)
    lifecycle_gate: LifecycleGate
    control_epoch: CanonicalPositiveInt64String | None
    identity: BoundLifecycleIdentity | None

    @field_validator("pools")
    @classmethod
    def _validate_pool_ids(
        cls, value: dict[str, PoolResidency]
    ) -> dict[str, PoolResidency]:
        if any(not pool_id or pool_id.strip() != pool_id for pool_id in value):
            raise ValueError("pool ids must be non-empty and canonical")
        return value

    @model_validator(mode="after")
    def _validate_snapshot_consistency(self) -> "BackendResidency":
        if self.gpu_loaded is False:
            if self.builders != 0 or self.borrowers != 0:
                raise ValueError(
                    "gpu_loaded=false requires zero builders and borrowers"
                )
            if any(pool.resident is not False for pool in self.pools.values()):
                raise ValueError(
                    "gpu_loaded=false requires every pool residency to be explicitly false"
                )
        if self.evictable:
            if self.generation is None or self.identity is None:
                raise ValueError(
                    "evictable residency requires managed generation and identity"
                )
            if self.lifecycle_gate is not LifecycleGate.ENFORCE:
                raise ValueError("evictable residency requires enforce lifecycle gate")
        return self


class GenerationTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation: CanonicalPositiveInt64String


class LifecycleModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate: LifecycleGate
    control_epoch: CanonicalPositiveInt64String


class LifecycleResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    control_epoch: CanonicalPositiveInt64String


class LifecycleModeResponse(BaseModel):
    """Confirmed backend gate and control epoch after a mode handshake."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    gate: LifecycleGate
    control_epoch: CanonicalPositiveInt64String
    residency: BackendResidency

    @model_validator(mode="after")
    def _validate_residency(self) -> "LifecycleModeResponse":
        if self.residency.lifecycle_gate is not self.gate:
            raise ValueError("response and residency lifecycle gate must match")
        if self.residency.control_epoch != self.control_epoch:
            raise ValueError("response and residency control epoch must match")
        return self


_LIFECYCLE_MODE_RESPONSE_KEYS = frozenset({"ok", "gate", "control_epoch", "residency"})
_LIFECYCLE_RESET_RESPONSE_KEYS = frozenset(
    {"ok", "control_epoch", "unloaded", "unloaded_count", "residency"}
)
_DRAIN_TRANSITION_RESPONSE_KEYS = frozenset(
    {
        "ok",
        "generation",
        "draining",
        "active_requests",
        "ready_to_unload",
        "residency",
    }
)
_MANAGED_UNLOAD_RESPONSE_KEYS = frozenset(
    {"ok", "generation", "unloaded", "unloaded_count", "residency"}
)
_BACKEND_RESIDENCY_KEYS = frozenset(
    {
        "state",
        "gpu_loaded",
        "active_requests",
        "builders",
        "borrowers",
        "draining",
        "evictable",
        "generation",
        "pools",
        "boot_id",
        "lifecycle_gate",
        "control_epoch",
        "identity",
    }
)
_POOL_RESIDENCY_KEYS = frozenset({"resident", "device", "provider"})
_BOUND_LIFECYCLE_IDENTITY_KEYS = frozenset(
    {"audience", "backend_registry_id", "gpu_resource_id"}
)


def _require_exact_json_object(
    value: object,
    expected_keys: frozenset[str],
    *,
    field: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected_keys:
        raise ValueError(f"{field} must contain the exact protocol fields")
    return value


def _parse_managed_response_payload(
    raw: str | bytes,
    *,
    response_name: str,
    response_keys: frozenset[str],
) -> dict[str, object]:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{response_name} must be UTF-8 JSON") from None
    elif not isinstance(raw, str):
        raise ValueError(f"{response_name} must be JSON text")

    class _DuplicateField(ValueError):
        pass

    def _reject_duplicates(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise _DuplicateField
            value[key] = item
        return value

    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicates)
    except (TypeError, json.JSONDecodeError, _DuplicateField):
        raise ValueError(f"{response_name} must be a JSON object") from None

    response = _require_exact_json_object(
        payload,
        response_keys,
        field=response_name,
    )
    if response["ok"] is not True:
        raise ValueError(f"{response_name} must acknowledge success")
    residency = _require_exact_json_object(
        response["residency"],
        _BACKEND_RESIDENCY_KEYS,
        field=f"{response_name} residency",
    )
    pools = residency["pools"]
    if type(pools) is not dict or not pools:
        raise ValueError(
            f"{response_name} residency pools must be a non-empty JSON object"
        )
    for pool_id, pool in pools.items():
        if type(pool_id) is not str:
            raise ValueError(f"{response_name} residency pool ids must be strings")
        _require_exact_json_object(
            pool,
            _POOL_RESIDENCY_KEYS,
            field=f"{response_name} pool residency",
        )
    identity = residency["identity"]
    if identity is None:
        raise ValueError(f"{response_name} residency identity must be bound")
    _require_exact_json_object(
        identity,
        _BOUND_LIFECYCLE_IDENTITY_KEYS,
        field=f"{response_name} residency identity",
    )
    return response


def parse_lifecycle_mode_response_json(raw: str | bytes) -> LifecycleModeResponse:
    """Strictly parse one untrusted remote ``/lifecycle/mode`` response.

    The local wire models retain construction defaults for backend authors.  A
    platform consumer must not let those defaults, JSON type coercion, or duplicate
    object keys manufacture a successful acknowledgement from a partial response.
    """

    response = _parse_managed_response_payload(
        raw,
        response_name="lifecycle mode response",
        response_keys=_LIFECYCLE_MODE_RESPONSE_KEYS,
    )

    try:
        canonical = json.dumps(
            response,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        return LifecycleModeResponse.model_validate_json(canonical, strict=True)
    except (TypeError, ValueError):
        raise ValueError("lifecycle mode response is invalid") from None


class LifecycleResetResponse(BaseModel):
    """Result of a signed full-pool reset in the legacy gate."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    control_epoch: CanonicalPositiveInt64String
    unloaded: bool
    unloaded_count: int = Field(ge=0)
    residency: BackendResidency

    @model_validator(mode="after")
    def _validate_residency(self) -> "LifecycleResetResponse":
        if self.residency.control_epoch != self.control_epoch:
            raise ValueError("response and residency control epoch must match")
        if self.unloaded:
            if self.residency.generation is not None:
                raise ValueError("successful reset must clear managed generation")
            if self.residency.active_requests != 0:
                raise ValueError("successful reset requires zero active requests")
            if (
                self.residency.state is not LifecycleState.UNLOADED
                or self.residency.gpu_loaded is not False
            ):
                raise ValueError("successful reset requires trusted unloaded residency")
        return self


def parse_lifecycle_reset_response_json(raw: str | bytes) -> LifecycleResetResponse:
    """Strictly parse one untrusted remote ``/lifecycle/reset`` response."""

    response = _parse_managed_response_payload(
        raw,
        response_name="lifecycle reset response",
        response_keys=_LIFECYCLE_RESET_RESPONSE_KEYS,
    )
    try:
        canonical = json.dumps(
            response,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        return LifecycleResetResponse.model_validate_json(canonical, strict=True)
    except (TypeError, ValueError):
        raise ValueError("lifecycle reset response is invalid") from None


class DrainTransitionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    generation: CanonicalPositiveInt64String
    draining: bool
    active_requests: int = Field(ge=0)
    ready_to_unload: bool
    residency: BackendResidency

    @model_validator(mode="after")
    def _validate_residency(self) -> "DrainTransitionResponse":
        if self.residency.generation != self.generation:
            raise ValueError("response and residency generation must match")
        if (
            self.residency.lifecycle_gate is not LifecycleGate.ENFORCE
            or self.residency.control_epoch is None
            or self.residency.identity is None
        ):
            raise ValueError("managed drain requires bound enforce residency")
        if self.residency.draining != self.draining:
            raise ValueError("response and residency draining state must match")
        if self.residency.active_requests != self.active_requests:
            raise ValueError("response and residency active request count must match")
        if self.draining and self.residency.state is not LifecycleState.DRAINING:
            raise ValueError("drain state must match the transition result")
        if not self.draining and self.residency.state in {
            LifecycleState.DRAINING,
            LifecycleState.UNLOADING,
        }:
            raise ValueError("drain state must match the transition result")
        ready = (
            self.draining
            and self.active_requests == 0
            and self.residency.builders == 0
            and self.residency.borrowers == 0
        )
        if self.ready_to_unload != ready:
            raise ValueError(
                "ready_to_unload must reflect active, builder, and borrower state"
            )
        return self


class ManagedUnloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    generation: CanonicalPositiveInt64String
    unloaded: bool
    unloaded_count: int = Field(ge=0)
    residency: BackendResidency

    @model_validator(mode="after")
    def _validate_residency(self) -> "ManagedUnloadResponse":
        if self.residency.generation != self.generation:
            raise ValueError("response and residency generation must match")
        if (
            self.residency.lifecycle_gate is not LifecycleGate.ENFORCE
            or self.residency.control_epoch is None
            or self.residency.identity is None
        ):
            raise ValueError("managed unload requires bound enforce residency")
        if self.unloaded:
            if self.residency.active_requests != 0:
                raise ValueError("successful unload requires zero active requests")
            if self.residency.draining or self.residency.evictable:
                raise ValueError(
                    "successful unload must close draining and eviction state"
                )
            if (
                self.residency.state is not LifecycleState.UNLOADED
                or self.residency.gpu_loaded is not False
            ):
                raise ValueError(
                    "successful unload requires trusted unloaded residency"
                )
        return self


def parse_drain_transition_response_json(
    raw: str | bytes,
) -> DrainTransitionResponse:
    """Strictly parse one untrusted remote drain or drain-cancel response."""

    response = _parse_managed_response_payload(
        raw,
        response_name="drain transition response",
        response_keys=_DRAIN_TRANSITION_RESPONSE_KEYS,
    )
    try:
        canonical = json.dumps(
            response,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        return DrainTransitionResponse.model_validate_json(canonical, strict=True)
    except (TypeError, ValueError):
        raise ValueError("drain transition response is invalid") from None


def parse_managed_unload_response_json(raw: str | bytes) -> ManagedUnloadResponse:
    """Strictly parse one untrusted remote managed-unload response."""

    response = _parse_managed_response_payload(
        raw,
        response_name="managed unload response",
        response_keys=_MANAGED_UNLOAD_RESPONSE_KEYS,
    )
    try:
        canonical = json.dumps(
            response,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        return ManagedUnloadResponse.model_validate_json(canonical, strict=True)
    except (TypeError, ValueError):
        raise ValueError("managed unload response is invalid") from None


class AdmissionTokenClaims(BaseModel):
    """Claims after EdDSA verification and before backend-local replay checks."""

    model_config = ConfigDict(extra="forbid")

    aud: Literal["aap-gpu-lifecycle"] = GPU_LIFECYCLE_AUDIENCE
    backend_registry_id: str = Field(min_length=1, max_length=128)
    gpu_resource_id: str = Field(min_length=1, max_length=512)
    boot_id: str = Field(min_length=1, max_length=128)
    generation: CanonicalPositiveInt64String | None = None
    control_epoch: CanonicalPositiveInt64String
    scope: AdmissionScope
    jti: str = Field(min_length=1, max_length=256)
    exp: StrictInt = Field(gt=0)
    owner: str | None = Field(default=None, min_length=1, max_length=256)
    operation: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _validate_scope_shape(self) -> "AdmissionTokenClaims":
        if self.scope in {AdmissionScope.MODE, AdmissionScope.RESET}:
            if self.generation is not None:
                raise ValueError("mode/reset claims must not carry generation")
        elif self.generation is None:
            raise ValueError("workload/transition claims require generation")

        if self.scope in CONTROL_ADMISSION_SCOPES:
            if self.owner is None or self.operation is None:
                raise ValueError(
                    "control/transition claims require owner and operation"
                )
        elif self.owner is not None or self.operation is not None:
            raise ValueError("workload claims must not carry owner or operation")
        return self


class AdmissionTokenError(ValueError):
    """The compact token, key id, signature, expiry, or claim shape is invalid."""


def encode_ed25519_public_key(public_key: Ed25519PublicKey) -> str:
    """Encode a raw Ed25519 public key as unpadded base64url for keyring config."""

    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def load_verify_keyring(raw_json: str) -> dict[str, Ed25519PublicKey]:
    """Load ``kid -> raw-public-key-base64url`` JSON used by backend verifiers."""

    def _reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("verify keyring contains duplicate key ids")
            value[key] = item
        return value

    try:
        payload = json.loads(raw_json, object_pairs_hook=_reject_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("verify keyring must be a JSON object") from exc
    if not isinstance(payload, dict) or not payload:
        raise ValueError("verify keyring must be a non-empty JSON object")

    keyring: dict[str, Ed25519PublicKey] = {}
    for kid, encoded_key in payload.items():
        if not isinstance(kid, str) or _KEY_ID_RE.fullmatch(kid) is None:
            raise ValueError("verify key ids must match [A-Za-z0-9._-]{1,64}")
        if (
            not isinstance(encoded_key, str)
            or _RAW_ED25519_PUBLIC_KEY_RE.fullmatch(encoded_key) is None
        ):
            raise ValueError(
                f"verify key {kid!r} must be an unpadded base64url Ed25519 key"
            )
        padded = encoded_key + "=" * (-len(encoded_key) % 4)
        try:
            key_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
            keyring[kid] = Ed25519PublicKey.from_public_bytes(key_bytes)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"verify key {kid!r} is invalid") from exc
    return keyring


def sign_admission_token(
    claims: AdmissionTokenClaims,
    *,
    private_key: Ed25519PrivateKey,
    kid: str,
) -> str:
    """Create the compact EdDSA JWS consumed through the dedicated token header."""

    if not isinstance(kid, str) or _KEY_ID_RE.fullmatch(kid) is None:
        raise ValueError("kid must match [A-Za-z0-9._-]{1,64}")
    return jwt.encode(
        claims.model_dump(mode="json", exclude_none=True),
        private_key,
        algorithm=GPU_ADMISSION_TOKEN_ALGORITHM,
        headers={"kid": kid, "typ": GPU_ADMISSION_TOKEN_TYPE},
    )


def verify_admission_token(
    token: str,
    *,
    keyring: Mapping[str, Ed25519PublicKey],
    leeway_seconds: int = 0,
) -> AdmissionTokenClaims:
    """Verify algorithm, key id, signature, audience, expiry, and claim shape."""

    if not token or not isinstance(token, str):
        raise AdmissionTokenError("admission token is missing")
    if (
        not isinstance(leeway_seconds, int)
        or isinstance(leeway_seconds, bool)
        or leeway_seconds < 0
    ):
        raise ValueError("leeway_seconds must be a non-negative integer")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise AdmissionTokenError("admission token header is invalid") from exc

    if set(header) != {"alg", "typ", "kid"}:
        raise AdmissionTokenError("admission token header fields are invalid")
    if header.get("alg") != GPU_ADMISSION_TOKEN_ALGORITHM:
        raise AdmissionTokenError("admission token algorithm is invalid")
    if header.get("typ") != GPU_ADMISSION_TOKEN_TYPE:
        raise AdmissionTokenError("admission token type is invalid")
    kid = header.get("kid")
    if not isinstance(kid, str) or _KEY_ID_RE.fullmatch(kid) is None:
        raise AdmissionTokenError("admission token kid is invalid")
    public_key = keyring.get(kid)
    if public_key is None:
        raise AdmissionTokenError("admission token kid is unknown")

    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=[GPU_ADMISSION_TOKEN_ALGORITHM],
            audience=GPU_LIFECYCLE_AUDIENCE,
            leeway=leeway_seconds,
            options={
                "require": [
                    "aud",
                    "backend_registry_id",
                    "gpu_resource_id",
                    "boot_id",
                    "control_epoch",
                    "scope",
                    "jti",
                    "exp",
                ],
            },
        )
        return AdmissionTokenClaims.model_validate(payload)
    except (jwt.PyJWTError, ValueError, TypeError, OverflowError) as exc:
        raise AdmissionTokenError("admission token verification failed") from exc


__all__ = [
    "AdmissionScope",
    "AdmissionTokenClaims",
    "AdmissionTokenError",
    "BackendResidency",
    "BoundLifecycleIdentity",
    "CONTROL_ADMISSION_SCOPES",
    "CanonicalPositiveInt64String",
    "DrainTransitionResponse",
    "GPU_ADMISSION_TOKEN_ALGORITHM",
    "GPU_ADMISSION_TOKEN_HEADER",
    "GPU_ADMISSION_TOKEN_TYPE",
    "GPU_GENERATION_HEADER",
    "GPU_HEALTH_CHALLENGE_HEADER",
    "GPU_HEALTH_CHALLENGE_QUERY_PARAM",
    "GPU_LIFECYCLE_AUDIENCE",
    "GenerationTransitionRequest",
    "LifecycleGate",
    "LifecycleModeRequest",
    "LifecycleModeResponse",
    "LifecycleResetRequest",
    "LifecycleResetResponse",
    "LifecycleState",
    "MANAGED_LIFECYCLE_PROTOCOL_VERSION",
    "MAX_POSITIVE_INT64",
    "ManagedLifecycleCapabilities",
    "ManagedUnloadResponse",
    "PoolResidency",
    "WORKLOAD_ADMISSION_SCOPES",
    "canonical_managed_lifecycle_capabilities",
    "encode_ed25519_public_key",
    "load_verify_keyring",
    "match_gpu_health_challenge",
    "managed_lifecycle_capability_sha256",
    "parse_lifecycle_mode_response_json",
    "parse_lifecycle_reset_response_json",
    "parse_drain_transition_response_json",
    "parse_gpu_admission_header_values",
    "parse_gpu_control_token_header_values",
    "parse_managed_unload_response_json",
    "sign_admission_token",
    "validate_canonical_positive_int64",
    "validate_gpu_health_challenge",
    "verify_admission_token",
]
