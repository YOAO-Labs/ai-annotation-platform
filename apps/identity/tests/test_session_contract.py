from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from jose import jwt

from aap_identity.contract import FailureReply, IdentityUnavailable
from aap_identity.session_contract import (
    NATIVE_SESSION_PROTOCOL,
    SESSION_PROTOCOL,
    LoginReply,
    LoginRequest,
    LoginCandidate,
    NativeLogin,
    NativeLoginCommand,
    NativeLoginReply,
    NativeIssuedData,
    NativeSessionFailureReply,
    SessionClaims,
    SessionFailure,
    SessionIssuedData,
    SessionPolicy,
    SessionFailureReply,
    decode_native_session_reply,
    decode_session_reply,
    decode_session_request,
    session_failure_reply,
)
from aap_identity.session_manager import sign_session_claims
from aap_identity.security import decode_access_token, signing_key

from session_helpers import (
    SECRET,
    ACTOR_ID,
    audit_context,
    fixed_jwt,
    principal,
    session_request,
)

OPERATION_ID = uuid.uuid4()


def claims(
    *, actor_id=ACTOR_ID, role="employee", gen=7, jti=OPERATION_ID, exp=2_050_000_000
):
    return SessionClaims(
        sub=actor_id,
        role=role,
        exp=exp,
        jti=jti,
        gen=gen,
        iss="aap-identity/v1",
    )


def test_wire_request_preserves_credentials_without_echoing_them_in_repr():
    token = "private-session-bearer"
    password = "Password!2026"
    request = decode_session_request(
        {
            "schema": SESSION_PROTOCOL,
            "operation": "login",
            "operation_id": str(OPERATION_ID),
            "token": None,
            "payload": {
                "email": "Exact+Tag@Example.test",
                "password": password,
                "captcha_verified": True,
                "client_ip": "2001:db8::10",
            },
            "audit_context": audit_context(ip="2001:db8::10"),
            "user_agent": "",
        }
    )
    assert isinstance(request, LoginRequest)
    assert request.payload.email == "Exact+Tag@Example.test"
    assert password not in repr(request)
    assert request.payload.password == password
    assert (
        request.model_dump(mode="json", by_alias=True)["payload"]["password"]
        == password
    )
    assert token not in repr(session_request("refresh", token=token))


@pytest.mark.parametrize(
    "changes",
    [
        {"token": "token", "operation": "login"},
        {"token": None, "operation": "refresh"},
        {
            "payload": {
                "email": "x",
                "password": "x",
                "captcha_verified": 1,
                "client_ip": "",
            }
        },
        {"audit_context": audit_context(ip="example.test")},
        {
            "payload": {
                "email": "x",
                "password": "x",
                "captcha_verified": True,
                "client_ip": "",
                "extra": 1,
            }
        },
        {"user_agent": "x" * 257},
        {"extra": "forbidden"},
    ],
)
def test_wire_request_rejects_wrong_nullability_types_and_extra_fields(changes):
    raw = {
        "schema": SESSION_PROTOCOL,
        "operation": "login",
        "operation_id": str(OPERATION_ID),
        "token": None,
        "payload": {
            "email": "person@example.test",
            "password": "p",
            "captcha_verified": True,
            "client_ip": "",
        },
        "audit_context": audit_context(),
        "user_agent": "browser",
    }
    raw.update(changes)
    with pytest.raises(ValidationError):
        decode_session_request(raw)


