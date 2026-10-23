from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import ValidationError

from aap_identity.account_contract import (
    NATIVE_ACCOUNT_PROTOCOL,
    AccountResultData,
    NativeAccountReply,
    NativePrepareData,
    NativePrepareReply,
    NativeAccountPolicyReply,
    decode_native_apply_command,
    decode_native_prepare_command,
)
from aap_identity.contract import Principal, principal_reply
from aap_identity.presence_contract import (
    PRESENCE_PROTOCOL,
    PresenceReply,
)
from aap_identity.security import ISSUER
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings

from account_helpers import ACCOUNT_PROTOCOL, account_record
from session_helpers import CONTROL, SECRET, make_jwt


def settings(**changes):
    return Settings(
        database_url="postgresql://auth-unused",
        token=CONTROL,
        signing_secret=SECRET,
        **changes,
    )


def account_body(token, *, operation="read_self_account", payload=None):
    return {
        "schema": ACCOUNT_PROTOCOL,
        "operation": operation,
        "operation_id": str(uuid.uuid4()),
        "token": token,
        "audit_context": {
            "method": "PATCH",
            "path": "/api/v1/auth/me",
            "ip": "192.0.2.10",
            "request_id": str(uuid.uuid4()),
        },
        "payload": payload
        if payload is not None
        else ({"patch": {}} if operation == "patch_preferences" else {}),
    }


class IdentityData:
    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": ISSUER,
            "refresh_grace_seconds": 604800,
        }

    async def read_jwt_principal(
        self, actor_id, jti, generation, expires_at, for_refresh=False
    ):
        return principal_reply(
            Principal(
                actor_id=actor_id,
                name="Current account",
                email="person@example.test",
                role="employee",
                is_active=True,
                credential_type="jwt",
                credential_id=jti,
                generation=generation,
                scopes=None,
                expires_at=expires_at,
            )
        )


class AccountTransaction:
    def __init__(self, data):
        self.data = data

    async def prepare_account_command(self, payload):
        assert self.data.in_transaction
        command = decode_native_prepare_command(payload)
        self.data.prepare_calls.append(command)
        if self.data.failure:
            return {
                "schema": NATIVE_ACCOUNT_PROTOCOL,
                "ok": False,
                "error": {
                    "code": self.data.failure,
                    "status_code": None,
                    "detail": None,
                },
            }
        return NativePrepareReply(
            schema=NATIVE_ACCOUNT_PROTOCOL,
            ok=True,
            operation=command.operation,
            operation_id=command.operation_id,
            principal=command.actor,
            data=NativePrepareData(
                account=account_record(), password_hash=None, replay=None
            ),
        )

    async def apply_account_command(self, payload):
        assert self.data.in_transaction
        command = decode_native_apply_command(payload)
        self.data.apply_calls.append(command)
        if command.operation == "patch_preferences":
            result = AccountResultData(
                account=None,
                preferences=command.response_preferences,
                previous_avatar_ref=None,
                changed=None,
            )
        elif command.operation == "update_name":
            result = AccountResultData(
                account=account_record(name=command.new_value),
                preferences=None,
                previous_avatar_ref=None,
                changed=True,
            )
        elif command.operation == "set_avatar_ref":
            changed = command.base_value != command.new_value.avatar_ref
            result = AccountResultData(
                account=account_record(avatar_ref=command.new_value.avatar_ref),
                preferences=None,
                previous_avatar_ref=command.base_value if changed else None,
                changed=changed,
            )
        else:
            result = AccountResultData(
                account=None,
                preferences=None,
                previous_avatar_ref=None,
                changed=None,
            )
        return NativeAccountReply(
            schema=NATIVE_ACCOUNT_PROTOCOL,
            ok=True,
            operation=command.operation,
            operation_id=command.operation_id,
            principal=command.actor,
            data=result,
        )


class AccountData:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.prepare_calls = []
        self.apply_calls = []
        self.in_transaction = False
        self.committed = False
        self.rolled_back = False

    async def account_policy(self):
        return NativeAccountPolicyReply(
            schema="aap-identity-accounts-policy/v1",
            policy="aap-identity-accounts/v1",
        )

    @asynccontextmanager
    async def transaction(self):
        self.in_transaction = True
        try:
            yield AccountTransaction(self)
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True
        finally:
            self.in_transaction = False


