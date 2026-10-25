"""Public actor fencing matches captured legacy responses without User hydration."""

from __future__ import annotations

import ast
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aap_identity.actor_contract import ActorContext, actor_failure_reply, actor_reply
from aap_identity.contract import Principal
from app.db.models.user import User
from app.deps import get_current_principal, get_current_user, get_db
from app.services.identity_contracts import (
    IdentityActorFacts,
    IdentityCredentialRejected,
    IdentityGatewayUnavailable,
)
from app.services.identity_gateway import IdentityGateway
from tests.factory import create_user
from tests.test_identity_gateway import _Client, _principal, _Response
from tests.test_identity_auth_native import _cleanup, _seed

_CAPTURE = json.loads(
    Path(__file__)
    .with_name("fixtures")
    .joinpath("identity_actor_context_legacy.json")
    .read_text()
)["records"]


@pytest.mark.parametrize("expected", _CAPTURE, ids=lambda item: item["case"])
async def test_embedded_actor_fence_matches_actual_legacy_http(db_session, expected):
    case = expected["case"]
    user = await create_user(
        db_session, "employee", case + "@actor-context.test", "  Current actor 😀"
    )
    user.id = uuid.uuid5(uuid.NAMESPACE_URL, "aap-identity-actor/" + case)
    user.weekly_target_default = 307
    await db_session.flush()
    principal = Principal(
        actor_id=user.id,
        name="Admitted name",
        email="admitted@actor-context.test",
        role="employee",
        is_active=True,
        credential_type="api_key",
        credential_id=uuid.UUID(int=71),
        generation=None,
        scopes=["annotations:read"],
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    if case == "profile_changed":
        user.name, user.email = "Updated 停 😀", "updated@actor-context.test"
    elif case == "role_changed":
        user.role = "viewer"
    elif case == "inactive":
        user.is_active, user.disabled_kind = False, "suspended"
    elif case == "missing":
        await db_session.execute(delete(User).where(User.id == user.id))
    await db_session.flush()
    if case in ("autoflush", "no_autoflush"):
        user.name, user.weekly_target_default = "Pending profile", 411

    app = FastAPI()

    async def database():
        yield db_session

    async def admitted(request: Request):
        request.state.identity_principal = principal
        request.state.api_key_scopes = list(principal.scopes)
        request.state.api_key_id = str(principal.credential_id)
        return principal

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_principal] = admitted
    states = []

    @app.middleware("http")
    async def state_after(request, call_next):
        response = await call_next(request)
        states.append(
            {
                key: hasattr(request.state, key)
                for key in ("identity_principal", "api_key_scopes", "api_key_id")
            }
        )
        return response

    @app.get("/actor")
    async def actor(request: Request, actor=Depends(get_current_user)):
        assert isinstance(actor, IdentityActorFacts)
        return {
            "id": str(actor.id),
            "name": actor.name,
            "email": actor.email,
            "role": actor.role,
            "is_active": actor.is_active,
            "weekly_target_default": actor.weekly_target_default,
            "admitted_name": request.state.identity_principal.name,
            "scopes": request.state.api_key_scopes,
            "key_id": request.state.api_key_id,
        }

    calls = []

    def statement(_conn, _cursor, sql, *_args):
        calls.append(sql)

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", statement)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://actor.test"
        ) as client:
            if case == "no_autoflush":
                with db_session.no_autoflush:
                    response = await client.get("/actor")
                # Read-only context must not discard the caller's pending work.
                assert user.name == "Pending profile" and user in db_session.dirty
            else:
                response = await client.get("/actor")
        assert {
            "case": case,
            "status": response.status_code,
            "body": response.json(),
            "www_authenticate": response.headers.get("www-authenticate"),
            "state": states[0],
        } == expected
        assert sum("aap_identity_v1.read_actor_context(" in sql for sql in calls) == 1
        assert not any("SELECT users." in sql for sql in calls)
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", statement)


