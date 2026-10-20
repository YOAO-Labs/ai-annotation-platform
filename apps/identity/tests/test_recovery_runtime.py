from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from aap_identity.recovery_contract import (
    NativeRecoveryPolicyReply,
    NativeRecoveryResult,
    decode_native_recovery_apply_command,
    decode_native_recovery_prepare_command,
)
from aap_identity.recovery_crypto import mail_key_from_secret
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings
from pydantic import ValidationError
from recovery_helpers import (
    CONTROL,
    SECRET,
    apply_reply,
    prepare_reply,
    recovery_request,
)


class Transaction:
    async def prepare_recovery_command(self, payload):
        command = decode_native_recovery_prepare_command(payload)
        return prepare_reply(command, eligible=False)

    async def apply_recovery_command(self, payload):
        command = decode_native_recovery_apply_command(payload)
        result = NativeRecoveryResult(
            account=None,
            claims=None,
            email_verification_required=None,
            completed=None,
        )
        return apply_reply(command, result)


class Data:
    @asynccontextmanager
    async def transaction(self):
        yield Transaction()

    async def recovery_policy(self):
        return NativeRecoveryPolicyReply(
            schema="aap-identity-recovery-policy/v1",
            policy="aap-identity-recovery/v1",
        )


def settings(**changes):
    values = {
        "database_url": "postgresql://unused-auth-database",
        "token": CONTROL,
        "signing_secret": SECRET,
        "recovery_database_url": "postgresql://unused-recovery-database",
        "recovery_mail_key": mail_key_from_secret(SECRET),
    }
    values.update(changes)
    return Settings(**values)


def test_independent_recovery_requires_an_explicit_mail_key():
    with pytest.raises(
        ValidationError, match="recovery_database_url requires recovery_mail_key"
    ):
        settings(recovery_mail_key=None)


@pytest.mark.asyncio
async def test_private_recovery_endpoint_requires_control_bearer_and_json():
    app = create_app(settings(), recovery_data=Data())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        unauthorized = await client.post("/v1/identity/recovery/execute")
        wrong_type = await client.post(
            "/v1/identity/recovery/execute",
            headers={
                "authorization": "Bearer " + CONTROL,
                "content-type": "text/plain",
            },
            content="{}",
        )
    assert unauthorized.status_code == 401
    assert wrong_type.status_code == 400
    assert wrong_type.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_private_recovery_endpoint_accepts_bounded_command_without_smtp():
    app = create_app(settings(), recovery_data=Data())
    request = recovery_request("request_password_reset")
    body = request.model_dump(mode="json", by_alias=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/recovery/execute",
            headers={"authorization": "Bearer " + CONTROL},
            json=body,
        )
    assert response.status_code == 200
    assert response.json()["operation"] == "request_password_reset"
    assert response.json()["data"] == {
        "account": None,
        "claims": None,
        "access_token": None,
        "email_verification_required": None,
        "completed": None,
    }


@pytest.mark.asyncio
async def test_private_recovery_endpoint_rejects_oversized_wire_body():
    app = create_app(settings(), recovery_data=Data())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/recovery/execute",
            headers={
                "authorization": "Bearer " + CONTROL,
                "content-type": "application/json",
            },
            content=b"{" + b" " * 65536,
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_recovery_runtime_reads_optional_secret_files_and_rejects_conflicts(
    tmp_path,
):
    dsn_file = tmp_path / "recovery-dsn"
    key_file = tmp_path / "mail-key"
    dsn_file.write_text("postgresql://recovery-file\n")
    key_file.write_text(mail_key_from_secret(SECRET) + "\n")

    configured = Settings(
        database_url="postgresql://unused-auth-database",
        token=CONTROL,
        signing_secret=SECRET,
        recovery_database_url_file=str(dsn_file),
        recovery_mail_key_file=str(key_file),
    )
    assert configured.recovery_database_url.get_secret_value() == (
        "postgresql://recovery-file"
    )
    assert configured.recovery_mail_key.get_secret_value() == mail_key_from_secret(
        SECRET
    )

    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://unused-auth-database",
            token=CONTROL,
            signing_secret=SECRET,
            recovery_database_url="postgresql://direct",
            recovery_database_url_file=str(dsn_file),
        )
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://unused-auth-database",
            token=CONTROL,
            signing_secret=SECRET,
            recovery_mail_key=mail_key_from_secret(SECRET),
            recovery_mail_key_file=str(key_file),
        )


def test_invalid_explicit_mail_key_never_falls_back_to_derived_key():
    with pytest.raises(ValueError, match="Invalid mail encryption key"):
        create_app(
            settings(recovery_mail_key="explicit-invalid-key"), recovery_data=Data()
        )
