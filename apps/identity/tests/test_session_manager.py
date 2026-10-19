from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import pytest

from aap_identity.authenticator import (
    Authenticator,
    CredentialDecodeFailure,
    VerifiedCredential,
)
from aap_identity.contract import (
    IdentityFailure,
    failure_reply,
    principal_reply,
)
from aap_identity.security import ISSUER, decode_access_token
from aap_identity.session_contract import (
    NATIVE_SESSION_PROTOCOL,
    NativeLoginCommand,
    NativeLoginReply,
    NativeLogoutAllCommand,
    NativeLogoutAllReply,
    NativeLogoutCommand,
    NativeLogoutReply,
    NativeLogoutData,
    NativeRefreshReply,
    NativeIssuedData,
    SessionFailure,
    SessionClaims,
    decode_native_session_command,
)
from aap_identity.session_manager import (
    SessionManager,
    login_request_fingerprint,
    session_derivation,
)

from session_helpers import ACTOR_ID, SECRET, fixed_jwt, principal, session_request


class Counter:
    def __init__(self, value=0):
        self.value = value
        self.calls = []
        self.events = []

    async def get_count(self, ip):
        self.calls.append(("get", ip))
        self.events.append("counter.get")
        return self.value

    async def increment(self, ip):
        self.value += 1
        self.calls.append(("increment", ip))
        self.events.append("counter.increment")
        return self.value

    async def reset(self, ip):
        self.value = 0
        self.calls.append(("reset", ip))
        self.events.append("counter.reset")


class BoundAuth:
    def __init__(self, credential):
        self.credential = credential
        self.calls = []

    async def authenticate_bound(self, token, *, for_refresh=False):
        self.calls.append((token, for_refresh))
        return self.credential


class SessionData:
    def __init__(self, *, candidate=None, failure=None, stale_claims=None):
        self.candidate = candidate
        self.failure = failure
        self.stale_claims = stale_claims
        self.events = []
        self.commands = []

    async def read_login_candidate(self, email):
        self.events.append("candidate.read")
        return {"schema": "aap-identity-login-candidate/v1", "item": self.candidate}

    async def execute_session_command(self, payload):
        self.events.append("command.execute")
        command = decode_native_session_command(payload)
        self.commands.append(command)
        if self.failure is not None:
            count = (
                command.login.failed_count
                if isinstance(command, NativeLoginCommand)
                and self.failure in {"invalid_credentials", "captcha_required"}
                else None
            )
            return {
                "schema": NATIVE_SESSION_PROTOCOL,
                "ok": False,
                "error": {"code": self.failure, "failed_count": count},
            }
        if isinstance(command, NativeLoginCommand):
            if command.login.actor_id is None:
                raise AssertionError("success fixture needs a login candidate")
            exp = int(time.time()) + command.policy.expire_minutes * 60
            claims = SessionClaims(
                sub=command.login.actor_id,
                role="super_admin",
                exp=exp,
                jti=command.operation_id,
                gen=12,
                iss=ISSUER,
            )
            current = principal(
                actor_id=claims.sub,
                credential_type="jwt",
                credential_id=claims.jti,
                generation=claims.gen,
                role=claims.role,
                scopes=None,
                expires_at=claims.expires_at,
            )
            reply = NativeLoginReply(
                schema=NATIVE_SESSION_PROTOCOL,
                ok=True,
                operation="login",
                operation_id=command.operation_id,
                principal=current,
                data=NativeIssuedData(claims=claims),
            )
            return reply.model_dump(mode="json", by_alias=True)

        actor = command.actor
        if isinstance(command, NativeLogoutCommand):
            reply = NativeLogoutReply(
                schema=NATIVE_SESSION_PROTOCOL,
                ok=True,
                operation="logout",
                operation_id=command.operation_id,
                principal=actor,
                data=NativeLogoutData(claims=None),
            )
            return reply.model_dump(mode="json", by_alias=True)

        claims = self.stale_claims or SessionClaims(
            sub=actor.actor_id,
            role=actor.role,
            exp=int(time.time()) + command.policy.expire_minutes * 60,
            jti=command.operation_id,
            gen=(actor.generation or 0)
            + (1 if isinstance(command, NativeLogoutAllCommand) else 0),
            iss=ISSUER,
        )
        reply_type = (
            NativeLogoutAllReply
            if isinstance(command, NativeLogoutAllCommand)
            else NativeRefreshReply
        )
        reply = reply_type(
            schema=NATIVE_SESSION_PROTOCOL,
            ok=True,
            operation=command.operation,
            operation_id=command.operation_id,
            principal=actor,
            data=NativeIssuedData(claims=claims),
        )
        return reply.model_dump(mode="json", by_alias=True)


def manager(auth, data, counter, **settings):
    return SessionManager(auth, data, SECRET, counter=counter, **settings)


