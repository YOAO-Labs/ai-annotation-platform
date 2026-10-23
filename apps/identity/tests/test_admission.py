from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import httpx
import pytest
from jose import JWTError, jwt

from aap_identity.authenticator import Authenticator, CredentialDecodeFailure
from aap_identity.contract import (
    IdentityFailure,
    IdentityUnavailable,
    Principal,
    decode_principal_reply,
    failure_reply,
    principal_reply,
)
from aap_identity.security import (
    ALGORITHM,
    ISSUER,
    create_access_token,
    decode_access_token,
    signing_key,
)
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings

SECRET = "identity-unit-signing-secret"
ACTOR = uuid.uuid4()


def token(**changes):
    claims = {
        "sub": str(ACTOR),
        "jti": str(uuid.uuid4()),
        "gen": 0,
        "role": "super_admin",
        "iss": ISSUER,
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
    }
    claims.update(changes)
    return jwt.encode(claims, signing_key(SECRET), algorithm=ALGORITHM)


def principal(kind="jwt", **changes):
    value = {
        "actor_id": str(ACTOR),
        "name": "Current account",
        "email": "admin",
        "role": "viewer",
        "is_active": True,
        "credential_type": kind,
        "credential_id": str(uuid.uuid4()),
        "generation": 0 if kind == "jwt" else None,
        "scopes": None if kind == "jwt" else ["datasets:read"],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        if kind == "jwt"
        else None,
    }
    value.update(changes)
    return Principal.model_validate_json(json.dumps(value))


class Data:
    def __init__(self):
        self.calls = []
        self.failure = None

    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": ISSUER,
            "refresh_grace_seconds": 604800,
        }

    async def read_jwt_principal(
        self, actor_id, jti, generation, expires_at, for_refresh=False
    ):
        self.calls.append((actor_id, jti, generation, expires_at, for_refresh))
        if self.failure:
            return failure_reply(self.failure)
        return principal_reply(
            principal(
                actor_id=str(actor_id),
                credential_id=str(jti),
                generation=generation,
                expires_at=expires_at.isoformat(),
            )
        )


@pytest.mark.asyncio
async def test_current_native_role_and_credential_scope_win_over_signed_role():
    data = Data()
    result = await Authenticator(data, SECRET).authenticate(token())
    assert (
        result.actor_id == ACTOR and result.role == "viewer" and result.scopes is None
    )
    assert len(data.calls) == 1 and data.calls[0][-1] is False


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"gen": True}, "malformed_token"),
        ({"gen": -1}, "malformed_token"),
        ({"gen": 2**63}, "malformed_token"),
        ({"gen": "0"}, "malformed_token"),
        ({"exp": 1.5}, "malformed_token"),
        ({"exp": True}, "malformed_token"),
        ({"exp": "later"}, "malformed_token"),
        ({"exp": 2**63}, "malformed_token"),
        ({"sub": "invalid"}, "malformed_token"),
        ({"sub": None}, "invalid_token"),
        ({"jti": "invalid"}, "malformed_token"),
        ({"jti": None}, "invalid_token"),
        ({"iss": "older-issuer"}, "invalid_token"),
    ],
)
@pytest.mark.asyncio
async def test_refresh_signed_claim_failures_keep_decode_provenance(changes, reason):
    data = Data()
    with pytest.raises(CredentialDecodeFailure) as error:
        await Authenticator(data, SECRET).authenticate_bound(
            token(**changes), for_refresh=True
        )
    assert error.value.code == "credential_invalid"
    assert error.value.reason == reason
    assert data.calls == []


@pytest.mark.asyncio
async def test_refresh_unparseable_and_api_key_credentials_have_invalid_token_provenance():
    auth = Authenticator(Data(), SECRET)
    for value in ("not-a-jwt", "ak_" + "x" * 32):
        with pytest.raises(CredentialDecodeFailure) as error:
            await auth.authenticate_bound(value, for_refresh=True)
        assert error.value.code == "credential_invalid"
        assert error.value.reason == "invalid_token"

    with pytest.raises(IdentityFailure) as ordinary:
        await auth.authenticate("not-a-jwt")
    assert ordinary.value.code == "credential_invalid"
    assert failure_reply(ordinary.value.code)["error"]["code"] == "credential_invalid"


