from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import Request

from app.api.v1 import batches, data_manager, projects
from app.schemas.data_manager_actions import DataManagerTaskExportRequest
from app.services import async_job as async_job_service
from app.services import jobs_queue
from app.services.audit import AuditService

_JOB_ID = uuid.UUID("f5a6e97a-173f-4c46-8c15-296b5b3ec5aa")
_PROJECT_ID = uuid.UUID("dfe8b01b-5e08-4aad-bbb6-50080942f742")
_BATCH_ID = uuid.UUID("c8a4aa71-2e47-4f42-a59b-f78f647198ac")


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )


def _route_boundary(monkeypatch, *, task_ids=(), failure=None):
    events = []
    job = None
    db = SimpleNamespace()

    async def commit():
        events.append(
            ("commit", job.status if job else None, job.celery_task_id if job else None)
        )

    async def execute(statement):
        events.append(("select", statement))
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: task_ids))

    db.commit = commit
    db.execute = execute

    async def create_job(_db, *, kind, user_id, project_id, payload):
        nonlocal job
        job = SimpleNamespace(
            id=_JOB_ID,
            kind=kind,
            user_id=user_id,
            project_id=project_id,
            payload=payload,
            status="pending",
            celery_task_id=None,
        )
        events.append(("create", job))
        return job

    async def audit(_db, *, action, **_kwargs):
        events.append(("audit", action))

    async def dispatch(**call):
        events.append(("dispatch", job.status, job.celery_task_id, call))
        if failure:
            raise failure
        return call["execution_id"]

    async def mark_failed(_db, job_id, *, error):
        assert job.id == job_id
        events.append(("failed", error))
        job.status = "failed"
        job.error_message = error

    monkeypatch.setattr(async_job_service, "create_job", create_job)
    monkeypatch.setattr(async_job_service, "mark_failed", mark_failed)
    monkeypatch.setattr(AuditService, "log", staticmethod(audit))
    monkeypatch.setattr(jobs_queue, "dispatch_job", dispatch)
    monkeypatch.setattr(data_manager, "dispatch_job", dispatch)
    return db, events


def _identity_project():
    actor = SimpleNamespace(
        id=uuid.UUID("10f468e1-944a-4d33-b958-2447f335a1dd"),
        name="Export Owner",
        email="export-owner@example.test",
        role="super_admin",
        is_active=True,
    )
    project = SimpleNamespace(
        id=_PROJECT_ID, owner_id=actor.id, data_type="image", display_id="P-EXPORT"
    )
    return actor, project


def _assert_wire(call, expected_kwargs):
    assert set(call) == {"task_name", "execution_id", "args", "kwargs"}
    assert call["task_name"] == "app.workers.export.run_export"
    assert str(uuid.UUID(call["execution_id"])) == call["execution_id"]
    assert call["args"] == []
    assert call["kwargs"] == expected_kwargs


