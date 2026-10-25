"""Recovery bindings validate owner responses before acknowledging persistence."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import uuid

import httpx
import pytest
from fastapi import HTTPException

from aap_identity.contract import IdentityUnavailable
from aap_identity.recovery_contract import RecoveryFailure, decode_recovery_request
from aap_identity.recovery_crypto import mail_key_from_secret
from aap_identity.recovery_manager import RecoveryManager
from app.api.v1.recovery_commands import _reject_recovery_error
from app.config import settings
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway
from app.services.identity_mail_delivery import IdentityMailCommandData
from app.services.identity_recovery_commands import IdentityRecoveryCommands
from app.services.identity_recovery_data import IdentityRecoveryCommandData


def _command():
    return decode_recovery_request(
        {
            "schema": "aap-identity-recovery/v1",
            "operation": "request_password_reset",
            "operation_id": str(uuid.uuid4()),
            "audit_context": {
                "method": "POST",
                "path": "/api/v1/auth/forgot-password",
                "ip": "127.0.0.1",
                "request_id": None,
            },
            "payload": {"email": "recovery@example.test", "captcha_verified": True},
        }
    )


def _reply(command):
    return {
        "schema": "aap-identity-recovery/v1",
        "ok": True,
        "operation": command.operation,
        "operation_id": str(command.operation_id),
        "data": dict.fromkeys(
            (
                "account",
                "claims",
                "access_token",
                "email_verification_required",
                "completed",
            )
        ),
    }


def _remote(monkeypatch, respond):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.identity_recovery_commands.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    return IdentityRecoveryCommands(
        object(),
        IdentityGateway(identity_url="http://identity.test", identity_token="control"),
    )


async def test_remote_recovery_uses_closed_command_and_no_local_account_lookup(
    monkeypatch,
):
    command = _command()

    def respond(request):
        assert request.url.path == "/v1/identity/recovery/execute"
        assert request.headers["authorization"] == "Bearer control"
        assert json.loads(request.content) == command.model_dump(
            mode="json", by_alias=True
        )
        return httpx.Response(200, json=_reply(command))

    assert (await _remote(monkeypatch, respond).execute(command)).data.completed is None


@pytest.mark.parametrize(
    "fault", ["operation", "operation_id", "extra_token", "boolean_ok", "capability"]
)
async def test_wrong_or_unavailable_remote_reply_never_selects_a_local_owner(
    monkeypatch, fault
):
    command = _command()
    body = _reply(command)
    if fault == "operation":
        body["operation"] = "resend_email_verification"
    elif fault == "operation_id":
        body["operation_id"] = str(uuid.uuid4())
    elif fault == "extra_token":
        body["data"]["token"] = "must-not-escape"
    elif fault == "boolean_ok":
        body["ok"] = 1

    def forbidden(*args, **kwargs):
        raise AssertionError("Remote failures must not select a local owner")

    monkeypatch.setattr(
        "app.services.identity_recovery_commands.IdentityRecoveryCommandData", forbidden
    )
    gateway = _remote(
        monkeypatch,
        lambda _request: httpx.Response(
            503 if fault == "capability" else 200, json=body
        ),
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await gateway.execute(command)


async def test_explicit_recovery_dsn_file_failure_does_not_select_caller_transaction(
    monkeypatch,
):
    monkeypatch.setattr(settings, "identity_recovery_database_url", "")
    monkeypatch.setattr(
        settings, "identity_recovery_database_url_file", "/missing-recovery-dsn-fixture"
    )
    gateway = IdentityRecoveryCommands(
        object(), IdentityGateway(signing_secret="fixture-only")
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await gateway.execute(_command())


async def test_dedicated_recovery_mail_requires_both_narrow_delivery_bindings(
    monkeypatch,
):
    from app.services import identity_mail_delivery

    monkeypatch.setattr(
        identity_mail_delivery,
        "get_identity_gateway",
        lambda: IdentityGateway(signing_secret="fixture-only"),
    )
    monkeypatch.setattr(
        settings,
        "identity_recovery_database_url",
        "postgresql://fixture@localhost/recovery_test",
    )
    for name in (
        "identity_recovery_database_url_file",
        "identity_mail_database_url",
        "identity_mail_database_url_file",
        "identity_mail_settings_database_url",
        "identity_mail_settings_database_url_file",
    ):
        monkeypatch.setattr(settings, name, "")

    def forbidden():
        raise AssertionError("Explicit owner selection must not poll the API database")

    with pytest.raises(IdentityGatewayUnavailable):
        await identity_mail_delivery.run_credential_mail(session_factory=forbidden)


class _Session:
    identity_map = {}

    def __init__(self, reply):
        self.reply = reply
        self.events = []

    async def flush(self):
        self.events.append("flush")

    async def scalar(self, statement, parameters):
        self.events.append(str(statement))
        return self.reply(str(statement), parameters)

    async def commit(self):
        self.events.append("commit")

    async def rollback(self):
        self.events.append("rollback")


async def test_invalid_native_apply_reply_rolls_back_before_acknowledging_command():
    command = _command()

    def reply(statement, parameters):
        native = json.loads(parameters["command"])
        assert statement.startswith("SELECT aap_identity_recovery_v1.")
        if "prepare_recovery_command" in statement:
            return {
                "schema": "aap-identity-recovery-native/v1",
                "ok": True,
                "operation": native["operation"],
                "operation_id": native["operation_id"],
                "data": {
                    "actor": None,
                    "password_hash": None,
                    "token": None,
                    "eligible": False,
                    "replay": None,
                    "policy": {
                        **native["config"],
                        "password_reset_ttl_seconds": 3600,
                        "email_verification_ttl_seconds": 86400,
                    },
                },
            }
        return {
            "schema": "aap-identity-recovery-native/v1",
            "ok": True,
            "wrong": "binding",
        }

    db = _Session(reply)
    with pytest.raises(IdentityUnavailable):
        await RecoveryManager(
            IdentityRecoveryCommandData(db),
            "fixture-only",
            mail_key_from_secret("fixture-only"),
        ).execute(command)
    assert db.events[-1] == "rollback" and "commit" not in db.events


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            {
                "schema": "aap-identity-recovery-mail-native/v1",
                "ok": True,
                "data": {"accepted": False},
            },
            False,
        ),
        (
            {
                "schema": "aap-identity-recovery-mail-native/v1",
                "ok": 1,
                "data": {"accepted": True},
            },
            None,
        ),
    ],
)
async def test_mail_stale_or_invalid_reply_is_not_committed(raw, expected):
    db = _Session(lambda *_args: raw)

    @asynccontextmanager
    async def session():
        yield db

    runner = IdentityMailCommandData(session)
    lease = SimpleNamespace(
        id=uuid.uuid4(), worker_id=uuid.uuid4(), lease_token="fixture-lease"
    )
    if expected is None:
        with pytest.raises(IdentityUnavailable):
            await runner.renew_mail(lease)
    else:
        assert await runner.renew_mail(lease) is expected
    assert db.events[-1] == "rollback" and "commit" not in db.events


@pytest.mark.parametrize(
    "operation,detail",
    [
        ("consume_password_reset", "重置链接无效或已过期"),
        ("consume_email_verification", "验证链接无效或已过期"),
    ],
)
def test_invalid_link_keeps_the_public_error(operation, detail):
    with pytest.raises(HTTPException) as failure:
        _reject_recovery_error(RecoveryFailure("invalid_request"), operation)
    assert failure.value.status_code == 400 and failure.value.detail == detail
