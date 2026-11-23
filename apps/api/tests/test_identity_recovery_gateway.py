"""Recovery bindings validate owner responses before acknowledging persistence."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi import HTTPException

from yoao_identity.contract import IdentityUnavailable
from yoao_identity.recovery_contract import RecoveryFailure, decode_recovery_request
from yoao_identity.recovery_crypto import mail_key_from_secret
from yoao_identity.recovery_manager import RecoveryManager
from app.api.v1.recovery_commands import _reject_recovery_error
from app.config import settings
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway
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


@pytest.mark.parametrize(
    "dsn",
    [
        "sqlite://fixture@localhost/recovery_test",
        "postgresql://localhost/recovery_test",
        "postgresql://fixture@localhost/recovery_test?host=other",
    ],
)
async def test_explicit_recovery_dsn_never_selects_an_implicit_login(monkeypatch, dsn):
    monkeypatch.setattr(settings, "identity_recovery_database_url", dsn)
    monkeypatch.setattr(settings, "identity_recovery_database_url_file", "")
    gateway = IdentityRecoveryCommands(
        object(), IdentityGateway(signing_secret="fixture-only")
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await gateway.execute(_command())


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
        assert statement.startswith("SELECT yoao_identity_recovery_v1.")
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
