"""Closed wire/native contracts for Identity-owned login and session commands."""

from __future__ import annotations

import base64
import ipaddress
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .contract import ClosedModel, FailureCode, IdentityUnavailable, Principal
from .security import ALGORITHM, ISSUER

SESSION_PROTOCOL = "aap-identity-sessions/v1"
NATIVE_SESSION_PROTOCOL = "aap-identity-sessions-native/v1"
SESSION_POLICY_PROTOCOL = "aap-identity-sessions-policy/v1"
LOGIN_CANDIDATE_PROTOCOL = "aap-identity-login-candidate/v1"
SESSION_DERIVATION = "aap-identity-sessions/v1"
_DERIVATION_PATTERN = r"^[0-9a-f]{64}$"
_FINGERPRINT_PATTERN = r"^[0-9a-f]{64}$"

SessionOperation = Literal["login", "refresh", "logout", "logout_all"]
SessionFailureCode: TypeAlias = (
    FailureCode
    | Literal[
        "invalid_credentials",
        "email_not_verified",
        "captcha_required",
        "invalid_token",
        "malformed_token",
        "operation_conflict",
    ]
)


def _json_bytes(payload: dict | bytes) -> bytes:
    if isinstance(payload, bytes):
        json.loads(payload, object_pairs_hook=_unique_object)
        return payload
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


class SessionAuditContext(ClosedModel):
    method: str | None = Field(max_length=8, strict=True)
    path: str | None = Field(max_length=256, strict=True)
    ip: str | None = Field(max_length=64, strict=True)
    request_id: str | None = Field(max_length=36, strict=True)

    @field_validator("ip")
    @classmethod
    def valid_ip(cls, value: str | None) -> str | None:
        if value:
            try:
                ipaddress.ip_address(value)
            except ValueError:
                raise ValueError("Audit IP must be an IP address") from None
        return value


class SessionEmptyPayload(ClosedModel):
    pass


class LoginPayload(ClosedModel):
    email: str = Field(max_length=16384, strict=True)
    password: str = Field(max_length=16384, strict=True, repr=False)
    captcha_verified: bool
    client_ip: str = Field(max_length=64, strict=True)

    @field_validator("captcha_verified", mode="before")
    @classmethod
    def strict_captcha_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("captcha_verified must be a boolean")
        return value


