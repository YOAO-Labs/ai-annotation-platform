"""Authority-only reads use existing Identity facts and lock capabilities."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.db.enums import PlatformRole
from app.db.models.async_job import AsyncJobKind
from app.services import async_job_access, batch, project_access, task_lock
from app.services.identity_contracts import IdentityActorFacts, IdentityActorStatus


def _facts(actor_id: uuid.UUID, *, role: str = "employee", active: bool = True):
    return IdentityActorFacts(
        id=actor_id,
        name="Actor",
        email="actor@example.test",
        role=role,
        is_active=active,
    )


def _identity_adapter(monkeypatch, module, *, accounts=None, status=None):
    data = SimpleNamespace(
        lock_accounts=AsyncMock(return_value=accounts or {}),
        read_actor_status=AsyncMock(return_value=status),
    )
    monkeypatch.setattr(module, "IdentityAccountLockData", lambda _db: data)
    return data


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


@pytest.mark.parametrize(
    ("actor", "expected"),
    [
        (_facts(uuid.UUID(int=1), role="historical-role"), True),
        (_facts(uuid.UUID(int=1), active=False), False),
        (None, False),
    ],
    ids=["active-legacy-role", "inactive", "missing"],
)
async def test_locked_task_guard_uses_waiting_share_without_role_gate(
    monkeypatch, actor, expected
):
    actor_id = uuid.UUID(int=1)
    data = _identity_adapter(
        monkeypatch,
        task_lock,
        accounts={actor_id: actor} if actor is not None else {},
    )

    assert await task_lock.assert_task_user_active(Mock(), actor_id) is expected
    data.lock_accounts.assert_awaited_once_with([actor_id], mode="share", nowait=False)
    data.read_actor_status.assert_not_awaited()


@pytest.mark.parametrize(
    ("actor", "expected"),
    [
        (IdentityActorStatus(uuid.UUID(int=2), "historical-role", True), True),
        (IdentityActorStatus(uuid.UUID(int=2), "employee", False), False),
        (None, False),
    ],
    ids=["active-legacy-role", "inactive", "missing"],
)
async def test_unlocked_task_guard_uses_status_without_lock(
    monkeypatch, actor, expected
):
    actor_id = uuid.UUID(int=2)
    data = _identity_adapter(monkeypatch, task_lock, status=actor)

    assert (
        await task_lock.assert_task_user_active(Mock(), actor_id, lock=False)
        is expected
    )
    data.read_actor_status.assert_awaited_once_with(actor_id)
    data.lock_accounts.assert_not_awaited()


@pytest.mark.parametrize(
    ("actor", "expected_status", "expected_detail"),
    [
        (
            IdentityActorStatus(uuid.UUID(int=3), PlatformRole.SUPER_ADMIN.value, True),
            200,
            None,
        ),
        (None, 401, "账号已停用或不存在"),
        (
            IdentityActorStatus(uuid.UUID(int=3), "employee", False),
            401,
            "账号已停用或不存在",
        ),
        (
            IdentityActorStatus(uuid.UUID(int=3), "historical-role", True),
            403,
            "账号角色无效，访问被拒绝",
        ),
    ],
    ids=["super-admin", "missing", "inactive", "historical-role"],
)
async def test_project_access_preserves_account_order_and_failures(
    monkeypatch, actor, expected_status, expected_detail
):
    actor_id = uuid.UUID(int=3)
    project = SimpleNamespace(id=uuid.UUID(int=30), owner_id=uuid.UUID(int=31))
    events = []
    data = _identity_adapter(monkeypatch, project_access, status=actor)
    data.read_actor_status.side_effect = lambda _actor_id: (
        events.append("actor_status") or actor
    )

    @asynccontextmanager
    async def transaction():
        yield object()

    async def read_project_row(_db, *, request, transaction, project_id):
        events.append(("get_row", request.admitted_actor.role, project_id))
        return project

    monkeypatch.setattr(
        project_access,
        "WorkspaceProjectQueryData",
        lambda _db: SimpleNamespace(transaction=transaction),
    )
    monkeypatch.setattr(project_access, "_read_project_query_row", read_project_row)
    db = SimpleNamespace()

    if expected_status == 200:
        access = await project_access.resolve_project_access(
            db, actor_id=actor_id, project=project
        )
        assert access.is_super_admin and access.is_manager
    else:
        with pytest.raises(HTTPException) as rejected:
            await project_access.resolve_project_access(
                db, actor_id=actor_id, project=project
            )
        assert rejected.value.status_code == expected_status
        assert rejected.value.detail == expected_detail
        if expected_status == 401:
            assert rejected.value.headers == {"WWW-Authenticate": "Bearer"}

    data.read_actor_status.assert_awaited_once_with(actor_id)
    if expected_status == 401:
        assert events == ["actor_status"]
    else:
        assert events == [
            "actor_status",
            ("get_row", actor.role, project.id),
        ]


async def test_batch_assignment_locks_sorted_accounts_before_members(monkeypatch):
    project_id = uuid.UUID(int=40)
    first_id, second_id = uuid.UUID(int=41), uuid.UUID(int=42)
    events = []
    data = _identity_adapter(
        monkeypatch,
        batch,
        accounts={first_id: _facts(first_id), second_id: _facts(second_id)},
    )

    async def read_members(db, selected_project, user_ids):
        events.append(("members", selected_project, user_ids))
        return [
            SimpleNamespace(user_id=second_id, role="annotator"),
            SimpleNamespace(user_id=first_id, role="reviewer"),
        ]

    async def lock_accounts(user_ids, **options):
        events.append(("accounts", user_ids, options))
        return {first_id: _facts(first_id), second_id: _facts(second_id)}

    data.lock_accounts.side_effect = lock_accounts
    member_lock = AsyncMock(side_effect=read_members)
    primer = AsyncMock(side_effect=lambda *_args: events.append(("primer",)))
    monkeypatch.setattr(batch, "lock_project_members", member_lock)
    monkeypatch.setattr(batch, "prime_expired_dirty_project_members", primer)
    db = SimpleNamespace()
    await batch.BatchService(db).lock_and_validate_assignment_targets(
        project_id, [("annotator", second_id), ("reviewer", first_id)]
    )

    data.lock_accounts.assert_awaited_once_with(
        [first_id, second_id], mode="share", nowait=False
    )
    member_lock.assert_awaited_once_with(db, project_id, [first_id, second_id])
    primer.assert_awaited_once_with(db, project_id, [first_id, second_id])
    assert events == [
        ("primer",),
        ("accounts", [first_id, second_id], {"mode": "share", "nowait": False}),
        ("members", project_id, [first_id, second_id]),
    ]


@pytest.mark.parametrize(
    ("actor", "expected_detail"),
    [
        (
            None,
            {
                "reason": "assignment_user_unavailable",
                "message": "接收账号已停用或不存在，请刷新成员列表后重新分派",
                "assignment_role": "annotator",
                "user_id": str(uuid.UUID(int=50)),
            },
        ),
        (
            _facts(uuid.UUID(int=50), active=False),
            {
                "reason": "assignment_user_unavailable",
                "message": "接收账号已停用或不存在，请刷新成员列表后重新分派",
                "assignment_role": "annotator",
                "user_id": str(uuid.UUID(int=50)),
            },
        ),
        (
            _facts(uuid.UUID(int=50), role="viewer"),
            {
                "reason": "assignment_role_mismatch",
                "message": "接收账号的平台角色与分派职责不匹配",
                "assignment_role": "annotator",
                "user_id": str(uuid.UUID(int=50)),
                "user_role": "viewer",
            },
        ),
    ],
    ids=["missing", "inactive", "incompatible-role"],
)
async def test_batch_assignment_keeps_target_failure_policy(
    monkeypatch, actor, expected_detail
):
    actor_id = uuid.UUID(int=50)
    data = _identity_adapter(
        monkeypatch,
        batch,
        accounts={actor_id: actor} if actor is not None else {},
    )
    member_lock = AsyncMock(
        return_value=[SimpleNamespace(user_id=actor_id, role="annotator")]
    )
    monkeypatch.setattr(batch, "lock_project_members", member_lock)
    monkeypatch.setattr(batch, "prime_expired_dirty_project_members", AsyncMock())
    db = SimpleNamespace()
    project_id = uuid.UUID(int=51)
    with pytest.raises(HTTPException) as rejected:
        await batch.BatchService(db).lock_and_validate_assignment_targets(
            project_id, [("annotator", actor_id)]
        )

    assert (rejected.value.status_code, rejected.value.detail) == (400, expected_detail)
    data.lock_accounts.assert_awaited_once_with([actor_id], mode="share", nowait=False)
    member_lock.assert_awaited_once_with(db, project_id, [actor_id])


async def test_batch_actor_scope_does_not_hydrate_user(monkeypatch):
    actor_id, project_id, batch_id = (
        uuid.UUID(int=60),
        uuid.UUID(int=61),
        uuid.UUID(int=62),
    )
    project = SimpleNamespace(id=project_id)
    batch_row = SimpleNamespace(id=batch_id)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Rows([project_id]),
                _Rows([batch_row]),
                _Rows([]),
            ]
        ),
        get=AsyncMock(return_value=project),
        rollback=AsyncMock(),
    )
    active_guard = AsyncMock(return_value=True)
    resolve_access = AsyncMock(return_value=SimpleNamespace(is_manager=True))
    project_reader = AsyncMock(return_value=project)
    project_lock = AsyncMock(return_value=[project_id])
    monkeypatch.setattr(batch, "get_workspace_project", project_reader)
    monkeypatch.setattr(batch, "lock_project_references_for_update", project_lock)
    monkeypatch.setattr(task_lock, "assert_task_user_active", active_guard)
    monkeypatch.setattr(
        "app.services.annotation_evidence.lock_tasks_for_evidence", AsyncMock()
    )
    monkeypatch.setattr(
        "app.services.project_access.resolve_project_access", resolve_access
    )

    assert await batch.BatchService(db)._lock_decision_scope(actor_id, [batch_id]) == {
        batch_id: batch_row
    }
    active_guard.assert_awaited_once_with(db, actor_id)
    project_lock.assert_awaited_once_with(db, [project_id])
    db.get.assert_not_awaited()
    project_reader.assert_awaited_once_with(
        db, project_id=project_id, populate_existing=True
    )
    resolve_access.assert_awaited_once_with(
        db, actor_id=actor_id, project=project, lock_membership=True
    )


@pytest.mark.parametrize(
    ("actor", "job_owner", "expected"),
    [
        (
            IdentityActorStatus(uuid.UUID(int=70), "employee", True),
            uuid.UUID(int=70),
            True,
        ),
        (
            IdentityActorStatus(uuid.UUID(int=70), "employee", False),
            uuid.UUID(int=70),
            False,
        ),
        (
            IdentityActorStatus(
                uuid.UUID(int=70), PlatformRole.SUPER_ADMIN.value, True
            ),
            uuid.UUID(int=72),
            True,
        ),
        (None, uuid.UUID(int=70), False),
    ],
    ids=["owned", "inactive", "super-admin", "missing"],
)
async def test_async_job_access_uses_unlocked_status(
    monkeypatch, actor, job_owner, expected
):
    actor_id = uuid.UUID(int=70)
    job = SimpleNamespace(
        id=uuid.UUID(int=71),
        project_id=None,
        retained_project_id=None,
        kind=AsyncJobKind.DATASET_IMPORT.value,
        user_id=job_owner,
    )
    data = _identity_adapter(monkeypatch, async_job_access, status=actor)
    db = SimpleNamespace(get=AsyncMock())
    get_job = AsyncMock(return_value=job)
    monkeypatch.setattr(async_job_access.jobs, "get_job", get_job)

    assert (
        await async_job_access._can_access_job(db, job=job, actor_id=actor_id)
    ) is expected
    data.read_actor_status.assert_awaited_once_with(actor_id)
    data.lock_accounts.assert_not_awaited()
    get_job.assert_awaited_once_with(db, job.id, populate_existing=True)
    db.get.assert_not_awaited()