def test_failure_counts_are_required_only_for_failed_login_and_captcha():
    failure = session_failure_reply("invalid_credentials", 3)
    assert failure == {
        "schema": SESSION_PROTOCOL,
        "ok": False,
        "error": {"code": "invalid_credentials", "failed_count": 3},
    }
    with pytest.raises(SessionFailure) as error:
        decode_session_reply(session_request("login"), failure)
    assert error.value.code == "invalid_credentials" and error.value.failed_count == 3

    session_busy = session_failure_reply("account_busy")
    assert session_busy["error"] == {"code": "account_busy", "failed_count": None}
    with pytest.raises(SessionFailure) as busy:
        decode_session_reply(session_request("refresh"), session_busy)
    assert busy.value.code == "account_busy"
    with pytest.raises(ValidationError):
        FailureReply.model_validate(
            {
                "schema": "aap-identity/v1",
                "ok": False,
                "error": {"code": "account_busy"},
            }
        )

    for invalid in (
        {"code": "invalid_credentials", "failed_count": None},
        {"code": "captcha_required", "failed_count": True},
        {"code": "credential_busy", "failed_count": 0},
        {"code": "account_busy", "failed_count": 0},
    ):
        with pytest.raises(ValidationError):
            SessionFailureReply.model_validate(
                {"schema": SESSION_PROTOCOL, "ok": False, "error": invalid}
            )


def test_claims_reject_boolean_generation_and_unrepresentable_expiry():
    with pytest.raises(ValidationError):
        claims(gen=True)
    with pytest.raises(ValidationError):
        claims(exp=True)
    with pytest.raises(ValidationError):
        claims(exp=10**100)


def test_reply_verifies_exact_claim_payload_and_binds_subject_to_principal():
    request = session_request("login", operation_id=OPERATION_ID)
    issued = claims()
    current = principal(
        "jwt",
        actor_id=str(issued.sub),
        credential_id=str(issued.jti),
        generation=issued.gen,
        role="super_admin",
        expires_at=issued.expires_at.isoformat(),
    )
    token = sign_session_claims(issued, SECRET)
    reply = LoginReply(
        schema=SESSION_PROTOCOL,
        ok=True,
        operation="login",
        operation_id=OPERATION_ID,
        principal=current,
        data=SessionIssuedData(claims=issued, access_token=token),
    )
    assert (
        decode_session_reply(
            request, reply.model_dump(mode="json", by_alias=True)
        ).data.access_token
        == token
    )
    assert token not in repr(reply.data)
    assert reply.principal.role == "super_admin" and issued.role == "employee"

    extra_claim_token = jwt.encode(
        {**issued.model_dump(mode="json"), "admin": True},
        signing_key(SECRET),
        algorithm="HS256",
        headers={"typ": "JWT"},
    )
    malformed = reply.model_copy(
        update={
            "data": SessionIssuedData(claims=issued, access_token=extra_claim_token)
        }
    )
    with pytest.raises(IdentityUnavailable):
        decode_session_reply(request, malformed.model_dump(mode="json", by_alias=True))

    other_actor = current.model_copy(update={"actor_id": uuid.uuid4()})
    wrong_principal = reply.model_copy(update={"principal": other_actor})
    with pytest.raises(IdentityUnavailable):
        decode_session_reply(
            request, wrong_principal.model_dump(mode="json", by_alias=True)
        )


def test_refresh_replay_can_return_frozen_older_claims_for_current_actor():
    current = principal(
        "jwt",
        credential_id=uuid.uuid4(),
        generation=9,
        role="super_admin",
        expires_at=datetime(2035, 1, 1, tzinfo=UTC),
    )
    request = session_request(
        "refresh",
        token=fixed_jwt(
            ACTOR_ID,
            "super_admin",
            int(current.expires_at.timestamp()),
            current.credential_id,
            9,
        ),
    )
    frozen = claims(role="employee", gen=7, jti=request.operation_id, exp=1_900_000_000)
    token = sign_session_claims(frozen, SECRET)
    raw = {
        "schema": SESSION_PROTOCOL,
        "ok": True,
        "operation": "refresh",
        "operation_id": str(request.operation_id),
        "principal": current.model_dump(mode="json"),
        "data": {"claims": frozen.model_dump(mode="json"), "access_token": token},
    }
    decoded = decode_session_reply(request, raw, expected_principal=current)
    assert decoded.data.claims.gen == 7 and decoded.data.claims.role == "employee"


