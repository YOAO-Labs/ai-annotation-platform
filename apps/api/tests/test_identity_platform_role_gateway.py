"""Public role operations preserve response and error contracts across owner bindings."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.platform_role_contract import (
    PlatformRoleFailure,
    decode_platform_role_request,
    decode_platform_role_reply,
)
from app.api.v1 import users
from app.api.v1.platform_role_commands import (
    _reject_platform_role_error,
    get_platform_role_commands,
)
from app.config import settings
from app.deps import get_current_user, get_db
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway
from app.services.identity_platform_role_commands import IdentityPlatformRoleCommands
from app.services.identity_platform_role_data import IdentityPlatformRoleCommandData

_WARNINGS = [
    "平台角色对所有项目生效；已有项目成员身份和批次负责人不会自动改变。",
    "若新平台角色与现有项目身份不匹配，成员可能无法继续标注或审核；请检查并重新分派。",
]


def _principal():
    return Principal(
        actor_id=uuid.uuid4(),
        name="Role manager",
        email="manager@role.test",
        role="super_admin",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=2**63 - 1,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def _command(operation="change_platform_role", target_id=None):
    return decode_platform_role_request(
        {
            "schema": "aap-identity-platform-roles/v1",
            "operation": operation,
            "operation_id": str(uuid.uuid4()),
            "token": "fixture-platform-role-token",
            "audit_context": {
                "method": "PATCH",
                "path": "/api/v1/users/role",
                "ip": None,
                "request_id": None,
            },
            "payload": {
                "target_user_id": str(target_id or uuid.uuid4()),
                "role": "viewer" if operation == "preview_role_change" else "employee",
            },
        }
    )


def _user(target_id, role):
    return {
        "id": str(target_id),
        "email": "target@role.test",
        "name": "Role target",
        "role": role,
        "group_name": None,
        "group_id": None,
        "status": "historical-status",
        "is_active": True,
        "disabled_kind": None,
        "disabled_at": None,
        "disabled_by": None,
        "disabled_reason": None,
        "email_verified_at": None,
        "last_login_at": None,
        "last_seen_at": None,
        "password_admin_reset_at": None,
        "deactivation_requested_at": None,
        "deactivation_scheduled_at": None,
        "preferences": {"unknown": {"count": 2**63 - 1, "items": ["keep", None]}},
        "avatar_ref": None,
        "created_at": "2030-01-01T00:00:00+00:00",
    }


def _reply(command, principal):
    if command.operation == "change_platform_role":
        data = {"user": _user(command.payload.target_user_id, command.payload.role)}
    else:
        data = {
            "user_id": str(command.payload.target_user_id),
            "email": "target@role.test",
            "current_role": "employee",
            "requested_role": command.payload.role,
            "can_change": False,
            "blockers": ["账号仍有未完成的标注/质检工作，请先交接"],
            "projects": [
                {
                    "project_id": str(uuid.uuid4()),
                    "project_name": "Project",
                    "membership_role": None,
                    "annotator_batch_count": 2,
                    "reviewer_batch_count": 3,
                    "assigned_task_count": 4,
                    "review_task_count": 5,
                }
            ],
            "assigned_batch_count": 5,
            "assigned_task_count": 4,
            "review_task_count": 5,
            "other_project_count": 0,
            "warnings": _WARNINGS,
        }
    return {
        "schema": "aap-identity-platform-roles/v1",
        "ok": True,
        "operation": command.operation,
        "operation_id": str(command.operation_id),
        "principal": principal.model_dump(mode="json"),
        "data": data,
    }


def _app(principal, commands):
    app = FastAPI()

    @app.middleware("http")
    async def admitted(request, call_next):
        request.state.identity_principal = principal
        return await call_next(request)

    def forbid():
        pytest.fail("Role handler tried to hydrate User or acquire a SQL session")

    app.dependency_overrides[get_platform_role_commands] = lambda: commands
    app.dependency_overrides[get_current_user] = forbid
    app.dependency_overrides[get_db] = forbid
    app.include_router(users.router, prefix="/api/v1/users")
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["preview_role_change", "change_platform_role"])
async def test_public_role_uses_owned_values_without_user_hydration(operation):
    principal, target, seen = _principal(), uuid.uuid4(), []

    class Commands:
        async def execute(self, command):
            seen.append(command)
            assert command.operation == operation
            assert command.payload.target_user_id == target
            return decode_platform_role_reply(
                command, _reply(command, principal), expected_principal=principal
            )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(principal, Commands())),
        base_url="http://role.test",
    ) as client:
        headers = {"Authorization": "Bearer fixture-platform-role-token"}
        if operation == "preview_role_change":
            response = await client.get(
                f"/api/v1/users/{target}/role/preview?role=viewer", headers=headers
            )
        else:
            response = await client.patch(
                f"/api/v1/users/{target}/role",
                json={"role": "employee", "preview_version": "ignored"},
                headers=headers,
            )
    assert response.status_code == 200, response.text
    assert len(seen) == 1
    body = response.json()
    if operation == "preview_role_change":
        assert len(body) == 12 and body["can_change"] is False
        assert body["projects"][0]["membership_role"] is None
        assert body["assigned_batch_count"] == 5 and body["warnings"] == _WARNINGS
    else:
        assert len(body) == 21 and body["role"] == "employee"
        assert body["status"] == "historical-status"
        assert body["preferences"]["unknown"]["count"] == 2**63 - 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,body,status",
    [
        ("PATCH", "/not-a-uuid/role", {"role": "employee"}, 404),
        ("PATCH", "/not-a-uuid/role", {"role": "reviewer"}, 400),
        (
            "GET",
            "/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/role/preview?role=reviewer",
            None,
            400,
        ),
        ("GET", "/not-a-uuid/role/preview?role=employee", None, 422),
        ("GET", "/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/role/preview?role=", None, 422),
    ],
)
async def test_role_input_errors_do_not_invoke_a_writer(method, path, body, status):
    class Commands:
        async def execute(self, command):
            pytest.fail("Invalid role input reached owner execution")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_principal(), Commands())),
        base_url="http://role.test",
    ) as client:
        response = await client.request(
            method,
            "/api/v1/users" + path,
            json=body,
            headers={"Authorization": "Bearer fixture-platform-role-token"},
        )
    assert response.status_code == status, response.text
    if status == 404:
        assert response.json() == {"detail": "用户不存在"}
    elif status == 400:
        assert response.json() == {"detail": "非法平台角色: reviewer"}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["preview_role_change", "change_platform_role"])
@pytest.mark.parametrize(
    "mutation",
    ["valid", "operation_id", "target", "principal", "data", "missing_principal"],
)
async def test_remote_role_reply_binds_operation_target_and_current_credential(
    monkeypatch, operation, mutation
):
    from app.services import identity_platform_role_commands as binding

    principal, command = _principal(), _command(operation)
    raw = _reply(command, principal)
    if mutation == "operation_id":
        raw["operation_id"] = str(uuid.uuid4())
    elif mutation == "principal":
        raw["principal"]["credential_id"] = str(uuid.uuid4())
    elif mutation == "target":
        if operation == "preview_role_change":
            raw["data"]["user_id"] = str(uuid.uuid4())
        else:
            raw["data"]["user"]["id"] = str(uuid.uuid4())
    elif mutation == "data":
        if operation == "preview_role_change":
            raw["data"]["assigned_batch_count"] = 0
        else:
            raw["data"]["user"]["role"] = "viewer"

    async def remote(identity, path, payload):
        assert mutation != "missing_principal", (
            "Unbound request reached a remote writer"
        )
        assert path == "/v1/identity/platform-roles/execute"
        return raw

    def no_fallback(*args, **kwargs):
        pytest.fail("Remote role command selected an alternative SQL path")

    monkeypatch.setattr(binding, "_remote_json", remote)
    monkeypatch.setattr(binding, "PlatformRolePostgresData", no_fallback)
    monkeypatch.setattr(binding, "IdentityPlatformRoleCommandData", no_fallback)
    adapter = IdentityPlatformRoleCommands(
        object(),
        IdentityGateway(identity_url="http://identity.test", identity_token="control"),
        expected_principal=None if mutation == "missing_principal" else principal,
    )
    if mutation == "valid":
        assert (await adapter.execute(command)).operation == operation
    else:
        with pytest.raises(IdentityGatewayUnavailable):
            await adapter.execute(command)


@pytest.mark.asyncio
async def test_explicit_role_dsn_readiness_failure_never_reaches_business(monkeypatch):
    from app.services import identity_platform_role_commands as binding

    class Data:
        def __init__(self, dsn):
            assert dsn == "postgresql://role-client@isolated.test/role_test"

        async def platform_role_policy(self):
            raise IdentityUnavailable()

        def transaction(self):
            pytest.fail("Unready role connection reached its command")

    def no_fallback(*args, **kwargs):
        pytest.fail("Explicit role DSN failure fell back to caller SQL")

    monkeypatch.setattr(binding, "PlatformRolePostgresData", Data)
    monkeypatch.setattr(binding, "IdentityPlatformRoleCommandData", no_fallback)
    monkeypatch.setattr(
        settings,
        "identity_platform_role_database_url",
        "postgresql://role-client@isolated.test/role_test",
    )
    monkeypatch.setattr(settings, "identity_platform_role_database_url_file", "")
    with pytest.raises(IdentityGatewayUnavailable):
        await IdentityPlatformRoleCommands(
            object(), IdentityGateway(signing_secret="synthetic-role-secret")
        ).execute(_command())


@pytest.mark.asyncio
async def test_role_native_misbinding_rolls_back_before_commit():
    principal, request = _principal(), _command()
    command = {
        "schema": "aap-identity-platform-roles-native/v1",
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
    raw.update(schema="aap-identity-platform-roles-native/v1", replayed=False)
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
        async with IdentityPlatformRoleCommandData(db).transaction() as transaction:
            await transaction.execute_platform_role_command(command)
    assert db.commits == 0 and db.rollbacks >= 1 and db.pending is False


@pytest.mark.parametrize(
    "failure,status,detail",
    [
        (PlatformRoleFailure("credential_invalid"), 401, "无效的认证凭据"),
        (PlatformRoleFailure("credential_busy"), 409, "账号正在更新，请刷新后重试"),
        (
            PlatformRoleFailure("super_admin_required", 403, "当前账号已无管理权限"),
            403,
            "当前账号已无管理权限",
        ),
        (PlatformRoleFailure("target_not_found", 404, "用户不存在"), 404, "用户不存在"),
        (
            PlatformRoleFailure("self_change", 400, "不能修改自己的角色"),
            400,
            "不能修改自己的角色",
        ),
        (
            PlatformRoleFailure("last_super_admin", 400, "不能降级最后一名超级管理员"),
            400,
            "不能降级最后一名超级管理员",
        ),
        (
            PlatformRoleFailure("project_busy", 409, {"reason": "project_busy"}),
            409,
            {"reason": "project_busy"},
        ),
        (
            PlatformRoleFailure(
                "platform_role_change_blocked",
                409,
                {
                    "reason": "platform_role_change_blocked",
                    "blockers": ["账号仍是项目负责人，请先转移项目后变更平台角色"],
                },
            ),
            409,
            {
                "reason": "platform_role_change_blocked",
                "blockers": ["账号仍是项目负责人，请先转移项目后变更平台角色"],
            },
        ),
        (
            PlatformRoleFailure("operation_conflict", 409, None),
            409,
            {"reason": "operation_conflict"},
        ),
    ],
)
def test_role_owner_errors_keep_public_status_and_challenge(failure, status, detail):
    with pytest.raises(HTTPException) as result:
        _reject_platform_role_error(failure)
    assert result.value.status_code == status and result.value.detail == detail
    if status == 401:
        assert result.value.headers == {"WWW-Authenticate": "Bearer"}