def jwt_actor(*, generation=7, role="employee", expires_at=None, credential_id=None):
    expires_at = expires_at or datetime.now(UTC).replace(microsecond=0) + timedelta(
        hours=1
    )
    return principal(
        "jwt",
        credential_id=credential_id or uuid.uuid4(),
        generation=generation,
        role=role,
        expires_at=expires_at,
    )


def actor_token(actor):
    return fixed_jwt(
        actor.actor_id,
        actor.role,
        int(actor.expires_at.timestamp()),
        actor.credential_id,
        actor.generation,
    )


@pytest.mark.asyncio
async def test_login_success_uses_frozen_native_claims_and_resets_counter_last():
    password = "Password!2026"
    candidate_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    counter = Counter(value=2)
    data = SessionData(
        candidate={"actor_id": str(ACTOR_ID), "password_hash": candidate_hash}
    )
    auth = BoundAuth(VerifiedCredential(principal(), None))
    request = session_request("login", operation_id=uuid.uuid4())
    result = await manager(auth, data, counter).execute(request)

    decoded = decode_access_token(result.data.access_token, secret=SECRET)
    assert result.operation == "login"
    assert decoded == result.data.claims.model_dump(mode="json")
    assert result.data.claims.jti == request.operation_id
    assert result.principal.role == result.data.claims.role == "super_admin"
    assert counter.events == ["counter.get", "counter.reset"]
    assert data.events == ["candidate.read", "command.execute"]
    command = data.commands[0]
    assert isinstance(command, NativeLoginCommand)
    assert command.login.password_matches is True
    assert command.login.failed_count == 2
    assert command.login.expected_password_hash == candidate_hash
    assert command.request_fingerprint == login_request_fingerprint(
        SECRET, request.payload.email, password
    )
    assert password not in json.dumps(command.model_dump(mode="json", by_alias=True))
    assert "token" not in command.model_dump(mode="json", by_alias=True)


@pytest.mark.asyncio
async def test_invalid_password_increments_before_audited_native_command_and_does_not_reset():
    password = "wrong-password"
    candidate_hash = bcrypt.hashpw(b"right-password", bcrypt.gensalt(rounds=4)).decode()
    counter = Counter(value=2)
    data = SessionData(
        candidate={"actor_id": str(ACTOR_ID), "password_hash": candidate_hash},
        failure="invalid_credentials",
    )
    request = session_request(
        "login",
        payload={
            "email": "person@example.test",
            "password": password,
            "captcha_verified": True,
            "client_ip": "192.0.2.10",
        },
    )
    with pytest.raises(SessionFailure) as failure:
        await manager(
            BoundAuth(VerifiedCredential(principal(), None)), data, counter
        ).execute(request)
    assert failure.value.code == "invalid_credentials"
    assert failure.value.failed_count == 3
    assert counter.events == ["counter.get", "counter.increment"]
    assert data.events == ["candidate.read", "command.execute"]
    assert data.commands[0].login.failed_count == 3


@pytest.mark.asyncio
async def test_captcha_gate_precedes_candidate_read_and_preserves_observed_count():
    counter = Counter(value=5)
    data = SessionData()
    request = session_request(
        "login",
        payload={
            "email": "person@example.test",
            "password": "x",
            "captcha_verified": False,
            "client_ip": "192.0.2.10",
        },
    )
    with pytest.raises(SessionFailure) as failure:
        await manager(
            BoundAuth(VerifiedCredential(principal(), None)), data, counter
        ).execute(request)
    assert failure.value.code == "captcha_required"
    assert failure.value.failed_count == 5
    assert data.events == [] and counter.events == ["counter.get"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["user_inactive", "email_not_verified"])
async def test_correct_password_gate_failures_do_not_change_counter(code):
    password = "Password!2026"
    candidate_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    counter = Counter(value=4)
    data = SessionData(
        candidate={"actor_id": str(ACTOR_ID), "password_hash": candidate_hash},
        failure=code,
    )
    with pytest.raises(SessionFailure) as failure:
        await manager(
            BoundAuth(VerifiedCredential(principal(), None)), data, counter
        ).execute(session_request("login"))
    assert failure.value.code == code and failure.value.failed_count is None
    assert counter.events == ["counter.get"]
    assert data.commands[0].login.password_matches is True


@pytest.mark.asyncio
async def test_refresh_calls_bound_authenticator_in_grace_mode_and_replays_native_claims():
    actor = jwt_actor(generation=9, role="super_admin")
    request = session_request("refresh", token=actor_token(actor))
    old_claims = SessionClaims(
        sub=actor.actor_id,
        role="employee",
        exp=1_700_000_000,
        jti=request.operation_id,
        gen=7,
        iss=ISSUER,
    )
    auth = BoundAuth(VerifiedCredential(actor, None))
    data = SessionData(stale_claims=old_claims)
    counter = Counter()
    reply = await manager(auth, data, counter).execute(request)
    assert auth.calls == [(request.token, True)]
    assert reply.data.claims == old_claims
    assert decode_access_token(
        reply.data.access_token, secret=SECRET, verify_exp=False
    ) == old_claims.model_dump(mode="json")
    assert counter.calls == []


