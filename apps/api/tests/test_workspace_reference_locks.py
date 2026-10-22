from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from app.services import project_write_guard


class _Session:
    def __init__(self, *, autoflush=True, reference=None):
        self.autoflush = autoflush
        self.reference = reference
        self.events = []

    async def flush(self):
        self.events.append(("flush",))

    async def scalar(self, statement, parameters):
        self.events.append(("project", str(statement), parameters))
        if isinstance(self.reference, BaseException):
            raise self.reference
        return self.reference


def _busy_error(sqlstate):
    original = RuntimeError("private database detail")
    original.sqlstate = sqlstate
    return DBAPIError("Project reference lock", {}, original, False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("nowait", "function_name", "autoflush"),
    [
        (True, "lock_project_reference_nowait", True),
        (False, "lock_project_reference", True),
        (True, "lock_project_reference_nowait", False),
        (False, "lock_project_reference", False),
    ],
)
async def test_project_authority_lock_preserves_order_and_lock_mode(
    monkeypatch, nowait, function_name, autoflush
):
    actor_id, project_id = uuid4(), uuid4()
    actor = object()
    session = _Session(autoflush=autoflush, reference=project_id)

    async def lock_accounts(_self, account_ids, *, mode, nowait):
        session.events.append(("actor", account_ids, mode, nowait))
        return {actor_id: actor}

    monkeypatch.setattr(
        project_write_guard.IdentityAccountLockData,
        "lock_accounts",
        lock_accounts,
    )

    result = await project_write_guard.lock_actor_project_authority(
        session, actor_id, project_id, nowait=nowait
    )

    assert result is actor
    expected = [("actor", [actor_id], "share", nowait)]
    if autoflush:
        expected.append(("flush",))
    assert session.events[:-1] == expected
    kind, statement, parameters = session.events[-1]
    assert kind == "project"
    assert function_name in statement
    assert parameters == {"project_id": project_id}


@pytest.mark.asyncio
async def test_missing_project_reference_keeps_actor_facts(monkeypatch):
    actor_id, project_id = uuid4(), uuid4()
    actor = object()
    session = _Session(reference=None)

    async def lock_accounts(_self, account_ids, *, mode, nowait):
        session.events.append(("actor", account_ids, mode, nowait))
        return {actor_id: actor}

    monkeypatch.setattr(
        project_write_guard.IdentityAccountLockData,
        "lock_accounts",
        lock_accounts,
    )

    result = await project_write_guard.lock_actor_project_authority(
        session, actor_id, project_id
    )

    assert result is actor
    assert len(session.events) == 3


@pytest.mark.asyncio
async def test_missing_actor_still_takes_project_reference_lock(monkeypatch):
    project_id = uuid4()
    session = _Session(reference=project_id)

    async def lock_accounts(_self, account_ids, *, mode, nowait):
        session.events.append(("actor", account_ids, mode, nowait))
        assert account_ids == []
        return {}

    monkeypatch.setattr(
        project_write_guard.IdentityAccountLockData,
        "lock_accounts",
        lock_accounts,
    )

    result = await project_write_guard.lock_actor_project_authority(
        session, None, project_id
    )

    assert result is None
    assert len(session.events) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlstate", ["55P03", "40001", "40P01"])
async def test_project_reference_contention_keeps_project_busy_mapping(
    monkeypatch, sqlstate
):
    actor_id, project_id = uuid4(), uuid4()
    session = _Session(reference=_busy_error(sqlstate))

    async def lock_accounts(_self, account_ids, *, mode, nowait):
        return {actor_id: object()}

    monkeypatch.setattr(
        project_write_guard.IdentityAccountLockData,
        "lock_accounts",
        lock_accounts,
    )

    with pytest.raises(HTTPException) as rejected:
        await project_write_guard.lock_actor_project_authority(
            session, actor_id, project_id
        )

    assert (rejected.value.status_code, rejected.value.detail) == (
        409,
        {"reason": "project_busy"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_type", [False, True])
async def test_wrong_project_reference_fails_closed_as_unavailable(
    monkeypatch, wrong_type
):
    actor_id, project_id = uuid4(), uuid4()
    reference = str(uuid4()) if wrong_type else uuid4()
    session = _Session(reference=reference)

    async def lock_accounts(_self, account_ids, *, mode, nowait):
        return {actor_id: object()}

    monkeypatch.setattr(
        project_write_guard.IdentityAccountLockData,
        "lock_accounts",
        lock_accounts,
    )

    with pytest.raises(HTTPException) as rejected:
        await project_write_guard.lock_actor_project_authority(
            session, actor_id, project_id
        )

    assert rejected.value.status_code == 503


@pytest.mark.asyncio
async def test_non_lock_database_error_is_not_rewritten(monkeypatch):
    actor_id, project_id = uuid4(), uuid4()
    database_error = _busy_error("42501")
    session = _Session(reference=database_error)

    async def lock_accounts(_self, account_ids, *, mode, nowait):
        return {actor_id: object()}

    monkeypatch.setattr(
        project_write_guard.IdentityAccountLockData,
        "lock_accounts",
        lock_accounts,
    )

    with pytest.raises(DBAPIError) as rejected:
        await project_write_guard.lock_actor_project_authority(
            session, actor_id, project_id
        )

    assert rejected.value is database_error