def test_input_jwt_binding_allows_legacy_header_role_and_extra_claims():
    now = int(time.time())
    current_exp = now + 3600
    current_jti = uuid.uuid4()
    current = principal(
        "jwt",
        credential_id=current_jti,
        generation=9,
        role="super_admin",
        expires_at=datetime.fromtimestamp(current_exp, UTC),
    )
    incoming = jwt.encode(
        {
            "sub": str(ACTOR_ID),
            "role": "historical-role-string",
            "exp": float(current_exp),
            "jti": str(current_jti),
            "gen": 9,
            "iss": "aap-identity/v1",
            "iat": now - 30,
            "nbf": now - 30,
        },
        signing_key(SECRET),
        algorithm="HS256",
        headers={"kid": "old-key-id", "typ": "SESSION"},
    )
    request = session_request("refresh", token=incoming)
    frozen = claims(
        role="employee",
        gen=8,
        jti=request.operation_id,
        exp=current_exp + 3600,
    )
    reply = {
        "schema": SESSION_PROTOCOL,
        "ok": True,
        "operation": "refresh",
        "operation_id": str(request.operation_id),
        "principal": current.model_dump(mode="json"),
        "data": {
            "claims": frozen.model_dump(mode="json"),
            "access_token": sign_session_claims(frozen, SECRET),
        },
    }
    assert decode_access_token(incoming, secret=SECRET, verify_exp=False)["role"] == (
        "historical-role-string"
    )
    assert (
        decode_session_reply(request, reply, expected_principal=current).principal.role
        == "super_admin"
    )


def test_native_login_command_drops_plaintext_and_valid_failure_stays_a_value():
    request = session_request("login", operation_id=OPERATION_ID)
    expected_hash = "$2b$04$" + "a" * 53
    command = NativeLoginCommand(
        schema=NATIVE_SESSION_PROTOCOL,
        operation="login",
        operation_id=OPERATION_ID,
        actor=None,
        expected_key_hash=None,
        login=NativeLogin(
            email=request.payload.email,
            actor_id=ACTOR_ID,
            expected_password_hash=expected_hash,
            password_matches=True,
            failed_count=1,
        ),
        policy=SessionPolicy(
            expire_minutes=1440,
            email_verification_required=False,
            derivation_id="a" * 64,
        ),
        request_fingerprint="b" * 64,
        audit_context=request.audit_context,
        user_agent=request.user_agent,
    )
    serialized = command.model_dump(mode="json", by_alias=True)
    assert request.payload.password not in json.dumps(serialized)
    assert expected_hash not in repr(command)
    assert serialized["login"]["expected_password_hash"] == expected_hash
    candidate = LoginCandidate(actor_id=ACTOR_ID, password_hash=expected_hash)
    assert expected_hash not in repr(candidate)
    assert candidate.model_dump()["password_hash"] == expected_hash

    failure = {
        "schema": NATIVE_SESSION_PROTOCOL,
        "ok": False,
        "error": {"code": "invalid_credentials", "failed_count": 1},
    }
    decoded = decode_native_session_reply(command, failure)
    assert isinstance(decoded, NativeSessionFailureReply)
    assert decoded.error.failed_count == 1
    with pytest.raises(IdentityUnavailable):
        decode_native_session_reply(
            command,
            {**failure, "error": {"code": "invalid_credentials", "failed_count": 0}},
        )

    issued = claims(
        actor_id=ACTOR_ID,
        role="employee",
        gen=12,
        jti=OPERATION_ID,
        exp=2_050_000_000,
    )
    current_profile = principal(
        "jwt",
        actor_id=ACTOR_ID,
        credential_id=OPERATION_ID,
        generation=issued.gen,
        role="super_admin",
        expires_at=issued.expires_at,
    )
    success = NativeLoginReply(
        schema=NATIVE_SESSION_PROTOCOL,
        ok=True,
        operation="login",
        operation_id=OPERATION_ID,
        principal=current_profile,
        data=NativeIssuedData(claims=issued),
    )
    assert (
        decode_native_session_reply(
            command, success.model_dump(mode="json", by_alias=True)
        ).principal.role
        == "super_admin"
    )
