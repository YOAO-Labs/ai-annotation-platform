from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import uuid

import pytest

from app.db.models.project import Project
from app.services import project_access


class _SyncSession:
    def __init__(self):
        self.identity_map = {}
        self.removed = []

    def _remove_newly_deleted(self, states):
        self.removed.extend(states)


class _Session:
    def __init__(self, *, autoflush):
        self.autoflush = autoflush
        self.sync_session = _SyncSession()
        self.events = []

    @property
    def no_autoflush(self):
        @contextmanager
        def _disable():
            previous = self.autoflush
            self.autoflush = False
            try:
                yield self
            finally:
                self.autoflush = previous

        return _disable()

    async def flush(self):
        self.events.append("flush")


def _identity_key(project_id):
    return Project.__mapper__.identity_key_from_primary_key((project_id,))


def _bind_project_reads(monkeypatch, session, actor_id, project_id, values):
    pending = iter(values)

    async def read(db, *, actor_id, project_id):
        assert db is session
        assert actor_id == actor_id_expected
        assert project_id == project_id_expected
        session.events.append("project_read")
        return next(pending)

    actor_id_expected = actor_id
    project_id_expected = project_id
    monkeypatch.setattr(project_access, "_workspace_project_values", read)


def _bind_merge(monkeypatch, session, result):
    async def merge(db, model, values):
        assert db is session
        assert model is Project
        session.events.append("merge")
        return result

    monkeypatch.setattr(project_access, "_merge_project_snapshot", merge)


def _bind_state(monkeypatch, project, *, expired, modified, unloaded):
    original_inspect = project_access.inspect
    fake_state = SimpleNamespace(
        expired=expired,
        modified=modified,
        unloaded=set(unloaded),
    )
    monkeypatch.setattr(
        project_access,
        "inspect",
        lambda instance: (
            fake_state if instance is project else original_inspect(instance)
        ),
    )
    return fake_state


@pytest.mark.asyncio
async def test_default_get_returns_partial_identity_hit_without_sql_or_flush(
    monkeypatch,
):
    actor_id, project_id = uuid.uuid4(), uuid.uuid4()
    project = Project(id=project_id, name="cached")
    session = _Session(autoflush=True)
    session.sync_session.identity_map[_identity_key(project_id)] = project
    assert project_access.inspect(project).unloaded

    async def no_read(*_args, **_kwargs):
        raise AssertionError("identity-map get must not read Workspace")

    monkeypatch.setattr(project_access, "_workspace_project_values", no_read)

    result = await project_access.get_workspace_project(
        session,
        actor_id=actor_id,
        project_id=project_id,
    )

    assert result is project
    assert session.events == []


@pytest.mark.asyncio
async def test_populate_existing_always_reads_and_refreshes_identity(monkeypatch):
    actor_id, project_id = uuid.uuid4(), uuid.uuid4()
    project = Project(id=project_id, name="cached")
    session = _Session(autoflush=False)
    session.sync_session.identity_map[_identity_key(project_id)] = project
    values = {column.key: None for column in Project.__table__.columns}
    values.update(id=project_id, name="database")
    _bind_project_reads(monkeypatch, session, actor_id, project_id, [values])
    _bind_merge(monkeypatch, session, project)

    result = await project_access.get_workspace_project(
        session,
        actor_id=actor_id,
        project_id=project_id,
        populate_existing=True,
    )

    assert result is project
    assert session.events == ["project_read", "merge"]


@pytest.mark.asyncio
async def test_expired_dirty_autoflush_reads_before_and_after_flush(monkeypatch):
    actor_id, project_id = uuid.uuid4(), uuid.uuid4()
    project = Project(id=project_id, name="pending local")
    session = _Session(autoflush=True)
    session.sync_session.identity_map[_identity_key(project_id)] = project
    state = _bind_state(
        monkeypatch,
        project,
        expired=True,
        modified=True,
        unloaded={"id", "status", "tool_bindings"},
    )
    before = {"id": project_id, "status": "completed", "tool_bindings": {}}
    after = {"id": project_id, "status": "completed", "tool_bindings": {}}
    _bind_project_reads(monkeypatch, session, actor_id, project_id, [before, after])

    result = await project_access.get_workspace_project(
        session,
        actor_id=actor_id,
        project_id=project_id,
    )

    assert result is project
    assert project.name == "pending local"
    assert session.events == ["project_read", "flush", "project_read"]
    assert session.autoflush is True
    assert state.unloaded == {"id", "status", "tool_bindings"}


@pytest.mark.asyncio
async def test_expired_deleted_default_get_removes_state_and_retries(monkeypatch):
    actor_id, project_id = uuid.uuid4(), uuid.uuid4()
    project = Project(id=project_id, name="cached")
    session = _Session(autoflush=False)
    session.sync_session.identity_map[_identity_key(project_id)] = project
    state = _bind_state(
        monkeypatch,
        project,
        expired=True,
        modified=False,
        unloaded={"id", "name"},
    )
    _bind_project_reads(monkeypatch, session, actor_id, project_id, [None, None])

    result = await project_access.get_workspace_project(
        session,
        actor_id=actor_id,
        project_id=project_id,
    )

    assert result is None
    assert session.events == ["project_read", "project_read"]
    assert session.sync_session.removed == [state]
