"""Suspension HTTP contracts and configured-owner failure boundaries."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from aap_identity.contract import IdentityUnavailable
from aap_identity.suspension_contract import (
    SuspensionFailure,
    decode_suspension_request,
    decode_suspension_reply,
)
from app.api.v1 import users
from app.api.v1.suspension_commands import (
    _reject_suspension_error,
    get_suspension_commands,
    require_suspension_manager,
)
from app.config import settings
from app.deps import get_current_user, get_db
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway
from app.services.identity_suspension_commands import IdentitySuspensionCommands
from app.services.identity_suspension_data import IdentitySuspensionCommandData
from tests.test_identity_platform_role_gateway import _principal, _user


def _command(operation="suspend_account", target_id=None, reason=None):
    payload = {"target_user_id": str(target_id or uuid.uuid4())}
    if operation == "reactivate_account":
        payload["reason"] = reason
    return decode_suspension_request(
        {
            "schema": "aap-identity-suspensions/v1",
            "operation": operation,
            "operation_id": str(uuid.uuid4()),
            "token": "fixture-suspension-token",
            "audit_context": {
                "method": "POST",
                "path": "/api/v1/users/suspension",
                "ip": None,
                "request_id": None,
            },
            "payload": payload,
        }
    )


def _reply(command, principal):
    user = _user(command.payload.target_user_id, "historical-role")
    user["is_active"] = command.operation == "reactivate_account"
    user["deactivation_requested_at"] = "2030-01-01T00:00:00+00:00"
    user["deactivation_scheduled_at"] = "2030-01-08T00:00:00+00:00"
    if command.operation == "suspend_account":
        user["disabled_kind"] = "historical_unknown"
    return {
        "schema": "aap-identity-suspensions/v1",
        "ok": True,
        "operation": command.operation,
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
        pytest.fail("Suspension handler hydrated User or acquired a SQL session")

    app.dependency_overrides[get_suspension_commands] = lambda: commands
    app.dependency_overrides[get_current_user] = forbid
    app.dependency_overrides[get_db] = forbid
    app.include_router(users.router, prefix="/api/v1/users")
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["suspend_account", "reactivate_account"])
async def test_public_suspension_uses_complete_owned_values_without_hydration(
    operation,
):
    principal, target, seen = _principal(), uuid.uuid4(), []
    principal = principal.model_copy(update={"role": "project_admin"})

    class Commands:
        async def execute(self, command):
            seen.append(command)
            assert command.operation == operation
            assert command.payload.target_user_id == target
            if operation == "reactivate_account":
                assert command.payload.reason == "\u3000returned\t"
            return decode_suspension_reply(
                command, _reply(command, principal), expected_principal=principal
            )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(principal, Commands())),
        base_url="http://suspension.test",
    ) as client:
        headers = {"Authorization": "Bearer fixture-suspension-token"}
        action = "deactivate" if operation == "suspend_account" else "reactivate"
        kwargs = (
            {"content": b"unparsed legacy body"}
            if operation == "suspend_account"
            else {"json": {"reason": "\u3000returned\t", "preview_version": "ignored"}}
        )
        response = await client.post(
            f"/api/v1/users/{target}/{action}", headers=headers, **kwargs
        )
    assert response.status_code == 200, response.text
    assert len(seen) == 1
    body = response.json()
    assert len(body) == 21 and body["role"] == "historical-role"
    assert body["status"] == "historical-status"
    assert body["is_active"] is (operation == "reactivate_account")
    assert body["preferences"]["unknown"]["count"] == 2**63 - 1
    assert body["deactivation_requested_at"] and body["deactivation_scheduled_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"reason": None},
        {"reason": ""},
        {"reason": "\u0000"},
        {"reason": "x" * 500},
    ],
)
async def test_optional_reactivation_body_preserves_reason_and_fresh_operation_ids(
    body,
):
    principal, target, calls = _principal(), uuid.uuid4(), []

    class Commands:
        async def execute(self, command):
            calls.append(command)
            assert command.payload.reason == (body.get("reason") if body else None)
            return decode_suspension_reply(
                command, _reply(command, principal), expected_principal=principal
            )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(principal, Commands())),
        base_url="http://suspension.test",
    ) as client:
        for _ in range(2):
            response = await client.post(
                f"/api/v1/users/{target}/reactivate",
                headers={
                    "Authorization": "Bearer fixture-suspension-token",
                    "Content-Type": "application/json",
                },
                content=b"null" if body is None else None,
                json=body if body is not None else None,
            )
            assert response.status_code == 200, response.text
    assert len(calls) == 2 and calls[0].operation_id != calls[1].operation_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,body,status",
    [
        ("not-a-uuid/deactivate", None, 404),
        ("not-a-uuid/reactivate", None, 422),
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/reactivate", {"reason": "x" * 501}, 422),
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/reactivate", {"reason": 1}, 422),
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/reactivate", [], 422),
    ],
)
async def test_public_suspension_keeps_distinct_path_and_body_validation(
    path, body, status
):
    class Commands:
        async def execute(self, command):
            pytest.fail("Invalid public input reached the owner")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_principal(), Commands())),
        base_url="http://suspension.test",
    ) as client:
        response = await client.post(
            f"/api/v1/users/{path}",
            headers={"Authorization": "Bearer fixture-suspension-token"},
            json=body,
        )
    assert response.status_code == status, response.text
    if status == 404:
        assert response.json()["detail"] == "用户不存在"


@pytest.mark.parametrize("role", ["super_admin", "project_admin", "employee", "viewer"])
def test_suspension_admission_uses_principal_manager_role(role):
    principal = _principal().model_copy(update={"role": role})
    if role in {"super_admin", "project_admin"}:
        assert require_suspension_manager(principal) is principal
    else:
        with pytest.raises(HTTPException) as result:
            require_suspension_manager(principal)
        assert result.value.status_code == 403
        assert result.value.detail == "需要角色权限: super_admin或project_admin"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["suspend_account", "reactivate_account"])
@pytest.mark.parametrize(
    "mutation",
    ["valid", "missing_principal", "operation_id", "actor", "target", "active"],
)
async def test_remote_suspension_validates_bindings_without_an_alternative_writer(
    monkeypatch, operation, mutation
):
    from app.services import identity_suspension_commands as binding

    principal, command = _principal(), _command(operation)
    raw = _reply(command, principal)
    if mutation == "operation_id":
        raw["operation_id"] = str(uuid.uuid4())
    elif mutation == "actor":
        raw["principal"]["actor_id"] = str(uuid.uuid4())
    elif mutation == "target":
        raw["data"]["user"]["id"] = str(uuid.uuid4())
    elif mutation == "active":
        raw["data"]["user"]["is_active"] = operation != "reactivate_account"

    async def remote(identity, path, payload):
        assert mutation != "missing_principal", "Unbound request reached remote writer"
        assert path == "/v1/identity/suspensions/execute"
        return raw

    def no_fallback(*args, **kwargs):
        pytest.fail("Remote suspension selected an alternative SQL writer")

    monkeypatch.setattr(binding, "_remote_json", remote)
    monkeypatch.setattr(binding, "SuspensionPostgresData", no_fallback)
    monkeypatch.setattr(binding, "IdentitySuspensionCommandData", no_fallback)
    adapter = IdentitySuspensionCommands(
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
async def test_explicit_suspension_dsn_must_pass_readiness_before_business(monkeypatch):
    from app.services import identity_suspension_commands as binding

    class Data:
        def __init__(self, dsn):
            assert dsn == "postgresql://suspension-client@isolated.test/suspension_test"

        async def suspension_policy(self):
            raise IdentityUnavailable()

        def transaction(self):
            pytest.fail("Unready suspension client reached business transaction")

    def no_fallback(*args, **kwargs):
        pytest.fail("Explicit suspension DSN failure fell back to caller SQL")

    monkeypatch.setattr(binding, "SuspensionPostgresData", Data)
    monkeypatch.setattr(binding, "IdentitySuspensionCommandData", no_fallback)
    monkeypatch.setattr(
        settings,
        "identity_suspension_database_url",
        "postgresql://suspension-client@isolated.test/suspension_test",
    )
    monkeypatch.setattr(settings, "identity_suspension_database_url_file", "")
    with pytest.raises(IdentityGatewayUnavailable):
        await IdentitySuspensionCommands(
            object(), IdentityGateway(signing_secret="synthetic-suspension-secret")
        ).execute(_command())


@pytest.mark.asyncio
async def test_suspension_native_misbinding_rolls_back_before_commit():
    principal, request = _principal(), _command()
    command = {
        "schema": "aap-identity-suspensions-native/v1",
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
    raw.update(schema="aap-identity-suspensions-native/v1", replayed=False)
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
        async with IdentitySuspensionCommandData(db).transaction() as transaction:
            await transaction.execute_suspension_command(command)
    assert db.commits == 0 and db.rollbacks >= 1 and not db.pending


@pytest.mark.parametrize(
    "failure,status,detail",
    [
        (SuspensionFailure("credential_invalid"), 401, "无效的认证凭据"),
        (SuspensionFailure("user_inactive"), 403, "当前账号已无管理权限"),
        (SuspensionFailure("credential_busy"), 409, "账号正在更新，请刷新后重试"),
        (
            SuspensionFailure("manager_required", 403, "当前账号已无管理权限"),
            403,
            "当前账号已无管理权限",
        ),
        (SuspensionFailure("target_not_found", 404, "用户不存在"), 404, "用户不存在"),
        (SuspensionFailure("self_suspend", 400, "不能停用自己"), 400, "不能停用自己"),
        (
            SuspensionFailure("self_reactivate", 400, "不能恢复自己"),
            400,
            "不能恢复自己",
        ),
        (
            SuspensionFailure("already_active", 409, "账号当前已启用"),
            409,
            "账号当前已启用",
        ),
        (
            SuspensionFailure("not_reactivatable", 409, "该停用状态不可恢复"),
            409,
            "该停用状态不可恢复",
        ),
        (
            SuspensionFailure("project_busy", 409, {"reason": "project_busy"}),
            409,
            {"reason": "project_busy"},
        ),
        (
            SuspensionFailure("operation_conflict", 409, None),
            409,
            {"reason": "operation_conflict"},
        ),
    ],
)
def test_suspension_owner_errors_keep_status_detail_and_auth_challenge(
    failure, status, detail
):
    with pytest.raises(HTTPException) as result:
        _reject_suspension_error(failure)
    assert result.value.status_code == status and result.value.detail == detail
    if status == 401:
        assert result.value.headers == {"WWW-Authenticate": "Bearer"}
