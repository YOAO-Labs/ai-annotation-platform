"""Real PostgreSQL regression checks for frozen configuration semantics."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import uuid

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.ml_backend_pool import (
    MLBackendPoolMember,
    MLBackendServicePool,
)
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from app.services.workspace_configuration import execute_configuration
from tests.conftest import create_registry_with_pool
from tests.factory import create_project, create_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="Workspace configuration semantic acceptance requires the owned verifier",
    ),
]


def _legacy_runtime_dsn(test_engine) -> str:
    value = os.environ.get("AAP_MODULE_DATA_LEGACY_RUNTIME_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.username == "legacy_runtime"
    assert parsed.database and parsed.database.endswith("_test")
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql+asyncpg").render_as_string(
        hide_password=False
    )


def _legacy_preflight() -> dict:
    fixture = (
        Path(__file__).resolve().parent
        / "fixtures/workspace_configuration_legacy/preflight.json"
    )
    return json.loads(fixture.read_text(encoding="utf-8"))


def _borrowed_configuration(monkeypatch) -> None:
    monkeypatch.setattr(settings, "workspace_configuration_url", "")
    monkeypatch.setattr(settings, "workspace_configuration_database_url", "")
    monkeypatch.setattr(settings, "workspace_configuration_database_url_file", "")


def _sqlstate(error: BaseException) -> str | None:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        state = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
        if state:
            return state
        error = getattr(error, "orig", None) or error.__cause__
    return None


async def _install_update_notices(
    test_engine, project_id: uuid.UUID, template_id: uuid.UUID
) -> tuple[str, dict[str, str]]:
    suffix = uuid.uuid4().hex[:12]
    function_name = f"workspace_cfg_semantics_{suffix}"
    triggers = {
        "projects": f"workspace_cfg_sem_projects_{suffix}",
        "project_templates": f"workspace_cfg_sem_templates_{suffix}",
    }
    async with test_engine.begin() as connection:
        await connection.execute(
            text(
                f"""CREATE FUNCTION public.{function_name}() RETURNS trigger
                    LANGUAGE plpgsql
                    SECURITY DEFINER
                    SET search_path = pg_catalog
                    AS $function$
                    BEGIN
                        RAISE NOTICE 'configuration_numeric:%', TG_TABLE_NAME;
                        RETURN NEW;
                    END;
                    $function$"""
            )
        )
        await connection.execute(
            text(f"REVOKE ALL ON FUNCTION public.{function_name}() FROM PUBLIC")
        )
        target_ids = {"projects": project_id, "project_templates": template_id}
        for table_name, trigger_name in triggers.items():
            await connection.execute(
                text(
                    f"CREATE TRIGGER {trigger_name} BEFORE UPDATE "
                    f"ON public.{table_name} FOR EACH ROW "
                    f"WHEN (OLD.id = '{target_ids[table_name]}'::uuid) "
                    f"EXECUTE FUNCTION public.{function_name}()"
                )
            )
    return function_name, triggers


async def _drop_update_notices(test_engine, function_name: str, triggers) -> None:
    async with test_engine.begin() as connection:
        for table_name, trigger_name in triggers.items():
            await connection.execute(
                text(f"DROP TRIGGER IF EXISTS {trigger_name} ON public.{table_name}")
            )
        await connection.execute(
            text(f"DROP FUNCTION IF EXISTS public.{function_name}()")
        )


@asynccontextmanager
async def _native_update_notices(db):
    connection = await db.connection()
    raw_connection = await connection.get_raw_connection()
    driver_connection = raw_connection.driver_connection
    notices: list[str] = []

    def record(_connection, message) -> None:
        value = getattr(message, "message", "")
        if value.startswith("configuration_numeric:"):
            notices.append(value.partition(":")[2])

    driver_connection.add_log_listener(record)
    try:
        yield notices
    finally:
        driver_connection.remove_log_listener(record)


@asynccontextmanager
async def _setup_barrier():
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            assert release.wait(timeout=15), "Owned setup barrier timed out"
            body = b'{"name":"owned-backend","supported_prompts":["bbox"]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", entered, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _http_backend_probe(origin: str):
    async def probe(registry_id, probe_subject, *, timeout, limits):
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            response = await client.get(origin + "/setup")
        response.raise_for_status()
        return ["image"]

    return probe


async def _cleanup_owned(test_engine, actor_id: uuid.UUID) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        await db.execute(
            text(
                "DELETE FROM public.workspace_configuration_operation_receipts "
                "WHERE actor_id=:actor_id"
            ),
            {"actor_id": actor_id},
        )
        await db.execute(
            delete(ProjectTemplate).where(ProjectTemplate.created_by == actor_id)
        )
        await db.execute(delete(Project).where(Project.owner_id == actor_id))
        await db.execute(delete(User).where(User.id == actor_id))


async def test_numeric_updates_match_legacy_postgres_semantics(
    test_engine, monkeypatch
):
    _borrowed_configuration(monkeypatch)
    monkeypatch.setattr(settings, "ml_health_timeout", 15)
    legacy = _legacy_preflight()["numeric"]
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = project_id = template_id = None
    runtime_engine = None
    function_name = None
    triggers = None
    try:
        async with owner.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"configuration-semantic-{uuid.uuid4().hex}@example.test",
                "Configuration semantic owner",
            )
            project = await create_project(
                db, owner_id=actor.id, name="Configuration numeric semantics"
            )
            template = ProjectTemplate(
                id=uuid.uuid4(),
                display_id="PT-" + uuid.uuid4().hex[:8],
                name="Configuration numeric semantics",
                type_label="Image",
                type_key="image-det",
                created_by=actor.id,
                box_threshold=0.35,
            )
            db.add(template)
            await db.flush()
            actor_id, project_id, template_id = actor.id, project.id, template.id

        function_name, triggers = await _install_update_notices(
            test_engine, project_id, template_id
        )
        runtime_engine = create_async_engine(
            _legacy_runtime_dsn(test_engine), hide_parameters=True
        )
        runtime = async_sessionmaker(runtime_engine, expire_on_commit=False)
        project_cases = (
            ("exact_loaded", "loaded"),
            ("decimal", 0.35),
            ("repeat_decimal", 0.35),
            ("exact_half", 0.5),
            ("repeat_half", 0.5),
            ("explicit_null", None),
        )
        for label, requested in project_cases:
            async with owner() as observer:
                current = await observer.get(Project, project_id)
                before_value, before_time = current.box_threshold, current.updated_at
            async with runtime() as db, _native_update_notices(db) as notices:
                transaction_time = await db.scalar(
                    text("SELECT transaction_timestamp()")
                )
                value = before_value if requested == "loaded" else requested
                failure = None
                try:
                    await execute_configuration(
                        db,
                        operation="update_project",
                        actor_id=actor_id,
                        target_id=project_id,
                        payload={"box_threshold": value},
                    )
                    await db.commit()
                except DBAPIError as error:
                    assert label == "explicit_null"
                    failure = {
                        "sqlstate": _sqlstate(error),
                        "in_transaction": db.in_transaction(),
                    }
                    with pytest.raises(DBAPIError) as aborted:
                        await db.scalar(text("SELECT 1"))
                    assert _sqlstate(aborted.value) == "25P02"
                    await db.rollback()

            async with owner() as observer:
                stored = await observer.get(Project, project_id)
                facts = {
                    "before_value": before_value,
                    "requested": value,
                    "after_value": stored.box_threshold,
                    "timestamp_changed": stored.updated_at != before_time,
                    "timestamp_is_caller_transaction": stored.updated_at
                    == transaction_time,
                    "project_updates": notices.count("projects"),
                    "failure": failure,
                }
            expected = legacy[label]
            assert facts["before_value"] == expected["before_value"]
            assert facts["requested"] == expected["requested"]
            assert facts["after_value"] == expected["after_value"]
            assert facts["timestamp_changed"] == expected["timestamp_changed"]
            assert (
                facts["timestamp_is_caller_transaction"]
                == expected["timestamp_is_caller_transaction"]
            )
            assert facts["project_updates"] == expected["project_updates"]
            assert facts["failure"] == (
                {
                    "sqlstate": expected["failure"]["sqlstate"],
                    "in_transaction": expected["failure"]["in_transaction"],
                }
                if expected["failure"] is not None
                else None
            )

        template_cases = (
            ("template_exact_loaded", "loaded"),
            ("template_decimal", 0.35),
        )
        for label, requested in template_cases:
            async with owner() as observer:
                current = await observer.get(ProjectTemplate, template_id)
                before_value, before_time = current.box_threshold, current.updated_at
            async with runtime() as db, _native_update_notices(db) as notices:
                value = before_value if requested == "loaded" else requested
                await execute_configuration(
                    db,
                    operation="update_template",
                    actor_id=actor_id,
                    target_id=template_id,
                    payload={"box_threshold": value},
                )
                await db.commit()
            async with owner() as observer:
                stored = await observer.get(ProjectTemplate, template_id)
                facts = {
                    "before_value": before_value,
                    "after_value": stored.box_threshold,
                    "timestamp_changed": stored.updated_at != before_time,
                    "template_updates": notices.count("project_templates"),
                }
            expected = legacy[label]
            assert facts["before_value"] == expected["before_value"]
            assert facts["after_value"] == expected["after_value"]
            assert facts["timestamp_changed"] == expected["timestamp_changed"]
            assert facts["template_updates"] == expected["template_updates"]
    finally:
        if runtime_engine is not None:
            await runtime_engine.dispose()
        if function_name is not None and triggers is not None:
            await _drop_update_notices(test_engine, function_name, triggers)
        if actor_id is not None:
            await _cleanup_owned(test_engine, actor_id)


async def test_account_lock_contention_preserves_create_transaction_semantics(
    test_engine, monkeypatch
):
    _borrowed_configuration(monkeypatch)
    legacy = _legacy_preflight()["account_busy"]
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    runtime_engine = None
    try:
        async with owner.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"configuration-busy-{uuid.uuid4().hex}@example.test",
                "Configuration lock owner",
            )
            actor_id = actor.id

        # The caller stages an ORM user write, so this borrowed transaction needs
        # the schema owner; the restricted runtime role has no direct users DML.
        runtime_engine = create_async_engine(
            test_engine.url.render_as_string(hide_password=False),
            hide_parameters=True,
        )
        for operation in ("create_project", "create_template"):
            for autoflush in (False, True):
                pending_id = uuid.uuid4()
                async with owner() as holder:
                    await holder.scalar(
                        select(User.id).where(User.id == actor_id).with_for_update()
                    )
                    runtime = async_sessionmaker(
                        runtime_engine,
                        autoflush=autoflush,
                        expire_on_commit=False,
                    )
                    async with runtime() as db:
                        pending = User(
                            id=pending_id,
                            name="Pending configuration write",
                            email=f"configuration-pending-{pending_id}@example.test",
                            password_hash="fixture",
                            role="employee",
                            is_active=True,
                        )
                        db.add(pending)
                        payload = (
                            {"name": "Busy project", "type_label": "Image"}
                            if operation == "create_project"
                            else {
                                "name": "Busy template",
                                "type_label": "Image",
                                "type_key": "image-det",
                            }
                        )
                        try:
                            await execute_configuration(
                                db,
                                operation=operation,
                                actor_id=actor_id,
                                payload=payload,
                            )
                        except HTTPException as error:
                            assert error.status_code == 409
                            assert error.detail == {"reason": "account_busy"}
                            facts = {
                                "status": error.status_code,
                                "detail": error.detail,
                                "pending_unflushed": pending in db.new,
                                "in_transaction": db.in_transaction(),
                            }
                            try:
                                await db.scalar(text("SELECT 1"))
                            except DBAPIError as aborted:
                                facts["next_query_sqlstate"] = _sqlstate(aborted)
                            else:
                                facts["next_query_sqlstate"] = None
                            assert facts["next_query_sqlstate"] == "25P02"
                            await db.rollback()
                        else:
                            raise AssertionError("Held account lock was bypassed")

                expected = legacy[f"{operation}_autoflush_{autoflush}"]
                assert facts == {
                    "status": expected["status"],
                    "detail": expected["detail"],
                    "pending_unflushed": expected["pending_unflushed"],
                    "in_transaction": expected["in_transaction"],
                    "next_query_sqlstate": expected["next_query_sqlstate"],
                }
                async with owner() as observer:
                    assert await observer.get(User, pending_id) is None
    finally:
        if runtime_engine is not None:
            await runtime_engine.dispose()
        if actor_id is not None:
            await _cleanup_owned(test_engine, actor_id)


async def test_setup_authority_revocation_rolls_back_draft_and_pool_changes(
    test_engine, monkeypatch
):
    _borrowed_configuration(monkeypatch)
    monkeypatch.setattr(settings, "ml_health_timeout", 15)
    legacy = _legacy_preflight()["setup_role_revocation"]
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = source_project_id = None
    runtime_engine = None
    backend_ids: list[uuid.UUID] = []
    pool_ids: list[uuid.UUID] = []
    suffix = uuid.uuid4().hex[:10]
    try:
        async with owner.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"configuration-revoke-{suffix}@example.test",
                "Configuration race owner",
            )
            source_project = await create_project(
                db,
                owner_id=actor.id,
                name=f"Configuration source {suffix}",
            )
            actor_id, source_project_id = actor.id, source_project.id

        runtime_engine = create_async_engine(
            _legacy_runtime_dsn(test_engine), hide_parameters=True
        )
        runtime = async_sessionmaker(
            runtime_engine, autoflush=False, expire_on_commit=False
        )
        for source_project in (False, True):
            async with _setup_barrier() as (origin, entered, release):
                monkeypatch.setattr(
                    "app.services.inference_backend_probe.backend_probe",
                    lambda: _http_backend_probe(origin),
                )
                async with owner.begin() as db:
                    backend, pool = await create_registry_with_pool(
                        db,
                        name=f"Configuration barrier {suffix}",
                        url=origin,
                        state="connected",
                        is_interactive=False,
                        auth_method="none",
                    )
                    project = await db.get(Project, source_project_id)
                    project.ml_backend_pool_id = pool.id
                    backend_ids.append(backend.id)
                    pool_ids.append(pool.id)
                    backend_id, pool_id = backend.id, pool.id

                async with runtime() as db:
                    payload = {
                        "name": f"Configuration revoked project {suffix}",
                        "ml_backend_source_id": backend_id,
                    }
                    if source_project:
                        payload["source_project_id"] = source_project_id
                    else:
                        payload["type_label"] = "Image"
                    task = asyncio.create_task(
                        execute_configuration(
                            db,
                            operation="create_project",
                            actor_id=actor_id,
                            payload=payload,
                        )
                    )
                    try:
                        assert await asyncio.to_thread(entered.wait, 10), (
                            "Configuration did not reach the setup barrier"
                        )
                        async with owner.begin() as updater:
                            await updater.execute(text("SET LOCAL lock_timeout='2s'"))
                            await updater.execute(
                                text("UPDATE users SET role='employee' WHERE id=:id"),
                                {"id": actor_id},
                            )
                    except BaseException:
                        release.set()
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise
                    finally:
                        release.set()
                    try:
                        await asyncio.wait_for(task, timeout=15)
                    except HTTPException as error:
                        expected = legacy[str(source_project)]
                        assert error.status_code == expected["status"]
                        assert error.detail == expected["detail"]
                        assert db.in_transaction() == expected["in_transaction"]
                    else:
                        raise AssertionError("Revoked creator was not rechecked")
                    finally:
                        await db.rollback()

                async with owner.begin() as observer:
                    created_count = await observer.scalar(
                        text("SELECT count(*) FROM public.projects WHERE name=:name"),
                        {"name": f"Configuration revoked project {suffix}"},
                    )
                    assert created_count == 0
                    enabled, generation = (
                        await observer.execute(
                            text(
                                "SELECT enabled,routing_generation "
                                "FROM public.ml_backend_service_pools WHERE id=:id"
                            ),
                            {"id": pool_id},
                        )
                    ).one()
                    assert enabled is False and generation == 1
                    assert (
                        await observer.scalar(
                            text(
                                "SELECT count(*) FROM public.project_ml_backend_pool WHERE pool_id=:id"
                            ),
                            {"id": pool_id},
                        )
                        == 0
                    )
                    await observer.execute(
                        text("UPDATE users SET role='project_admin' WHERE id=:id"),
                        {"id": actor_id},
                    )
    finally:
        if runtime_engine is not None:
            await runtime_engine.dispose()
        if actor_id is not None:
            async with owner.begin() as db:
                await db.execute(
                    text(
                        "DELETE FROM public.workspace_configuration_operation_receipts "
                        "WHERE actor_id=:actor_id"
                    ),
                    {"actor_id": actor_id},
                )
                await db.execute(
                    delete(ProjectMLBackendPool).where(
                        ProjectMLBackendPool.project_id == source_project_id
                    )
                )
                await db.execute(delete(Project).where(Project.owner_id == actor_id))
                if pool_ids:
                    await db.execute(
                        delete(MLBackendPoolMember).where(
                            MLBackendPoolMember.pool_id.in_(pool_ids)
                        )
                    )
                    await db.execute(
                        delete(MLBackendServicePool).where(
                            MLBackendServicePool.id.in_(pool_ids)
                        )
                    )
                if backend_ids:
                    await db.execute(
                        delete(MLBackendRegistry).where(
                            MLBackendRegistry.id.in_(backend_ids)
                        )
                    )
                await db.execute(delete(User).where(User.id == actor_id))
