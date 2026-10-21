"""Verify the narrow Labeling-owned class-name write capability."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from tests.factory import create_project, create_task, create_user
from tests.test_asset_storage_cleanup_runtime import _verify_postgres


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_RENAME_SIGNATURE = "aap_labeling_v1.rename_project_annotation_class(uuid,text,text)"
_RENAME_SQL = text(
    "SELECT aap_labeling_v1.rename_project_annotation_class("
    "CAST(:project_id AS uuid), CAST(:old_name AS text), CAST(:new_name AS text))"
)
_FIXED_TIME = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


def _verify_owned_test_database(test_engine) -> None:
    assert os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    url = test_engine.url
    assert url.database and url.database.endswith("_test")
    _verify_postgres(url)


async def _set_workspace_code(connection: AsyncConnection) -> None:
    await connection.execute(text("SET LOCAL ROLE workspace_code"))
    assert await connection.scalar(text("SELECT current_user")) == "workspace_code"


async def _invoke_rename(
    connection: AsyncConnection,
    *,
    project_id: uuid.UUID,
    old_name: str,
    new_name: str,
) -> None:
    await connection.execute(
        _RENAME_SQL,
        {
            "project_id": project_id,
            "old_name": old_name,
            "new_name": new_name,
        },
    )


async def _snapshot_connection(connection: AsyncConnection, annotation_ids):
    result = await connection.execute(
        select(*Annotation.__table__.columns)
        .where(Annotation.id.in_(annotation_ids))
        .order_by(Annotation.id)
    )
    return {row["id"]: dict(row) for row in result.mappings()}


async def _snapshot_session(db: AsyncSession, annotation_ids):
    result = await db.execute(
        select(*Annotation.__table__.columns)
        .where(Annotation.id.in_(annotation_ids))
        .order_by(Annotation.id)
    )
    return {row["id"]: dict(row) for row in result.mappings()}


def _assert_only_target_class_names_changed(
    before,
    after,
    *,
    project_id: uuid.UUID,
    old_name: str,
    new_name: str,
) -> set[uuid.UUID]:
    assert set(after) == set(before)
    changed_ids = {
        annotation_id
        for annotation_id, row in before.items()
        if row["project_id"] == project_id and row["class_name"] == old_name
    }
    assert len(changed_ids) == 4
    for annotation_id, previous in before.items():
        current = after[annotation_id]
        expected_name = (
            new_name if annotation_id in changed_ids else previous["class_name"]
        )
        assert current["class_name"] == expected_name
        assert {
            key: value for key, value in current.items() if key != "class_name"
        } == {key: value for key, value in previous.items() if key != "class_name"}
    return changed_ids


async def _function_privileges(connection: AsyncConnection) -> dict:
    result = await connection.execute(
        text(
            """
            SELECT pg_catalog.pg_get_userbyid(function.proowner) AS owner_name,
                   function.provolatile::text AS volatility,
                   function.prosecdef AS security_definer,
                   pg_catalog.pg_get_function_result(function.oid) AS result_type,
                   function.proconfig AS configuration,
                   pg_catalog.has_function_privilege(
                       'workspace_code', function.oid, 'EXECUTE'
                   ) AS workspace_execute,
                   pg_catalog.has_function_privilege(
                       'workspace_configuration_client', function.oid, 'EXECUTE'
                   ) AS configuration_client_execute,
                   pg_catalog.has_function_privilege(
                       'labeling_class_query_client', function.oid, 'EXECUTE'
                   ) AS class_query_client_execute,
                   EXISTS (
                       SELECT 1
                       FROM pg_catalog.aclexplode(
                           COALESCE(
                               function.proacl,
                               pg_catalog.acldefault('f', function.proowner)
                           )
                       ) AS privilege
                       WHERE privilege.grantee = 0::oid
                         AND privilege.privilege_type = 'EXECUTE'
                   ) AS public_execute,
                   pg_catalog.has_column_privilege(
                       'workspace_code', 'public.annotations', 'class_name', 'UPDATE'
                   ) AS workspace_raw_class_update
            FROM pg_catalog.pg_proc AS function
            WHERE function.oid = pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": _RENAME_SIGNATURE},
    )
    row = result.mappings().one()
    return dict(row)


async def _cleanup_owned(
    maker,
    *,
    actor_ids: list[uuid.UUID],
    project_ids: list[uuid.UUID],
    task_ids: list[uuid.UUID],
    annotation_ids: list[uuid.UUID],
) -> None:
    async with maker.begin() as db:
        if annotation_ids:
            await db.execute(
                delete(Annotation).where(Annotation.id.in_(annotation_ids))
            )
        if task_ids:
            await db.execute(delete(Task).where(Task.id.in_(task_ids)))
        if project_ids:
            await db.execute(delete(Project).where(Project.id.in_(project_ids)))
        if actor_ids:
            await db.execute(delete(User).where(User.id.in_(actor_ids)))


