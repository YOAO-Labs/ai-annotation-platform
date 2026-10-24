from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from aap_identity_admission.actor_status_contract import (
    ACTOR_STATUS_PROTOCOL,
    ActorStatus,
    actor_status_reply,
)
from aap_identity.actor_status_reader import ActorStatusData as ActorStatusDataProtocol
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings

GENERAL_TOKEN = "identity-general-control"
ADMISSION_TOKEN = "identity-scoped-admission"
SIGNING_SECRET = "identity-runtime-test-signing-secret-32-bytes"
POLICY = {
    "schema": "aap-identity-policy/v1",
    "issuer": "aap-identity/v1",
    "refresh_grace_seconds": 604800,
}


class AuthenticationData:
    def __init__(self):
        self.status_calls = []

    async def authentication_policy(self):
        return POLICY

    async def read_actor_status(self, actor_id):
        self.status_calls.append(actor_id)
        raise AssertionError("actor status must not fall back to admission data")


class ActorStatusData:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def read_actor_status(self, actor_id):
        self.calls.append(actor_id)
        if self.error is not None:
            raise self.error
        return self.result


def _settings(monkeypatch, **changes):
    for name in (
        "IDENTITY_ADMISSION_TOKEN",
        "IDENTITY_ADMISSION_TOKEN_FILE",
        "IDENTITY_ACTOR_STATUS_DATABASE_URL",
        "IDENTITY_ACTOR_STATUS_DATABASE_URL_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    values = {
        "_env_file": None,
        "database_url": SecretStr("postgresql://identity.invalid/identity"),
        "token": SecretStr(GENERAL_TOKEN),
        "signing_secret": SecretStr(SIGNING_SECRET),
    }
    values.update(changes)
    return Settings(**values)


def _status_request(actor_id):
    return {"schema": ACTOR_STATUS_PROTOCOL, "actor_id": str(actor_id)}


@pytest.mark.asyncio
async def test_general_and_scoped_bearers_authenticate_and_read_current_status(
    monkeypatch,
):
    actor_id = uuid.uuid4()
    data = ActorStatusData({"actor_id": actor_id, "is_active": False})
    app = create_app(
        _settings(monkeypatch, admission_token=SecretStr(ADMISSION_TOKEN)),
        data=AuthenticationData(),
        actor_status_data=data,
    )

    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://identity.test"
    ) as client:
        general_status = await client.post(
            "/v1/identity/actor-status",
            headers={"Authorization": f"Bearer {GENERAL_TOKEN}"},
            json=_status_request(actor_id),
        )
        scoped_status = await client.post(
            "/v1/identity/actor-status",
            headers={"Authorization": f"Bearer {ADMISSION_TOKEN}"},
            json=_status_request(actor_id),
        )
        general_auth = await client.post(
            "/v1/identity/authenticate",
            headers={"Authorization": f"Bearer {GENERAL_TOKEN}"},
            json={"token": "not-a-user-credential"},
        )
        scoped_auth = await client.post(
            "/v1/identity/authenticate",
            headers={"Authorization": f"Bearer {ADMISSION_TOKEN}"},
            json={"token": "not-a-user-credential"},
        )

    expected = actor_status_reply(ActorStatus(actor_id=actor_id, is_active=False))
    assert general_status.status_code == scoped_status.status_code == 200
    assert general_status.json() == scoped_status.json() == expected
    assert data.calls == [actor_id, actor_id]
    assert general_auth.status_code == scoped_auth.status_code == 200
    assert general_auth.json()["error"]["code"] == "credential_invalid"
    assert scoped_auth.json()["error"]["code"] == "credential_invalid"
    assert "not-a-user-credential" not in general_auth.text + scoped_auth.text


@pytest.mark.asyncio
async def test_missing_status_source_returns_503_without_auth_database_fallback(
    monkeypatch,
):
    authentication_data = AuthenticationData()
    app = create_app(_settings(monkeypatch), data=authentication_data)
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/actor-status",
            headers={"Authorization": f"Bearer {GENERAL_TOKEN}"},
            json=_status_request(uuid.uuid4()),
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Identity actor status unavailable"}
    assert authentication_data.status_calls == []


@pytest.mark.asyncio
async def test_ready_checks_only_the_configured_status_datasource(monkeypatch):
    actor_id = uuid.UUID(int=0)
    configured = ActorStatusData(None)
    app = create_app(
        _settings(monkeypatch), data=AuthenticationData(), actor_status_data=configured
    )
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert configured.calls == [actor_id]

    unconfigured_authentication_data = AuthenticationData()
    app = create_app(_settings(monkeypatch), data=unconfigured_authentication_data)
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert unconfigured_authentication_data.status_calls == []


