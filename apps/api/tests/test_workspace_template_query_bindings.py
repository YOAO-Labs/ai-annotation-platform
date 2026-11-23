"""Real caller and function-only bindings for the portable template reader."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yoao_workspace.configuration import ConfigurationManager
from yoao_workspace.configuration_contract import configuration_request
from yoao_workspace.configuration_postgres import ConfigurationPostgresData
from yoao_workspace.configuration_requests import (
    ProjectTemplateCreate,
    ProjectTemplateOut,
)
from yoao_workspace.template_queries import TemplateQueryManager
from yoao_workspace.template_query_contract import (
    materialize_template_query_data,
    template_query_request,
)
from yoao_workspace.template_query_postgres import TemplateQueryPostgresData
from app.api.v1.project_templates import list_templates
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from app.services.identity_contracts import IdentityActorFacts
from app.services.workspace_configuration import execute_configuration
from app.services.workspace_template_query import execute_template_query
from tests import test_workspace_template_queries_legacy as scenarios
from tests.factory import create_user
from tests.workspace_configuration_helpers import configuration_dsn
from tests.workspace_project_query_helpers import (
    expected_project_sample,
    query_with_pending_orm_work,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _dsn(test_engine, kind, username):
    value = os.environ[f"AAP_MODULE_DATA_{kind}_URL"]
    parsed = make_url(value)
    assert parsed.username == username
    assert parsed.database and parsed.database.endswith("_test")
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed


async def _remove_actor(maker, actor_id):
    async with maker.begin() as db:
        await db.execute(
            text(
                "DELETE FROM public.workspace_configuration_operation_receipts WHERE actor_id=:id"
            ),
            {"id": actor_id},
        )
        await db.execute(
            delete(ProjectTemplate).where(ProjectTemplate.created_by == actor_id)
        )
        await db.execute(delete(User).where(User.id == actor_id))


async def test_function_only_reader_owns_read_transaction_and_preserves_receipt_projection(
    test_engine,
):
    import asyncpg

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db, "project_admin", f"query-reader-{uuid.uuid4().hex}@example.test", ""
            )
            actor_id = actor.id
        operation_id = uuid.uuid4()
        payload = ProjectTemplateCreate(
            name="Function-only reader", type_label="Image", type_key="image-det"
        ).model_dump(exclude_unset=True)
        write_reply = await ConfigurationManager(
            ConfigurationPostgresData(configuration_dsn(test_engine))
        ).execute(
            configuration_request(
                "create_template", actor_id, None, payload, operation_id=operation_id
            )
        )
        assert write_reply.ok
        template_id = write_reply.result.resource_id
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.project_templates SET box_threshold='-0'::double precision WHERE id=:id"
                ),
                {"id": template_id},
            )

        dsn = _dsn(
            test_engine,
            "WORKSPACE_TEMPLATE_QUERY",
            "yoao_workspace_template_query_client",
        )
        data = TemplateQueryPostgresData(dsn.render_as_string(hide_password=False))
        await data.template_query_policy()
        async with data.transaction() as transaction:
            assert (
                await transaction.connection.fetchval("SHOW transaction_read_only")
                == "on"
            )
        manager = TemplateQueryManager(data)
        list_request = template_query_request(
            "list_templates",
            actor_id,
            "project_admin",
            filters={"scope": "private", "type_key": None, "search": "Function-only"},
        )
        rows = materialize_template_query_data(await manager.execute(list_request))
        assert [row["id"] for row in rows] == [template_id]
        assert rows[0]["created_by_name"] == ""
        assert math.copysign(1, rows[0]["box_threshold"]) == -1

        projection = template_query_request(
            "get_template_projection",
            actor_id,
            "project_admin",
            target_id=template_id,
            configuration_receipt={
                "operation_id": operation_id,
                "operation": "create_template",
            },
        )
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET is_active=false, role='employee' WHERE id=:id"
                ),
                {"id": actor_id},
            )
            await db.execute(
                text(
                    "UPDATE public.project_templates SET label_config='null'::jsonb WHERE id=:id"
                ),
                {"id": template_id},
            )
        raw = materialize_template_query_data(await manager.execute(projection))
        assert raw["label_config"] is None
        with pytest.raises(ValidationError):
            ProjectTemplateOut.model_validate(raw)

        wrong = projection.model_copy(update={"target_id": uuid.uuid4()})
        with pytest.raises(asyncpg.PostgresError) as invalid_projection:
            await manager.execute(wrong)
        assert invalid_projection.value.sqlstate == "22023"
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.workspace_configuration_operation_receipts WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 1
            )
            for table in (
                "workspace_configuration_operation_states",
                "workspace_configuration_operation_attempts",
            ):
                assert (
                    await db.scalar(
                        text(
                            f"SELECT count(*) FROM public.{table} WHERE operation_id=:id"
                        ),
                        {"id": operation_id},
                    )
                    == 0
                )
        connection = await data._connect()
        try:
            for statement in (
                "SELECT id FROM public.project_templates",
                "CREATE TEMP TABLE forbidden_query_table(id integer)",
            ):
                with pytest.raises(asyncpg.PostgresError) as forbidden:
                    await connection.execute(statement)
                assert forbidden.value.sqlstate == "42501"
        finally:
            await connection.close()
    finally:
        if actor_id is not None:
            await _remove_actor(maker, actor_id)


@pytest.mark.parametrize("no_autoflush", [False, True])
async def test_borrowed_reader_preserves_pending_work_and_caller_timezone(
    test_engine, no_autoflush
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime = _dsn(test_engine, "LEGACY_RUNTIME", "legacy_runtime")
    engine = create_async_engine(runtime, hide_parameters=True)
    caller = async_sessionmaker(engine, expire_on_commit=False)
    actor_id = None
    template_id = uuid.uuid4()
    original_name = "Persisted query caller"
    pending_name = "Pending query caller"
    original_threshold = 0.5
    pending_threshold = 0.3500000000000001
    created_at = datetime(2020, 1, 1, tzinfo=UTC)
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"query-caller-{uuid.uuid4().hex}@example.test",
                "Query caller",
            )
            actor_id = actor.id
            db.add(
                ProjectTemplate(
                    id=template_id,
                    display_id=f"TQ-{uuid.uuid4().hex[:8]}",
                    name=original_name,
                    type_label="Image",
                    type_key="image-det",
                    scope="private",
                    created_by=actor_id,
                    box_threshold=original_threshold,
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
        arguments = {
            "operation": "list_templates",
            "actor": actor,
            "filters": {"scope": "private", "type_key": None, "search": pending_name},
        }
        async with caller() as db:
            await db.execute(text("SET LOCAL TIME ZONE 'Asia/Shanghai'"))
            await db.execute(text("SET LOCAL extra_float_digits=0"))
            assert await execute_template_query(db, **arguments) == []
            staged = await execute_configuration(
                db,
                operation="update_template",
                actor_id=actor_id,
                target_id=template_id,
                payload={"name": pending_name, "box_threshold": pending_threshold},
            )
            assert staged.resource_id == template_id
            async with query_with_pending_orm_work(db, no_autoflush=no_autoflush):
                (row,) = await execute_template_query(db, **arguments)
            assert row["id"] == template_id
            assert row["name"] == pending_name
            assert (
                await db.scalar(text("SELECT current_setting('TimeZone')"))
                == "Asia/Shanghai"
            )
            assert row["created_at"].isoformat() == created_at.isoformat()
            assert row["box_threshold"] == pending_threshold
            assert await db.scalar(text("SHOW extra_float_digits")) == "0"
            async with maker() as observer:
                stored = await observer.get(ProjectTemplate, template_id)
                assert stored is not None
                assert (stored.name, stored.box_threshold) == (
                    original_name,
                    original_threshold,
                )
            await db.rollback()
            assert await execute_template_query(db, **arguments) == []
        async with maker() as observer:
            stored = await observer.get(ProjectTemplate, template_id)
            assert stored is not None
            assert (stored.name, stored.box_threshold) == (
                original_name,
                original_threshold,
            )
    finally:
        await engine.dispose()
        if actor_id is not None:
            await _remove_actor(maker, actor_id)


async def _borrowed_template_probe(
    *, admin_maker, runtime_maker, actor: User, no_autoflush: bool
):
    facts = IdentityActorFacts(
        id=actor.id,
        name=actor.name,
        email=actor.email,
        role=actor.role,
        is_active=True,
    )
    name = f"Uncommitted template query probe {uuid.uuid4()}"
    payload = ProjectTemplateCreate(
        name=name, type_label="Image", type_key="image-det", scope="private"
    ).model_dump(exclude_unset=True)
    async with runtime_maker() as db:

        async def read_templates():
            return await list_templates(scope="private", search=name, db=db, user=facts)

        assert await read_templates() == []
        staged = await execute_configuration(
            db, operation="create_template", actor_id=actor.id, payload=payload
        )
        async with query_with_pending_orm_work(
            db, no_autoflush=no_autoflush
        ) as pending:
            rows = await read_templates()
        included = [row["id"] for row in rows] == [staged.resource_id]
        pending_in_new = pending in db.new
        transaction_open = db.in_transaction()
        async with admin_maker() as observer:
            committed = (
                await observer.get(ProjectTemplate, staged.resource_id) is not None
            )
        await db.rollback()
        assert await read_templates() == []
    async with admin_maker() as observer:
        assert await observer.get(ProjectTemplate, staged.resource_id) is None
    return {
        "no_autoflush": no_autoflush,
        "included_in_route_result": included,
        "pending_in_session_new_after_read": pending_in_new,
        "caller_transaction_open_after_read": transaction_open,
        "visible_from_owner_before_caller_rollback": committed,
    }


async def test_borrowed_template_owner_matches_frozen_http_and_caller_probes(
    test_engine, app_module, monkeypatch, tmp_path
):
    fixture_path = (
        Path(__file__).parent
        / "fixtures/workspace_template_queries_legacy/samples.json"
    )
    fixture_bytes = fixture_path.read_bytes()
    assert hashlib.sha256(fixture_bytes).hexdigest() == (
        "9c2d7207b6baf0168001b474885e9eccf83449de2cfde7903834cfa1abbb5634"
    )
    reference = json.loads(fixture_bytes)
    assert len(reference["samples"]) == 40
    compared = []

    def compare(current):
        assert current["samples"] == [
            expected_project_sample(sample) for sample in reference["samples"]
        ]
        expected_with_native_write_staged = [
            {**probe, "included_in_route_result": True}
            for probe in reference["direct_read_probes"]
        ]
        assert current["direct_read_probes"] == expected_with_native_write_staged
        assert current["timezone_read_probe"] == reference["timezone_read_probe"]
        compared.append(len(current["samples"]))

    monkeypatch.setattr(scenarios, "_CAPTURE_PATH", tmp_path / "unused-current-run")
    monkeypatch.setattr(scenarios, "_source_metadata", lambda: {"current_owner": True})
    monkeypatch.setattr(scenarios, "_write_capture", compare)
    await scenarios.test_capture_legacy_template_query_and_projection_samples(
        test_engine, app_module, monkeypatch, direct_read_probe=_borrowed_template_probe
    )
    assert compared == [40]
