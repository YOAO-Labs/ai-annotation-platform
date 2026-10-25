"""Directory presentation and transport keep the owner boundary intact."""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from aap_identity.contract import Principal
from aap_identity.directory_contract import (
    decode_directory_reply,
    decode_directory_request,
)
from app.api.v1 import users
from app.api.v1.directory_commands import get_directory_commands
from app.config import settings
from app.deps import get_current_user, get_db
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_directory_commands import IdentityDirectoryCommands
from app.services.identity_gateway import IdentityGateway


def _principal():
    return Principal(
        actor_id=uuid.uuid4(),
        name="Directory manager",
        email="manager@directory.test",
        role="super_admin",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=2**63 - 1,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def _command(operation="list_users", payload=None):
    return decode_directory_request(
        {
            "schema": "aap-identity-directory/v1",
            "operation": operation,
            "operation_id": str(uuid.uuid4()),
            "token": "fixture-directory-token",
            "audit_context": {
                "method": "GET",
                "path": "/api/v1/users",
                "ip": None,
                "request_id": None,
            },
            "payload": payload or {},
        }
    )


def _user():
    return {
        "id": str(uuid.uuid4()),
        "email": "user@directory.test",
        "name": "=user",
        "role": "historical-role",
        "group_name": None,
        "group_id": None,
        "status": "historical-status",
        "is_active": False,
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


def _reply(request, principal):
    row = _user()
    operation = request.operation
    if operation == "list_users":
        data = {"users": [row]}
    elif operation == "query_users":
        row = {**row, "role": "super_admin", "is_active": True, "status": "online"}
        data = {
            "items": [{**row, "is_managed": False, "is_lifecycle_managed": False}],
            "total": 1,
            "page": request.payload.page,
            "page_size": request.payload.page_size,
            "pages": 1,
        }
    elif operation == "user_stats":
        data = {"total": 1, "online": 0, "weekly_active": 1}
    else:
        data = {
            "users": [
                {
                    key: row[key]
                    for key in (
                        "id",
                        "email",
                        "name",
                        "role",
                        "group_name",
                        "group_id",
                        "status",
                        "created_at",
                    )
                }
            ],
            "exported_by": principal.email,
            "exported_at": "2030-01-02T03:04:05+00:00",
            "request_id": None,
        }
    return {
        "schema": "aap-identity-directory/v1",
        "ok": True,
        "operation": operation,
        "operation_id": str(request.operation_id),
        "principal": principal.model_dump(mode="json"),
        "data": data,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,operation",
    [
        ("/api/v1/users?status=all", "list_users"),
        ("/api/v1/users/query?status=all", "query_users"),
        ("/api/v1/users/stats", "user_stats"),
        ("/api/v1/users/export?format=json&status=all", "export_users"),
        ("/api/v1/users/export?format=csv&status=all", "export_users"),
    ],
)
async def test_public_directory_uses_values_without_user_hydration(path, operation):
    principal = _principal()
    if operation == "query_users":
        principal = principal.model_copy(update={"role": "project_admin"})
    seen = []

    class Commands:
        async def execute(self, command):
            seen.append(command)
            assert command.operation == operation
            return decode_directory_reply(
                command, _reply(command, principal), expected_principal=principal
            )

    app = FastAPI()

    @app.middleware("http")
    async def admitted(request, call_next):
        request.state.identity_principal = principal
        return await call_next(request)

    def forbid():
        pytest.fail("Directory handler tried to hydrate User or obtain a SQL session")

    app.dependency_overrides[get_directory_commands] = lambda: Commands()
    app.dependency_overrides[get_current_user] = forbid
    app.dependency_overrides[get_db] = forbid
    app.include_router(users.router, prefix="/api/v1/users")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://directory.test"
    ) as client:
        response = await client.get(
            path, headers={"Authorization": "Bearer fixture-directory-token"}
        )
    assert response.status_code == 200, response.text
    assert len(seen) == 1
    if operation == "list_users":
        row = response.json()[0]
        assert row["role"] == "historical-role" and row["status"] == "historical-status"
        assert row["preferences"]["unknown"]["count"] == 2**63 - 1
    elif operation == "query_users":
        assert response.json()["items"][0]["is_managed"] is False
        assert response.json()["items"][0]["is_lifecycle_managed"] is False
    elif operation == "export_users":
        if "format=json" in path:
            assert response.json()["_export_meta"] == {
                "exported_by": principal.email,
                "exported_at": "2030-01-02T03:04:05+00:00",
                "request_id": None,
                "count": 1,
            }
            assert len(response.json()["users"][0]) == 8
        else:
            assert response.text.startswith("\ufeff# Exported by: " + principal.email)
            assert "# Exported at: 2030-01-02T03:04:05+00:00" in response.text
            assert "'=user" in response.text
        assert (
            'filename="users_20300102_030405.'
            in response.headers["content-disposition"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["valid", "operation_id", "principal", "extra_user_field", "count_type"]
)
async def test_remote_directory_validates_values_without_sql_fallback(
    monkeypatch, mutation
):
    import app.services.identity_directory_commands as owner

    request = _command(
        "user_stats" if mutation == "count_type" else "list_users", {"status": "all"}
    )
    principal = _principal()
    raw = _reply(request, principal)
    if mutation == "operation_id":
        raw["operation_id"] = str(uuid.uuid4())
    elif mutation == "principal":
        raw["principal"]["actor_id"] = str(uuid.uuid4())
    elif mutation == "extra_user_field":
        raw["data"]["users"][0]["password_hash"] = "not-a-public-field"
    elif mutation == "count_type":
        raw["data"]["total"] = True

    async def remote(identity, path, payload):
        assert path == "/v1/identity/directory/execute"
        assert json.loads(payload)["token"] == request.token
        return json.dumps(raw).encode()

    def forbid(*args, **kwargs):
        pytest.fail("Remote owner failure fell back to caller SQL")

    monkeypatch.setattr(owner, "_remote_json", remote)
    monkeypatch.setattr(owner, "IdentityDirectoryCommandData", forbid)
    gateway = IdentityGateway(
        identity_url="http://identity.test", identity_token="control"
    )
    commands = IdentityDirectoryCommands(
        object(), gateway, expected_principal=principal
    )
    if mutation == "valid":
        reply = await commands.execute(request)
        assert reply.data.users[0].preferences["unknown"]["count"] == 2**63 - 1
    else:
        with pytest.raises(IdentityGatewayUnavailable):
            await commands.execute(request)


@pytest.mark.asyncio
async def test_explicit_directory_dsn_never_uses_caller_sql_on_connection_failure(
    monkeypatch,
):
    import app.services.identity_directory_commands as owner
    from aap_identity.contract import IdentityUnavailable

    request = _command()
    monkeypatch.setattr(
        settings,
        "identity_directory_database_url",
        "postgresql://offline.invalid/owned",
    )
    monkeypatch.setattr(settings, "identity_directory_database_url_file", "")

    class Data:
        def __init__(self, dsn):
            assert dsn == "postgresql://offline.invalid/owned"

        async def directory_policy(self):
            return {
                "schema": "aap-identity-directory-policy/v1",
                "policy": "aap-identity-directory/v1",
            }

    class Manager:
        def __init__(self, auth, data, secret):
            assert isinstance(data, Data)

        async def execute(self, command):
            raise IdentityUnavailable()

    def forbid(*args, **kwargs):
        pytest.fail("Explicit owner DSN failure fell back to caller SQL")

    monkeypatch.setattr(owner, "DirectoryPostgresData", Data)
    monkeypatch.setattr(owner, "DirectoryManager", Manager)
    monkeypatch.setattr(owner, "IdentityDirectoryCommandData", forbid)
    with pytest.raises(IdentityGatewayUnavailable):
        await IdentityDirectoryCommands(
            object(), IdentityGateway(signing_secret="directory-secret")
        ).execute(request)


@pytest.mark.parametrize(
    "endpoint",
    [users.list_users, users.query_users, users.users_stats, users.export_users],
)
def test_directory_handlers_have_no_caller_orm_authority(endpoint):
    parameters = inspect.signature(endpoint).parameters
    assert "commands" in parameters
    assert not {"db", "actor", "user"} & set(parameters)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,expected_status", [("generation_outdated", 401), ("credential_busy", 409)]
)
async def test_owner_credential_failure_after_admission_keeps_public_http_status(
    code, expected_status
):
    from aap_identity.directory_contract import DirectoryFailure

    principal = _principal()

    class Commands:
        async def execute(self, command):
            raise DirectoryFailure(code)

    app = FastAPI()

    @app.middleware("http")
    async def admitted(request, call_next):
        request.state.identity_principal = principal
        return await call_next(request)

    app.dependency_overrides[get_directory_commands] = lambda: Commands()
    app.include_router(users.router, prefix="/api/v1/users")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://directory.test"
    ) as client:
        response = await client.get(
            "/api/v1/users", headers={"Authorization": "Bearer fixture-directory-token"}
        )
    assert response.status_code == expected_status
    if expected_status == 401:
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["detail"] == "无效的认证凭据"
    else:
        assert response.json()["detail"] == "账号正在更新，请刷新后重试"
