"""The public reset route consumes owner values and fails closed on bad bindings."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from aap_identity.authenticator import VerifiedCredential
from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.admin_password_contract import (
    AdminPasswordFailure,
    decode_admin_password_reply,
    decode_admin_password_request,
)
from app.api.v1 import users
from app.api.v1.admin_password_commands import (
    _reject_admin_password_error,
    get_admin_password_commands,
)
from app.config import settings
from app.deps import get_current_user, get_db
from app.services.identity_admin_password_commands import IdentityAdminPasswordCommands
from app.services.identity_admin_password_data import IdentityAdminPasswordCommandData
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway

_SECRET = "GLwLvE4ri9v0V!#y"


def _principal():
    return Principal(
        actor_id=uuid.uuid4(),
        name="Reset manager",
        email="manager@password.test",
        role="super_admin",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=2**63 - 1,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def _command():
    target = uuid.uuid4()
    return decode_admin_password_request(
        {
            "schema": "aap-identity-admin-passwords/v1",
            "operation": "admin_reset_password",
            "operation_id": str(uuid.uuid4()),
            "token": "fixture-admin-password-token",
            "audit_context": {
                "method": "POST",
                "path": f"/api/v1/users/{target}/admin-reset-password",
                "ip": None,
                "request_id": None,
            },
            "payload": {"target_user_id": str(target)},
        }
    )


def _reply(command, principal):
    return {
        "schema": "aap-identity-admin-passwords/v1",
        "ok": True,
        "operation": command.operation,
        "operation_id": str(command.operation_id),
        "principal": principal.model_dump(mode="json"),
        "data": {
            "target_user_id": str(command.payload.target_user_id),
            "target_email": "target@password.test",
            "temp_password": _SECRET,
            "password_admin_reset_at": "2030-01-02T03:04:05+00:00",
        },
    }


@pytest.mark.asyncio
async def test_public_reset_keeps_three_fields_without_user_hydration(monkeypatch):
    principal, target, seen = _principal(), uuid.uuid4(), []

    class Commands:
        async def execute(self, command):
            seen.append(command)
            assert command.payload.target_user_id == target
            assert command.token == "fixture-admin-password-token"
            assert command.audit_context.method == "POST"
            assert (
                command.audit_context.path
                == f"/api/v1/users/{target}/admin-reset-password"
            )
            return decode_admin_password_reply(
                command, _reply(command, principal), expected_principal=principal
            )

    app = FastAPI()

    @app.middleware("http")
    async def admitted(request, call_next):
        request.state.identity_principal = principal
        return await call_next(request)

    def forbid():
        pytest.fail("Password reset tried to hydrate User or acquire a SQL session")

    monkeypatch.setattr(users.limiter, "enabled", False)
    app.dependency_overrides[get_admin_password_commands] = lambda: Commands()
    app.dependency_overrides[get_current_user] = forbid
    app.dependency_overrides[get_db] = forbid
    app.include_router(users.router, prefix="/api/v1/users")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://password.test"
    ) as client:
        for _ in range(2):
            response = await client.post(
                f"/api/v1/users/{target}/admin-reset-password",
                headers={"Authorization": "Bearer fixture-admin-password-token"},
            )
            assert response.status_code == 200, response.text
            assert response.json() == {
                "temp_password": _SECRET,
                "message": "请通过安全渠道告知用户首次登录后立即修改密码",
                "target_email": "target@password.test",
            }
            assert _SECRET not in repr(
                users.AdminResetPasswordResponse(**response.json())
            )
    assert len(seen) == 2
    assert seen[0].operation_id != seen[1].operation_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "operation_id",
        "target",
        "principal",
        "extra",
        "password",
        "missing_principal",
    ],
)
async def test_remote_reset_validates_reply_and_never_falls_back(monkeypatch, mutation):
    from app.services import identity_admin_password_commands as binding

    principal, command = _principal(), _command()
    raw = _reply(command, principal)
    if mutation == "operation_id":
        raw["operation_id"] = str(uuid.uuid4())
    elif mutation == "target":
        raw["data"]["target_user_id"] = str(uuid.uuid4())
    elif mutation == "principal":
        raw["principal"]["credential_id"] = str(uuid.uuid4())
    elif mutation == "extra":
        raw["data"]["password_hash"] = "forbidden-private-value"
    elif mutation == "password":
        raw["data"]["temp_password"] = "weak"

    async def remote(identity, path, payload):
        assert mutation != "missing_principal", (
            "Unbound request reached the remote writer"
        )
        assert path == "/v1/identity/admin-passwords/execute"
        assert identity.identity_url == "http://identity.test"
        return raw

    def no_fallback(*args, **kwargs):
        pytest.fail("Remote reset attempted a SQL fallback")

    monkeypatch.setattr(binding, "_remote_json", remote)
    monkeypatch.setattr(binding, "IdentityAdminPasswordCommandData", no_fallback)
    monkeypatch.setattr(binding, "AdminPasswordPostgresData", no_fallback)
    adapter = IdentityAdminPasswordCommands(
        object(),
        IdentityGateway(identity_url="http://identity.test", identity_token="control"),
        expected_principal=None if mutation == "missing_principal" else principal,
    )
    if mutation == "valid":
        result = await adapter.execute(command)
        assert result.data.temp_password == _SECRET
        assert _SECRET not in repr(result)
    else:
        with pytest.raises(IdentityGatewayUnavailable) as rejected:
            await adapter.execute(command)
        assert _SECRET not in repr(rejected.value)


@pytest.mark.asyncio
async def test_explicit_reset_dsn_failure_cannot_select_caller_connection(monkeypatch):
    from app.services import identity_admin_password_commands as binding

    principal = _principal()

    class Auth:
        async def authenticate_bound(self, token):
            return VerifiedCredential(principal=principal, key_hash=None)

    class UnavailableData:
        def __init__(self, dsn):
            assert dsn == "postgresql://reset-client@isolated.test/reset_test"

        async def admin_password_policy(self):
            raise IdentityUnavailable()

        def transaction(self):
            pytest.fail("Reset command ran before exact DSN policy readiness")

    def no_fallback(*args, **kwargs):
        pytest.fail("Explicit reset DSN failure selected caller SQL")

    monkeypatch.setattr(binding, "_AccountAuthenticator", lambda *args: Auth())
    monkeypatch.setattr(binding, "AdminPasswordPostgresData", UnavailableData)
    monkeypatch.setattr(binding, "IdentityAdminPasswordCommandData", no_fallback)
    monkeypatch.setattr(
        settings,
        "identity_admin_password_database_url",
        "postgresql://reset-client@isolated.test/reset_test",
    )
    monkeypatch.setattr(settings, "identity_admin_password_database_url_file", "")
    adapter = IdentityAdminPasswordCommands(
        object(), IdentityGateway(signing_secret="synthetic-reset-secret")
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await adapter.execute(_command())


@pytest.mark.asyncio
async def test_native_misbound_reply_rolls_back_before_commit():
    principal, request = _principal(), _command()
    command = {
        "schema": "aap-identity-admin-passwords-native/v1",
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "actor": principal.model_dump(mode="json"),
        "expected_key_hash": None,
        "request_fingerprint": "1" * 64,
        "derivation_id": "2" * 64,
        "audit_context": request.audit_context.model_dump(mode="json"),
        "payload": request.payload.model_dump(mode="json"),
        "new_password_hash": "$2b$12$" + "a" * 53,
    }
    raw = _reply(request, principal)
    raw["schema"] = "aap-identity-admin-passwords-native/v1"
    raw["replayed"] = False
    raw["data"].pop("temp_password")
    raw["data"]["target_user_id"] = str(uuid.uuid4())

    class DB:
        commits = 0
        rollbacks = 0
        pending = False

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
    data = IdentityAdminPasswordCommandData(db)
    with pytest.raises(IdentityUnavailable):
        async with data.transaction() as transaction:
            await transaction.execute_admin_password_command(command)
    assert db.commits == 0
    assert db.rollbacks >= 1
    assert db.pending is False


@pytest.mark.parametrize(
    "failure,status,detail",
    [
        (AdminPasswordFailure("credential_invalid"), 401, "无效的认证凭据"),
        (AdminPasswordFailure("generation_outdated"), 401, "无效的认证凭据"),
        (AdminPasswordFailure("credential_busy"), 409, "账号正在更新，请刷新后重试"),
        (
            AdminPasswordFailure("manager_required", 403, "当前账号已无管理权限"),
            403,
            "当前账号已无管理权限",
        ),
        (
            AdminPasswordFailure("target_not_found", 404, "用户不存在"),
            404,
            "用户不存在",
        ),
        (
            AdminPasswordFailure(
                "self_reset", 400, "不能给自己重置密码（请用「修改密码」）"
            ),
            400,
            "不能给自己重置密码（请用「修改密码」）",
        ),
        (
            AdminPasswordFailure("operation_conflict", 409, None),
            409,
            {"reason": "operation_conflict"},
        ),
        (AdminPasswordFailure("unavailable", 503, None), 503, "身份密码服务暂不可用"),
    ],
)
def test_reset_owner_errors_keep_public_status_and_bearer_challenge(
    failure, status, detail
):
    with pytest.raises(HTTPException) as result:
        _reject_admin_password_error(failure)
    assert result.value.status_code == status
    assert result.value.detail == detail
    if status == 401:
        assert result.value.headers == {"WWW-Authenticate": "Bearer"}
