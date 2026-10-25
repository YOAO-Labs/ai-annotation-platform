"""Invitation HTTP/owner composition keeps value boundaries and durable send polling."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from jose import jwt
from starlette.requests import Request

from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.invitation_contract import (
    NativeInvitationConfig,
    NativeInvitationDerived,
    decode_invitation_reply,
    decode_invitation_request,
    native_invitation_apply_command,
    native_invitation_prepare_command,
)
from app.api.v1 import invitations, invitations_admin, users
from app.api.v1.invitation_commands import (
    execute_invitation,
    get_authenticated_invitation_commands,
    get_invitation_commands,
    get_invitation_manager_commands,
    require_invitation_manager,
)
from app.config import settings
from app.deps import get_current_principal, get_current_user, get_db
from app.schemas.user import UserOut
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway
from app.services.identity_invitation_commands import IdentityInvitationCommands
from app.services.identity_invitation_data import IdentityInvitationCommandData


def _principal(role="super_admin"):
    return Principal(
        actor_id=uuid.uuid4(),
        name="Invitation manager",
        email="manager@invite.test",
        role=role,
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=2**63 - 1,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def _command(operation="list_invitations", payload=None):
    return decode_invitation_request(
        {
            "schema": "aap-identity-invitations/v1",
            "operation": operation,
            "operation_id": str(uuid.uuid4()),
            "token": None
            if operation in {"resolve_invitation", "register_invitation"}
            else "fixture-token",
            "payload": payload or {},
            "audit_context": {
                "method": "GET",
                "path": "/api/v1/invitations",
                "ip": None,
                "request_id": None,
            },
        }
    )


def _reply(command, actor, *, mail_status="sent"):
    now = datetime.now(UTC)
    proof = "邀请+token"
    link = "http://frontend.test/register?" + urlencode({"token": proof})
    record = {
        "id": str(uuid.uuid4()),
        "email": "target@invite.test",
        "role": "employee",
        "group_name": None,
        "project_id": None,
        "project_name": None,
        "project_member_role": None,
        "status": "pending",
        "expires_at": (now + timedelta(days=7)).isoformat(),
        "invited_by": str(actor.actor_id),
        "invited_by_name": actor.name,
        "accepted_at": None,
        "revoked_at": None,
        "created_at": now.isoformat(),
    }
    operation = command.operation
    if operation in {"create_invitation", "resend_invitation"}:
        data = {"token": proof, "invite_url": link, "expires_at": record["expires_at"]}
    elif operation == "list_invitations":
        data = {"items": [record]}
    elif operation == "query_invitations":
        data = {
            "items": [record],
            "total": 1,
            "page": command.payload.page,
            "page_size": command.payload.page_size,
            "pages": 1,
        }
    elif operation == "invitation_stats":
        data = {"total": 1, "pending": 1, "accepted": 0, "expired": 0, "revoked": 0}
    elif operation == "export_invitations":
        data = {
            "items": [record],
            "exported_by": actor.email,
            "exported_at": now.isoformat(),
        }
    elif operation == "revoke_invitation":
        data = {}
    elif operation in {"preview_invitation_batch", "apply_invitation_batch"}:
        preview = operation == "preview_invitation_batch"
        data = {
            "items": [
                {
                    "index": 0,
                    "email": command.payload.items[0].email,
                    "ok": True,
                    "retryable": False,
                    "invitation_id": record["id"],
                    "token": None if preview else proof,
                    "invite_url": None if preview else link,
                    "project_id": None,
                    "project_name": None,
                    "error": None,
                }
            ],
            "succeeded": 1,
            "failed": 0,
            "preview": preview,
        }
    elif operation == "send_invitation_email":
        data = {
            "ok": True,
            "invitation_id": str(command.payload.invitation_id),
            "email": record["email"],
            "invite_url": link,
            "message": "邀请邮件已发送" if mail_status == "sent" else "邀请邮件发送中",
            "mail_status": mail_status,
            "mail_failure_code": None,
        }
    elif operation == "resolve_invitation":
        data = {
            name: record[name]
            for name in (
                "email",
                "role",
                "group_name",
                "project_id",
                "project_name",
                "project_member_role",
                "expires_at",
                "invited_by_name",
            )
        }
    else:
        registering = operation == "register_invitation"
        user_id = command.operation_id if registering else actor.actor_id
        account = UserOut(
            id=user_id,
            name=command.payload.name if registering else actor.name,
            email=actor.email,
            role=actor.role,
            group_name=None,
            status="online",
            email_verified_at=now,
            created_at=now,
        ).model_dump(mode="json")
        data = {
            "user": account,
            "acceptance": {
                "project_id": None,
                "project_name": None,
                "project_member_role": None,
                "next_action": "go_home",
                "next_action_label": "进入平台",
                "responsible_person_name": None,
                "active_batch_count": 0,
            },
        }
        if registering:
            data.update(
                access_token=jwt.encode(
                    {
                        "sub": str(user_id),
                        "jti": str(command.operation_id),
                        "gen": 0,
                        "role": actor.role,
                        "iss": "aap-identity/v1",
                        "exp": int((now + timedelta(hours=1)).timestamp()),
                    },
                    "fixture-only-signing-secret",
                    algorithm="HS256",
                ),
                token_type="bearer",
                email_verification_required=False,
            )
    return {
        "schema": "aap-identity-invitations/v1",
        "ok": True,
        "operation": operation,
        "operation_id": str(command.operation_id),
        "principal": None
        if operation in {"resolve_invitation", "register_invitation"}
        else actor.model_dump(mode="json"),
        "data": data,
    }


def _remote(monkeypatch, respond, actor):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.identity_account_commands.httpx.AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    return IdentityInvitationCommands(
        object(),
        IdentityGateway(identity_url="http://identity.test", identity_token="control"),
        expected_principal=actor,
    )


async def test_remote_invitation_uses_control_bearer_and_exact_principal(monkeypatch):
    actor, command = _principal(), _command()

    def respond(request):
        assert request.url.path == "/v1/identity/invitations/execute"
        assert request.headers["authorization"] == "Bearer control"
        assert json.loads(request.content)["token"] == command.token
        return httpx.Response(200, json=_reply(command, actor))

    reply = await _remote(monkeypatch, respond, actor).execute(command)
    assert reply.principal.generation == 2**63 - 1
    assert len(reply.data.items) == 1


@pytest.mark.parametrize(
    "fault", ["actor", "credential", "operation", "generation", "secret", "unavailable"]
)
async def test_remote_invitation_faults_never_select_a_local_owner(monkeypatch, fault):
    actor, command = _principal(), _command()
    body = _reply(command, actor)
    if fault == "actor":
        body["principal"]["actor_id"] = str(uuid.uuid4())
    elif fault == "credential":
        body["principal"]["credential_id"] = str(uuid.uuid4())
    elif fault == "operation":
        body["operation_id"] = str(uuid.uuid4())
    elif fault == "generation":
        body["principal"]["generation"] = True
    elif fault == "secret":
        body["data"]["password_hash"] = "must-not-escape"

    def forbidden(*args, **kwargs):
        raise AssertionError("Remote invitation errors must not select caller SQL")

    monkeypatch.setattr(
        "app.services.identity_invitation_commands.IdentityInvitationCommandData",
        forbidden,
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await _remote(
            monkeypatch,
            lambda _: httpx.Response(503 if fault == "unavailable" else 200, json=body),
            actor,
        ).execute(command)


async def test_explicit_invitation_dsn_file_error_never_selects_caller_sql(monkeypatch):
    monkeypatch.setattr(settings, "identity_invitation_database_url", "")
    monkeypatch.setattr(
        settings,
        "identity_invitation_database_url_file",
        "/missing-invitation-dsn-fixture",
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Explicit invitation binding must never fall back")

    monkeypatch.setattr(
        "app.services.identity_invitation_commands.IdentityInvitationCommandData",
        forbidden,
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await IdentityInvitationCommands(
            object(), IdentityGateway(signing_secret="fixture-only")
        ).execute(_command())


@pytest.mark.parametrize("phase", ["prepare", "apply"])
async def test_invalid_native_reply_rolls_back_before_acknowledging_owner_writes(phase):
    command = native_invitation_prepare_command(
        _command(
            "create_invitation", {"email": "target@invite.test", "role": "employee"}
        ),
        actor=_principal(),
        expected_key_hash=None,
        request_fingerprint="a" * 64,
        derivation_id="b" * 64,
        config=NativeInvitationConfig(
            invitation_ttl_days=7,
            max_invitations_per_day=30,
            frontend_base_url="https://frontend.test",
            expire_minutes=1440,
        ),
    )
    if phase == "apply":
        command = native_invitation_apply_command(
            command,
            derived=NativeInvitationDerived(
                token="derived-invitation-proof",
                frontend_base_url="https://frontend.test",
            ),
        )

    class Session:
        def __init__(self):
            self.events = []

        async def flush(self):
            self.events.append("flush")

        async def scalar(self, statement, parameters):
            assert str(statement).startswith(
                f"SELECT aap_identity_invitations_v1.{phase}_invitation_command("
            )
            assert json.loads(parameters["command"])["operation_id"] == str(
                command.operation_id
            )
            self.events.append("sql")
            return {
                "schema": "aap-identity-invitations-native/v1",
                "ok": True,
                "password_hash": "must-not-escape",
            }

        async def commit(self):
            self.events.append("commit")

        async def rollback(self):
            self.events.append("rollback")

    session = Session()
    data = IdentityInvitationCommandData(session)
    with pytest.raises(IdentityUnavailable):
        async with data.transaction() as transaction:
            execute = getattr(transaction, f"{phase}_invitation_command")
            await execute(command.model_dump(mode="json", by_alias=True))
    assert session.events == ["flush", "sql", "rollback"]


async def test_mail_polls_same_operation_without_api_local_remote_sender(monkeypatch):
    actor = _principal()
    command = _command("send_invitation_email", {"invitation_id": str(uuid.uuid4())})
    seen = []

    async def forbidden(*args, **kwargs):
        raise AssertionError("Remote invitation owner uses its own durable sender")

    monkeypatch.setattr(
        "app.services.identity_invitation_commands.kick_invitation_mail", forbidden
    )

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_reply(
                command, actor, mail_status="pending" if len(seen) == 1 else "sent"
            ),
        )

    reply = await _remote(monkeypatch, respond, actor).execute(command)
    assert reply.data.mail_status == "sent"
    assert len(seen) == 2 and seen[0] == seen[1]


async def test_pending_send_timeout_preserves_accepted_intent_and_public_502(
    monkeypatch,
):
    actor = _principal()
    command = _command("send_invitation_email", {"invitation_id": str(uuid.uuid4())})
    monkeypatch.setattr(settings, "identity_invitation_mail_timeout_seconds", 0.02)
    gateway = _remote(
        monkeypatch,
        lambda _: httpx.Response(
            200, json=_reply(command, actor, mail_status="pending")
        ),
        actor,
    )
    reply = await gateway.execute(command)
    assert reply.data.mail_status == "pending"
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/send",
            "headers": [],
            "state": {"identity_principal": actor},
        }
    )

    class Commands:
        async def execute(self, _command):
            return reply

    with pytest.raises(HTTPException) as caught:
        await execute_invitation(
            request,
            SimpleNamespace(credentials="fixture"),
            Commands(),
            command.operation,
            command.payload.model_dump(mode="json"),
        )
    assert caught.value.status_code == 502
    assert caught.value.detail["code"] == "invitation_email_failed"


@pytest.mark.parametrize("role", ["employee", "viewer"])
def test_invitation_manager_gate(role):
    with pytest.raises(HTTPException) as caught:
        require_invitation_manager(_principal(role))
    assert caught.value.status_code == 403
    assert caught.value.detail == "需要角色权限: super_admin或project_admin"


@pytest.mark.parametrize("proof", ["", "x" * 65])
async def test_unknown_oversized_or_empty_proof_keeps_public_not_found(proof):
    request = Request(
        {"type": "http", "method": "POST", "path": "/register", "headers": []}
    )

    class Commands:
        async def execute(self, _command):
            raise AssertionError("An impossible stored proof must not reach the owner")

    with pytest.raises(HTTPException) as caught:
        await execute_invitation(
            request,
            None,
            Commands(),
            "register_invitation",
            {
                "invitation_token": proof,
                "name": "Invited",
                "password": "Strong123",
            },
        )
    assert caught.value.status_code == 404 and caught.value.detail == "邀请链接无效"


async def test_all_invitation_routes_use_values_without_user_or_business_hydration(
    monkeypatch,
):
    app, manager, receiver, seen = FastAPI(), _principal(), _principal("employee"), []
    app.include_router(users.router, prefix="/users")
    app.include_router(invitations.router, prefix="/auth")
    app.include_router(invitations_admin.router, prefix="/invitations")
    invitation_id = uuid.uuid4()

    async def admitted(request: Request):
        actor = receiver if request.url.path.endswith("/accept") else manager
        request.state.identity_principal = actor
        return actor

    async def forbidden():
        raise AssertionError(
            "Invitation HTTP presentation must not hydrate local ORM rows"
        )

    class Commands:
        async def execute(self, command):
            seen.append(command.operation)
            actor = (
                receiver
                if command.operation in {"register_invitation", "accept_invitation"}
                else manager
            )
            return decode_invitation_reply(
                command, _reply(command, actor), expected_principal=actor
            )

    app.dependency_overrides[get_current_principal] = admitted
    app.dependency_overrides[get_current_user] = forbidden
    app.dependency_overrides[get_db] = forbidden

    async def owner_commands(request: Request):
        await admitted(request)
        return Commands()

    for dependency in (
        get_invitation_commands,
        get_authenticated_invitation_commands,
        get_invitation_manager_commands,
    ):
        app.dependency_overrides[dependency] = owner_commands
    monkeypatch.setattr(
        "app.core.security.create_access_token",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("API must not sign invitation registration JWTs")
        ),
    )
    batch = {"items": [{"email": "target@invite.test", "role": "employee"}]}
    cases = [
        (
            "POST",
            "/users/invite",
            {"email": "target@invite.test", "role": "employee"},
            201,
        ),
        ("POST", "/users/bulk-invite/preview", batch, 200),
        ("POST", "/users/bulk-invite", batch, 200),
        ("GET", "/invitations", None, 200),
        ("GET", "/invitations/query", None, 200),
        ("GET", "/invitations/stats", None, 200),
        ("GET", "/invitations/export?format=json", None, 200),
        ("GET", "/invitations/export?format=csv", None, 200),
        ("DELETE", f"/invitations/{invitation_id}", None, 204),
        ("POST", f"/invitations/{invitation_id}/resend", None, 200),
        ("POST", f"/invitations/{invitation_id}/send-email", None, 200),
        ("GET", "/auth/invitations/proof", None, 200),
        (
            "POST",
            "/auth/register",
            {"token": "proof", "name": "Invited", "password": "Strong123"},
            201,
        ),
        ("POST", "/auth/invitations/accept", {"token": "proof"}, 200),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for method, path, payload, expected in cases:
            response = await client.request(
                method, path, json=payload, headers={"Authorization": "Bearer fixture"}
            )
            assert response.status_code == expected, response.text
            if path.endswith("/send-email"):
                assert set(response.json()) == {
                    "ok",
                    "invitation_id",
                    "email",
                    "invite_url",
                    "message",
                }
            if path.endswith("format=csv"):
                assert response.text.startswith("\ufeff# Exported by:")
    assert len(set(seen)) == 13
