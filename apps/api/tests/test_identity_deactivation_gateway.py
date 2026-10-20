"""Self-deactivation and scheduled execution keep owner and admission boundaries."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
import uuid

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.deactivation_contract import (
    DeactivationFailure,
    decode_deactivation_due_request,
    decode_deactivation_reply,
    decode_deactivation_request,
)
from app.api.v1 import me
from app.api.v1.deactivation_commands import (
    execute_deactivation,
    get_deactivation_commands,
)
from app.config import settings
from app.deps import get_current_principal, get_current_user
from app.schemas.user import UserOut
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_deactivation_commands import (
    DeactivationOperatorBusy,
    IdentityDeactivationCommands,
    IdentityDeactivationDueCommands,
)
from app.services.identity_gateway import IdentityGateway
from app.services.identity_deactivation_data import IdentityDeactivationDueData


def principal():
    return Principal(
        actor_id=uuid.uuid4(),
        name="Employee",
        email="user@deactivation.test",
        role="employee",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=2**63 - 1,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def command():
    return decode_deactivation_request(
        {
            "schema": "aap-identity-deactivation/v1",
            "operation": "request_self_deactivation",
            "operation_id": str(uuid.uuid4()),
            "token": "fixture-token",
            "payload": {"reason": "Leaving"},
            "audit_context": {
                "method": "POST",
                "path": "/api/v1/auth/me/deactivation-request",
                "ip": None,
                "request_id": None,
            },
        }
    )


def reply(request, actor):
    account = UserOut(
        id=actor.actor_id,
        email=actor.email,
        name=actor.name,
        role=actor.role,
        group_name=None,
        status="offline",
        preferences={},
        created_at=datetime.now(UTC),
    ).model_dump(mode="json")
    if request.operation == "request_self_deactivation":
        requested_at = datetime.now(UTC)
        account["deactivation_requested_at"] = requested_at.isoformat()
        account["deactivation_scheduled_at"] = (
            requested_at + timedelta(days=7)
        ).isoformat()
    return {
        "schema": "aap-identity-deactivation/v1",
        "ok": True,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "principal": actor.model_dump(mode="json"),
        "data": {"account": account},
    }


def remote(monkeypatch, respond, actor):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.identity_account_commands.httpx.AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    return IdentityDeactivationCommands(
        object(),
        IdentityGateway(identity_url="http://identity.test", identity_token="control"),
        expected_principal=actor,
    )


async def test_remote_deactivation_binds_exact_generation_and_control_bearer(
    monkeypatch,
):
    actor, request = principal(), command()

    def respond(incoming):
        assert incoming.url.path == "/v1/identity/deactivation/execute"
        assert incoming.headers["authorization"] == "Bearer control"
        assert json.loads(incoming.content)["token"] == request.token
        return httpx.Response(200, json=reply(request, actor))

    result = await remote(monkeypatch, respond, actor).execute(request)
    assert result.principal.generation == 2**63 - 1
    assert result.data.account.id == actor.actor_id


@pytest.mark.parametrize(
    "fault", ["actor", "credential", "operation", "numeric_alias", "secret"]
)
async def test_deactivation_rejects_invalid_owner_values(monkeypatch, fault):
    actor, request = principal(), command()
    body = reply(request, actor)
    if fault == "actor":
        body["data"]["account"]["id"] = str(uuid.uuid4())
    elif fault == "credential":
        body["principal"]["credential_id"] = str(uuid.uuid4())
    elif fault == "operation":
        body["operation_id"] = str(uuid.uuid4())
    elif fault == "numeric_alias":
        body["principal"]["generation"] = True
    else:
        body["data"]["account"]["deactivation_reason"] = "private-reason"
    with pytest.raises(IdentityGatewayUnavailable):
        await remote(
            monkeypatch, lambda _: httpx.Response(200, json=body), actor
        ).execute(request)


async def test_remote_deactivation_failure_never_selects_local_writer(monkeypatch):
    def forbidden(*args):
        raise AssertionError("A failed remote owner must not select caller SQL")

    monkeypatch.setattr(
        "app.services.identity_deactivation_commands.IdentityDeactivationCommandData",
        forbidden,
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await remote(monkeypatch, lambda _: httpx.Response(503), principal()).execute(
            command()
        )


@pytest.mark.parametrize(
    "binding", ["identity_deactivation", "identity_deactivation_operator"]
)
async def test_explicit_deactivation_binding_failure_never_selects_caller_sql(
    monkeypatch, binding
):
    monkeypatch.setattr(settings, f"{binding}_database_url", "")
    monkeypatch.setattr(
        settings, f"{binding}_database_url_file", "/missing-deactivation-binding"
    )

    def forbidden(*args):
        raise AssertionError("An invalid explicit binding must not select caller SQL")

    monkeypatch.setattr(
        "app.services.identity_deactivation_commands.IdentityDeactivationCommandData",
        forbidden,
    )
    monkeypatch.setattr(
        "app.services.identity_deactivation_commands.IdentityDeactivationDueData",
        forbidden,
    )
    with pytest.raises(IdentityGatewayUnavailable):
        if binding == "identity_deactivation":
            await IdentityDeactivationCommands(
                object(), IdentityGateway(signing_secret="fixture")
            ).execute(command())
        else:
            await IdentityDeactivationDueCommands(
                object(), IdentityGateway()
            ).process_due_deactivations(
                decode_deactivation_due_request(
                    {
                        "schema": "aap-identity-deactivation-due/v1",
                        "operation_id": str(uuid.uuid4()),
                        "limit": 256,
                    }
                )
            )


async def test_deactivation_composer_rejects_another_admitted_credential():
    admitted, unrelated = principal(), principal()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/me/deactivation-request",
            "headers": [],
            "state": {"identity_principal": admitted},
        }
    )

    class Commands:
        async def execute(self, request):
            return decode_deactivation_reply(request, reply(request, unrelated))

    with pytest.raises(HTTPException) as caught:
        await execute_deactivation(
            request,
            SimpleNamespace(credentials="fixture"),
            Commands(),
            "request_self_deactivation",
            {"reason": None},
        )
    assert caught.value.status_code == 503
    assert request.state.identity_principal == admitted


@pytest.mark.parametrize("code", ["account_busy", "invariant_busy", "credential_busy"])
async def test_deactivation_busy_failures_return_retryable_conflict(code):
    actor = principal()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/me/deactivation-request",
            "headers": [],
            "state": {"identity_principal": actor},
        }
    )

    class Commands:
        async def execute(self, command):
            raise DeactivationFailure(code)

    with pytest.raises(HTTPException) as caught:
        await execute_deactivation(
            request,
            SimpleNamespace(credentials="fixture"),
            Commands(),
            "request_self_deactivation",
            {"reason": None},
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == "账号正在更新，请刷新后重试"
    assert request.state.identity_principal == actor


async def test_deactivation_http_uses_principal_values_without_user_hydration():
    app, actor, seen = FastAPI(), principal(), []
    app.include_router(me.router, prefix="/auth/me")

    async def admitted(request: Request):
        request.state.identity_principal = actor
        return actor

    async def forbidden():
        raise AssertionError("Self-deactivation must not hydrate a local User")

    class Commands:
        async def execute(self, request):
            seen.append(request.operation)
            return decode_deactivation_reply(request, reply(request, actor))

    app.dependency_overrides[get_current_principal] = admitted
    app.dependency_overrides[get_current_user] = forbidden
    app.dependency_overrides[get_deactivation_commands] = lambda: Commands()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for method, args in (("POST", {"json": {"reason": "Leaving"}}), ("DELETE", {})):
            result = await client.request(
                method,
                "/auth/me/deactivation-request",
                headers={"Authorization": "Bearer fixture"},
                **args,
            )
            assert result.status_code == 200, result.text
            assert result.json()["id"] == str(actor.actor_id)
            assert "deactivation_reason" not in result.json()
    assert seen == ["request_self_deactivation", "cancel_self_deactivation"]


async def test_due_worker_drains_pages_and_replays_same_page_ids_after_transient_failure(
    monkeypatch,
):
    from app.workers import deactivation

    attempts, seen = 0, []

    @asynccontextmanager
    async def session():
        yield object()

    class Operator:
        def __init__(self, *args):
            pass

        async def process_due_deactivations(self, request):
            nonlocal attempts
            seen.append(request.operation_id)
            if len(seen) == 2:
                attempts += 1
                raise DeactivationOperatorBusy()
            page = request.operation_id == uuid.uuid5(run_id, "batch:0")
            return SimpleNamespace(
                processed=256 if page else 3, blocked_last_super_admin=0, has_more=page
            )

    monkeypatch.setattr(deactivation, "task_session", session)
    monkeypatch.setattr(deactivation, "IdentityDeactivationDueCommands", Operator)
    run_id = uuid.uuid4()
    with pytest.raises(DeactivationOperatorBusy):
        await deactivation._process_async(run_id)
    assert await deactivation._process_async(run_id) == {"processed": 259}
    assert attempts == 1
    assert seen == [uuid.uuid5(run_id, f"batch:{page}") for page in (0, 1, 0, 1)]


async def test_due_worker_stops_when_only_last_administrator_is_blocked(monkeypatch):
    from app.workers import deactivation

    @asynccontextmanager
    async def session():
        yield object()

    class Operator:
        def __init__(self, *args):
            self.calls = 0

        async def process_due_deactivations(self, request):
            self.calls += 1
            assert self.calls == 1
            return SimpleNamespace(
                processed=0, blocked_last_super_admin=1, has_more=True
            )

    monkeypatch.setattr(deactivation, "task_session", session)
    monkeypatch.setattr(deactivation, "IdentityDeactivationDueCommands", Operator)
    assert await deactivation._process_async() == {"processed": 0}


@pytest.mark.parametrize("fault", [None, "operation", "count"])
async def test_embedded_due_validates_native_and_public_projection_before_commit(fault):
    request = decode_deactivation_due_request(
        {
            "schema": "aap-identity-deactivation-due/v1",
            "operation_id": str(uuid.uuid4()),
            "limit": 2,
        }
    )
    events = []

    class DB:
        async def flush(self):
            pass

        async def scalar(self, statement, parameters):
            command = json.loads(parameters["command"])
            assert command == {
                **request.model_dump(mode="json", by_alias=True),
                "schema": "aap-identity-deactivation-due-native/v1",
            }
            return {
                "schema": "aap-identity-deactivation-due-native/v1",
                "ok": True,
                "operation_id": str(uuid.uuid4())
                if fault == "operation"
                else str(request.operation_id),
                "processed": 3 if fault == "count" else 1,
                "examined": 3 if fault == "count" else 1,
                "blocked_last_super_admin": 0,
                "has_more": False,
            }

        async def commit(self):
            events.append("commit")

        async def rollback(self):
            events.append("rollback")

    data = IdentityDeactivationDueData(DB())
    if fault:
        with pytest.raises(IdentityUnavailable):
            await data.process_due_deactivations(request)
        assert events == ["rollback"]
    else:
        result = await data.process_due_deactivations(request)
        assert result.processed == 1 and result.operation_id == request.operation_id
        assert result.protocol == "aap-identity-deactivation-due/v1"
        assert events == ["commit"]
