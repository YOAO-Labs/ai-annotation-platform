from __future__ import annotations

from contextlib import asynccontextmanager, nullcontext
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException

from aap_workspace_contract.member_values import PROJECT_MEMBER_COLUMNS
from aap_workspace_contract.project_query_contract import PROJECT_ROW_COLUMNS
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.services import project_access
from app.services.identity_contracts import IdentityActorStatus

_STEP_SCHEMA = "aap-workspace-project-access-step/v1"
_QUERY_SCHEMA = "aap-workspace-project-query/v1"


def _project_row(project_id, owner_id):
    row = {column: None for column in PROJECT_ROW_COLUMNS}
    row.update(id=str(project_id), owner_id=str(owner_id) if owner_id else None)
    row["name"] = "Project"
    return json.dumps(row)


def _access_step(actor_id, project_id, phase, facts):
    return {
        "schema": _STEP_SCHEMA,
        "actor_id": str(actor_id),
        "project_id": str(project_id),
        "phase": phase,
        "facts": facts,
    }


def _query_step(request, action, facts):
    return {
        "schema": _QUERY_SCHEMA,
        "request": request.model_dump(mode="json", by_alias=True),
        "action": action,
        "parameters": None,
        "facts": facts,
    }


def _member_rows(actor_id, project_id, *, role="annotator", version=3):
    member_id = uuid4()
    row = {column: None for column in PROJECT_MEMBER_COLUMNS}
    row.update(
        id=str(member_id),
        project_id=str(project_id),
        user_id=str(actor_id),
        role=role,
        version=version,
    )
    return json.dumps([row]), member_id


class _Session:
    autoflush = True

    def __init__(self, native_responses, events):
        self.no_autoflush = nullcontext()
        self.native_responses = iter(native_responses)
        self.events = events
        self.merges = []

    async def flush(self):
        self.events.append("flush")

    async def scalar(self, _statement, parameters):
        self.events.append(("native", parameters["phase"]))
        return next(self.native_responses)

    async def merge(self, instance, *, load):
        self.merges.append((instance, load))
        return instance


class _QueryData:
    def __init__(self, events, project_id, owner_id, member_payload=None):
        self.events = events
        self.project_id = project_id
        self.owner_id = owner_id
        self.member_payload = member_payload
        self.request = None

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def step(self, request, action, parameters=None, **_kwargs):
        self.events.append(("query", action, request.admitted_actor.role))
        self.request = request
        if action == "get_row":
            facts = {"row_json": _project_row(self.project_id, self.owner_id)}
        else:
            assert action == "access_member_rows"
            facts = {"rows_json": self.member_payload}
        return _query_step(request, action, facts)


def _patch_actor_status(monkeypatch, actor, events):
    async def read_actor_status(self, actor_id):
        events.append("actor_status")
        assert actor_id == actor.id
        return actor

    monkeypatch.setattr(
        project_access.IdentityAccountLockData,
        "read_actor_status",
        read_actor_status,
    )


def _patch_query_data(monkeypatch, events, project_id, owner_id, member_payload=None):
    monkeypatch.setattr(
        project_access,
        "WorkspaceProjectQueryData",
        lambda _db: _QueryData(events, project_id, owner_id, member_payload),
    )


@pytest.mark.asyncio
async def test_direct_unlocked_access_reads_current_actor_project_then_member(
    monkeypatch,
):
    actor_id, project_id, owner_id = uuid4(), uuid4(), uuid4()
    member_payload, member_id = _member_rows(actor_id, project_id)
    events = []
    actor = IdentityActorStatus(actor_id, "employee", True)
    _patch_actor_status(monkeypatch, actor, events)
    _patch_query_data(monkeypatch, events, project_id, owner_id, member_payload)
    db = _Session([], events)
    stale_project = Project(id=project_id, owner_id=actor_id, name="Stale")

    access = await project_access.resolve_project_access(
        db, actor_id=actor_id, project=stale_project
    )

    assert access.membership_id == member_id
    assert access.project_role == "annotator"
    assert events == [
        "actor_status",
        ("query", "get_row", "employee"),
        ("query", "access_member_rows", "employee"),
    ]
    assert [type(row) for row, _ in db.merges] == [Project, ProjectMember]
    assert all(load is False for _, load in db.merges)


@pytest.mark.asyncio
async def test_direct_unlocked_manager_access_skips_member_read(monkeypatch):
    actor_id, project_id = uuid4(), uuid4()
    events = []
    actor = IdentityActorStatus(actor_id, "project_admin", True)
    _patch_actor_status(monkeypatch, actor, events)
    _patch_query_data(monkeypatch, events, project_id, actor_id)
    db = _Session([], events)

    access = await project_access.resolve_project_access(
        db,
        actor_id=actor_id,
        project=Project(id=project_id, owner_id=uuid4(), name="Stale"),
    )

    assert access.is_manager is True and access.is_owner is True
    assert events == ["actor_status", ("query", "get_row", "project_admin")]
    assert [type(row) for row, _ in db.merges] == [Project]


@pytest.mark.asyncio
async def test_by_id_missing_project_precedes_identity_status(monkeypatch):
    actor_id, project_id = uuid4(), uuid4()
    events = []
    db = _Session(
        [_access_step(actor_id, project_id, "read_project_row", {"row_json": None})],
        events,
    )

    with pytest.raises(HTTPException) as error:
        await project_access.resolve_project_access_by_id(
            db, actor_id=actor_id, project_id=project_id
        )

    assert error.value.status_code == 404
    assert events == ["flush", ("native", "read_project_row")]


@pytest.mark.asyncio
async def test_by_id_reads_initial_row_before_actor_and_current_query(monkeypatch):
    actor_id, project_id, owner_id = uuid4(), uuid4(), uuid4()
    member_payload, member_id = _member_rows(actor_id, project_id)
    events = []
    actor = IdentityActorStatus(actor_id, "employee", True)
    _patch_actor_status(monkeypatch, actor, events)
    _patch_query_data(monkeypatch, events, project_id, owner_id, member_payload)
    db = _Session(
        [
            _access_step(
                actor_id,
                project_id,
                "read_project_row",
                {"row_json": _project_row(project_id, owner_id)},
            )
        ],
        events,
    )

    project, access = await project_access.resolve_project_access_by_id(
        db, actor_id=actor_id, project_id=project_id
    )

    assert project.id == project_id and project.owner_id == owner_id
    assert access.membership_id == member_id
    assert events == [
        "flush",
        ("native", "read_project_row"),
        "actor_status",
        ("query", "get_row", "employee"),
        ("query", "access_member_rows", "employee"),
    ]
    assert [type(row) for row, _ in db.merges] == [Project, Project, ProjectMember]