@pytest.mark.parametrize("autoflush", [True, False])
async def test_actor_read_keeps_uncommitted_work_in_caller_transaction(
    test_engine, autoflush
):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    principal = _principal().model_copy(
        update={"actor_id": fixture["actor_id"], "role": "employee"}
    )
    try:
        async with maker(autoflush=autoflush) as caller:
            user = await caller.get(User, fixture["actor_id"])
            old_name = user.name
            user.name = "Caller pending work"
            actor = await IdentityGateway().actor_context(principal, db=caller)
            assert actor.name == ("Caller pending work" if autoflush else old_name)
            assert caller.in_transaction()
            async with maker() as observer:
                assert (
                    await observer.scalar(
                        select(User.name).where(User.id == fixture["actor_id"])
                    )
                    == old_name
                )
            if not autoflush:
                assert user in caller.dirty and user.name == "Caller pending work"
            await caller.rollback()
        async with maker() as observer:
            assert (
                await observer.scalar(
                    select(User.name).where(User.id == fixture["actor_id"])
                )
                == old_name
            )
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.parametrize("target, expected", [(None, 200), (0, 200), (307, 307)])
async def test_dashboard_keeps_account_weekly_target_and_legacy_fallback(
    httpx_client, db_session, annotator, target, expected
):
    user, token = annotator
    user.weekly_target_default = target
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/dashboard/annotator",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["weekly_target"] == expected


def _actor(principal):
    return ActorContext(
        id=principal.actor_id,
        name="Fresh profile",
        email="fresh@example.test",
        role=principal.role,
        is_active=True,
        weekly_target_default=0,
    )


async def test_remote_actor_is_bound_and_does_not_refresh_admitted_scopes(monkeypatch):
    principal = _principal()
    actor = _actor(principal)
    client = _Client(_Response(200, actor_reply(actor)))
    monkeypatch.setattr(
        "app.services.identity_gateway.httpx.AsyncClient", lambda **_kw: client
    )
    gateway = IdentityGateway(
        identity_url="https://identity.internal", identity_token="control"
    )

    def forbidden(_db):
        raise AssertionError("selected remote actor context must never use local data")

    monkeypatch.setattr(gateway, "embedded_data", forbidden)
    assert await gateway.actor_context(principal, db=object()) == actor
    assert client.call["url"] == "https://identity.internal/v1/identity/actor-context"
    assert client.call["json"] == {
        "schema": "aap-identity-actor/v1",
        "actor_id": str(principal.actor_id),
        "admitted_role": principal.role,
    }
    assert principal.name != actor.name
    assert principal.scopes is None


@pytest.mark.parametrize(
    "fault", ["rejected", "bad_control", "outage", "wrong_id", "wrong_role", "extra"]
)
async def test_selected_remote_actor_rejects_failures_without_fallback(
    monkeypatch, fault
):
    principal = _principal()
    payload = actor_reply(_actor(principal))
    status = 200
    if fault == "rejected":
        payload = actor_failure_reply("credential_invalid")
    elif fault == "bad_control":
        status = 401
    elif fault == "outage":
        status = 503
    elif fault == "wrong_id":
        payload["data"]["id"] = str(uuid.uuid4())
    elif fault == "wrong_role":
        payload["data"]["role"] = "viewer"
    else:
        payload["data"]["password_hash"] = "must-not-leak"
    client = _Client(_Response(status, payload))
    monkeypatch.setattr(
        "app.services.identity_gateway.httpx.AsyncClient", lambda **_kw: client
    )
    gateway = IdentityGateway(
        identity_url="http://identity.internal", identity_token="control"
    )

    def forbidden(_db):
        raise AssertionError("no local fallback")

    monkeypatch.setattr(gateway, "embedded_data", forbidden)
    error = (
        IdentityCredentialRejected
        if fault == "rejected"
        else IdentityGatewayUnavailable
    )
    with pytest.raises(error):
        await gateway.actor_context(principal, db=object())


async def test_remote_actor_enforces_response_budget_and_timeout(monkeypatch):
    import asyncio

    principal = _principal()
    client = _Client(_Response(200, actor_reply(_actor(principal))))
    monkeypatch.setattr(
        "app.services.identity_gateway.httpx.AsyncClient", lambda **_kw: client
    )
    gateway = IdentityGateway(
        identity_url="http://identity.internal", identity_token="c"
    )
    monkeypatch.setattr("app.services.identity_gateway.MAX_RESPONSE_BYTES", 64)
    with pytest.raises(IdentityGatewayUnavailable):
        await gateway.actor_context(principal)

    async def stalled(path, payload):
        await asyncio.sleep(1)

    gateway.timeout_seconds = 0.001
    monkeypatch.setattr(gateway, "_remote_reply", stalled)
    with pytest.raises(IdentityGatewayUnavailable):
        await gateway.actor_context(principal)


def test_shared_actor_dependency_has_no_user_orm_or_credential_reauthentication():
    from app import deps
    import inspect

    source = inspect.getsource(deps.get_current_user)
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.Name) and node.id == "User" for node in ast.walk(tree)
    )
    assert "identity.actor_context" in source
    assert "authenticate(" not in source
