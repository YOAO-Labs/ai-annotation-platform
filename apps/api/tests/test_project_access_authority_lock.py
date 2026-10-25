from __future__ import annotations

import json
from contextlib import nullcontext
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from aap_workspace_contract.member_values import PROJECT_MEMBER_COLUMNS
from aap_workspace_contract.project_query_contract import PROJECT_ROW_COLUMNS
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.services import project_access
from app.services.identity_contracts import IdentityActorFacts

_STEP_SCHEMA = "aap-workspace-project-access-step/v1"


def _project_row(project_id, owner_id):
    row = {column: None for column in PROJECT_ROW_COLUMNS}
    row.update(id=str(project_id), owner_id=str(owner_id) if owner_id else None)
    row["name"] = "Project"
    return json.dumps(row)


def _step(actor_id, project_id, phase, facts):
    return {
        "schema": _STEP_SCHEMA,
        "actor_id": str(actor_id),
        "project_id": str(project_id),
        "phase": phase,
        "facts": facts,
    }


def _member_rows(actor_id, project_id, *, role="annotator", version=1):
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


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, responses):
        self.no_autoflush = nullcontext()
        self.responses = iter(responses)
        self.steps = []
        self.merges = []

    async def execute(self, _statement, parameters):
        self.steps.append(parameters)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return _Result(response)

    async def merge(self, instance, *, load):
        self.merges.append((instance, load))
        return instance


def _actor(actor_id, role="employee"):
    return IdentityActorFacts(
        id=actor_id,
        name="Actor",
        email="actor@example.test",
        role=role,
        is_active=True,
        weekly_target_default=None,
    )


