from __future__ import annotations

from contextlib import nullcontext
import json
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.exc import DBAPIError

from aap_workspace_contract.project_query_contract import PROJECT_ROW_COLUMNS
from app.db.models.project import Project
from app.services import project_workspace
from app.application import point_cloud_quality_promotion
from app.application import project_workspace as project_workspace_application

_LOCK_SCHEMA = "aap-workspace-project-lock/v1"


@pytest.mark.asyncio
@pytest.mark.parametrize("autoflush", [False, True])
async def test_project_set_lock_preserves_flush_and_exact_borrowed_call(autoflush):
    ids = sorted([uuid.uuid4(), uuid.uuid4()])
    events = []

    async def flush():
        events.append("flush")

    async def scalar(statement, parameters):
        assert "aap_workspace_v1.lock_project_references_for_update" in str(statement)
        assert parameters == {"project_ids": ids}
        events.append("scalar")
        return ids

    session = SimpleNamespace(autoflush=autoflush, flush=flush, scalar=scalar)
    assert (
        await project_workspace.lock_project_references_for_update(session, ids) == ids
    )
    assert events == (["flush", "scalar"] if autoflush else ["scalar"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply", [None, "ids", ["uuid"], "duplicate", "unsorted", "foreign"]
)
async def test_project_set_lock_rejects_malformed_reply_without_transaction_control(
    reply,
):
    ids = sorted([uuid.uuid4(), uuid.uuid4()])
    replies = {
        "duplicate": [ids[0], ids[0]],
        "unsorted": list(reversed(ids)),
        "foreign": [uuid.uuid4()],
    }
    if isinstance(reply, str) and reply in replies:
        reply = replies[reply]

    async def scalar(_statement, _parameters):
        return reply

    session = SimpleNamespace(autoflush=False, scalar=scalar)
    with pytest.raises(HTTPException) as rejected:
        await project_workspace.lock_project_references_for_update(session, ids)
    assert rejected.value.status_code == 503


def _project_row(
    project_id: uuid.UUID,
    *,
    owner_id: uuid.UUID | None = None,
    name: str = "Project",
    updated_at: str | None = None,
) -> str:
    row = {column: None for column in PROJECT_ROW_COLUMNS}
    row.update(
        id=str(project_id),
        owner_id=str(owner_id) if owner_id is not None else None,
        name=name,
        updated_at=updated_at,
    )
    return json.dumps(row)


def _lock_reply(project_id, nowait, row_json):
    return {
        "schema": _LOCK_SCHEMA,
        "project_id": str(project_id),
        "nowait": nowait,
        "row_json": row_json,
    }


class _Session:
    def __init__(self, reply, *, autoflush=True, project=None, events=None):
        self.autoflush = autoflush
        self.no_autoflush = nullcontext()
        self.sync_session = SimpleNamespace(identity_map={})
        self.events = [] if events is None else events
        self.reply = reply
        self.project = project

    async def flush(self):
        self.events.append("flush")

    async def scalar(self, _statement, parameters):
        self.events.append(("lock_project_row", parameters["nowait"]))
        return self.reply

    async def merge(self, instance, *, load):
        self.events.append(("merge", type(instance), load))
        return self.project or instance


@pytest.mark.asyncio
async def test_lock_project_row_flushes_and_merges_complete_snapshot(monkeypatch):
    project_id, owner_id = uuid.uuid4(), uuid.uuid4()
    events = []
    values_seen = []
    merged = Project(id=project_id, name="Fresh")
    session = _Session(
        _lock_reply(project_id, True, _project_row(project_id, owner_id=owner_id)),
        events=events,
    )

    async def merge(db, model, values):
        assert db is session
        assert model is Project
        values_seen.append(values)
        return merged

    monkeypatch.setattr(project_workspace, "merge_project_snapshot", merge)

    project = await project_workspace.lock_project_row(
        session,
        project_id,
        nowait=True,
        populate_existing=True,
    )

    assert project is merged
    assert events == ["flush", ("lock_project_row", True)]
    assert values_seen[0]["owner_id"] == owner_id
    assert set(values_seen[0]) == {column.key for column in Project.__table__.columns}


@pytest.mark.asyncio
async def test_nonrefresh_lock_preserves_loaded_dirty_fields_and_fills_unloaded():
    project_id, owner_id = uuid.uuid4(), uuid.uuid4()
    project = Project(id=project_id, name="cached")
    project.name = "local dirty value"
    identity_key = Project.__mapper__.identity_key_from_primary_key((project_id,))
    reply = _lock_reply(
        project_id,
        False,
        _project_row(project_id, owner_id=owner_id, name="database value"),
    )
    session = _Session(reply, autoflush=False)
    session.sync_session.identity_map[identity_key] = project

    result = await project_workspace.lock_project_row(
        session,
        project_id,
        nowait=False,
        populate_existing=False,
    )

    assert result is project
    assert project.name == "local dirty value"
    assert inspect(project).attrs.name.history.has_changes()
    assert project.owner_id == owner_id
    assert "owner_id" not in inspect(project).unloaded
    assert session.events == [("lock_project_row", False)]


@pytest.mark.asyncio
async def test_nonrefresh_lock_decodes_full_row_even_when_project_is_cached():
    project_id = uuid.uuid4()
    project = Project(id=project_id, name="cached")
    identity_key = Project.__mapper__.identity_key_from_primary_key((project_id,))
    session = _Session(
        _lock_reply(
            project_id,
            False,
            _project_row(
                project_id,
                updated_at="10000-01-01T00:00:00+00:00",
            ),
        ),
        autoflush=False,
    )
    session.sync_session.identity_map[identity_key] = project

    with pytest.raises(OverflowError):
        await project_workspace.lock_project_row(
            session,
            project_id,
            nowait=False,
            populate_existing=False,
        )

    assert project.name == "cached"


@pytest.mark.asyncio
async def test_invalid_lock_reply_fails_as_service_unavailable():
    project_id = uuid.uuid4()
    session = _Session({"schema": _LOCK_SCHEMA, "project_id": str(project_id)})

    with pytest.raises(HTTPException) as unavailable:
        await project_workspace.lock_project_row(
            session,
            project_id,
            nowait=True,
            populate_existing=True,
        )

    assert unavailable.value.status_code == 503


@pytest.mark.asyncio
async def test_lock_workspace_project_preserves_nowait_and_busy_mapping(monkeypatch):
    project_id = uuid.uuid4()
    events = []
    project = Project(id=project_id, name="Fresh")

    async def lock(db, requested_id, *, nowait, populate_existing):
        assert db is session
        assert requested_id == project_id
        events.append((nowait, populate_existing))
        return project

    monkeypatch.setattr(project_workspace_application, "lock_project_row", lock)
    session = SimpleNamespace(no_autoflush=nullcontext())

    assert (
        await project_workspace_application.lock_workspace_project(session, project_id)
        is project
    )
    assert events == [(True, True)]

    class _Busy:
        sqlstate = "55P03"

    async def busy(*_args, **_kwargs):
        raise DBAPIError("lock project", {}, _Busy())

    monkeypatch.setattr(project_workspace_application, "lock_project_row", busy)
    with pytest.raises(HTTPException) as rejected:
        await project_workspace_application.lock_workspace_project(session, project_id)

    assert rejected.value.status_code == 409
    assert rejected.value.detail == {"reason": "project_busy"}


@pytest.mark.asyncio
async def test_promotion_keeps_blocking_lock_evaluation_order_and_flush(monkeypatch):
    project_id, evaluation_id, actor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = []
    locked_project = Project(
        id=project_id,
        point_cloud_quality_config={"config_revision": 1},
    )
    evaluation = SimpleNamespace(project_id=project_id)
    candidate_config = {"config_revision": 2}
    candidate = SimpleNamespace(
        config_revision=2, model_dump=lambda *, mode: candidate_config
    )

    async def lock(db, requested_id, *, nowait, populate_existing):
        assert db is session
        events.append(("lock", requested_id, nowait, populate_existing))
        return locked_project

    async def get(model, requested_id):
        events.append(("get", model, requested_id))
        return evaluation

    async def set_config(db, *, project, config):
        assert db is session and project is locked_project
        events.append(("set_config", config))

    def prepare(_evaluation, *, current_config):
        events.append(("prepare", current_config))
        return candidate

    def record(_evaluation, *, actor_id, config_revision):
        events.append(("record", actor_id, config_revision))

    async def flush():
        events.append("flush")

    session = SimpleNamespace(get=get, flush=flush)
    monkeypatch.setattr(point_cloud_quality_promotion, "lock_project_row", lock)
    monkeypatch.setattr(
        point_cloud_quality_promotion,
        "set_point_cloud_quality_config",
        set_config,
    )
    monkeypatch.setattr(
        point_cloud_quality_promotion, "prepare_evaluation_promotion", prepare
    )
    monkeypatch.setattr(
        point_cloud_quality_promotion, "record_evaluation_promotion", record
    )

    result = await point_cloud_quality_promotion.promote_point_cloud_quality_evaluation(
        session,
        project_id=project_id,
        evaluation_id=evaluation_id,
        actor_id=actor_id,
    )

    assert result == (evaluation, locked_project)
    assert events == [
        ("lock", project_id, False, False),
        (
            "get",
            point_cloud_quality_promotion.PointCloudQualityEvaluation,
            evaluation_id,
        ),
        ("prepare", {"config_revision": 1}),
        ("set_config", candidate_config),
        ("record", actor_id, 2),
        "flush",
    ]


@pytest.mark.asyncio
async def test_promotion_checks_evaluation_after_missing_project_lock(monkeypatch):
    project_id, evaluation_id, actor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = []

    async def lock(_db, requested_id, *, nowait, populate_existing):
        events.append(("lock", requested_id, nowait, populate_existing))
        return None

    async def get(model, requested_id):
        events.append(("get", model, requested_id))
        return None

    session = SimpleNamespace(get=get)
    monkeypatch.setattr(point_cloud_quality_promotion, "lock_project_row", lock)

    with pytest.raises(point_cloud_quality_promotion.PointCloudQualityError) as error:
        await point_cloud_quality_promotion.promote_point_cloud_quality_evaluation(
            session,
            project_id=project_id,
            evaluation_id=evaluation_id,
            actor_id=actor_id,
        )

    assert error.value.status_code == 404
    assert error.value.detail == {"reason": "point_cloud_quality_evaluation_not_found"}
    assert events == [
        ("lock", project_id, False, False),
        (
            "get",
            point_cloud_quality_promotion.PointCloudQualityEvaluation,
            evaluation_id,
        ),
    ]
