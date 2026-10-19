"""Native authority regressions for video-input Task and durable Job facts."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from tests.factory import create_project
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset

pytestmark = pytest.mark.asyncio

_READ_TASK = text(
    "SELECT aap_labeling_v1.read_video_input_task_source("
    "CAST(:task_id AS uuid),CAST(:actor_id AS uuid))"
)
_LOCK_TASK = text(
    "SELECT aap_labeling_v1.lock_video_input_task_source("
    "CAST(:task_id AS uuid),CAST(:actor_id AS uuid),CAST(:expected_binding AS jsonb))"
)
_PROJECT_JOB = text(
    "SELECT aap_jobs_v1.project_video_input_job("
    "CAST(:job_id AS uuid),CAST(:task_id AS uuid),CAST(:actor_id AS uuid),"
    "CAST(:project_id AS uuid),CAST(:selection AS jsonb),"
    "CAST(:selection_digest AS text),CAST(:require_active AS boolean))"
)


def _reply(value):
    return json.loads(value) if isinstance(value, str) else value


def _data(value, schema: str) -> dict:
    reply = _reply(value)
    assert reply.get("schema") == schema, reply
    assert reply.get("ok") is True, reply.get("error")
    assert set(reply) == {"schema", "ok", "data"}
    assert isinstance(reply["data"], dict)
    return reply["data"]


def _error(value, code: str, schema: str) -> None:
    assert _reply(value) == {
        "schema": schema,
        "ok": False,
        "error": {"code": code},
    }


async def _seed_video_task(
    db,
    *,
    owner_id: uuid.UUID | None = None,
    file_type: str = "video",
    linked: bool = True,
    status: str = "pending",
):
    owner_id = owner_id or await _seed_actor(db)
    project = await create_project(db, owner_id=owner_id, type_key="video-track")
    dataset_id = await _seed_dataset(db, actor_id=owner_id, data_type="video")
    item_id = uuid.uuid4() if linked else None
    if item_id is not None:
        db.add(
            DatasetItem(
                id=item_id,
                dataset_id=dataset_id,
                file_name="source.mp4",
                file_path=f"source/{uuid.uuid4()}.mp4",
                file_type=file_type,
                file_size=128,
                content_hash="a" * 64,
                metadata_={},
            )
        )
    task_id = uuid.uuid4()
    db.add(
        Task(
            id=task_id,
            project_id=project.id,
            dataset_item_id=item_id,
            display_id=f"T-VIDEO-{task_id.hex[:12]}",
            file_name="source.mp4",
            file_path=f"task-source/{task_id}.mp4",
            file_type=file_type,
            status=status,
        )
    )
    await db.flush()
    return owner_id, project, dataset_id, item_id, task_id


def _read_task(db, *, task_id, actor_id):
    return db.scalar(_READ_TASK, {"task_id": task_id, "actor_id": actor_id})


def _lock_task(db, *, task_id, actor_id, expected_binding):
    return db.scalar(
        _LOCK_TASK,
        {
            "task_id": task_id,
            "actor_id": actor_id,
            "expected_binding": json.dumps(expected_binding),
        },
    )


async def test_video_task_source_visibility_type_and_missing_link(db_session):
    actor_id, project, _, _, video_task_id = await _seed_video_task(db_session)
    visible = _data(
        await _read_task(db_session, task_id=video_task_id, actor_id=actor_id),
        "aap-labeling-video-input-source/v1",
    )
    assert visible["actor_id"] == str(actor_id)
    assert visible["binding"]["file_type"] == "video"
    assert visible["binding"]["dataset_item_id"]
    assert visible["upload_ready"] is True

    outsider = await _seed_actor(db_session, role="employee")
    _error(
        await _read_task(db_session, task_id=video_task_id, actor_id=outsider),
        "task_not_visible",
        "aap-labeling-video-input-source/v1",
    )

    _, _, _, _, image_task_id = await _seed_video_task(
        db_session, owner_id=actor_id, file_type="image"
    )
    _error(
        await _read_task(db_session, task_id=image_task_id, actor_id=actor_id),
        "not_video",
        "aap-labeling-video-input-source/v1",
    )

    _, _, _, _, unlinked_task_id = await _seed_video_task(
        db_session, owner_id=actor_id, linked=False
    )
    _error(
        await _read_task(db_session, task_id=unlinked_task_id, actor_id=actor_id),
        "source_missing",
        "aap-labeling-video-input-source/v1",
    )
    _error(
        await _read_task(db_session, task_id=unlinked_task_id, actor_id=outsider),
        "task_not_visible",
        "aap-labeling-video-input-source/v1",
    )


async def test_task_workflow_version_is_not_source_but_aba_link_and_path_are(
    db_session,
):
    actor_id, _, dataset_id, original_item_id, task_id = await _seed_video_task(
        db_session
    )
    first = _data(
        await _read_task(db_session, task_id=task_id, actor_id=actor_id),
        "aap-labeling-video-input-source/v1",
    )
    expected = first["binding"]

    await db_session.execute(
        text("UPDATE public.tasks SET version=version+1 WHERE id=:id"),
        {"id": task_id},
    )
    current = _data(
        await _lock_task(
            db_session,
            task_id=task_id,
            actor_id=actor_id,
            expected_binding=expected,
        ),
        "aap-labeling-video-input-source/v1",
    )
    assert current["binding"]["task_version"] == expected["task_version"] + 1
    assert {
        key: value for key, value in current["binding"].items() if key != "task_version"
    } == {key: value for key, value in expected.items() if key != "task_version"}

    original_path = expected["file_path"]
    await db_session.execute(
        text("UPDATE public.tasks SET file_path=:path WHERE id=:id"),
        {"path": "temporary/aba.mp4", "id": task_id},
    )
    await db_session.execute(
        text("UPDATE public.tasks SET file_path=:path WHERE id=:id"),
        {"path": original_path, "id": task_id},
    )
    _error(
        await _lock_task(
            db_session,
            task_id=task_id,
            actor_id=actor_id,
            expected_binding=expected,
        ),
        "source_changed",
        "aap-labeling-video-input-source/v1",
    )

    other_item_id = uuid.uuid4()
    db_session.add(
        DatasetItem(
            id=other_item_id,
            dataset_id=dataset_id,
            file_name="other.mp4",
            file_path=f"source/{other_item_id}.mp4",
            file_type="video",
            file_size=128,
            content_hash="b" * 64,
            metadata_={},
        )
    )
    await db_session.flush()
    after_path_aba = _data(
        await _read_task(db_session, task_id=task_id, actor_id=actor_id),
        "aap-labeling-video-input-source/v1",
    )["binding"]
    await db_session.execute(
        text("UPDATE public.tasks SET dataset_item_id=:item WHERE id=:id"),
        {"item": other_item_id, "id": task_id},
    )
    await db_session.execute(
        text("UPDATE public.tasks SET dataset_item_id=:item WHERE id=:id"),
        {"item": original_item_id, "id": task_id},
    )
    _error(
        await _lock_task(
            db_session,
            task_id=task_id,
            actor_id=actor_id,
            expected_binding=after_path_aba,
        ),
        "source_changed",
        "aap-labeling-video-input-source/v1",
    )


async def test_task_lock_rechecks_authority_before_reporting_contention(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        actor_id, project, dataset_id, item_id, task_id = await _seed_video_task(db)
        hidden_actor = await _seed_actor(db, role="employee")
    async with test_engine.connect() as reader:
        source = _data(
            await reader.scalar(_READ_TASK, {"task_id": task_id, "actor_id": actor_id}),
            "aap-labeling-video-input-source/v1",
        )
    try:
        async with test_engine.connect() as blocker:
            blocker_tx = await blocker.begin()
            await blocker.execute(
                text("SELECT id FROM public.tasks WHERE id=:id FOR UPDATE"),
                {"id": task_id},
            )
            async with test_engine.connect() as caller:
                _error(
                    await caller.scalar(
                        _LOCK_TASK,
                        {
                            "task_id": task_id,
                            "actor_id": hidden_actor,
                            "expected_binding": json.dumps(source["binding"]),
                        },
                    ),
                    "task_not_visible",
                    "aap-labeling-video-input-source/v1",
                )
                _error(
                    await caller.scalar(
                        _LOCK_TASK,
                        {
                            "task_id": task_id,
                            "actor_id": actor_id,
                            "expected_binding": json.dumps(source["binding"]),
                        },
                    ),
                    "task_busy",
                    "aap-labeling-video-input-source/v1",
                )
            await blocker_tx.rollback()
    finally:
        async with test_engine.begin() as cleanup:
            await cleanup.execute(
                text("DELETE FROM public.tasks WHERE id=:id"), {"id": task_id}
            )
            if item_id is not None:
                await cleanup.execute(
                    text("DELETE FROM public.dataset_items WHERE id=:id"),
                    {"id": item_id},
                )
            await cleanup.execute(
                text("DELETE FROM public.datasets WHERE id=:id"), {"id": dataset_id}
            )
            await cleanup.execute(
                text("DELETE FROM public.projects WHERE id=:id"), {"id": project.id}
            )
            await cleanup.execute(
                text("DELETE FROM public.users WHERE id=ANY(CAST(:ids AS uuid[]))"),
                {"ids": [actor_id, hidden_actor]},
            )


async def test_task_source_lock_rejects_uploading_after_current_visibility(db_session):
    actor_id, _, _, _, task_id = await _seed_video_task(db_session)
    source = _data(
        await _read_task(db_session, task_id=task_id, actor_id=actor_id),
        "aap-labeling-video-input-source/v1",
    )
    await db_session.execute(
        text("UPDATE public.tasks SET status='uploading' WHERE id=:id"),
        {"id": task_id},
    )
    _error(
        await _lock_task(
            db_session,
            task_id=task_id,
            actor_id=actor_id,
            expected_binding=source["binding"],
        ),
        "source_not_ready",
        "aap-labeling-video-input-source/v1",
    )


def _selection(*, task_ids=None, execution_scope="bulk", batch_id=None, sampling=None):
    return {
        "batch_id": str(batch_id) if batch_id is not None else None,
        "task_ids": task_ids,
        "predict_mode": "skip_predicted",
        "execution_scope": execution_scope,
        "sampling": sampling
        or {"max_frames": 48, "step": 1, "width": 1280, "format": "jpeg"},
    }


def _job_payload(job_id, selection, *, digest=None, **extra):
    payload = {
        "batch_id": selection["batch_id"],
        "execution_scope": selection["execution_scope"],
        "execution_unit": "frame",
        "accepted_video_inputs_protocol": "aap-assets-video-inputs/v1",
        "accepted_video_manifest_id": str(job_id),
        "accepted_video_selection": selection,
    }
    if digest is not None:
        payload["accepted_video_selection_digest"] = digest
    if selection["task_ids"] is not None:
        payload["task_ids"] = selection["task_ids"]
        payload["scope"] = {"task_ids": selection["task_ids"]}
    payload.update(extra)
    return payload


def _project_job(
    db, *, job_id, task_id, actor_id, project_id, selection, digest=None, active=True
):
    return db.scalar(
        _PROJECT_JOB,
        {
            "job_id": job_id,
            "task_id": task_id,
            "actor_id": actor_id,
            "project_id": project_id,
            "selection": json.dumps(selection),
            "selection_digest": digest,
            "require_active": active,
        },
    )


async def _seed_video_job(
    db,
    *,
    actor_id,
    project_id,
    selection,
    payload=None,
    status="pending",
    job_id=None,
    celery_task_id=None,
):
    job_id = job_id or uuid.uuid4()
    db.add(
        AsyncJob(
            id=job_id,
            kind="batch_predict",
            project_id=project_id,
            user_id=actor_id,
            status=status,
            celery_task_id=celery_task_id or str(job_id),
            payload=payload if payload is not None else _job_payload(job_id, selection),
            result={},
        )
    )
    await db.flush()
    return job_id


async def test_video_job_requires_strict_frozen_frame_selection_and_active_row(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    project = await create_project(
        db_session, owner_id=actor_id, type_key="video-track"
    )
    task_id = uuid.uuid4()
    task_ids = sorted([str(task_id), str(uuid.uuid4())])
    selection = _selection(task_ids=task_ids)
    job_id = await _seed_video_job(
        db_session, actor_id=actor_id, project_id=project.id, selection=selection
    )
    facts = _data(
        await _project_job(
            db_session,
            job_id=job_id,
            task_id=task_id,
            actor_id=actor_id,
            project_id=project.id,
            selection=selection,
        ),
        "aap-jobs-video-input-job/v1",
    )["job"]
    assert facts["active"] is True
    assert facts["cancel_requested"] is False
    assert facts["selection"] == selection

    wrong_scope_job_id = uuid.uuid4()
    wrong_scope_payload = _job_payload(
        wrong_scope_job_id, selection, execution_scope="workbench"
    )
    wrong_scope_job = await _seed_video_job(
        db_session,
        actor_id=actor_id,
        project_id=project.id,
        selection=selection,
        payload=wrong_scope_payload,
        job_id=wrong_scope_job_id,
    )
    _error(
        await _project_job(
            db_session,
            job_id=wrong_scope_job,
            task_id=task_id,
            actor_id=actor_id,
            project_id=project.id,
            selection=selection,
        ),
        "manifest_scope_mismatch",
        "aap-jobs-video-input-job/v1",
    )

    mismatched_job_id = uuid.uuid4()
    mismatched_payload = _job_payload(
        mismatched_job_id,
        selection,
        accepted_input_manifest_protocol="aap-assets-input-manifest/v1",
    )
    mismatched_job = await _seed_video_job(
        db_session,
        actor_id=actor_id,
        project_id=project.id,
        selection=selection,
        payload=mismatched_payload,
        job_id=mismatched_job_id,
    )
    _error(
        await _project_job(
            db_session,
            job_id=mismatched_job,
            task_id=task_id,
            actor_id=actor_id,
            project_id=project.id,
            selection=selection,
        ),
        "manifest_scope_mismatch",
        "aap-jobs-video-input-job/v1",
    )

    invalid_sampling = _selection(
        task_ids=task_ids,
        sampling={"max_frames": 48, "step": 1, "width": True, "format": "jpeg"},
    )
    _error(
        await _project_job(
            db_session,
            job_id=job_id,
            task_id=task_id,
            actor_id=actor_id,
            project_id=project.id,
            selection=invalid_sampling,
        ),
        "invalid_request",
        "aap-jobs-video-input-job/v1",
    )

    workbench_selection = _selection(
        task_ids=[str(task_id)], execution_scope="workbench"
    )
    workbench_job_id = uuid.uuid4()
    workbench_job = await _seed_video_job(
        db_session,
        actor_id=actor_id,
        project_id=project.id,
        selection=workbench_selection,
        job_id=workbench_job_id,
    )
    workbench = _data(
        await _project_job(
            db_session,
            job_id=workbench_job,
            task_id=task_id,
            actor_id=actor_id,
            project_id=project.id,
            selection=workbench_selection,
        ),
        "aap-jobs-video-input-job/v1",
    )["job"]
    assert workbench["selection"]["execution_scope"] == "workbench"

    wrong_broker_job_id = uuid.uuid4()
    wrong_broker_job = await _seed_video_job(
        db_session,
        actor_id=actor_id,
        project_id=project.id,
        selection=selection,
        job_id=wrong_broker_job_id,
        celery_task_id=str(uuid.uuid4()),
    )
    _error(
        await _project_job(
            db_session,
            job_id=wrong_broker_job,
            task_id=task_id,
            actor_id=actor_id,
            project_id=project.id,
            selection=selection,
        ),
        "manifest_scope_mismatch",
        "aap-jobs-video-input-job/v1",
    )

    missing_job_id = uuid.uuid4()
    _error(
        await _project_job(
            db_session,
            job_id=missing_job_id,
            task_id=None,
            actor_id=actor_id,
            project_id=project.id,
            selection=_selection(),
        ),
        "job_not_active",
        "aap-jobs-video-input-job/v1",
    )
    missing = _data(
        await _project_job(
            db_session,
            job_id=missing_job_id,
            task_id=None,
            actor_id=actor_id,
            project_id=project.id,
            selection=_selection(),
            active=False,
        ),
        "aap-jobs-video-input-job/v1",
    )
    assert missing == {"job": None}

    digest = "b" * 64
    digest_job_id = uuid.uuid4()
    digest_job = await _seed_video_job(
        db_session,
        actor_id=actor_id,
        project_id=project.id,
        selection=selection,
        job_id=digest_job_id,
        payload=_job_payload(digest_job_id, selection, digest=digest),
    )
    valid_digest = _data(
        await _project_job(
            db_session,
            job_id=digest_job,
            task_id=task_id,
            actor_id=actor_id,
            project_id=project.id,
            selection=selection,
            digest=digest,
        ),
        "aap-jobs-video-input-job/v1",
    )["job"]
    assert valid_digest["active"] is True
    _error(
        await _project_job(
            db_session,
            job_id=digest_job,
            task_id=task_id,
            actor_id=actor_id,
            project_id=project.id,
            selection=selection,
            digest="c" * 64,
        ),
        "manifest_scope_mismatch",
        "aap-jobs-video-input-job/v1",
    )

    invalid_selection = _selection(task_ids=list(reversed(task_ids)))
    _error(
        await _project_job(
            db_session,
            job_id=job_id,
            task_id=task_id,
            actor_id=actor_id,
            project_id=project.id,
            selection=invalid_selection,
        ),
        "invalid_request",
        "aap-jobs-video-input-job/v1",
    )

    cancelled_payload = _job_payload(job_id, selection, cancel_requested=True)
    await db_session.execute(
        text(
            "UPDATE public.async_jobs SET status='cancelled',payload=CAST(:payload AS jsonb) WHERE id=:id"
        ),
        {"payload": json.dumps(cancelled_payload), "id": job_id},
    )
    cancelled = _data(
        await _project_job(
            db_session,
            job_id=job_id,
            task_id=task_id,
            actor_id=actor_id,
            project_id=project.id,
            selection=selection,
            active=False,
        ),
        "aap-jobs-video-input-job/v1",
    )["job"]
    assert cancelled["active"] is False and cancelled["cancel_requested"] is True
    _error(
        await _project_job(
            db_session,
            job_id=job_id,
            task_id=task_id,
            actor_id=actor_id,
            project_id=project.id,
            selection=selection,
            active=True,
        ),
        "job_not_active",
        "aap-jobs-video-input-job/v1",
    )


async def test_video_job_historical_scope_survives_actor_and_project_fk_nulling(
    db_session,
):
    owner_id = await _seed_actor(db_session)
    actor_id = await _seed_actor(db_session, role="employee")
    project = await create_project(
        db_session, owner_id=owner_id, type_key="video-track"
    )
    selection = _selection()
    job_id = await _seed_video_job(
        db_session, actor_id=actor_id, project_id=project.id, selection=selection
    )
    await db_session.execute(
        text("DELETE FROM public.users WHERE id=:id"), {"id": actor_id}
    )
    retained = _data(
        await _project_job(
            db_session,
            job_id=job_id,
            task_id=None,
            actor_id=actor_id,
            project_id=project.id,
            selection=selection,
            active=False,
        ),
        "aap-jobs-video-input-job/v1",
    )["job"]
    assert retained["actor_id"] is None
    assert retained["project_id"] == str(project.id)
    assert retained["active"] is False
    _error(
        await _project_job(
            db_session,
            job_id=job_id,
            task_id=None,
            actor_id=actor_id,
            project_id=project.id,
            selection=selection,
            active=True,
        ),
        "job_not_active",
        "aap-jobs-video-input-job/v1",
    )

    second_project = await create_project(
        db_session, owner_id=owner_id, type_key="video-track"
    )
    second_selection = _selection(
        execution_scope="workbench", task_ids=[str(uuid.uuid4())]
    )
    second_job = await _seed_video_job(
        db_session,
        actor_id=owner_id,
        project_id=second_project.id,
        selection=second_selection,
    )
    second_project_id = second_project.id
    await db_session.execute(
        text("DELETE FROM public.projects WHERE id=:id"), {"id": second_project_id}
    )
    retained_project = _data(
        await _project_job(
            db_session,
            job_id=second_job,
            task_id=uuid.UUID(second_selection["task_ids"][0]),
            actor_id=owner_id,
            project_id=second_project_id,
            selection=second_selection,
            active=False,
        ),
        "aap-jobs-video-input-job/v1",
    )["job"]
    assert retained_project["project_id"] is None
    assert retained_project["retained_project_id"] == str(second_project_id)
    assert retained_project["active"] is False
