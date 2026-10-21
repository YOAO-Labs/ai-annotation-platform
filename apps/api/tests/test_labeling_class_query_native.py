"""Use the actual narrow Labeling credential and its complete JSON row sets."""

from contextlib import asynccontextmanager
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.class_queries import (
    LabelingClassQueryManager,
    materialize_labeling_class_query_data,
)
from aap_labeling.class_query_contract import (
    LabelingClassQueryUnavailable,
    labeling_class_query_request,
)
from aap_labeling.class_query_postgres import (
    CLASS_QUERY_FUNCTIONS,
    CLASS_QUERY_SCHEMA,
    LabelingClassQueryPostgresData,
)
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.annotation import Annotation
from app.services.labeling_class_query_data import LabelingClassQueryData
from tests.factory import create_project, create_task, create_user
from tests.test_labeling_class_usage_reads_legacy import (
    _cleanup_owned,
    _create_annotation,
)
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def class_query_dsn(test_engine):
    return _dsn(
        test_engine,
        "AAP_MODULE_DATA_LABELING_CLASS_QUERY_URL",
        "labeling_class_query_client",
    )


@asynccontextmanager
async def seeded(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids, project_ids, task_ids = [], [], []
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"class-native-{uuid.uuid4().hex}@example.test",
                "Class native owner",
            )
            actor_ids.append(actor.id)
            project = await create_project(db, owner_id=actor.id)
            project_ids.append(project.id)
            task = await create_task(db, project_id=project.id)
            task_ids.append(task.id)
            for name, attributes, active, cancelled in (
                ("car", {"color": "red"}, True, False),
                ("cancelled", {"color": "blue", "雪": 1}, True, True),
                ("inactive", {"excluded": True}, False, False),
                ("", {"": "count empty attribute key"}, True, False),
            ):
                await _create_annotation(
                    db,
                    project_id=project.id,
                    task_id=task.id,
                    user_id=actor.id,
                    class_name=name,
                    attributes=attributes,
                    active=active,
                    cancelled=cancelled,
                )
        yield maker, actor.id, project.id, task.id
    finally:
        await _cleanup_owned(
            maker, actor_ids=actor_ids, project_ids=project_ids, task_ids=task_ids
        )


