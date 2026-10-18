"""Administrator invitation and storage-connection CLI safety boundaries."""

import json
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from ai_annotation.cli import invitations as invitations_cli
from ai_annotation.cli import storage_connections as storage_cli
from ai_annotation.invitation_models import InvitationEmailSent, InvitationResend
from ai_annotation.storage_connection_models import (
    StorageConnection,
    StorageConnectionTestResult,
)

INVITATION_ID, CONNECTION_ID, CREATOR_ID = [str(uuid4()) for _ in range(3)]
CONNECTION = StorageConnection.model_validate(
    {
        "id": CONNECTION_ID,
        "name": "archive",
        "kind": "s3",
        "config": {"endpoint": "https://s3.example.test", "bucket": "data"},
        "scope": "owner",
        "secret_set": True,
        "created_by": CREATOR_ID,
        "created_at": "2026-10-17T00:00:00Z",
        "updated_at": "2026-10-17T00:00:00Z",
    }
)
runner = CliRunner()


@pytest.mark.parametrize(
    ("module", "args", "input_text"),
    [
        (invitations_cli, ["revoke", INVITATION_ID, "--json"], None),
        (invitations_cli, ["resend", INVITATION_ID, "--json"], None),
        (invitations_cli, ["send-email", INVITATION_ID, "--json"], None),
        (
            storage_cli,
            ["create", "--body", "-", "--json"],
            json.dumps(
                {
                    "name": "archive",
                    "kind": "s3",
                    "config": {"endpoint": "https://s3.example.test", "bucket": "data"},
                    "secret": {"access_key": "AK-private", "secret_key": "SK-private"},
                }
            ),
        ),
        (
            storage_cli,
            ["update", CONNECTION_ID, "--body", "-", "--json"],
            json.dumps({"name": "renamed"}),
        ),
        (storage_cli, ["delete", CONNECTION_ID, "--json"], None),
        (storage_cli, ["test", CONNECTION_ID, "--json"], None),
        (
            storage_cli,
            ["allowlist", "update", "--body", "-", "--json"],
            json.dumps({"entries": []}),
        ),
        (storage_cli, ["allowlist", "reset", "--json"], None),
    ],
)
def test_mutating_or_external_commands_require_yes_before_client(
    monkeypatch, module, args, input_text
):
    opened = []
    monkeypatch.setattr(
        module,
        "get_client",
        lambda *_args: opened.append(True) or nullcontext(SimpleNamespace()),
    )
    app = module.app

    result = runner.invoke(app, args, input=input_text)

    assert result.exit_code == 2, result.output
    assert "--yes" in result.output
    assert opened == []


@pytest.mark.parametrize("command", ["create", "update"])
def test_storage_secret_stdin_is_not_echoed(command, monkeypatch):
    secret_value = "SFTP-PRIVATE-SECRET-DO-NOT-ECHO"
    if command == "create":
        api = SimpleNamespace(create=lambda _body, **_kwargs: CONNECTION)
        args = ["create", "--body", "-", "--yes", "--json"]
        body = {
            "name": "archive",
            "kind": "sftp",
            "config": {"host": "sftp.example.test", "username": "svc"},
            "secret": {"password": secret_value},
        }
    else:
        api = SimpleNamespace(update=lambda _id, _body, **_kwargs: CONNECTION)
        args = ["update", CONNECTION_ID, "--body", "-", "--yes", "--json"]
        body = {"secret": {"password": secret_value}}
    monkeypatch.setattr(
        storage_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(storage_connections=api)),
    )

    result = runner.invoke(storage_cli.app, args, input=json.dumps(body))

    assert result.exit_code == 0, result.output
    assert secret_value not in result.stdout
    assert secret_value not in result.stderr
    assert json.loads(result.stdout)["secret_set"] is True


def test_storage_connection_cli_forwards_caller_idempotency_keys(monkeypatch):
    key = "24000209-03c1-45bd-a2fb-90216c2d80c8"
    sent = []

    def create(_body, *, idempotency_key=None):
        sent.append(("create", idempotency_key))
        return CONNECTION

    def update(_id, _body, *, idempotency_key=None):
        sent.append(("update", idempotency_key))
        return CONNECTION

    def delete(_id, *, idempotency_key=None):
        sent.append(("delete", idempotency_key))

    def test(_id, *, idempotency_key=None):
        sent.append(("test", idempotency_key))
        return StorageConnectionTestResult(ok=True, message="连接成功", sample_count=1)

    api = SimpleNamespace(
        create=create,
        update=update,
        delete=delete,
        test=test,
    )
    monkeypatch.setattr(
        storage_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(storage_connections=api)),
    )

    create_result = runner.invoke(
        storage_cli.app,
        ["create", "--body", "-", "--yes", "--idempotency-key", key, "--json"],
        input=json.dumps(
            {
                "name": "archive",
                "kind": "s3",
                "config": {"endpoint": "https://s3.example.test", "bucket": "data"},
                "secret": {"access_key": "AK-private", "secret_key": "SK-private"},
            }
        ),
    )
    update_result = runner.invoke(
        storage_cli.app,
        [
            "update",
            CONNECTION_ID,
            "--body",
            "-",
            "--yes",
            "--idempotency-key",
            key,
            "--json",
        ],
        input=json.dumps({"name": "renamed"}),
    )
    delete_result = runner.invoke(
        storage_cli.app,
        ["delete", CONNECTION_ID, "--yes", "--idempotency-key", key, "--json"],
    )
    test_result = runner.invoke(
        storage_cli.app,
        ["test", CONNECTION_ID, "--yes", "--idempotency-key", key, "--json"],
    )

    assert [
        create_result.exit_code,
        update_result.exit_code,
        delete_result.exit_code,
        test_result.exit_code,
    ] == [0, 0, 0, 0]
    assert sent == [
        ("create", key),
        ("update", key),
        ("delete", key),
        ("test", key),
    ]
    assert "AK-private" not in create_result.stdout
    assert "SK-private" not in create_result.stdout


