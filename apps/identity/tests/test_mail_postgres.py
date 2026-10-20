from __future__ import annotations

import json
import sys
import types
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aap_identity.contract import IdentityUnavailable
from aap_identity.mail_contract import MAIL_NATIVE_PROTOCOL
from aap_identity.mail_postgres import MailPostgresData, MailSettingsPostgresData


def _lease(worker_id: uuid.UUID) -> dict:
    now = datetime.now(UTC)
    return {
        "id": str(uuid.uuid4()),
        "actor_id": str(uuid.uuid4()),
        "purpose": "password_reset",
        "email": "user@example.test",
        "frontend_base_url": "https://example.test",
        "token_hash": "a" * 64,
        "token_ciphertext": "ciphertext",
        "encryption_id": "b" * 64,
        "expires_at": (now + timedelta(minutes=55)).isoformat(),
        "attempt": 1,
        "worker_id": str(worker_id),
        "lease_token": "lease-token",
        "lease_expires_at": (now + timedelta(seconds=60)).isoformat(),
    }


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class FakeConnection:
    def __init__(self, calls):
        self.calls = calls

    def transaction(self):
        return FakeTransaction()

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if "has_function_privilege" in sql:
            return True
        if "mail_policy()" in sql:
            return json.dumps(
                {
                    "schema": "aap-identity-recovery-mail-policy/v1",
                    "policy": "aap-identity-recovery-mail/v1",
                }
            )
        if "claim_mail" in sql:
            request = json.loads(args[0])
            return json.dumps(
                {
                    "schema": MAIL_NATIVE_PROTOCOL,
                    "ok": True,
                    "data": {"delivery": _lease(uuid.UUID(request["worker_id"]))},
                }
            )
        if "renew_mail" in sql or "finish_mail" in sql:
            return json.dumps(
                {
                    "schema": MAIL_NATIVE_PROTOCOL,
                    "ok": True,
                    "data": {"accepted": True},
                }
            )
        if "mail_delivery_policy" in sql:
            return json.dumps(
                {
                    "smtp_host": "smtp.example.test",
                    "smtp_port": 587,
                    "smtp_user": "mailer",
                    "smtp_password": "smtp-secret",
                    "smtp_from": "noreply@example.test",
                }
            )
        raise AssertionError("Unexpected SQL capability")

    async def close(self, *, timeout):
        return None


@pytest.fixture
def fake_asyncpg(monkeypatch):
    module = types.ModuleType("asyncpg")
    module.PostgresError = type("PostgresError", (Exception,), {})
    module.InterfaceError = type("InterfaceError", (Exception,), {})
    calls = []

    async def connect(*_args, **_kwargs):
        return FakeConnection(calls)

    module.connect = connect
    monkeypatch.setitem(sys.modules, "asyncpg", module)
    return calls


@pytest.mark.asyncio
async def test_mail_data_uses_only_closed_native_functions_and_validates_policy(
    fake_asyncpg,
):
    data = MailPostgresData("postgresql://role/db")
    capability = await data.mail_policy()
    worker_id = uuid.uuid4()
    lease = await data.claim_mail(worker_id, lease_seconds=999, delivery_id=None)
    assert capability.policy == "aap-identity-recovery-mail/v1"
    assert lease is not None
    assert lease.worker_id == worker_id
    assert await data.renew_mail(lease) is True
    assert await data.finish_mail(
        lease,
        outcome="retry",
        reason="connect_failed",
        retry_delay_seconds=30,
    )

    functions = [sql for sql, _ in fake_asyncpg if "SELECT " in sql]
    assert any("claim_mail($1::jsonb)" in sql for sql in functions)
    assert any("renew_mail($1::jsonb)" in sql for sql in functions)
    assert any("finish_mail($1::jsonb)" in sql for sql in functions)
    assert all("FROM " not in sql or "unnest(" in sql for sql in functions)
    claim_payload = next(args[0] for sql, args in fake_asyncpg if "claim_mail" in sql)
    assert json.loads(claim_payload) == {
        "schema": MAIL_NATIVE_PROTOCOL,
        "worker_id": str(worker_id),
        "lease_seconds": 300,
        "delivery_id": None,
    }


@pytest.mark.asyncio
async def test_mail_settings_client_calls_only_its_capability(fake_asyncpg):
    settings = MailSettingsPostgresData("postgresql://settings-role/db")
    fallbacks = {
        "smtp_host": "",
        "smtp_port": 0,
        "smtp_user": "",
        "smtp_password": "",
        "smtp_from": "",
    }
    result = await settings.mail_delivery_policy(fallbacks)
    assert result.smtp_host == "smtp.example.test"
    assert "smtp-secret" not in repr(result)
    assert [sql for sql, _ in fake_asyncpg if "SELECT " in sql] == [
        "SELECT aap_settings_v1.mail_delivery_policy($1::jsonb)",
        "SELECT has_function_privilege(current_user, "
        "'aap_settings_v1.mail_delivery_policy(jsonb)', 'EXECUTE')",
    ]


@pytest.mark.asyncio
async def test_mail_settings_client_rejects_extra_fallback_fields(fake_asyncpg):
    settings = MailSettingsPostgresData("postgresql://settings-role/db")
    with pytest.raises(IdentityUnavailable):
        await settings.mail_delivery_policy(
            {
                "smtp_host": "",
                "smtp_port": 587,
                "smtp_user": "",
                "smtp_password": "",
                "smtp_from": "",
                "token": "not-allowed",
            }
        )
    assert fake_asyncpg == []
