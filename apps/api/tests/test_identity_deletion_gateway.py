"""Account-deletion HTTP compatibility and configured-owner boundaries."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from aap_identity.contract import IdentityUnavailable
from aap_identity.deletion_contract import (
    DeletionFailure,
    decode_deletion_request,
    decode_deletion_reply,
)
from app.api.v1 import users
from app.api.v1.deletion_commands import (
    _reject_deletion_error,
    get_deletion_commands,
    require_deletion_manager,
)
from app.config import settings
from app.deps import get_current_user, get_db
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_deletion_commands import IdentityDeletionCommands
from app.services.identity_deletion_data import IdentityDeletionCommandData
from app.services.identity_gateway import IdentityGateway
from tests.test_identity_platform_role_gateway import _principal, _user


def _command(target_id=None, receiver_id=None):
    return decode_deletion_request(
        {
            "schema": "aap-identity-deletions/v1",
            "operation": "delete_account",
            "operation_id": str(uuid.uuid4()),
            "token": "fixture-deletion-token",
            "audit_context": {
                "method": "DELETE",
                "path": "/api/v1/users/deletion",
                "ip": None,
                "request_id": None,
            },
            "payload": {
                "target_user_id": str(target_id or uuid.uuid4()),
                "transfer_to_user_id": str(receiver_id) if receiver_id else None,
            },
        }
    )


def _reply(command, principal):
    user = _user(command.payload.target_user_id, "historical-role")
    user.update(
        is_active=False,
        disabled_kind="historical_unknown",
        deactivation_requested_at="2030-01-01T00:00:00+00:00",
        deactivation_scheduled_at="2030-01-08T00:00:00+00:00",
    )
    return {
        "schema": "aap-identity-deletions/v1",
        "ok": True,
        "operation": "delete_account",
        "operation_id": str(command.operation_id),
        "principal": principal.model_dump(mode="json"),
        "data": {"user": user},
    }


def _app(principal, commands):
    app = FastAPI()

    @app.middleware("http")
    async def admitted(request, call_next):
        request.state.identity_principal = principal
        return await call_next(request)

    def forbid():
        pytest.fail("Deletion handler hydrated User or acquired a SQL session")

    app.dependency_overrides[get_deletion_commands] = lambda: commands
    app.dependency_overrides[get_current_user] = forbid
    app.dependency_overrides[get_db] = forbid
    app.include_router(users.router, prefix="/api/v1/users")
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body_kind", ["absent", "null", "empty", "null_receiver", "receiver"]
)
async def test_delete_optional_body_and_full_owned_user_without_hydration(body_kind):
    principal, target, receiver, calls = _principal(), uuid.uuid4(), uuid.uuid4(), []
    principal = principal.model_copy(update={"role": "project_admin"})
    expected_receiver = receiver if body_kind == "receiver" else None

    class Commands:
        async def execute(self, command):
            calls.append(command)
            assert command.operation == "delete_account"
            assert command.payload.target_user_id == target
            assert command.payload.transfer_to_user_id == expected_receiver
            assert command.audit_context.method == "DELETE"
            assert command.audit_context.path == f"/api/v1/users/{target}"
            return decode_deletion_reply(
                command, _reply(command, principal), expected_principal=principal
            )

    kwargs = {
        "absent": {},
        "null": {"content": b"null"},
        "empty": {"json": {}},
        "null_receiver": {"json": {"transfer_to_user_id": None}},
        "receiver": {
            "json": {"transfer_to_user_id": str(receiver), "preview_version": "ignored"}
        },
    }[body_kind]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(principal, Commands())),
        base_url="http://deletion.test",
    ) as client:
        for _ in range(2):
            response = await client.request(
                "DELETE",
                f"/api/v1/users/{target}",
                headers={
                    "Authorization": "Bearer fixture-deletion-token",
                    "Content-Type": "application/json",
                },
                **kwargs,
            )
            assert response.status_code == 200, response.text
    assert calls[0].operation_id != calls[1].operation_id
    body = response.json()
    assert len(body) == 21 and body["id"] == str(target)
    assert not body["is_active"] and body["disabled_kind"] == "historical_unknown"
    assert body["role"] == "historical-role" and body["status"] == "historical-status"
    assert body["preferences"]["unknown"]["count"] == 2**63 - 1
    assert body["deactivation_requested_at"] and body["deactivation_scheduled_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,body,status",
    [
        ("not-a-uuid", None, 404),
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", {"transfer_to_user_id": "bad"}, 422),
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", {"transfer_to_user_id": 1}, 422),
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", [], 422),
    ],
)
async def test_delete_preserves_path_and_receiver_validation(path, body, status):
    class Commands:
        async def execute(self, command):
            pytest.fail("Invalid public input reached the deletion owner")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_principal(), Commands())),
        base_url="http://deletion.test",
    ) as client:
        response = await client.request(
            "DELETE",
            f"/api/v1/users/{path}",
            headers={"Authorization": "Bearer fixture-deletion-token"},
            json=body,
        )
    assert response.status_code == status, response.text
    if status == 404:
        assert response.json()["detail"] == "用户不存在"


@pytest.mark.parametrize("role", ["super_admin", "project_admin", "employee", "viewer"])
def test_deletion_manager_admission_uses_current_principal(role):
    principal = _principal().model_copy(update={"role": role})
    if role in {"super_admin", "project_admin"}:
        assert require_deletion_manager(principal) is principal
    else:
        with pytest.raises(HTTPException) as result:
            require_deletion_manager(principal)
        assert result.value.status_code == 403
        assert result.value.detail == "需要角色权限: super_admin或project_admin"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["valid", "missing_principal", "operation_id", "actor", "target", "active"],
)
async def test_remote_deletion_rejects_misbinding_without_alternative_writer(
    monkeypatch, mutation
):
    from app.services import identity_deletion_commands as binding

    principal, command = _principal(), _command()
    raw = _reply(command, principal)
    if mutation == "operation_id":
        raw["operation_id"] = str(uuid.uuid4())
    elif mutation == "actor":
        raw["principal"]["actor_id"] = str(uuid.uuid4())
    elif mutation == "target":
        raw["data"]["user"]["id"] = str(uuid.uuid4())
    elif mutation == "active":
        raw["data"]["user"]["is_active"] = True

    async def remote(identity, path, payload):
        assert mutation != "missing_principal", "Unbound request reached remote writer"
        assert path == "/v1/identity/deletions/execute"
        return raw

    def no_fallback(*args, **kwargs):
        pytest.fail("Remote deletion selected an alternative SQL writer")

    monkeypatch.setattr(binding, "_remote_json", remote)
    monkeypatch.setattr(binding, "DeletionPostgresData", no_fallback)
    monkeypatch.setattr(binding, "IdentityDeletionCommandData", no_fallback)
    adapter = IdentityDeletionCommands(
        object(),
        IdentityGateway(identity_url="http://identity.test", identity_token="control"),
        expected_principal=None if mutation == "missing_principal" else principal,
    )
    if mutation == "valid":
        assert (await adapter.execute(command)).operation == "delete_account"
    else:
        with pytest.raises(IdentityGatewayUnavailable):
            await adapter.execute(command)


@pytest.mark.asyncio
async def test_explicit_deletion_dsn_requires_readiness_without_fallback(monkeypatch):
    from app.services import identity_deletion_commands as binding

    class Data:
        def __init__(self, dsn):
            assert dsn == "postgresql://deletion-client@isolated.test/deletion_test"

        async def deletion_policy(self):
            raise IdentityUnavailable()

        def transaction(self):
            pytest.fail("Unready deletion client reached business transaction")

    def no_fallback(*args, **kwargs):
        pytest.fail("Explicit deletion DSN failure fell back to caller SQL")

    monkeypatch.setattr(binding, "DeletionPostgresData", Data)
    monkeypatch.setattr(binding, "IdentityDeletionCommandData", no_fallback)
    monkeypatch.setattr(
        settings,
        "identity_deletion_database_url",
        "postgresql://deletion-client@isolated.test/deletion_test",
    )
    monkeypatch.setattr(settings, "identity_deletion_database_url_file", "")
    with pytest.raises(IdentityGatewayUnavailable):
        await IdentityDeletionCommands(
            object(), IdentityGateway(signing_secret="synthetic-deletion-secret")
        ).execute(_command())


@pytest.mark.asyncio
async def test_native_deletion_misbinding_rolls_back_before_commit():
    principal, request = _principal(), _command()
    command = {
        "schema": "aap-identity-deletions-native/v1",
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "actor": principal.model_dump(mode="json"),
        "expected_key_hash": None,
        "request_fingerprint": "1" * 64,
        "derivation_id": "2" * 64,
        "audit_context": request.audit_context.model_dump(mode="json"),
        "payload": request.payload.model_dump(mode="json"),
    }
    raw = _reply(request, principal)
    raw.update(schema="aap-identity-deletions-native/v1", replayed=False)
    raw["data"]["user"]["id"] = str(uuid.uuid4())

    class DB:
        commits, rollbacks, pending = 0, 0, False

        async def flush(self):
            pass

        async def scalar(self, statement, parameters):
            self.pending = True
            return raw

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1
            self.pending = False

    db = DB()
    with pytest.raises(IdentityUnavailable):
        async with IdentityDeletionCommandData(db).transaction() as transaction:
            await transaction.execute_deletion_command(command)
    assert db.commits == 0 and db.rollbacks >= 1 and not db.pending


@pytest.mark.parametrize(
    "failure,status,detail",
    [
        (DeletionFailure("credential_invalid"), 401, "无效的认证凭据"),
        (DeletionFailure("user_inactive"), 403, "当前账号已无管理权限"),
        (DeletionFailure("credential_busy"), 409, "账号正在更新，请刷新后重试"),
        (DeletionFailure("self_delete", 400, "不能删除自己"), 400, "不能删除自己"),
        (
            DeletionFailure("project_busy", 409, {"reason": "project_busy"}),
            409,
            {"reason": "project_busy"},
        ),
        (
            DeletionFailure("operation_conflict", 409, None),
            409,
            {"reason": "operation_conflict"},
        ),
    ],
)
def test_deletion_errors_keep_legacy_status_detail_and_auth_challenge(
    failure, status, detail
):
    with pytest.raises(HTTPException) as result:
        _reject_deletion_error(failure)
    assert result.value.status_code == status and result.value.detail == detail
    if status == 401:
        assert result.value.headers == {"WWW-Authenticate": "Bearer"}