@pytest.mark.asyncio
async def test_refresh_and_logout_reject_api_keys_but_logout_all_keeps_existing_policy():
    key_hash = "$2b$04$" + "a" * 53
    actor = principal("api_key", credential_id=str(uuid.uuid4()), scopes=[])
    auth = BoundAuth(VerifiedCredential(actor, key_hash))
    counter = Counter()
    for operation in ("refresh", "logout"):
        with pytest.raises(SessionFailure) as failure:
            await manager(auth, SessionData(), counter).execute(
                session_request(operation, token="ak_" + "k" * 32)
            )
        assert failure.value.code == "credential_invalid"

    request = session_request("logout_all", token="ak_" + "k" * 32)
    data = SessionData()
    reply = await manager(auth, data, counter).execute(request)
    assert reply.operation == "logout_all"
    assert reply.data.claims.gen == (actor.generation or 0) + 1
    assert reply.data.claims.jti == request.operation_id
    assert key_hash not in repr(data.commands[0])
    assert (
        data.commands[0].model_dump(mode="json", by_alias=True)["expected_key_hash"]
        == key_hash
    )


@pytest.mark.asyncio
async def test_authenticator_for_refresh_passes_native_flag_and_enforces_grace():
    expiry = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=10)
    actor_id = uuid.uuid4()
    jti = uuid.uuid4()
    token = fixed_jwt(actor_id, "employee", int(expiry.timestamp()), jti, 3)

    class Data:
        def __init__(self):
            self.for_refresh = []

        async def read_jwt_principal(
            self, subject, token_id, generation, expires_at, for_refresh=False
        ):
            self.for_refresh.append(for_refresh)
            current = principal(
                actor_id=subject,
                credential_id=token_id,
                generation=generation,
                expires_at=expires_at,
            )
            return principal_reply(current)

    data = Data()
    verified = await Authenticator(data, SECRET).authenticate_bound(
        token, for_refresh=True
    )
    assert verified.principal.credential_id == jti and data.for_refresh == [True]
    with pytest.raises(IdentityFailure) as error:
        await Authenticator(data, SECRET).authenticate_bound(token)
    assert error.value.code == "token_expired"

    expired = fixed_jwt(
        actor_id,
        "employee",
        int((datetime.now(UTC) - timedelta(seconds=604801)).timestamp()),
        uuid.uuid4(),
        3,
    )
    before = len(data.for_refresh)
    with pytest.raises(IdentityFailure) as stale:
        await Authenticator(data, SECRET).authenticate_bound(expired, for_refresh=True)
    assert stale.value.code == "token_expired" and len(data.for_refresh) == before


@pytest.mark.asyncio
async def test_refresh_maps_decode_provenance_missing_actor_and_native_failures():
    actor = jwt_actor()
    request = session_request("refresh", token=actor_token(actor))

    class FailedAuth:
        def __init__(self, failure):
            self.failure = failure

        async def authenticate_bound(self, token, *, for_refresh=False):
            assert for_refresh is True
            raise self.failure

    for reason in ("invalid_token", "malformed_token"):
        with pytest.raises(SessionFailure) as failure:
            await manager(
                FailedAuth(CredentialDecodeFailure(reason)), SessionData(), Counter()
            ).execute(request)
        assert failure.value.code == reason

    class MissingActor:
        async def read_jwt_principal(
            self, actor_id, jti, generation, expires_at, for_refresh=False
        ):
            assert for_refresh is True
            return failure_reply("credential_invalid")

    with pytest.raises(SessionFailure) as missing:
        await manager(
            Authenticator(MissingActor(), SECRET), SessionData(), Counter()
        ).execute(request)
    assert missing.value.code == "user_inactive"

    for code in (
        "credential_busy",
        "account_busy",
        "credential_revoked",
        "generation_outdated",
        "token_expired",
    ):
        with pytest.raises(SessionFailure) as failure:
            await manager(
                BoundAuth(VerifiedCredential(actor, None)),
                SessionData(failure=code),
                Counter(),
            ).execute(request)
        assert failure.value.code == code


def test_session_derivation_and_fingerprint_are_stable_and_domain_separated():
    master, derivation_id = session_derivation(SECRET)
    expected = login_request_fingerprint(SECRET, "a@example.test", "private")
    other_email = login_request_fingerprint(SECRET, "b@example.test", "private")
    other_password = login_request_fingerprint(SECRET, "a@example.test", "private2")
    assert len(master) == 32 and len(derivation_id) == 64
    assert expected == login_request_fingerprint(SECRET, "a@example.test", "private")
    assert expected != other_email and expected != other_password
    assert expected != login_request_fingerprint(
        SECRET + "x", "a@example.test", "private"
    )