async def test_labeling_login_has_only_three_functions_and_no_external_data(
    test_engine,
):
    dsn = class_query_dsn(test_engine)
    data = LabelingClassQueryPostgresData(dsn)
    await data.class_query_policy()
    connection = await asyncpg.connect(dsn)
    try:
        await verify_function_only_role(
            connection, schema=CLASS_QUERY_SCHEMA, functions=CLASS_QUERY_FUNCTIONS
        )
        for statement in (
            "SELECT attributes FROM public.annotations LIMIT 1",
            "SELECT id FROM public.projects LIMIT 1",
            "SELECT id FROM public.users LIMIT 1",
            "CREATE TEMP TABLE forbidden_class_query_temp(id integer)",
            "SELECT aap_workspace_project_query_v1.project_query_policy()",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
        for function in ("class_counts", "attribute_rows"):
            with pytest.raises(asyncpg.InvalidParameterValueError):
                await connection.fetch(
                    f"SELECT * FROM {CLASS_QUERY_SCHEMA}.{function}(NULL::uuid)"
                )
    finally:
        await connection.close()
    async with data.transaction() as transaction:
        assert (
            await transaction.connection.fetchval("SHOW transaction_read_only") == "on"
        )
        assert (
            await transaction.connection.fetchval("SHOW transaction_isolation")
            == "read committed"
        )


async def test_labeling_rows_include_active_cancelled_and_preserve_full_json_decode(
    test_engine,
):
    data = LabelingClassQueryPostgresData(class_query_dsn(test_engine))
    async with seeded(test_engine) as (maker, actor_id, project_id, task_id):
        request = labeling_class_query_request(project_id)
        reply = await LabelingClassQueryManager(data).execute(request)
        assert materialize_labeling_class_query_data(reply) == {
            "classes": {"car": 1, "cancelled": 1},
            "attributes": {"color": 2, "雪": 1, "": 1},
        }
        async with maker.begin() as db:
            annotation = await _create_annotation(
                db,
                project_id=project_id,
                task_id=task_id,
                user_id=actor_id,
                class_name="large-number",
                attributes={},
            )
            await db.execute(
                text(
                    "UPDATE annotations SET attributes=CAST(:payload AS jsonb) WHERE id=:id"
                ),
                {"payload": "9" * 5000, "id": annotation.id},
            )
        async with data.transaction() as transaction:
            raw = await transaction.attribute_rows(project_id)
            assert "9" * 5000 in raw
        with pytest.raises(ValueError, match="integer string conversion"):
            await LabelingClassQueryManager(data).execute(request)


async def test_labeling_readiness_rejects_lost_function_or_extra_column_grant(
    test_engine,
):
    data = LabelingClassQueryPostgresData(class_query_dsn(test_engine))
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    try:
        async with maker.begin() as db:
            await db.execute(
                text(
                    "REVOKE EXECUTE ON FUNCTION aap_labeling_class_query_v1.attribute_rows(uuid) "
                    "FROM labeling_class_query_client"
                )
            )
        with pytest.raises(LabelingClassQueryUnavailable):
            await data.class_query_policy()
    finally:
        async with maker.begin() as db:
            await db.execute(
                text(
                    "GRANT EXECUTE ON FUNCTION aap_labeling_class_query_v1.attribute_rows(uuid) "
                    "TO labeling_class_query_client"
                )
            )
    try:
        async with maker.begin() as db:
            await db.execute(
                text(
                    "GRANT SELECT (attributes) ON public.annotations TO labeling_class_query_client"
                )
            )
        with pytest.raises(LabelingClassQueryUnavailable):
            await data.class_query_policy()
    finally:
        async with maker.begin() as db:
            await db.execute(
                text(
                    "REVOKE SELECT (attributes) ON public.annotations FROM labeling_class_query_client"
                )
            )
    await data.class_query_policy()


async def test_borrowed_labeling_read_keeps_autoflush_and_caller_rollback(test_engine):
    async with seeded(test_engine) as (maker, actor_id, project_id, task_id):
        request = labeling_class_query_request(project_id)
        pending_id = uuid.uuid4()
        async with maker() as caller:
            caller.add(
                Annotation(
                    id=pending_id,
                    project_id=project_id,
                    task_id=task_id,
                    user_id=actor_id,
                    source="manual",
                    annotation_type="bbox",
                    tool_unit_id="bbox",
                    class_name="pending",
                    geometry={"type": "bbox"},
                    attributes={"pending_key": True},
                    is_active=True,
                    was_cancelled=False,
                )
            )
            manager = LabelingClassQueryManager(LabelingClassQueryData(caller))
            with caller.no_autoflush:
                result = materialize_labeling_class_query_data(
                    await manager.execute(request)
                )
                assert "pending" not in result["classes"]
                assert "pending_key" not in result["attributes"]
            result = materialize_labeling_class_query_data(
                await manager.execute(request)
            )
            assert result["classes"]["pending"] == 1
            assert result["attributes"]["pending_key"] == 1
            assert caller.in_transaction()
            async with maker() as outside:
                assert (
                    await outside.scalar(
                        text(
                            "SELECT count(*) FROM public.annotations WHERE id=CAST(:id AS uuid)"
                        ),
                        {"id": pending_id},
                    )
                    == 0
                )
            await caller.rollback()
        async with maker() as outside:
            assert (
                await outside.scalar(
                    text(
                        "SELECT count(*) FROM public.annotations WHERE id=CAST(:id AS uuid)"
                    ),
                    {"id": pending_id},
                )
                == 0
            )
