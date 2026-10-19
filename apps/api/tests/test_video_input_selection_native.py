"""Native Labeling cursor and Task-lock behavior for retained video selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from tests.test_video_input_authorities_native import _seed_actor, _seed_video_task

pytestmark = pytest.mark.asyncio

_SCHEMA = "aap-labeling-video-input-selection/v1"
_OPEN = text(
    "SELECT aap_labeling_v1.open_video_input_manifest_members("
    "CAST(:actor AS uuid),CAST(:project AS uuid),CAST(:selection AS jsonb))"
)
_LOCK = text(
    "SELECT aap_labeling_v1.lock_video_input_manifest_member("
    "CAST(:actor AS uuid),CAST(:project AS uuid),CAST(:task AS uuid),"
    "CAST(:selection AS jsonb),CAST(:captured AS jsonb))"
)


def _reply(value):
    return json.loads(value) if isinstance(value, str) else value


def _ok(value):
    reply = _reply(value)
    assert reply.get("schema") == _SCHEMA, reply
    assert reply.get("ok") is True, reply.get("error")
    assert set(reply) == {"schema", "ok", "data"}
    assert isinstance(reply["data"], dict)
    return reply["data"]


def _error(value, code):
    assert _reply(value) == {
        "schema": _SCHEMA,
        "ok": False,
        "error": {"code": code},
    }


def _selection(
    *, task_ids=None, scope="bulk", batch_id=None, predict_mode="skip_predicted"
):
    return {
        "batch_id": str(batch_id) if batch_id is not None else None,
        "task_ids": sorted([str(value) for value in task_ids]) if task_ids else None,
        "predict_mode": predict_mode,
        "execution_scope": scope,
        "sampling": {"max_frames": 48, "step": 1, "width": 1280, "format": "jpeg"},
    }


async def _open_rows(db, *, actor, project, selection):
    opened = _reply(
        await db.scalar(
            _OPEN,
            {
                "actor": actor,
                "project": project,
                "selection": json.dumps(selection),
            },
        )
    )
    if opened.get("ok") is not True:
        return opened["error"]
    cursor = opened["data"]["cursor"]
    assert re.fullmatch(r"aap_video_members_[0-9a-f]{32}", cursor)
    rows = await db.execute(text(f'FETCH ALL FROM "{cursor}"'))
    return [_reply(row[0]) for row in rows]


async def _lock(db, *, actor, project, selection, captured_task):
    return await db.scalar(
        _LOCK,
        {
            "actor": actor,
            "project": project,
            "task": captured_task["task_id"],
            "selection": json.dumps(selection),
            "captured": json.dumps(captured_task),
        },
    )


async def _add_task(
    db,
    *,
    project_id,
    dataset_id,
    file_type="video",
    status="pending",
    total_predictions=0,
    batch_id=None,
    linked=True,
):
    task_id = uuid.uuid4()
    item_id = uuid.uuid4() if linked else None
    if item_id is not None:
        db.add(
            DatasetItem(
                id=item_id,
                dataset_id=dataset_id,
                file_name=f"{task_id}.mp4",
                file_path=f"source/{task_id}.mp4",
                file_type=file_type,
                file_size=128,
                content_hash="c" * 64,
                metadata_={},
            )
        )
    db.add(
        Task(
            id=task_id,
            project_id=project_id,
            dataset_item_id=item_id,
            batch_id=batch_id,
            display_id=f"T-VIDEO-{task_id.hex[:12]}",
            file_name=f"{task_id}.mp4",
            file_path=f"task-source/{task_id}.mp4",
            file_type=file_type,
            status=status,
            total_predictions=total_predictions,
        )
    )
    await db.flush()
    return task_id, item_id


async def test_implicit_selection_streams_complete_scope_and_skip_counts_before_filter(
    db_session,
):
    actor, project, dataset_id, _, first_task = await _seed_video_task(db_session)
    second_task, _ = await _add_task(
        db_session,
        project_id=project.id,
        dataset_id=dataset_id,
        total_predictions=2,
    )
    rows = await _open_rows(
        db_session,
        actor=actor,
        project=project.id,
        selection=_selection(),
    )
    assert rows[0] == {"kind": "header", "candidate_count": 2, "selected_count": 1}
    assert rows[1]["kind"] == "member"
    assert rows[1]["ordinal"] == 1
    assert rows[1]["task"]["task_id"] == str(first_task)
    assert all(
        row.get("task", {}).get("task_id") != str(second_task) for row in rows[1:]
    )

    image_task, _ = await _add_task(
        db_session,
        project_id=project.id,
        dataset_id=dataset_id,
        file_type="image",
    )
    mixed = await _open_rows(
        db_session,
        actor=actor,
        project=project.id,
        selection=_selection(task_ids=[first_task, image_task]),
    )
    assert mixed == {"code": "unsupported_media"}


async def test_raw_selection_rejects_null_numeric_and_boolean_enum_values(db_session):
    actor, project, _, _, _ = await _seed_video_task(db_session)
    valid = _selection()
    for field in ("predict_mode", "execution_scope"):
        for invalid in (None, 17, True):
            malformed = {**valid, field: invalid}
            _error(
                await db_session.scalar(
                    _OPEN,
                    {
                        "actor": actor,
                        "project": project.id,
                        "selection": json.dumps(malformed),
                    },
                ),
                "invalid_request",
            )


async def test_raw_unlinked_capture_rejects_null_scalar_source_facts(db_session):
    actor, project, dataset_id, _, _ = await _seed_video_task(db_session)
    task_id, _ = await _add_task(
        db_session,
        project_id=project.id,
        dataset_id=dataset_id,
        linked=False,
    )
    selection = _selection(task_ids=[task_id])
    rows = await _open_rows(
        db_session, actor=actor, project=project.id, selection=selection
    )
    captured = rows[1]["task"]
    for field in (
        "task_id",
        "project_id",
        "task_version",
        "task_source_generation",
        "task_source_snapshot",
        "file_path",
        "file_type",
    ):
        malformed = {**captured, field: None}
        _error(
            await _lock(
                db_session,
                actor=actor,
                project=project.id,
                selection=selection,
                captured_task=malformed,
            ),
            "invalid_request",
        )


async def test_workbench_requires_single_task_allows_own_lock_and_rejects_other_lock(
    db_session,
):
    actor, project, dataset_id, _, task_id = await _seed_video_task(db_session)
    own_lock = TaskLock(
        task_id=task_id,
        user_id=actor,
        expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        unique_id=uuid.uuid4(),
    )
    db_session.add(own_lock)
    await db_session.flush()
    selection = _selection(task_ids=[task_id], scope="workbench")
    bulk_locked = await _open_rows(
        db_session,
        actor=actor,
        project=project.id,
        selection=_selection(task_ids=[task_id]),
    )
    assert bulk_locked == {"code": "task_scope_changed"}
    rows = await _open_rows(
        db_session, actor=actor, project=project.id, selection=selection
    )
    assert rows[0] == {"kind": "header", "candidate_count": 1, "selected_count": 1}
    assert rows[1]["task"]["batch_id"] is None
    locked = _reply(
        await _lock(
            db_session,
            actor=actor,
            project=project.id,
            selection=selection,
            captured_task=rows[1]["task"],
        )
    )
    assert locked["ok"] is True
    assert locked["data"]["upload_ready"] is True

    other_actor = await _seed_actor(db_session, role="employee")
    db_session.add(
        TaskLock(
            task_id=task_id,
            user_id=other_actor,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            unique_id=uuid.uuid4(),
        )
    )
    await db_session.flush()
    rows = await _open_rows(
        db_session, actor=actor, project=project.id, selection=selection
    )
    assert rows == {"code": "task_scope_changed"}


async def test_explicit_and_batch_scope_fail_closed_and_unlinked_member_is_retained(
    db_session,
):
    actor, project, dataset_id, _, task_id = await _seed_video_task(db_session)
    _, _, _, _, foreign_task = await _seed_video_task(
        db_session, owner_id=await _seed_actor(db_session, role="project_admin")
    )
    foreign = await _open_rows(
        db_session,
        actor=actor,
        project=project.id,
        selection=_selection(task_ids=[foreign_task]),
    )
    assert foreign == {"code": "task_scope_changed"}

    batch_id = uuid.uuid4()
    db_session.add(
        TaskBatch(
            id=batch_id,
            project_id=project.id,
            display_id=f"BT-{batch_id.hex[:8]}",
            name="Unassigned draft",
            status="draft",
            annotator_id=None,
            reviewer_id=None,
            admin_locked=False,
        )
    )
    await db_session.execute(
        text("UPDATE public.tasks SET batch_id=:batch WHERE id=:task"),
        {"batch": batch_id, "task": task_id},
    )
    allowed = await _open_rows(
        db_session,
        actor=actor,
        project=project.id,
        selection=_selection(task_ids=[task_id], batch_id=batch_id),
    )
    assert allowed[0]["candidate_count"] == 1
    workbench = await _open_rows(
        db_session,
        actor=actor,
        project=project.id,
        selection=_selection(task_ids=[task_id], scope="workbench"),
    )
    assert workbench[0]["candidate_count"] == 1
    assert workbench[1]["task"]["batch_id"] == str(batch_id)

    await db_session.execute(
        text("UPDATE public.task_batches SET admin_locked=true WHERE id=:batch"),
        {"batch": batch_id},
    )
    denied = await _open_rows(
        db_session,
        actor=actor,
        project=project.id,
        selection=_selection(task_ids=[task_id], batch_id=batch_id),
    )
    assert denied == {"code": "task_scope_changed"}

    unlinked_task, _ = await _add_task(
        db_session,
        project_id=project.id,
        dataset_id=dataset_id,
        linked=False,
    )
    unlinked_selection = _selection(task_ids=[unlinked_task])
    unlinked = await _open_rows(
        db_session,
        actor=actor,
        project=project.id,
        selection=unlinked_selection,
    )
    assert unlinked[1]["task"]["dataset_item_id"] is None
    _error(
        await _lock(
            db_session,
            actor=actor,
            project=project.id,
            selection=unlinked_selection,
            captured_task=unlinked[1]["task"],
        ),
        "source_missing",
    )


async def test_member_lock_ignores_workflow_version_and_detects_source_aba(db_session):
    actor, project, _, _, task_id = await _seed_video_task(db_session)
    selection = _selection(task_ids=[task_id])
    rows = await _open_rows(
        db_session, actor=actor, project=project.id, selection=selection
    )
    captured = rows[1]["task"]
    await db_session.execute(
        text("UPDATE public.tasks SET version=version+1 WHERE id=:task"),
        {"task": task_id},
    )
    accepted = _reply(
        await _lock(
            db_session,
            actor=actor,
            project=project.id,
            selection=selection,
            captured_task=captured,
        )
    )
    assert accepted["ok"] is True
    assert accepted["data"]["task"]["task_version"] == captured["task_version"] + 1

    await db_session.execute(
        text(
            "UPDATE public.tasks SET file_path='temporary/aba.mp4', "
            "media_source_generation=media_source_generation+1 WHERE id=:task"
        ),
        {"task": task_id},
    )
    await db_session.execute(
        text(
            "UPDATE public.tasks SET file_path=:path, "
            "media_source_generation=media_source_generation+1 WHERE id=:task"
        ),
        {"path": captured["file_path"], "task": task_id},
    )
    _error(
        await _lock(
            db_session,
            actor=actor,
            project=project.id,
            selection=selection,
            captured_task=captured,
        ),
        "task_scope_changed",
    )


async def test_member_lock_rejects_uploading_task_after_current_authority(db_session):
    actor, project, _, _, task_id = await _seed_video_task(db_session)
    selection = _selection(task_ids=[task_id])
    rows = await _open_rows(
        db_session, actor=actor, project=project.id, selection=selection
    )
    captured = rows[1]["task"]
    await db_session.execute(
        text("UPDATE public.tasks SET status='uploading' WHERE id=:task"),
        {"task": task_id},
    )
    _error(
        await _lock(
            db_session,
            actor=actor,
            project=project.id,
            selection=selection,
            captured_task=captured,
        ),
        "source_not_ready",
    )


async def test_current_authority_precedes_task_nowait_contention(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        actor, project, dataset_id, _, task_id = await _seed_video_task(db)
        hidden_actor = await _seed_actor(db, role="employee")
    async with test_engine.connect() as reader:
        opened = _reply(
            await reader.scalar(
                _OPEN,
                {
                    "actor": actor,
                    "project": project.id,
                    "selection": json.dumps(_selection(task_ids=[task_id])),
                },
            )
        )
        cursor = opened["data"]["cursor"]
        rows = await reader.execute(text(f'FETCH ALL FROM "{cursor}"'))
        captured = list(rows)[1][0]["task"]
    try:
        async with test_engine.connect() as blocker:
            blocker_tx = await blocker.begin()
            await blocker.execute(
                text("SELECT id FROM public.tasks WHERE id=:id FOR UPDATE"),
                {"id": task_id},
            )
            async with test_engine.connect() as caller:
                hidden = await caller.scalar(
                    _LOCK,
                    {
                        "actor": hidden_actor,
                        "project": project.id,
                        "task": task_id,
                        "selection": json.dumps(_selection(task_ids=[task_id])),
                        "captured": json.dumps(captured),
                    },
                )
                _error(hidden, "task_not_visible")
                contended = await caller.scalar(
                    _LOCK,
                    {
                        "actor": actor,
                        "project": project.id,
                        "task": task_id,
                        "selection": json.dumps(_selection(task_ids=[task_id])),
                        "captured": json.dumps(captured),
                    },
                )
                _error(contended, "task_busy")
            await blocker_tx.rollback()
    finally:
        async with test_engine.begin() as cleanup:
            await cleanup.execute(
                text("DELETE FROM public.tasks WHERE id=:id"), {"id": task_id}
            )
            await cleanup.execute(
                text("DELETE FROM public.dataset_items WHERE dataset_id=:id"),
                {"id": dataset_id},
            )
            await cleanup.execute(
                text("DELETE FROM public.datasets WHERE id=:id"),
                {"id": dataset_id},
            )
            await cleanup.execute(
                text("DELETE FROM public.projects WHERE id=:id"), {"id": project.id}
            )
            await cleanup.execute(
                text("DELETE FROM public.users WHERE id=ANY(CAST(:ids AS uuid[]))"),
                {"ids": [actor, hidden_actor]},
            )
