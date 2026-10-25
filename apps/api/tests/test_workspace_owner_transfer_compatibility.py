"""Compare extracted owner transfer with actual frozen public/caller behavior."""

from __future__ import annotations

from contextlib import AsyncExitStack
import json
import secrets
import asyncio
import os
from pathlib import Path
import uuid

import httpx
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.security import create_access_token
from app.db.base import engine as api_engine
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.deps import get_db
from app.main import app
from app.application.project_workspace import transfer_project_owner
from app.services.identity_contracts import IdentityActorFacts
from tests.factory import create_project, create_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires disposable module-data verifier state",
    ),
]


def _actor_facts(user: User) -> IdentityActorFacts:
    return IdentityActorFacts(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
    )


@pytest.mark.parametrize(
    "binding", ["caller", "dedicated", "remote_python", "remote_node"]
)
async def test_owner_transfer_matches_actual_legacy(test_engine, monkeypatch, binding):
    for field in (
        "identity_url",
        "identity_auth_database_url",
        "identity_auth_database_url_file",
        "identity_briefs_database_url",
        "identity_briefs_database_url_file",
        "workspace_owner_transfer_url",
        "workspace_owner_transfer_database_url",
        "workspace_owner_transfer_database_url_file",
    ):
        monkeypatch.setattr(settings, field, "")
    monkeypatch.setattr(settings, "audit_async", False)
    runtime = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    assert runtime.username == "legacy_runtime"
    assert (runtime.host, runtime.port, runtime.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert runtime.database.endswith("_test")
    configured = make_url(settings.database_url)
    assert (configured.host, configured.port, configured.database) == (
        runtime.host,
        runtime.port,
        runtime.database,
    )
    service_engine = create_async_engine(runtime, hide_parameters=True)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    service = async_sessionmaker(service_engine, expire_on_commit=False)
    aliases, users, projects = {}, {}, {}
    async with maker.begin() as db:
        for name, role in (
            ("actor", "super_admin"),
            ("old_owner", "project_admin"),
            ("receiver", "project_admin"),
            ("employee", "employee"),
            ("viewer", "viewer"),
            ("inactive", "project_admin"),
        ):
            users[name] = await create_user(
                db,
                role,
                f"owner-transfer-{name}-{uuid.uuid4().hex}@example.test",
                name,
            )
            aliases[str(users[name].id)] = name
        users["inactive"].is_active = False
        for name in ("main", "direct"):
            projects[name] = await create_project(
                db,
                owner_id=users["old_owner"].id,
                name=f"Owner transfer {name}",
            )
            aliases[str(projects[name].id)] = f"project_{name}"
            aliases[projects[name].display_id] = f"display_{name}"
    missing_user, missing_project = uuid.uuid4(), uuid.uuid4()
    aliases[str(missing_user)], aliases[str(missing_project)] = (
        "missing_user",
        "missing_project",
    )

    def normalize(value, field=None):
        if isinstance(value, dict):
            return {key: normalize(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item, field) for item in value]
        if isinstance(value, str):
            if value in aliases:
                return "$" + aliases[value]
            if field in {"created_at", "updated_at"}:
                return "<timestamp>"
        return value

    async def session_per_request():
        async with service() as db:
            yield db

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = session_per_request
    counter = [0]
    audit_started = asyncio.Event()

    def count_sql(_connection, _cursor, statement, *_):
        counter[0] += 1
        if statement.startswith("INSERT INTO audit_logs"):
            audit_started.set()

    engines = {service_engine.sync_engine, api_engine.sync_engine}
    for engine in engines:
        event.listen(engine, "before_cursor_execute", count_sql)
    responses, counts, transactions = {}, {}, {}

    async def call(
        client, name, *, target=None, project=None, actor="actor", body=None
    ):
        actor_value = users[actor]
        token = create_access_token(subject=str(actor_value.id), role=actor_value.role)
        counter[0] = 0
        response = await client.post(
            f"/api/v1/projects/{project or projects['main'].id}/transfer",
            headers={"Authorization": f"Bearer {token}"},
            json=body
            if body is not None
            else {"new_owner_id": str(target or users["receiver"].id)},
        )
        counts[name] = counter[0]
        payload = response.json()
        responses[name] = {
            "status": response.status_code,
            "body": normalize(payload),
            "www_authenticate": response.headers.get("www-authenticate"),
        }
        return response, payload

    stack = AsyncExitStack()
    try:
        if binding != "caller":
            from tests.workspace_owner_transfer_helpers import (
                api_transfer_binding,
                transfer_dsn,
                python_provider_url,
                node_provider_url,
            )

            dsn, token = transfer_dsn(test_engine), secrets.token_urlsafe(32)
            if binding == "dedicated":
                await stack.enter_async_context(
                    api_transfer_binding(dsn=dsn, token=token)
                )
            else:
                factory = (
                    python_provider_url
                    if binding == "remote_python"
                    else node_provider_url
                )
                origin = await stack.enter_async_context(factory(dsn, token))
                await stack.enter_async_context(
                    api_transfer_binding(url=origin, token=token)
                )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://owned-test"
        ) as client:
            response, _ = await call(client, "project_admin_actor", actor="old_owner")
            assert response.status_code == 403
            response, _ = await call(
                client, "employee_target", target=users["employee"].id
            )
            assert response.status_code == 400
            response, _ = await call(client, "viewer_target", target=users["viewer"].id)
            assert response.status_code == 400
            response, _ = await call(
                client, "inactive_target", target=users["inactive"].id
            )
            assert response.status_code == 404
            response, _ = await call(client, "missing_target", target=missing_user)
            assert response.status_code == 404
            response, _ = await call(client, "missing_project", project=missing_project)
            assert response.status_code == 404
            response, _ = await call(
                client,
                "missing_project_before_wrong_role",
                project=missing_project,
                target=users["employee"].id,
            )
            assert response.status_code == 404
            response, _ = await call(
                client,
                "missing_target_before_missing_project",
                project=missing_project,
                target=missing_user,
            )
            assert response.status_code == 404
            response, _ = await call(
                client, "invalid_uuid", body={"new_owner_id": "not-a-uuid"}
            )
            assert response.status_code == 422
            response, _ = await call(client, "missing_body_field", body={})
            assert response.status_code == 422

            async with maker() as holder:
                await holder.scalar(
                    select(Project)
                    .where(Project.id == projects["main"].id)
                    .with_for_update()
                )
                response, _ = await call(client, "project_busy")
                assert response.status_code == 409
                response, _ = await call(
                    client,
                    "project_busy_before_wrong_role",
                    target=users["employee"].id,
                )
                assert response.status_code == 409
                response, _ = await call(
                    client,
                    "inactive_target_before_project_busy",
                    target=users["inactive"].id,
                )
                assert response.status_code == 404
                await holder.rollback()
            for name in ("actor", "receiver"):
                async with maker() as holder:
                    await holder.scalar(
                        select(User).where(User.id == users[name].id).with_for_update()
                    )
                    audit_started.clear()
                    pending = asyncio.create_task(call(client, f"{name}_account_busy"))
                    try:
                        await asyncio.wait_for(audit_started.wait(), timeout=10)
                    finally:
                        await holder.rollback()
                    response, _ = await asyncio.wait_for(pending, timeout=10)
                    assert response.status_code == 409
            response, first = await call(client, "success")
            assert response.status_code == 200
            response, repeated = await call(client, "same_owner_noop")
            assert response.status_code == 200
            assert repeated == first
            assert first["owner_id"] == str(users["receiver"].id)
            assert first["owner_name"] == "receiver"
            async with maker() as db:
                assert (
                    await db.scalar(
                        select(func.count(ProjectMember.id)).where(
                            ProjectMember.project_id == projects["main"].id
                        )
                    )
                    == 0
                )
                business = await db.scalar(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.actor_id.in_([user.id for user in users.values()]),
                        ~AuditLog.action.startswith("http."),
                    )
                )
                assert business == 0

        if binding == "caller":
            for phase, table, key in (
                ("accounts", User, users["receiver"].id),
                ("project", Project, projects["direct"].id),
            ):
                pending_id = uuid.uuid4()
                async with maker() as holder, service(autoflush=True) as caller:
                    await holder.scalar(
                        select(table).where(table.id == key).with_for_update()
                    )
                    caller.add(
                        User(
                            id=pending_id,
                            name="pending",
                            email=f"pending-{pending_id}@example.test",
                            role="employee",
                            password_hash="fixture",
                            is_active=True,
                        )
                    )
                    with pytest.raises(Exception) as rejected:
                        await transfer_project_owner(
                            caller,
                            actor=_actor_facts(users["actor"]),
                            project_id=projects["direct"].id,
                            new_owner_id=users["receiver"].id,
                        )
                    assert getattr(rejected.value, "status_code", None) == 409
                    state = {
                        "status": rejected.value.status_code,
                        "detail": rejected.value.detail,
                        "in_transaction": caller.in_transaction(),
                    }
                    if phase == "accounts":
                        assert not caller.in_transaction()
                        state["query_after_error"] = await caller.scalar(
                            text("SELECT 1")
                        )
                    else:
                        with pytest.raises(DBAPIError) as aborted:
                            await caller.scalar(text("SELECT 1"))
                        state["query_after_error_sqlstate"] = (
                            aborted.value.orig.sqlstate
                        )
                        assert state["query_after_error_sqlstate"] == "25P02"
                    await caller.rollback()
                    await holder.rollback()
                    async with maker() as observer:
                        assert await observer.get(User, pending_id) is None
                    transactions[phase + "_busy"] = state

            for autoflush in (False, True):
                pending_id = uuid.uuid4()
                async with service(autoflush=autoflush) as caller:
                    pending = User(
                        id=pending_id,
                        name="pending",
                        email=f"pending-{pending_id}@example.test",
                        role="employee",
                        password_hash="fixture",
                        is_active=True,
                    )
                    caller.add(pending)
                    evidence = await transfer_project_owner(
                        caller,
                        actor=_actor_facts(users["actor"]),
                        project_id=projects["direct"].id,
                        new_owner_id=users["receiver"].id,
                    )
                    assert evidence.source == "owner_transfer"
                    assert evidence.new_owner_id == users["receiver"].id
                    state = {
                        "in_transaction": caller.in_transaction(),
                        "pending_unflushed": pending in caller.new,
                    }
                    assert state == {
                        "in_transaction": True,
                        "pending_unflushed": not autoflush,
                    }
                    async with maker() as observer:
                        assert (
                            await observer.get(Project, projects["direct"].id)
                        ).owner_id == users["old_owner"].id
                        assert await observer.get(User, pending_id) is None
                        for table, key in (
                            (User, users["actor"].id),
                            (User, users["receiver"].id),
                            (Project, projects["direct"].id),
                        ):
                            with pytest.raises(DBAPIError) as held:
                                await observer.scalar(
                                    select(table.id)
                                    .where(table.id == key)
                                    .with_for_update(nowait=True)
                                )
                            assert held.value.orig.sqlstate == "55P03"
                            await observer.rollback()
                    await caller.rollback()
                    transactions[f"success_autoflush_{autoflush}"] = state
        frozen = json.loads(
            (
                Path(__file__).parent / "fixtures/workspace_owner_transfer_legacy.json"
            ).read_text()
        )
        assert responses == frozen["responses"]
        if binding == "caller":
            assert transactions == frozen["caller_transactions"]
        # Counts are round trips at the API SQLAlchemy boundary, not database work/capacity.
        evidence = {
            "api_sqlalchemy_round_trips": counts,
            "counter_scope": frozen["counter_scope"],
            "responses_compared": len(responses),
            "caller_probes_compared": len(transactions),
        }
        destination = (
            Path(__file__).resolve().parents[3]
            / f".worktree/workspace-owner-transfer-current-samples-{binding}.json"
        )
        destination.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
        )
    finally:
        await stack.aclose()
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
        for engine in engines:
            event.remove(engine, "before_cursor_execute", count_sql)
        await service_engine.dispose()
        await api_engine.dispose()