async def _export_project_or_batch(route, monkeypatch, db, actor, project, **options):
    async def no_video_scope(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.exporting.video_scope.normalize_video_export_scope",
        no_video_scope,
    )
    route_options = {
        "include_attributes": True,
        "video_frame_mode": "keyframes",
        "axis_frame": "iso",
        "video_overlap_policy": "error",
        "mots_frame_base": 0,
        **options,
    }
    common = {
        "request": _request(
            f"/api/v1/{'projects' if route == 'project' else 'batches'}/export"
        ),
        "body": None,
        "targets": ["aap_json"],
        "project": project,
        "actor": actor,
        "db": db,
        "_access": None,
        **route_options,
    }
    if route == "project":
        common["indexed_overlap_policy"] = options.get(
            "indexed_overlap_policy", "error"
        )
        return await projects.export_project(**common)

    common.pop("indexed_overlap_policy", None)

    class BatchService:
        def __init__(self, _db):
            pass

        async def get(self, batch_id):
            assert batch_id == _BATCH_ID
            return SimpleNamespace(
                id=_BATCH_ID, project_id=project.id, display_id="B-EXPORT"
            )

    monkeypatch.setattr(batches, "BatchService", BatchService)
    return await batches.export_batch(
        project_id=project.id, batch_id=_BATCH_ID, **common
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["project", "batch"])
async def test_project_and_batch_export_wire_and_commit_order(route, monkeypatch):
    actor, project = _identity_project()
    db, events = _route_boundary(monkeypatch)
    options = {
        "include_attributes": False,
        "video_frame_mode": "all_frames",
        "axis_frame": "source",
        "video_overlap_policy": "larger_area",
        "mots_frame_base": 1,
    }
    await _export_project_or_batch(
        route,
        monkeypatch,
        db,
        actor,
        project,
        **options,
        **({"indexed_overlap_policy": "z_order"} if route == "project" else {}),
    )

    assert [event[0] for event in events] == ["create", "audit", "commit", "dispatch"]
    assert events[2] == ("commit", "pending", None)
    assert events[3][1:3] == ("pending", None)
    assert db.job.celery_task_id is None
    _assert_wire(
        events[3][3],
        {
            "project_id": str(project.id),
            "batch_id": None if route == "project" else str(_BATCH_ID),
            "targets": ["aap_json"],
            "opts": {
                **options,
                **({"indexed_overlap_policy": "z_order"} if route == "project" else {}),
            },
            "async_job_id": str(_JOB_ID),
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["project", "batch"])
async def test_project_and_batch_dispatch_failure_preserves_committed_pending_job(
    route, monkeypatch
):
    actor, project = _identity_project()
    failure = RuntimeError("queue unavailable")
    db, events = _route_boundary(monkeypatch, failure=failure)

    with pytest.raises(RuntimeError) as raised:
        await _export_project_or_batch(route, monkeypatch, db, actor, project)

    assert raised.value is failure
    assert [event[0] for event in events] == ["create", "audit", "commit", "dispatch"]
    assert events[2] == ("commit", "pending", None)
    assert db.job.status == "pending"
    assert db.job.celery_task_id is None


def _query_ids(statement):
    values = statement.compile().params.values()
    return {
        value
        for group in values
        for value in (group if isinstance(group, (tuple, list, set)) else (group,))
        if isinstance(value, uuid.UUID)
    }


def _export_data_manager(db, actor, project, payload):
    return data_manager.export_data_manager_tasks(
        project_id=project.id,
        payload=payload,
        request=_request("/api/v1/projects/data-manager/tasks/export"),
        project=project,
        db=db,
        actor=actor,
        access=SimpleNamespace(project_role=None),
        idempotency_key=None,
    )


@pytest.mark.asyncio
async def test_data_manager_scope_wire_and_bind_happen_after_durable_publish(
    monkeypatch,
):
    actor, project = _identity_project()
    selected = [
        uuid.UUID("00000000-0000-0000-0000-000000000001"),
        uuid.UUID("00000000-0000-0000-0000-000000000002"),
    ]
    db, events = _route_boundary(monkeypatch, task_ids=selected)
    payload = DataManagerTaskExportRequest(
        task_ids=list(reversed(selected)),
        targets=["aap_json"],
        include_attributes=False,
        video_frame_mode="all_frames",
        axis_frame="source",
        indexed_overlap_policy="z_order",
        video_overlap_policy="larger_area",
        mots_frame_base=1,
    )

    await _export_data_manager(db, actor, project, payload)

    assert db.job.payload["scope"] == {"task_ids": [str(item) for item in selected]}
    assert db.job.payload["task_ids"] == [str(item) for item in selected]
    query = next(event[1] for event in events if event[0] == "select")
    assert {project.id, *selected} <= _query_ids(query)
    assert [event[0] for event in events] == [
        "select",
        "create",
        "commit",
        "audit",
        "dispatch",
        "commit",
    ]
    assert events[2] == ("commit", "pending", None)
    assert events[4][1:3] == ("pending", None)
    _assert_wire(
        events[4][3],
        {
            "project_id": str(project.id),
            "batch_id": None,
            "task_ids": [str(item) for item in selected],
            "targets": ["aap_json"],
            "opts": {
                "include_attributes": False,
                "video_frame_mode": "all_frames",
                "axis_frame": "source",
                "indexed_overlap_policy": "z_order",
                "video_overlap_policy": "larger_area",
                "mots_frame_base": 1,
            },
            "async_job_id": str(_JOB_ID),
        },
    )
    assert events[5] == ("commit", "pending", db.job.celery_task_id)
    assert db.job.celery_task_id == events[4][3]["execution_id"]


@pytest.mark.asyncio
async def test_data_manager_dispatch_failure_marks_failed_and_propagates(monkeypatch):
    actor, project = _identity_project()
    selected = [uuid.UUID("00000000-0000-0000-0000-000000000001")]
    failure = RuntimeError("queue unavailable")
    db, events = _route_boundary(monkeypatch, task_ids=selected, failure=failure)
    payload = DataManagerTaskExportRequest(task_ids=selected, targets=["aap_json"])

    with pytest.raises(RuntimeError) as raised:
        await _export_data_manager(db, actor, project, payload)

    assert raised.value is failure
    assert [event[0] for event in events] == [
        "select",
        "create",
        "commit",
        "audit",
        "dispatch",
        "failed",
        "commit",
    ]
    assert events[2] == ("commit", "pending", None)
    assert events[4][1:3] == ("pending", None)
    assert events[5][1] == "dispatch failed: RuntimeError: queue unavailable"
    assert db.job.status == "failed"
    assert db.job.celery_task_id is None
    assert events[6] == ("commit", "failed", None)
