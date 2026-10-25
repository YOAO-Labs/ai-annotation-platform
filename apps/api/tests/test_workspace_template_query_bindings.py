"""Real caller and function-only bindings for the portable template reader."""

from __future__ import annotations

from datetime import UTC, datetime
import math
import os
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_workspace.configuration import ConfigurationManager
from aap_workspace.configuration_contract import configuration_request
from aap_workspace.configuration_postgres import ConfigurationPostgresData
from aap_workspace.configuration_requests import (
    ProjectTemplateCreate,
    ProjectTemplateOut,
)
from aap_workspace.template_queries import TemplateQueryManager
from aap_workspace.template_query_contract import (
    materialize_template_query_data,
    template_query_request,
)
from aap_workspace.template_query_postgres import TemplateQueryPostgresData
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from app.services.workspace_template_query_data import WorkspaceTemplateQueryData
from tests.factory import create_user
from tests.workspace_configuration_helpers import configuration_dsn

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
            test_engine, "WORKSPACE_TEMPLATE_QUERY", "workspace_template_query_client"
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

        # This admission snapshot precedes the state change, as in a request
        # whose successful command already committed before response hydration.
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
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"query-caller-{uuid.uuid4().hex}@example.test",
                "Query caller",
            )
            actor_id = actor.id
        pending = ProjectTemplate(
            id=uuid.uuid4(),
            display_id=f"TQ-{uuid.uuid4().hex[:8]}",
            name="Pending query caller",
            type_label="Image",
            type_key="image-det",
            created_by=actor_id,
            box_threshold=0.3500000000000001,
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
            updated_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        request = template_query_request(
            "list_templates",
            actor_id,
            "project_admin",
            filters={"scope": "private", "type_key": None, "search": None},
        )
        async with caller() as db:
            await db.execute(text("SET LOCAL TIME ZONE 'Asia/Shanghai'"))
            await db.execute(text("SET LOCAL extra_float_digits=0"))
            db.add(pending)
            reader = TemplateQueryManager(WorkspaceTemplateQueryData(db))
            if no_autoflush:
                with db.no_autoflush:
                    result = await reader.execute(request)
            else:
                result = await reader.execute(request)
            rows = materialize_template_query_data(result)
            assert any(row["id"] == pending.id for row in rows) is (not no_autoflush)
            assert (pending in db.new) is no_autoflush
            assert db.in_transaction()
            assert (
                await db.scalar(text("SELECT current_setting('TimeZone')"))
                == "Asia/Shanghai"
            )
            if rows:
                assert rows[0]["created_at"].isoformat() == "2020-01-01T00:00:00+00:00"
                assert rows[0]["box_threshold"] == 0.3500000000000001
            assert await db.scalar(text("SHOW extra_float_digits")) == "0"
            async with maker() as observer:
                assert await observer.get(ProjectTemplate, pending.id) is None
            await db.rollback()
    finally:
        await engine.dispose()
        if actor_id is not None:
            await _remove_actor(maker, actor_id)