def test_legacy_signature_and_issuer_are_rejected_by_new_codec():
    old = jwt.encode(
        {
            "sub": str(ACTOR),
            "jti": str(uuid.uuid4()),
            "gen": 0,
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        SECRET,
        algorithm=ALGORITHM,
    )
    with pytest.raises(JWTError):
        decode_access_token(old, secret=SECRET)
    new = create_access_token(str(ACTOR), "employee", secret=SECRET, expire_minutes=15)
    assert decode_access_token(new, secret=SECRET)["iss"] == ISSUER
    with pytest.raises(JWTError):
        jwt.decode(new, SECRET, algorithms=[ALGORITHM])


@pytest.mark.asyncio
async def test_expired_credential_keeps_its_declared_error_without_native_io():
    data = Data()
    with pytest.raises(IdentityFailure) as error:
        await Authenticator(data, SECRET).authenticate(token(exp=1))
    assert error.value.code == "token_expired" and data.calls == []


@pytest.mark.parametrize(
    "code",
    ["credential_revoked", "generation_outdated", "user_inactive", "credential_busy"],
)
@pytest.mark.asyncio
async def test_native_rejection_is_not_replaced_by_cached_identity(code):
    data = Data()
    auth = Authenticator(data, SECRET)
    value = token()
    assert (await auth.authenticate(value)).actor_id == ACTOR
    data.failure = code
    with pytest.raises(IdentityFailure) as error:
        await auth.authenticate(value)
    assert error.value.code == code and len(data.calls) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"is_active": 1},
        {"is_active": False},
        {"generation": True},
        {"generation": None},
        {"scopes": []},
        {"extra": "ignored?"},
        {"expires_at": "2030-01-01T00:00:00"},
    ],
)
def test_principal_contract_refuses_ambiguous_or_partial_success(change):
    raw = principal_reply(principal())
    raw["data"].update(change)
    with pytest.raises(IdentityUnavailable):
        decode_principal_reply(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"ok": True},
        {"schema": ISSUER, "ok": 1},
        {"schema": "unknown", "ok": False, "error": {"code": "credential_invalid"}},
        {"schema": ISSUER, "ok": False, "error": {"code": "unknown"}},
    ],
)
def test_unknown_or_partial_envelope_fails_closed(raw):
    with pytest.raises(IdentityUnavailable):
        decode_principal_reply(raw)


@pytest.mark.asyncio
async def test_prefix_collisions_page_without_truncation_and_confirm_verified_hash():
    value = "ak_" + "a" * 32
    ids = sorted([uuid.uuid4(), uuid.uuid4()])
    good_hash = bcrypt.hashpw(value.encode(), bcrypt.gensalt(rounds=4)).decode()

    class Keys(Data):
        async def read_api_key_candidates(self, prefix, after, limit):
            self.calls.append((prefix, after, limit))
            index = 0 if after is None else 1
            return {
                "schema": "aap-identity-candidates/v1",
                "items": [
                    {
                        "id": str(ids[index]),
                        "key_hash": "malformed hash" if index == 0 else good_hash,
                    }
                ],
                "next_after": str(ids[0]) if index == 0 else None,
            }

        async def confirm_api_key_principal(self, key_id, expected_hash):
            assert key_id == ids[1] and expected_hash == good_hash
            return principal_reply(principal("api_key", credential_id=str(key_id)))

    data = Keys()
    result = await Authenticator(data, SECRET).authenticate(value)
    assert result.scopes == ["datasets:read"] and result.generation is None
    assert data.calls == [(value[:12], None, 100), (value[:12], ids[0], 100)]


