"""Group transport and HTTP boundaries fail closed without local identity reads."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from aap_identity.contract import Principal
from aap_identity.group_contract import decode_group_reply, decode_group_request
from app.api.v1 import groups, users
from app.api.v1.group_commands import (
    execute_group,
    get_group_commands,
    require_group_manager,
)
from app.config import settings
from app.deps import get_current_principal, get_current_user
from app.schemas.user import UserOut
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway
from app.services.identity_group_commands import IdentityGroupCommands


def _principal(role="project_admin"):
    return Principal(
        actor_id=uuid.uuid4(),
        name="Group manager",
        email="manager@groups.test",
        role=role,
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=2**63 - 1,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def _command():
    return decode_group_request(
        {
            "schema": "aap-identity-groups/v1",
            "operation": "list_groups",
            "operation_id": str(uuid.uuid4()),
            "token": "fixture-group-token",
            "payload": {},
            "audit_context": {
                "method": "GET",
                "path": "/api/v1/groups",
                "ip": None,
                "request_id": None,
            },
        }
    )


def _reply(command, principal, data=None):
    return {
        "schema": "aap-identity-groups/v1",
        "ok": True,
        "operation": command.operation,
        "operation_id": str(command.operation_id),
        "principal": principal.model_dump(mode="json"),
        "data": {"items": []} if data is None else data,
    }


def _remote(monkeypatch, respond, principal):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.identity_account_commands.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    return IdentityGroupCommands(
        object(),
        IdentityGateway(identity_url="http://identity.test", identity_token="control"),
        expected_principal=principal,
    )


async def test_group_remote_success_keeps_exact_generation_and_uses_control_bearer(
    monkeypatch,
):
    principal, command = _principal(), _command()

    def respond(request):
        assert request.url.path == "/v1/identity/groups/execute"
        assert request.headers["authorization"] == "Bearer control"
        assert json.loads(request.content)["token"] == command.token
        return httpx.Response(200, json=_reply(command, principal))

    result = await _remote(monkeypatch, respond, principal).execute(command)
    assert result.principal.generation == 2**63 - 1
    assert result.data.items == []


@pytest.mark.parametrize(
    "fault", ["actor", "operation", "credential", "numeric_alias", "secret"]
)
async def test_group_remote_reply_binding_rejects_invalid_owner_values(
    monkeypatch, fault
):
    principal, command = _principal(), _command()
    body = _reply(command, principal)
    if fault == "actor":
        body["principal"]["actor_id"] = str(uuid.uuid4())
    elif fault == "credential":
        body["principal"]["credential_id"] = str(uuid.uuid4())
    elif fault == "operation":
        body["operation_id"] = str(uuid.uuid4())
    elif fault == "numeric_alias":
        body["principal"]["generation"] = True
    else:
        body["data"]["password_hash"] = "must-not-escape"
    with pytest.raises(IdentityGatewayUnavailable):
        await _remote(
            monkeypatch, lambda _: httpx.Response(200, json=body), principal
        ).execute(command)


async def test_unavailable_remote_group_owner_never_opens_local_data(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Remote group failure must not select a local writer")

    monkeypatch.setattr(
        "app.services.identity_group_commands.IdentityGroupCommandData", forbidden
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await _remote(monkeypatch, lambda _: httpx.Response(503), _principal()).execute(
            _command()
        )


async def test_explicit_group_dsn_file_failure_never_falls_back_to_caller_sql(
    monkeypatch,
):
    monkeypatch.setattr(settings, "identity_group_database_url", "")
    monkeypatch.setattr(
        settings, "identity_group_database_url_file", "/not-a-group-dsn-fixture"
    )

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Explicit group binding failure must not select caller SQL"
        )

    monkeypatch.setattr(
        "app.services.identity_group_commands.IdentityGroupCommandData", forbidden
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await IdentityGroupCommands(
            object(), IdentityGateway(signing_secret="fixture-only")
        ).execute(_command())


async def test_group_http_composer_rejects_another_admitted_actor():
    principal, unrelated = _principal(), _principal()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/groups",
            "headers": [],
            "state": {"identity_principal": principal},
        }
    )

    class Commands:
        async def execute(self, command):
            return decode_group_reply(command, _reply(command, unrelated))

    with pytest.raises(HTTPException) as caught:
        await execute_group(
            request,
            SimpleNamespace(credentials="fixture"),
            Commands(),
            "list_groups",
            {},
        )
    assert caught.value.status_code == 503
    assert request.state.identity_principal == principal


@pytest.mark.parametrize("role", ["employee", "viewer"])
def test_group_manager_gate_preserves_platform_role_rule(role):
    with pytest.raises(HTTPException) as caught:
        require_group_manager(_principal(role))
    assert caught.value.status_code == 403


async def test_group_http_routes_use_principal_values_without_user_hydration():
    app = FastAPI()
    app.include_router(groups.router, prefix="/groups")
    app.include_router(users.router, prefix="/users")
    principal, group_id = _principal(), uuid.uuid4()
    seen = []

    async def admitted(request: Request):
        request.state.identity_principal = principal
        return principal

    async def forbidden():
        raise AssertionError("Group endpoints must not hydrate a local User")

    class Commands:
        async def execute(self, command):
            seen.append(command.operation)
            group = {
                "id": str(group_id),
                "name": "Group",
                "description": None,
                "member_count": 0,
                "created_at": datetime.now(UTC).isoformat(),
            }
            account = UserOut(
                id=principal.actor_id,
                email=principal.email,
                name=principal.name,
                role=principal.role,
                group_name=None,
                status="offline",
                preferences={},
                created_at=datetime.now(UTC),
            ).model_dump(mode="json")
            data = {
                "list_groups": {"items": []},
                "query_groups": {
                    "items": [],
                    "total": 0,
                    "page": 1,
                    "page_size": 50,
                    "pages": 0,
                },
                "create_group": {"group": {**group, "id": str(command.operation_id)}},
                "update_group": {"group": group},
                "delete_group": {"group_id": str(group_id), "deleted": True},
                "preview_group_assignments": {
                    "group_id": None,
                    "group_name": None,
                    "items": [
                        {
                            "user_id": str(principal.actor_id),
                            "email": None,
                            "name": None,
                            "ok": False,
                            "error": "用户不存在或不在管理范围内",
                            "current_group_id": None,
                            "current_group_name": None,
                            "next_group_id": None,
                            "next_group_name": None,
                        }
                    ],
                    "applicable": 0,
                    "blocked": 1,
                },
                "apply_group_assignments": {
                    "items": [
                        {
                            "user_id": str(principal.actor_id),
                            "ok": False,
                            "retryable": True,
                            "error": "用户不存在或不在管理范围内",
                        }
                    ],
                    "succeeded": 0,
                    "failed": 1,
                },
                "assign_user_group": {"account": account},
            }[command.operation]
            return decode_group_reply(command, _reply(command, principal, data))

    app.dependency_overrides[get_current_principal] = admitted
    app.dependency_overrides[get_current_user] = forbidden

    async def owner_commands(request: Request):
        await admitted(request)
        return Commands()

    app.dependency_overrides[get_group_commands] = owner_commands
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://groups.test",
        headers={"Authorization": "Bearer fixture"},
    ) as client:
        for method, path, body, expected in [
            ("GET", "/groups", None, 200),
            ("GET", "/groups/query", None, 200),
            ("POST", "/groups", {"name": "Group"}, 201),
            ("PATCH", f"/groups/{group_id}", {"name": "Group"}, 200),
            ("DELETE", f"/groups/{group_id}", None, 204),
            (
                "POST",
                "/users/groups/bulk/preview",
                {"user_ids": [str(principal.actor_id)], "group_id": None},
                200,
            ),
            (
                "POST",
                "/users/groups/bulk",
                {"user_ids": [str(principal.actor_id)], "group_id": None},
                200,
            ),
            ("PATCH", f"/users/{principal.actor_id}/group", {"group_id": None}, 200),
        ]:
            response = await client.request(method, path, json=body)
            assert response.status_code == expected, response.text
        invalid_target = await client.patch(
            "/users/not-a-uuid/group", json={"group_id": None}
        )
        assert invalid_target.status_code == 404
    assert len(seen) == 8
