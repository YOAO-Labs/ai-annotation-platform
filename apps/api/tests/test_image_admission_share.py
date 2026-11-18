"""Media admission keeps source and authority fences without excluding readers."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from yoao_assets_images.policy import ImagePolicy
from app.db.models.task import Task
from tests.factory import create_project
from tests.test_identity_auth_native import _cleanup, _seed


@pytest.mark.asyncio
async def test_image_admission_shares_task_authority_and_serializes_work(test_engine):
    manager = await _seed(test_engine, create_key=False, role="super_admin")
    stranger = await _seed(test_engine, create_key=False)
    actor_id = manager["actor_id"]
    task_id = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        project = await create_project(session, owner_id=actor_id)
        project_id = project.id
        session.add(
            Task(
                id=task_id,
                project_id=project_id,
                display_id=f"T-ADMIT-{task_id.hex[:12]}",
                file_name="source.png",
                file_path=f"admission/{task_id}.png",
                file_type="image",
                status="pending",
                assignee_id=actor_id,
                media_source_generation=1,
                media_object_identity={
                    "etag": "source",
                    "byte_size": 128,
                    "version_id": None,
                },
                media_source_drifted=False,
            )
        )
    prepare = text(
        "SELECT yoao_assets_v1.prepare_image_media("
        "CAST(:operation AS uuid), CAST(:actor AS uuid), CAST(:task AS uuid), "
        "NULL::uuid, 'thumbnail', NULL::text, CAST(:policy AS jsonb), false)"
    )
    params = {
        "operation": uuid.uuid4(),
        "actor": actor_id,
        "task": task_id,
        "policy": json.dumps(ImagePolicy().model_dump(mode="json")),
    }
    try:
        async with maker() as writer, maker() as reader:
            accepted = await writer.scalar(prepare, params)
            assert accepted["ok"] is True
            rows = await reader.scalar(
                text(
                    "SELECT count(*) FROM yoao_labeling_v1.lock_task_for_core_read("
                    ":task, 'share', true, NULL)"
                ),
                {"task": task_id},
            )
            assert rows == 1
            await reader.rollback()

            async with maker() as unauthorized:
                denied = await unauthorized.scalar(
                    prepare,
                    {
                        **params,
                        "operation": uuid.uuid4(),
                        "actor": stranger["actor_id"],
                    },
                )
                assert denied["error"]["code"] == "task_not_visible"

            for statement, identifier in (
                (
                    "UPDATE public.tasks SET file_path='changed.png' WHERE id=:id",
                    task_id,
                ),
                ("UPDATE public.users SET is_active=false WHERE id=:id", actor_id),
            ):
                async with maker() as mutator:
                    await mutator.execute(text("SET LOCAL lock_timeout='200ms'"))
                    with pytest.raises(DBAPIError) as blocked:
                        await mutator.execute(text(statement), {"id": identifier})
                    assert blocked.value.orig.sqlstate == "55P03"
                    await mutator.rollback()

            async with maker() as contender:
                await contender.execute(text("SET LOCAL lock_timeout='200ms'"))
                conflict = await contender.scalar(
                    prepare, {**params, "operation": uuid.uuid4()}
                )
                assert conflict["error"]["code"] in {
                    "operation_busy",
                    "operation_conflict",
                }
                await contender.rollback()
            assert (
                await writer.scalar(
                    text(
                        "SELECT count(*) FROM public.asset_image_jobs "
                        "WHERE owner_kind='task' AND owner_id=:id AND slot_key='thumbnail'"
                    ),
                    {"id": task_id},
                )
                == 1
            )
            await writer.rollback()
    finally:
        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.tasks WHERE id=:id"), {"id": task_id}
            )
            await session.execute(
                text("DELETE FROM public.projects WHERE id=:id"), {"id": project_id}
            )
        await _cleanup(test_engine, stranger)
        await _cleanup(test_engine, manager)