@pytest.mark.asyncio
async def test_missing_actor_is_null_and_status_mismatch_or_bad_boolean_is_unavailable(
    monkeypatch,
):
    actor_id = uuid.uuid4()
    missing = ActorStatusData(None)
    missing_app = create_app(
        _settings(monkeypatch), data=AuthenticationData(), actor_status_data=missing
    )
    async with AsyncClient(
        transport=ASGITransport(app=missing_app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/actor-status",
            headers={"Authorization": f"Bearer {GENERAL_TOKEN}"},
            json=_status_request(actor_id),
        )
    assert response.status_code == 200
    assert response.json() == actor_status_reply(None)

    for value in (
        {"actor_id": uuid.uuid4(), "is_active": True},
        {"actor_id": actor_id, "is_active": 1},
    ):
        bad = ActorStatusData(value)
        app = create_app(
            _settings(monkeypatch), data=AuthenticationData(), actor_status_data=bad
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://identity.test"
        ) as client:
            response = await client.post(
                "/v1/identity/actor-status",
                headers={"Authorization": f"Bearer {GENERAL_TOKEN}"},
                json=_status_request(actor_id),
            )
        assert response.status_code == 503
        assert "actor_id" not in response.text


@pytest.mark.asyncio
async def test_scoped_bearer_is_rejected_by_every_non_admission_surface(monkeypatch):
    app = create_app(
        _settings(monkeypatch, admission_token=SecretStr(ADMISSION_TOKEN)),
        data=AuthenticationData(),
        actor_status_data=ActorStatusData(None),
    )
    headers = {
        "Authorization": f"Bearer {ADMISSION_TOKEN}",
        "Content-Type": "application/json",
    }
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://identity.test"
    ) as client:
        responses = [
            await client.post(
                path,
                headers=headers,
                json={
                    "schema": "aap-identity-actor/v1",
                    "actor_id": str(uuid.uuid4()),
                    "admitted_role": "employee",
                },
            )
            for path in (
                "/v1/identity/actor-context",
                "/v1/identity/api-keys/execute",
                "/v1/identity/sessions/execute",
                "/v1/identity/accounts/execute",
                "/v1/identity/admin-passwords/execute",
            )
        ]

    assert [response.status_code for response in responses] == [401] * len(responses)


@pytest.mark.asyncio
async def test_actor_status_request_bounds_and_deadline_are_sanitized(monkeypatch):
    data = ActorStatusData(None)
    app = create_app(
        _settings(monkeypatch), data=AuthenticationData(), actor_status_data=data
    )
    headers = {
        "Authorization": f"Bearer {GENERAL_TOKEN}",
        "Content-Type": "application/json",
    }
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://identity.test"
    ) as client:
        invalid_type = await client.post(
            "/v1/identity/actor-status",
            headers={**headers, "Content-Type": "text/plain"},
            content=b"{}",
        )
        oversized = await client.post(
            "/v1/identity/actor-status",
            headers=headers,
            content=b" " * 65537,
        )
        malformed = await client.post(
            "/v1/identity/actor-status", headers=headers, json={"actor_id": "bad"}
        )

    assert (
        invalid_type.status_code
        == oversized.status_code
        == malformed.status_code
        == 400
    )
    assert data.calls == []

    stopped = asyncio.Event()

    class Slow(ActorStatusDataProtocol):
        def __init__(self):
            self.calls = []

        async def read_actor_status(self, actor_id):
            self.calls.append(actor_id)
            try:
                await asyncio.Future()
            finally:
                stopped.set()

    slow = Slow()
    slow_app = create_app(
        _settings(monkeypatch, request_timeout_seconds=0.01),
        data=AuthenticationData(),
        actor_status_data=slow,
    )
    async with AsyncClient(
        transport=ASGITransport(app=slow_app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/actor-status",
            headers=headers,
            json=_status_request(uuid.uuid4()),
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "Identity actor status unavailable"}
    assert stopped.is_set()


def test_admission_token_sources_are_canonical_distinct_and_redacted(
    monkeypatch, tmp_path
):
    configured = _settings(
        monkeypatch,
        admission_token=SecretStr(ADMISSION_TOKEN),
    )
    assert configured.admission_token.get_secret_value() == ADMISSION_TOKEN

    scoped_file = tmp_path / "admission-token"
    scoped_file.write_text(f"{ADMISSION_TOKEN}\n", encoding="utf-8")
    file_settings = _settings(
        monkeypatch,
        admission_token_file=str(scoped_file),
    )
    assert file_settings.admission_token.get_secret_value() == ADMISSION_TOKEN

    invalid = [
        {"admission_token": SecretStr(" " + ADMISSION_TOKEN)},
        {"admission_token": SecretStr(ADMISSION_TOKEN + " ")},
        {
            "admission_token": SecretStr("same-as-general"),
            "token": SecretStr(" same-as-general "),
        },
        {
            "admission_token": SecretStr(ADMISSION_TOKEN),
            "admission_token_file": str(scoped_file),
        },
        {"admission_token": SecretStr("")},
    ]
    for overrides in invalid:
        secret_values = [
            value.get_secret_value()
            for key, value in overrides.items()
            if key == "admission_token" and isinstance(value, SecretStr)
        ]
        with pytest.raises(ValidationError) as error:
            _settings(monkeypatch, **overrides)
        assert all(secret not in str(error.value) for secret in secret_values if secret)

    oversized = tmp_path / "oversized-admission"
    oversized.write_text("secret-value-" * 500, encoding="utf-8")
    with pytest.raises(ValidationError) as error:
        _settings(monkeypatch, admission_token_file=str(oversized))
    assert "secret-value" not in str(error.value)


def test_actor_status_dsn_sources_are_optional_but_unambiguous(monkeypatch, tmp_path):
    assert _settings(monkeypatch).actor_status_database_url is None
    dsn_file = tmp_path / "actor-status-dsn"
    dsn_file.write_text("postgresql://status.invalid/db\n", encoding="utf-8")
    configured = _settings(monkeypatch, actor_status_database_url_file=str(dsn_file))
    assert (
        configured.actor_status_database_url.get_secret_value()
        == "postgresql://status.invalid/db"
    )
    with pytest.raises(ValidationError):
        _settings(
            monkeypatch,
            actor_status_database_url=SecretStr("postgresql://one.invalid/db"),
            actor_status_database_url_file=str(dsn_file),
        )