@pytest.mark.asyncio
async def test_invalid_candidate_cursor_does_not_loop_or_grant_access():
    class Broken(Data):
        async def read_api_key_candidates(self, *args):
            return {
                "schema": "aap-identity-candidates/v1",
                "items": [],
                "next_after": str(uuid.uuid4()),
            }

    with pytest.raises(IdentityUnavailable):
        await Authenticator(Broken(), SECRET).authenticate("ak_" + "x" * 32)


@pytest.mark.parametrize("retain_schema", [False, True])
@pytest.mark.asyncio
async def test_candidate_page_rejects_internal_protocol_name(retain_schema):
    class Broken(Data):
        async def read_api_key_candidates(self, *args):
            page = {
                "schema": "aap-identity-candidates/v1",
                "protocol": "aap-identity-candidates/v1",
                "items": [],
                "next_after": None,
            }
            if not retain_schema:
                del page["schema"]
            return page

    with pytest.raises(IdentityUnavailable):
        await Authenticator(Broken(), SECRET).authenticate("ak_" + "x" * 32)


@pytest.mark.asyncio
async def test_runtime_authentication_boundary_and_no_token_echo():
    data = Data()
    app = create_app(
        Settings(
            database_url="postgresql://unused",
            token="control-secret",
            signing_secret=SECRET,
        ),
        data=data,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        assert (await client.get("/ready")).status_code == 200
        assert (
            await client.post("/v1/identity/authenticate", json={"token": token()})
        ).status_code == 401
        headers = {"Authorization": "Bearer control-secret"}
        value = token()
        good = await client.post(
            "/v1/identity/authenticate", headers=headers, json={"token": value}
        )
        assert decode_principal_reply(good.content).role == "viewer"
        for payload in (
            {"token": value, "actor_id": str(ACTOR)},
            {"token": "x" * 70000},
        ):
            bad = await client.post(
                "/v1/identity/authenticate", headers=headers, json=payload
            )
            assert (
                bad.status_code == 400
                and value not in bad.text
                and len(bad.content) < 200
            )
        data.failure = "credential_revoked"
        denied = await client.post(
            "/v1/identity/authenticate", headers=headers, json={"token": value}
        )
        assert (
            denied.status_code == 200
            and denied.json()["error"]["code"] == "credential_revoked"
        )


@pytest.mark.asyncio
async def test_runtime_deadline_cancels_owner_read_and_releases_capacity():
    stopped = asyncio.Event()

    class Slow(Data):
        async def read_jwt_principal(self, *args, **kwargs):
            try:
                await asyncio.Future()
            finally:
                stopped.set()

    app = create_app(
        Settings(
            database_url="postgresql://unused",
            token="control-secret",
            signing_secret=SECRET,
            request_timeout_seconds=0.01,
            max_concurrency=1,
        ),
        data=Slow(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        reply = await client.post(
            "/v1/identity/authenticate",
            headers={"Authorization": "Bearer control-secret"},
            json={"token": token()},
        )
    assert reply.status_code == 503 and stopped.is_set()


@pytest.mark.asyncio
async def test_owner_reply_for_another_actor_is_rejected():
    class OtherActor(Data):
        async def read_jwt_principal(self, *args, **kwargs):
            return principal_reply(principal(actor_id=str(uuid.uuid4())))

    with pytest.raises(IdentityUnavailable):
        await Authenticator(OtherActor(), SECRET).authenticate(token())


@pytest.mark.asyncio
async def test_runtime_deadline_includes_reading_the_request_body():
    stopped = asyncio.Event()
    data = Data()

    async def body():
        try:
            yield b'{"token":"'
            await asyncio.Future()
        finally:
            stopped.set()

    app = create_app(
        Settings(
            database_url="postgresql://unused",
            token="control",
            signing_secret=SECRET,
            request_timeout_seconds=0.01,
        ),
        data=data,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        result = await client.post(
            "/v1/identity/authenticate",
            content=body(),
            headers={
                "Authorization": "Bearer control",
                "Content-Type": "application/json",
            },
        )
    assert result.status_code == 503 and stopped.is_set() and data.calls == []