async def test_workspace_code_invokes_only_the_labeling_class_rename_effect(
    test_engine,
):
    _verify_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    annotation_ids: list[uuid.UUID] = []

    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"class-rename-effect-{uuid.uuid4().hex}@example.test",
                "Class rename effect owner",
            )
            actor_ids.append(actor.id)
            target_project = await create_project(db, owner_id=actor.id)
            other_project = await create_project(db, owner_id=actor.id)
            project_ids.extend((target_project.id, other_project.id))
            target_task = await create_task(db, project_id=target_project.id)
            foreign_task = await create_task(db, project_id=other_project.id)
            task_ids.extend((target_task.id, foreign_task.id))

            row_specs = (
                (
                    target_project.id,
                    target_task.id,
                    "Car",
                    "manual",
                    True,
                    False,
                    "bbox",
                ),
                (
                    target_project.id,
                    target_task.id,
                    "Car",
                    "ai",
                    False,
                    False,
                    "region",
                ),
                (
                    target_project.id,
                    target_task.id,
                    "Car",
                    "manual",
                    True,
                    True,
                    "disabled_unit",
                ),
                (
                    target_project.id,
                    foreign_task.id,
                    "Car",
                    "ai",
                    True,
                    False,
                    "foreign_task_unit",
                ),
                (
                    target_project.id,
                    target_task.id,
                    "Dog",
                    "manual",
                    True,
                    False,
                    "bbox",
                ),
                (
                    other_project.id,
                    foreign_task.id,
                    "Car",
                    "manual",
                    True,
                    False,
                    "bbox",
                ),
            )
            for index, (
                row_project_id,
                task_id,
                class_name,
                source,
                is_active,
                was_cancelled,
                tool_unit_id,
            ) in enumerate(row_specs):
                annotation_id = uuid.uuid4()
                annotation_ids.append(annotation_id)
                db.add(
                    Annotation(
                        id=annotation_id,
                        task_id=task_id,
                        project_id=row_project_id,
                        user_id=actor.id,
                        source=source,
                        annotation_type="bbox",
                        tool_unit_id=tool_unit_id,
                        class_name=class_name,
                        geometry={
                            "type": "bbox",
                            "x": index / 10,
                            "y": 0.2,
                            "w": 0.3,
                            "h": 0.4,
                        },
                        confidence=0.75 if source == "ai" else None,
                        attributes={"marker": f"before-{index}", "雪": index},
                        attributes_meta={"marker": {"origin": source}},
                        is_active=is_active,
                        was_cancelled=was_cancelled,
                        version=10 + index,
                        z_order=index,
                        is_locked=bool(index % 2),
                        is_hidden=bool(index % 3 == 0),
                        created_at=_FIXED_TIME,
                        updated_at=_FIXED_TIME,
                    )
                )

        async with maker() as db:
            before = await _snapshot_session(db, annotation_ids)

        async with test_engine.connect() as connection:
            metadata = await _function_privileges(connection)
        assert metadata["owner_name"] == "labeling_code"
        assert metadata["volatility"] == "v"
        assert metadata["security_definer"] is True
        assert metadata["result_type"] == "void"
        assert metadata["configuration"] == ["search_path=pg_catalog, pg_temp"]
        assert metadata["workspace_execute"] is True
        assert metadata["configuration_client_execute"] is False
        assert metadata["class_query_client_execute"] is False
        assert metadata["public_execute"] is False
        assert metadata["workspace_raw_class_update"] is False

        async with test_engine.connect() as caller:
            caller_transaction = await caller.begin()
            await _set_workspace_code(caller)
            await _invoke_rename(
                caller,
                project_id=target_project.id,
                old_name="Car",
                new_name="Truck",
            )
            await caller.execute(text("RESET ROLE"))
            assert caller.in_transaction()
            inside = await _snapshot_connection(caller, annotation_ids)
            changed_ids = _assert_only_target_class_names_changed(
                before,
                inside,
                project_id=target_project.id,
                old_name="Car",
                new_name="Truck",
            )
            assert any(
                annotation_id in changed_ids
                and before[annotation_id]["task_id"] == foreign_task.id
                for annotation_id in before
            )
            assert caller_transaction.is_active
            async with maker() as outside:
                assert await _snapshot_session(outside, annotation_ids) == before
            await caller_transaction.rollback()

        async with maker() as after_rollback:
            assert await _snapshot_session(after_rollback, annotation_ids) == before

        async with test_engine.connect() as caller:
            caller_transaction = await caller.begin()
            await _set_workspace_code(caller)
            await _invoke_rename(
                caller,
                project_id=target_project.id,
                old_name="Car",
                new_name="Truck",
            )
            await caller.execute(text("RESET ROLE"))
            assert caller_transaction.is_active
            await caller_transaction.commit()

        async with maker() as after_commit:
            committed = await _snapshot_session(after_commit, annotation_ids)
        assert (
            _assert_only_target_class_names_changed(
                before,
                committed,
                project_id=target_project.id,
                old_name="Car",
                new_name="Truck",
            )
            == changed_ids
        )
    finally:
        await _cleanup_owned(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            task_ids=task_ids,
            annotation_ids=annotation_ids,
        )


@pytest.mark.parametrize("null_argument", ("project_id", "old_name", "new_name"))
async def test_workspace_code_class_rename_rejects_each_null_argument(
    test_engine, null_argument
):
    _verify_owned_test_database(test_engine)
    arguments = {
        "project_id": uuid.uuid4(),
        "old_name": "Car",
        "new_name": "Truck",
    }
    arguments[null_argument] = None

    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await _set_workspace_code(connection)
            savepoint = await connection.begin_nested()
            with pytest.raises(DBAPIError) as error:
                await connection.execute(_RENAME_SQL, arguments)
            sqlstate = getattr(error.value.orig, "sqlstate", None) or getattr(
                error.value.orig, "pgcode", None
            )
            assert sqlstate == "22023"
            await savepoint.rollback()
        finally:
            if transaction.is_active:
                await transaction.rollback()
