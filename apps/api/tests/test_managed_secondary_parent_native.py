import os
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.services.content_snapshot import ContentError
from app.services.labeling_core_read_relations import (
    annotation_row,
    get_core_row,
    task_row,
)
from app.services.managed_secondary_inference import ManagedSecondaryOperation
from tests.factory import create_task
from tests.test_workspace_project_access_step_native import _assert_nowait_conflict
from tests.test_workspace_project_authority_locking import _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier and actual API LOGIN",
    ),
]


async def test_managed_secondary_parent_fences_current_owner_row(test_engine):
    async with _world(test_engine) as (owner, api, world, _url):
        async with owner.begin() as db:
            task = await create_task(db, project_id=world["project_id"])
            other_task = await create_task(db, project_id=world["project_id"])
            parent = Annotation(
                task_id=task.id,
                project_id=task.project_id,
                user_id=world["member_id"],
                class_name="car",
                geometry={"type": "bbox", "x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5},
            )
            db.add(parent)
            await db.flush()
            task_id, other_task_id, parent_id = task.id, other_task.id, parent.id

        try:
            async with api() as db:
                assert (
                    await db.scalar(text("SELECT current_user"))
                    == "identity_api_consumer"
                )
                for statement in (
                    "SELECT id FROM public.annotations LIMIT 1",
                    "UPDATE public.annotations SET version=1 WHERE false",
                ):
                    with pytest.raises(DBAPIError) as denied:
                        async with db.begin_nested():
                            await db.execute(text(statement))
                    assert denied.value.orig.sqlstate == "42501"

                task = await get_core_row(db, task_row, task_id)
                other_task = await get_core_row(db, task_row, other_task_id)
                parent = await get_core_row(db, annotation_row, parent_id)
                actor = SimpleNamespace(id=world["member_id"])
                request = SimpleNamespace(state=SimpleNamespace())
                operation = ManagedSecondaryOperation(db, task, actor, request, parent)
                foreign = ManagedSecondaryOperation(
                    db, other_task, actor, request, parent
                )

                await operation._freeze_extra(db, task)
                await _assert_nowait_conflict(
                    test_engine,
                    "SELECT id FROM public.annotations WHERE id=$1 FOR UPDATE NOWAIT",
                    parent_id,
                )
                await db.commit()

                async with owner.begin() as holder:
                    await holder.execute(
                        select(Annotation.id)
                        .where(Annotation.id == parent_id)
                        .with_for_update(read=True, nowait=True)
                    )
                    await operation._check_extra(db, task)
                    assert operation._locked_parent is None
                    await db.commit()
                    async with api() as contender:
                        with pytest.raises(DBAPIError) as busy:
                            await operation._check_extra(contender, task, final=True)
                        assert busy.value.orig.sqlstate == "55P03"

                with pytest.raises(ContentError, match="secondary_annotation_changed"):
                    await foreign._check_extra(db, other_task, final=True)
                assert foreign._locked_parent is None
                await operation._check_extra(db, task, final=True)
                assert operation._locked_parent is parent
                await _assert_nowait_conflict(
                    test_engine,
                    "SELECT id FROM public.annotations WHERE id=$1 FOR SHARE NOWAIT",
                    parent_id,
                )
                await db.commit()

                async with owner.begin() as writer:
                    current = await writer.get(
                        Annotation, parent_id, with_for_update={"nowait": True}
                    )
                    current.geometry = {**current.geometry, "x": 0.5}
                assert parent.geometry["x"] == 0.25
                stale = ManagedSecondaryOperation(db, task, actor, request, parent)
                with pytest.raises(ContentError, match="secondary_annotation_changed"):
                    await stale._check_extra(db, task, final=True)
                assert parent.geometry["x"] == 0.5
                assert stale._locked_parent is None
        finally:
            async with owner.begin() as db:
                await db.execute(delete(Annotation).where(Annotation.id == parent_id))
                await db.execute(
                    delete(Task).where(Task.id.in_([task_id, other_task_id]))
                )