class PresenceData:
    def __init__(self):
        self.cutoffs = []

    async def check_capability(self):
        return None

    async def mark_inactive_accounts_offline(self, cutoff):
        self.cutoffs.append(cutoff)
        return PresenceReply(schema=PRESENCE_PROTOCOL, affected=7)


@pytest.mark.asyncio
async def test_auth_only_readiness_stays_ready_and_account_command_is_disabled():
    app = create_app(settings(), data=IdentityData())
    body = account_body(make_jwt())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        ready = await client.get("/ready")
        unavailable = await client.post(
            "/v1/identity/accounts/execute",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json=body,
        )
    assert ready.status_code == 200
    assert unavailable.status_code == 503 and body["token"] not in unavailable.text


@pytest.mark.asyncio
async def test_account_readiness_checks_policy_and_preserves_large_preferences_body():
    data = AccountData()
    app = create_app(
        settings(account_database_url="postgresql://account-unused"),
        data=IdentityData(),
        account_data=data,
    )
    token = make_jwt()
    large = "x" * 80_000
    body = account_body(
        token,
        operation="patch_preferences",
        payload={"patch": {"ai": {"model_by_backend": {"large": large}}}},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        ready = await client.get("/ready")
        reply = await client.post(
            "/v1/identity/accounts/execute",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json=body,
        )
    assert ready.status_code == 200
    assert reply.status_code == 200
    assert (
        len(reply.json()["data"]["preferences"]["ai"]["model_by_backend"]["large"])
        == 80_000
    )
    assert token not in reply.text and data.committed


@pytest.mark.asyncio
async def test_account_runtime_failure_is_closed_and_control_auth_is_required():
    data = AccountData(failure="operation_conflict")
    app = create_app(
        settings(account_database_url="postgresql://account-unused"),
        data=IdentityData(),
        account_data=data,
    )
    token = make_jwt()
    body = account_body(token)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        unauthorized = await client.post(
            "/v1/identity/accounts/execute",
            headers={"Authorization": "Bearer wrong"},
            json=body,
        )
        failure = await client.post(
            "/v1/identity/accounts/execute",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json=body,
        )
    assert unauthorized.status_code == 401
    assert failure.status_code == 200
    assert failure.json() == {
        "schema": ACCOUNT_PROTOCOL,
        "ok": False,
        "error": {"code": "operation_conflict", "status_code": None, "detail": None},
    }
    assert token not in failure.text
    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_presence_endpoint_is_optional_strict_and_uses_operator_port():
    app_without = create_app(settings(), data=IdentityData())
    request = {
        "schema": PRESENCE_PROTOCOL,
        "cutoff": "2026-10-20T01:02:03Z",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app_without), base_url="http://identity"
    ) as client:
        unavailable = await client.post(
            "/v1/identity/presence/mark-inactive",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json=request,
        )
    assert unavailable.status_code == 503

    data = PresenceData()
    app = create_app(
        settings(presence_database_url="postgresql://presence-unused"),
        data=IdentityData(),
        presence_data=data,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        ready = await client.get("/ready")
        reply = await client.post(
            "/v1/identity/presence/mark-inactive",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json=request,
        )
        invalid = await client.post(
            "/v1/identity/presence/mark-inactive",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json={"schema": PRESENCE_PROTOCOL, "cutoff": "2026-10-20T01:02:03"},
        )
    assert ready.status_code == 200
    assert reply.status_code == 200
    assert reply.json() == {"schema": PRESENCE_PROTOCOL, "affected": 7}
    assert data.cutoffs[0].utcoffset().total_seconds() == 0
    assert invalid.status_code == 400


def test_account_and_presence_dsns_accept_exclusive_direct_or_small_file_sources(
    tmp_path,
):
    account_file = tmp_path / "account-dsn"
    account_file.write_text("postgresql://account-file\n")
    configured = settings(
        account_database_url_file=str(account_file),
        presence_database_url="postgresql://presence-direct",
    )
    assert (
        configured.account_database_url.get_secret_value()
        == "postgresql://account-file"
    )
    assert (
        configured.presence_database_url.get_secret_value()
        == "postgresql://presence-direct"
    )

    with pytest.raises(ValidationError):
        settings(
            account_database_url="postgresql://account-direct",
            account_database_url_file=str(account_file),
        )

    oversized = tmp_path / "oversized-dsn"
    oversized.write_text("x" * 4097)
    with pytest.raises(ValidationError):
        settings(account_database_url_file=str(oversized))