class _SessionRequest(ClosedModel):
    protocol: Literal["aap-identity-sessions/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    token: str | None = Field(max_length=16384, strict=True, repr=False)
    audit_context: SessionAuditContext
    user_agent: str = Field(max_length=256, strict=True)


class LoginRequest(_SessionRequest):
    operation: Literal["login"]
    token: None
    payload: LoginPayload


class RefreshRequest(_SessionRequest):
    operation: Literal["refresh"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: SessionEmptyPayload


class LogoutRequest(_SessionRequest):
    operation: Literal["logout"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: SessionEmptyPayload


class LogoutAllRequest(_SessionRequest):
    operation: Literal["logout_all"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: SessionEmptyPayload


SessionRequest: TypeAlias = Annotated[
    LoginRequest | RefreshRequest | LogoutRequest | LogoutAllRequest,
    Field(discriminator="operation"),
]
_SESSION_REQUESTS = TypeAdapter(SessionRequest)


def decode_session_request(payload: dict | bytes) -> SessionRequest:
    """Decode a closed session request; request validation errors remain local."""
    return _SESSION_REQUESTS.validate_json(_json_bytes(payload))


class SessionClaims(ClosedModel):
    sub: uuid.UUID
    role: Literal["super_admin", "project_admin", "employee", "viewer"]
    exp: int = Field(gt=0, strict=True)
    jti: uuid.UUID
    gen: int = Field(ge=0, lt=2**63, strict=True)
    iss: Literal["aap-identity/v1"]

    @model_validator(mode="after")
    def expiration_is_representable(self) -> SessionClaims:
        try:
            datetime.fromtimestamp(self.exp, UTC)
        except (OverflowError, OSError, ValueError):
            raise ValueError("Session expiry is outside the supported range") from None
        return self

    @property
    def expires_at(self) -> datetime:
        return datetime.fromtimestamp(self.exp, UTC)


class SessionIssuedData(ClosedModel):
    claims: SessionClaims
    access_token: str = Field(min_length=1, strict=True, repr=False)


class SessionLogoutData(ClosedModel):
    claims: None
    access_token: None


class SessionFailureDetail(ClosedModel):
    code: SessionFailureCode
    failed_count: int | None = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def count_matches_failure(self) -> SessionFailureDetail:
        has_count = self.code in {"invalid_credentials", "captcha_required"}
        if has_count != (self.failed_count is not None):
            raise ValueError("failed_count does not match session failure code")
        return self


class SessionFailureReply(ClosedModel):
    protocol: Literal["aap-identity-sessions/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: SessionFailureDetail


class _SessionSuccess(ClosedModel):
    protocol: Literal["aap-identity-sessions/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal


class LoginReply(_SessionSuccess):
    operation: Literal["login"]
    data: SessionIssuedData


class RefreshReply(_SessionSuccess):
    operation: Literal["refresh"]
    data: SessionIssuedData


class LogoutReply(_SessionSuccess):
    operation: Literal["logout"]
    data: SessionLogoutData


class LogoutAllReply(_SessionSuccess):
    operation: Literal["logout_all"]
    data: SessionIssuedData


SessionReply: TypeAlias = Annotated[
    LoginReply | RefreshReply | LogoutReply | LogoutAllReply,
    Field(discriminator="operation"),
]
_SESSION_REPLIES = TypeAdapter(SessionReply)


class SessionFailure(Exception):
    def __init__(self, code: SessionFailureCode, failed_count: int | None = None):
        has_count = code in {"invalid_credentials", "captcha_required"}
        if has_count:
            if type(failed_count) is not int or failed_count < 0:
                raise ValueError("This session failure requires a nonnegative count")
        elif failed_count is not None:
            raise ValueError("This session failure cannot carry a count")
        self.code = code
        self.failed_count = failed_count
        super().__init__(code)


def session_failure_reply(
    failure: SessionFailure | SessionFailureCode,
    failed_count: int | None = None,
) -> dict:
    if isinstance(failure, SessionFailure):
        code, count = failure.code, failure.failed_count
    else:
        code, count = failure, failed_count
    return SessionFailureReply(
        schema=SESSION_PROTOCOL,
        ok=False,
        error=SessionFailureDetail(code=code, failed_count=count),
    ).model_dump(mode="json", by_alias=True)


class SessionPolicy(ClosedModel):
    expire_minutes: int = Field(ge=1, le=525600, strict=True)
    email_verification_required: bool
    derivation_id: str = Field(pattern=_DERIVATION_PATTERN, strict=True)

    @field_validator("email_verification_required", mode="before")
    @classmethod
    def strict_email_policy(cls, value):
        if type(value) is not bool:
            raise ValueError("Email verification policy must be a boolean")
        return value


class SessionPolicyReply(ClosedModel):
    protocol: Literal["aap-identity-sessions-policy/v1"] = Field(alias="schema")
    issuer: Literal["aap-identity/v1"]
    algorithm: Literal["HS256"]
    refresh_grace_seconds: int = Field(ge=0, strict=True)


class LoginCandidate(ClosedModel):
    actor_id: uuid.UUID
    password_hash: str = Field(min_length=1, max_length=255, strict=True, repr=False)


class LoginCandidateReply(ClosedModel):
    protocol: Literal["aap-identity-login-candidate/v1"] = Field(alias="schema")
    item: LoginCandidate | None


def decode_login_candidate(payload: dict | bytes) -> LoginCandidateReply:
    try:
        return LoginCandidateReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


class NativeLogin(ClosedModel):
    email: str = Field(max_length=16384, strict=True)
    actor_id: uuid.UUID | None
    expected_password_hash: str | None = Field(
        min_length=1, max_length=255, strict=True, repr=False
    )
    password_matches: bool
    failed_count: int = Field(ge=0, strict=True)

    @field_validator("password_matches", mode="before")
    @classmethod
    def strict_password_match_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("password_matches must be a boolean")
        return value

    @model_validator(mode="after")
    def candidate_shape(self) -> NativeLogin:
        if self.actor_id is None:
            if self.expected_password_hash is not None or self.password_matches:
                raise ValueError("Missing login candidate has no password evidence")
        elif self.expected_password_hash is None:
            raise ValueError("Candidate login needs its observed hash")
        return self


class _NativeCommandBase(ClosedModel):
    protocol: Literal["aap-identity-sessions-native/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    policy: SessionPolicy
    audit_context: SessionAuditContext
    user_agent: str = Field(max_length=256, strict=True)


class NativeLoginCommand(_NativeCommandBase):
    operation: Literal["login"]
    actor: None
    expected_key_hash: None = Field(repr=False)
    login: NativeLogin
    request_fingerprint: str = Field(pattern=_FINGERPRINT_PATTERN, strict=True)


class _NativeAuthenticatedCommand(_NativeCommandBase):
    actor: Principal
    expected_key_hash: str | None = Field(
        min_length=1, max_length=255, strict=True, repr=False
    )
    login: None
    request_fingerprint: None

    @model_validator(mode="after")
    def key_hash_matches_credential_kind(self):
        if self.actor.credential_type == "jwt":
            if self.expected_key_hash is not None:
                raise ValueError("JWT command cannot include an API-key hash")
        elif self.expected_key_hash is None:
            raise ValueError("API-key command needs its verified hash")
        return self


class NativeRefreshCommand(_NativeAuthenticatedCommand):
    operation: Literal["refresh"]


class NativeLogoutCommand(_NativeAuthenticatedCommand):
    operation: Literal["logout"]


class NativeLogoutAllCommand(_NativeAuthenticatedCommand):
    operation: Literal["logout_all"]


NativeSessionCommand: TypeAlias = Annotated[
    NativeLoginCommand
    | NativeRefreshCommand
    | NativeLogoutCommand
    | NativeLogoutAllCommand,
    Field(discriminator="operation"),
]
_NATIVE_SESSION_COMMANDS = TypeAdapter(NativeSessionCommand)


def decode_native_session_command(payload: dict | bytes) -> NativeSessionCommand:
    try:
        return _NATIVE_SESSION_COMMANDS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def native_session_command(
    request: SessionRequest,
    *,
    actor: Principal | None,
    expected_key_hash: str | None,
    login: NativeLogin | None,
    policy: SessionPolicy,
    request_fingerprint: str | None,
) -> NativeSessionCommand:
    command_types = {
        "login": NativeLoginCommand,
        "refresh": NativeRefreshCommand,
        "logout": NativeLogoutCommand,
        "logout_all": NativeLogoutAllCommand,
    }
    payload = {
        "schema": NATIVE_SESSION_PROTOCOL,
        "operation": request.operation,
        "operation_id": request.operation_id,
        "actor": actor,
        "expected_key_hash": expected_key_hash,
        "login": login,
        "policy": policy,
        "request_fingerprint": request_fingerprint,
        "audit_context": request.audit_context,
        "user_agent": request.user_agent,
    }
    return command_types[request.operation].model_validate(payload)


class NativeIssuedData(ClosedModel):
    claims: SessionClaims


class NativeLogoutData(ClosedModel):
    claims: None


class _NativeSuccess(ClosedModel):
    protocol: Literal["aap-identity-sessions-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal


class NativeLoginReply(_NativeSuccess):
    operation: Literal["login"]
    data: NativeIssuedData


class NativeRefreshReply(_NativeSuccess):
    operation: Literal["refresh"]
    data: NativeIssuedData


class NativeLogoutReply(_NativeSuccess):
    operation: Literal["logout"]
    data: NativeLogoutData


class NativeLogoutAllReply(_NativeSuccess):
    operation: Literal["logout_all"]
    data: NativeIssuedData


NativeSessionReply: TypeAlias = Annotated[
    NativeLoginReply | NativeRefreshReply | NativeLogoutReply | NativeLogoutAllReply,
    Field(discriminator="operation"),
]
_NATIVE_SESSION_REPLIES = TypeAdapter(NativeSessionReply)


class NativeSessionFailureReply(ClosedModel):
    protocol: Literal["aap-identity-sessions-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: SessionFailureDetail


NativeSessionResult: TypeAlias = NativeSessionReply | NativeSessionFailureReply


def _decode_native_result(payload: dict | bytes) -> NativeSessionResult:
    encoded = _json_bytes(payload)
    raw = json.loads(encoded, object_pairs_hook=_unique_object)
    if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
        raise IdentityUnavailable()
    try:
        if raw["ok"] is False:
            return NativeSessionFailureReply.model_validate_json(encoded)
        return _NATIVE_SESSION_REPLIES.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def _same_authenticated_credential(actual: Principal, expected: Principal) -> bool:
    same_identity = (
        actual.actor_id == expected.actor_id
        and actual.credential_type == expected.credential_type
        and actual.credential_id == expected.credential_id
        and actual.generation == expected.generation
    )
    return same_identity and (
        expected.credential_type != "jwt" or actual.expires_at == expected.expires_at
    )


def _verify_jws_claims(access_token: str, claims: SessionClaims) -> None:
    try:
        pieces = access_token.split(".")
        if len(pieces) != 3:
            raise ValueError()
        header_bytes = _base64url_decode(pieces[0])
        payload_bytes = _base64url_decode(pieces[1])
        signature = _base64url_decode(pieces[2])
        header = json.loads(header_bytes, object_pairs_hook=_unique_object)
        payload = json.loads(payload_bytes, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None
    if (
        not isinstance(header, dict)
        or header != {"alg": ALGORITHM, "typ": "JWT"}
        or not isinstance(payload, dict)
        or len(signature) != 32
        or payload != claims.model_dump(mode="json")
    ):
        raise IdentityUnavailable()


def _base64url_decode(value: str) -> bytes:
    import re

    if not value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("Invalid base64url segment")
    padded = value + "=" * (-len(value) % 4)
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def _request_jwt_claims(request: SessionRequest) -> dict | None:
    if request.token is None or request.token.startswith("ak_"):
        return None
    try:
        pieces = request.token.split(".")
        if len(pieces) != 3:
            raise ValueError()
        header = json.loads(
            _base64url_decode(pieces[0]), object_pairs_hook=_unique_object
        )
        payload = json.loads(
            _base64url_decode(pieces[1]), object_pairs_hook=_unique_object
        )
        signature = _base64url_decode(pieces[2])
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None
    if (
        not isinstance(header, dict)
        or header.get("alg") != ALGORITHM
        or not isinstance(payload, dict)
        or len(signature) != 32
        or payload.get("iss") != ISSUER
    ):
        raise IdentityUnavailable()
    return payload


def _validate_session_success(
    request: SessionRequest,
    reply: SessionReply,
    *,
    expected_principal: Principal | None,
) -> None:
    if (
        reply.operation != request.operation
        or reply.operation_id != request.operation_id
    ):
        raise IdentityUnavailable()
    if isinstance(
        request, (RefreshRequest, LogoutRequest)
    ) and request.token.startswith("ak_"):
        raise IdentityUnavailable()
    if isinstance(request, LogoutRequest):
        if not isinstance(reply, LogoutReply):
            raise IdentityUnavailable()
        if expected_principal is not None and not _same_authenticated_credential(
            reply.principal, expected_principal
        ):
            raise IdentityUnavailable()
        input_claims = _request_jwt_claims(request)
        if input_claims is not None:
            _bind_input_jwt(reply.principal, input_claims)
        elif expected_principal is None:
            raise IdentityUnavailable()
        return
    if isinstance(reply, LogoutReply):
        raise IdentityUnavailable()
    claims = reply.data.claims
    if claims.jti != request.operation_id or claims.sub != reply.principal.actor_id:
        raise IdentityUnavailable()
    _verify_jws_claims(reply.data.access_token, claims)
    if isinstance(request, LoginRequest):
        if (
            reply.principal.credential_type != "jwt"
            or reply.principal.credential_id != claims.jti
            or reply.principal.generation != claims.gen
            or reply.principal.expires_at != claims.expires_at
        ):
            raise IdentityUnavailable()
        return
    if expected_principal is not None:
        if not _same_authenticated_credential(reply.principal, expected_principal):
            raise IdentityUnavailable()
    input_claims = _request_jwt_claims(request)
    if input_claims is not None:
        _bind_input_jwt(reply.principal, input_claims)
    elif expected_principal is None:
        raise IdentityUnavailable()


def _bind_input_jwt(principal: Principal, claims: dict) -> None:
    try:
        actor_id = uuid.UUID(claims["sub"])
        credential_id = uuid.UUID(claims["jti"])
        generation = claims.get("gen", 0)
        expires_at = claims["exp"]
        if (
            type(generation) is not int
            or not 0 <= generation < 2**63
            or type(expires_at) not in (int, float)
            or int(expires_at) != expires_at
        ):
            raise ValueError()
        expiry = datetime.fromtimestamp(expires_at, UTC)
    except (KeyError, ValueError, TypeError, OverflowError, OSError, AttributeError):
        raise IdentityUnavailable() from None
    if (
        principal.actor_id != actor_id
        or principal.credential_type != "jwt"
        or principal.credential_id != credential_id
        or principal.generation != generation
        or principal.expires_at != expiry
    ):
        raise IdentityUnavailable()


def decode_session_reply(
    request: SessionRequest,
    payload: dict | bytes,
    *,
    expected_principal: Principal | None = None,
) -> SessionReply:
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(encoded, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise IdentityUnavailable()
        if raw["ok"] is False:
            failure = SessionFailureReply.model_validate_json(encoded)
            raise SessionFailure(failure.error.code, failure.error.failed_count)
        reply = _SESSION_REPLIES.validate_json(encoded)
        _validate_session_success(request, reply, expected_principal=expected_principal)
        return reply
    except SessionFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


def _validate_native_binding(
    command: NativeSessionCommand,
    reply: NativeSessionResult,
) -> None:
    if isinstance(reply, NativeSessionFailureReply):
        if reply.error.code in {"invalid_credentials", "captcha_required"}:
            if not isinstance(command, NativeLoginCommand):
                raise IdentityUnavailable()
            if reply.error.failed_count != command.login.failed_count:
                raise IdentityUnavailable()
        return
    if (
        reply.operation != command.operation
        or reply.operation_id != command.operation_id
    ):
        raise IdentityUnavailable()
    if isinstance(command, NativeLoginCommand):
        if not isinstance(reply, NativeLoginReply):
            raise IdentityUnavailable()
        claims = reply.data.claims
        if (
            claims.jti != command.operation_id
            or claims.iss != ISSUER
            or command.login.actor_id is None
            or claims.sub != command.login.actor_id
            or reply.principal.actor_id != claims.sub
            or reply.principal.credential_type != "jwt"
            or reply.principal.credential_id != claims.jti
            or reply.principal.generation != claims.gen
            or reply.principal.expires_at != claims.expires_at
        ):
            raise IdentityUnavailable()
        return
    actor = command.actor
    if not _same_authenticated_credential(reply.principal, actor):
        raise IdentityUnavailable()
    if isinstance(command, NativeLogoutCommand):
        if not isinstance(reply, NativeLogoutReply):
            raise IdentityUnavailable()
        return
    if isinstance(reply, NativeLogoutReply):
        raise IdentityUnavailable()
    claims = reply.data.claims
    if (
        claims.jti != command.operation_id
        or claims.sub != actor.actor_id
        or claims.iss != ISSUER
    ):
        raise IdentityUnavailable()


def decode_native_session_reply(
    command: NativeSessionCommand, payload: dict | bytes
) -> NativeSessionResult:
    """Strictly bind a native reply without raising valid business failures."""
    try:
        result = _decode_native_result(payload)
        _validate_native_binding(command, result)
        return result
    except IdentityUnavailable:
        raise
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None