def test_storage_connection_cli_rejects_invalid_idempotency_key_before_client(
    monkeypatch,
):
    opened = []
    monkeypatch.setattr(
        storage_cli,
        "get_client",
        lambda *_args: opened.append(True) or nullcontext(SimpleNamespace()),
    )

    result = runner.invoke(
        storage_cli.app,
        ["delete", CONNECTION_ID, "--yes", "--idempotency-key", "not-a-uuid", "--json"],
    )

    assert result.exit_code == 2
    assert opened == []


def test_storage_negative_connection_result_is_json_not_cli_exception(monkeypatch):
    api = SimpleNamespace(
        test=lambda _id, **_kwargs: StorageConnectionTestResult(
            ok=False, message="连接失败: ConnectionError", sample_count=None
        )
    )
    monkeypatch.setattr(
        storage_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(storage_connections=api)),
    )

    result = runner.invoke(storage_cli.app, ["test", CONNECTION_ID, "--yes", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": False,
        "message": "连接失败: ConnectionError",
        "sample_count": None,
    }


def test_invitation_send_email_is_separate_from_token_resend(monkeypatch):
    sent = []
    response = InvitationEmailSent(
        invitation_id=INVITATION_ID,
        email="person@example.test",
        invite_url="https://app.example.test/register?token=existing",
        message="sent",
    )
    api = SimpleNamespace(
        send_email=lambda invitation_id: sent.append(invitation_id) or response
    )
    monkeypatch.setattr(
        invitations_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(invitations=api)),
    )

    result = runner.invoke(
        invitations_cli.app,
        ["send-email", INVITATION_ID, "--yes", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert sent == [INVITATION_ID]
    assert "existing" in json.loads(result.stdout)["invite_url"]


def test_invitation_resend_requires_secret_output_opt_in_before_client(monkeypatch):
    opened = []
    monkeypatch.setattr(
        invitations_cli,
        "get_client",
        lambda *_args: opened.append(True) or nullcontext(SimpleNamespace()),
    )

    result = runner.invoke(
        invitations_cli.app,
        ["resend", INVITATION_ID, "--yes"],
    )

    assert result.exit_code == 2
    assert "--show-secret" in result.output and "--json" in result.output
    assert opened == []


def test_invitation_resend_show_secret_explicitly_displays_rotated_token(monkeypatch):
    rotated = "rotated-private-token"
    response = InvitationResend(
        invite_url=f"https://app.example.test/register?token={rotated}",
        token=rotated,
        expires_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )
    api = SimpleNamespace(resend=lambda _id: response)
    monkeypatch.setattr(
        invitations_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(invitations=api)),
    )

    result = runner.invoke(
        invitations_cli.app,
        ["resend", INVITATION_ID, "--yes", "--show-secret"],
    )

    assert result.exit_code == 0, result.output
    assert rotated in result.stdout


def test_invitation_send_email_text_omits_token_link(monkeypatch):
    token = "existing-private-token"
    response = InvitationEmailSent(
        invitation_id=INVITATION_ID,
        email="person@example.test",
        invite_url=f"https://app.example.test/register?token={token}",
        message="sent",
    )
    api = SimpleNamespace(send_email=lambda _id: response)
    monkeypatch.setattr(
        invitations_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(invitations=api)),
    )

    result = runner.invoke(
        invitations_cli.app,
        ["send-email", INVITATION_ID, "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert token not in result.stdout
    assert "register?token" not in result.stdout
    assert "person@example.test" in result.stdout


def test_invitation_revoke_output_describes_soft_revocation(monkeypatch):
    revoked = []
    api = SimpleNamespace(revoke=lambda invitation_id: revoked.append(invitation_id))
    monkeypatch.setattr(
        invitations_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(invitations=api)),
    )

    result = runner.invoke(
        invitations_cli.app,
        ["revoke", INVITATION_ID, "--yes", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert revoked == [INVITATION_ID]
    assert json.loads(result.stdout) == {
        "revoked": True,
        "invitation_id": INVITATION_ID,
    }