def _bind_actor_lock(monkeypatch, actor, events):
    async def lock_accounts(self, account_ids, *, mode, nowait):
        events.append(("actor", list(account_ids), mode, nowait))
        return {actor.id: actor}

    monkeypatch.setattr(
        project_access.IdentityAccountLockData, "lock_accounts", lock_accounts
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("uppercase_scope", [False, True])
async def test_locked_project_snapshot_hydrates_existing_interface_without_select(
    monkeypatch,
    uppercase_scope,
):
    actor_id, project_id, owner_id = uuid4(), uuid4(), uuid4()
    actor = _actor(actor_id)
    events = []
    _bind_actor_lock(monkeypatch, actor, events)
    reply = _step(
        actor_id,
        project_id,
        "lock_project_key_share",
        {"row_json": _project_row(project_id, owner_id)},
    )
    if uppercase_scope:
        reply["actor_id"] = reply["actor_id"].upper()
        reply["project_id"] = reply["project_id"].upper()
    session = _Session([reply])
    execute = session.execute

    async def record_execute(statement, parameters):
        events.append(
            (parameters["phase"], parameters["actor_id"], parameters["project_id"])
        )
        return await execute(statement, parameters)

    session.execute = record_execute

    locked_actor, project = await project_access.lock_current_project_rows(
        session, actor_id=actor_id, project_id=project_id
    )

    assert locked_actor is actor
    assert isinstance(project, Project)
    assert (project.id, project.owner_id, project.name) == (
        project_id,
        owner_id,
        "Project",
    )
    assert events == [
        ("actor", [actor_id], "share", True),
        ("lock_project_key_share", actor_id, project_id),
    ]
    assert len(session.merges) == 1 and session.merges[0][1] is False


@pytest.mark.asyncio
async def test_locked_member_snapshot_preserves_project_tuple_and_phase_order(
    monkeypatch,
):
    actor_id, project_id, owner_id = uuid4(), uuid4(), uuid4()
    actor = _actor(actor_id)
    member_json, member_id = _member_rows(
        actor_id, project_id, role="reviewer", version=4
    )
    events = []
    _bind_actor_lock(monkeypatch, actor, events)
    session = _Session(
        [
            _step(
                actor_id,
                project_id,
                "lock_project_key_share",
                {"row_json": _project_row(project_id, owner_id)},
            ),
            _step(
                actor_id,
                project_id,
                "lock_member_share",
                {"rows_json": member_json},
            ),
        ]
    )
    execute = session.execute

    async def record_execute(statement, parameters):
        events.append(parameters["phase"])
        return await execute(statement, parameters)

    session.execute = record_execute

    locked_actor, project, access = await project_access.lock_current_project_access(
        session, actor_id=actor_id, project_id=project_id
    )

    assert locked_actor is actor
    assert isinstance(project, Project)
    assert project.owner_id == owner_id
    assert (access.membership_id, access.project_role, access.membership_version) == (
        member_id,
        "reviewer",
        4,
    )
    assert events == [
        ("actor", [actor_id], "share", True),
        "lock_project_key_share",
        "lock_member_share",
    ]
    assert [type(row) for row, _ in session.merges] == [Project, ProjectMember]
    assert all(load is False for _, load in session.merges)


@pytest.mark.asyncio
async def test_locked_manager_snapshot_skips_membership_phase(monkeypatch):
    actor_id, project_id = uuid4(), uuid4()
    actor = _actor(actor_id, role="project_admin")
    _bind_actor_lock(monkeypatch, actor, [])
    session = _Session(
        [
            _step(
                actor_id,
                project_id,
                "lock_project_key_share",
                {"row_json": _project_row(project_id, actor_id)},
            )
        ]
    )

    locked_actor, project, access = await project_access.lock_current_project_access(
        session, actor_id=actor_id, project_id=project_id
    )

    assert locked_actor is actor
    assert project.id == project_id
    assert access.is_manager is True and access.is_owner is True
    assert [parameters["phase"] for parameters in session.steps] == [
        "lock_project_key_share"
    ]
    assert [type(row) for row, _ in session.merges] == [Project]


@pytest.mark.asyncio
async def test_locked_project_snapshot_rejects_bad_native_scope_without_select(
    monkeypatch,
):
    actor_id, project_id = uuid4(), uuid4()
    actor = _actor(actor_id)
    _bind_actor_lock(monkeypatch, actor, [])
    reply = _step(
        actor_id,
        project_id,
        "lock_project_key_share",
        {"row_json": _project_row(project_id, uuid4())},
    )
    session = _Session([{**reply, "extra": True}])

    with pytest.raises(ValueError, match="Invalid Workspace Project access step"):
        await project_access.lock_current_project_rows(
            session, actor_id=actor_id, project_id=project_id
        )
    assert len(session.steps) == 1
    assert session.merges == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "sqlstate", "reason"),
    [
        ("lock_project_key_share", "55P03", "project_busy"),
        ("lock_project_key_share", "40001", "project_busy"),
        ("lock_project_key_share", "40P01", "project_busy"),
        ("lock_member_share", "55P03", "membership_busy"),
        ("lock_member_share", "40001", "membership_busy"),
        ("lock_member_share", "40P01", "membership_busy"),
    ],
)
async def test_locked_workspace_phase_contention_maps_to_existing_conflicts(
    monkeypatch, phase, sqlstate, reason
):
    actor_id, project_id, owner_id = uuid4(), uuid4(), uuid4()
    actor = _actor(actor_id)
    _bind_actor_lock(monkeypatch, actor, [])
    driver_error = RuntimeError("private database details")
    driver_error.sqlstate = sqlstate
    busy_error = DBAPIError("Workspace Project access step", {}, driver_error, False)
    project_step = _step(
        actor_id,
        project_id,
        "lock_project_key_share",
        {"row_json": _project_row(project_id, owner_id)},
    )
    responses = (
        [busy_error]
        if phase == "lock_project_key_share"
        else [
            project_step,
            busy_error,
        ]
    )
    session = _Session(responses)

    with pytest.raises(HTTPException) as rejected:
        await project_access.lock_current_project_access(
            session, actor_id=actor_id, project_id=project_id
        )

    assert (rejected.value.status_code, rejected.value.detail) == (
        409,
        {"reason": reason},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlstate", ["55P03", "40001", "40P01"])
async def test_extracted_actor_lock_keeps_account_contention_mapping(
    monkeypatch, sqlstate
):
    actor_id, project_id = uuid4(), uuid4()
    driver_error = RuntimeError("private identity details")
    driver_error.sqlstate = sqlstate
    busy_error = DBAPIError("Identity account lock", {}, driver_error, False)

    async def lock_accounts(self, *_args, **_kwargs):
        raise busy_error

    monkeypatch.setattr(
        project_access.IdentityAccountLockData, "lock_accounts", lock_accounts
    )
    session = _Session([])

    with pytest.raises(HTTPException) as rejected:
        await project_access.lock_current_project_rows(
            session, actor_id=actor_id, project_id=project_id
        )

    assert (rejected.value.status_code, rejected.value.detail) == (
        409,
        {"reason": "account_busy"},
    )
    assert session.steps == []
    assert session.merges == []
