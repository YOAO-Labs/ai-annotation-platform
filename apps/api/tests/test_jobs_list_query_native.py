"""PostgreSQL parity and borrowed-session regressions for Jobs list queries."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.async_job_read import (
    SqlAsyncJobReadAdapter,
    _build_async_job_query,
)
from app.services.jobs_list_query_data import JobsListQueryPostgresData
from aap_jobs.async_job_list_query_contract import AsyncJobListQuery
from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_contracts import IdentityActorStatus
from tests.factory import create_membership, create_project, create_user


async def _assert_matches_legacy_list(
    db_session,
    actor,
    *,
    status=None,
    kind=None,
    project_id=None,
    search=None,
    limit=50,
    offset=0,
):
    """Compare the retained pre-extraction SQL oracle with the Jobs owner path."""
    actor_snapshot = IdentityActorStatus(
        id=actor.id,
        role=actor.role,
        is_active=actor.is_active,
    )
    legacy = _build_async_job_query(
        current_user=actor_snapshot,
        status=status,
        kind=kind,
        project_id=project_id,
        search=search,
    )
    legacy_page = (
        legacy.order_by(AsyncJob.created_at.desc(), AsyncJob.id.desc())
        .offset(offset)
        .limit(limit)
        .execution_options(populate_existing=True)
    )
    legacy_total = select(func.count()).select_from(legacy.order_by(None).subquery())
    expected_rows = (await db_session.execute(legacy_page)).scalars().all()
    expected_total = (await db_session.execute(legacy_total)).scalar_one()

    actual = await SqlAsyncJobReadAdapter(db_session).list_jobs(
        actor_id=actor.id,
        status=status,
        kind=kind,
        project_id=project_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    assert [item.id for item in actual.items] == [row.id for row in expected_rows]
    assert actual.total == expected_total
    return actual


async def _list_page(adapter, actor, **overrides):
    arguments = {
        "actor_id": actor.id,
        "status": None,
        "kind": None,
        "project_id": None,
        "search": None,
        "limit": 50,
        "offset": 0,
    }
    arguments.update(overrides)
    return await adapter.list_jobs(**arguments)


def _job(
    *,
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    kind: str,
    status: str = "pending",
    project_id: uuid.UUID | None = None,
    payload,
    created_at: datetime | None = None,
) -> AsyncJob:
    values = {
        "id": job_id,
        "user_id": user_id,
        "kind": kind,
        "status": status,
        "project_id": project_id,
        "payload": payload,
        "result": {},
    }
    if created_at is not None:
        values["created_at"] = created_at
    return AsyncJob(**values)


@asynccontextmanager
async def _committed_identity_scope_world(test_engine, *, actor_role="employee"):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    async with sessions() as setup:
        actor = await create_user(
            setup, actor_role, f"jobs-list-actor-{suffix}@test.local", "List actor"
        )
        manager = await create_user(
            setup,
            "project_admin",
            f"jobs-list-manager-{suffix}@test.local",
            "List manager",
        )
        project = await create_project(setup, owner_id=manager.id)
        membership = await create_membership(
            setup,
            project_id=project.id,
            user_id=actor.id,
            role="annotator",
            assigned_by=manager.id,
        )
        jobs = [
            _job(
                job_id=uuid.uuid4(),
                user_id=actor.id,
                kind="batch_predict",
                project_id=project.id,
                payload={},
            ),
            _job(
                job_id=uuid.uuid4(),
                user_id=actor.id,
                kind="audit_archive",
                payload={},
            ),
            _job(
                job_id=uuid.uuid4(),
                user_id=manager.id,
                kind="batch_predict",
                project_id=project.id,
                payload={},
            ),
        ]
        setup.add_all(jobs)
        await setup.commit()
        world = {
            "sessions": sessions,
            "actor_id": actor.id,
            "manager_id": manager.id,
            "project_id": project.id,
            "membership_id": membership.id,
            "project_job_id": jobs[0].id,
            "global_job_id": jobs[1].id,
            "manager_job_id": jobs[2].id,
        }

    try:
        yield world
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(AsyncJob).where(
                    AsyncJob.id.in_(
                        [
                            world["project_job_id"],
                            world["global_job_id"],
                            world["manager_job_id"],
                        ]
                    )
                )
            )
            await cleanup.execute(
                delete(ProjectMember).where(ProjectMember.id == world["membership_id"])
            )
            await cleanup.execute(
                delete(Project).where(Project.id == world["project_id"])
            )
            await cleanup.execute(
                delete(User).where(
                    User.id.in_([world["actor_id"], world["manager_id"]])
                )
            )
            await cleanup.commit()


async def _commit_user_change(sessions, user_id, **values):
    async with sessions() as mutation:
        await mutation.execute(update(User).where(User.id == user_id).values(**values))
        await mutation.commit()


def _return_preflight_snapshot(monkeypatch, actor_id, snapshot):
    async def read_actor_status(_self, requested_actor_id):
        assert requested_actor_id == actor_id
        return snapshot

    monkeypatch.setattr(IdentityAccountLockData, "read_actor_status", read_actor_status)


@pytest.mark.asyncio
async def test_owner_page_and_total_match_legacy_filters_jsonb_search_and_ties(
    db_session, annotator
):
    actor, _ = annotator
    same_created_at = datetime(2026, 10, 24, tzinfo=timezone.utc)
    jobs = [
        _job(
            job_id=uuid.UUID(int=100),
            user_id=actor.id,
            kind="audit_archive",
            status="completed",
            payload={"prompt": "city bus"},
            created_at=same_created_at,
        ),
        _job(
            job_id=uuid.UUID(int=101),
            user_id=actor.id,
            kind="audit_archive",
            status="failed",
            payload={"ml_backend_name": "BUS probe"},
            created_at=same_created_at,
        ),
        _job(
            job_id=uuid.UUID(int=102),
            user_id=actor.id,
            kind="dataset_import",
            status="completed",
            payload={"task_display_id": "bus-42"},
            created_at=same_created_at,
        ),
        _job(
            job_id=uuid.UUID(int=103),
            user_id=actor.id,
            kind="dataset_import",
            status="completed",
            payload=["bus", "legacy-jsonb-array"],
            created_at=same_created_at,
        ),
        _job(
            job_id=uuid.UUID(int=104),
            user_id=actor.id,
            kind="dataset_import",
            status="failed",
            payload="bus legacy scalar",
            created_at=same_created_at,
        ),
        _job(
            job_id=uuid.UUID(int=105),
            user_id=actor.id,
            kind="future_unrecognized_kind",
            status="completed",
            payload={"prompt": "bus"},
            created_at=same_created_at,
        ),
    ]
    db_session.add_all(jobs)
    await db_session.flush()

    first = await _assert_matches_legacy_list(
        db_session,
        actor,
        status=["completed", "failed"],
        kind=["audit_archive", "dataset_import"],
        search="  bus  ",
        limit=2,
    )
    assert [row.id for row in first.items] == [uuid.UUID(int=102), uuid.UUID(int=101)]
    assert first.total == 3

    remainder = await _assert_matches_legacy_list(
        db_session,
        actor,
        status=["completed", "failed"],
        kind=["audit_archive", "dataset_import"],
        search="bus",
        limit=2,
        offset=2,
    )
    assert [row.id for row in remainder.items] == [uuid.UUID(int=100)]
    assert remainder.total == 3

    empty_page = await _assert_matches_legacy_list(
        db_session,
        actor,
        status=["completed", "failed"],
        kind=["audit_archive", "dataset_import"],
        search="bus",
        limit=2,
        offset=50,
    )
    assert empty_page.items == []
    assert empty_page.total == 3

    unknown_unscoped = await _assert_matches_legacy_list(
        db_session,
        actor,
        kind=["future_unrecognized_kind"],
        limit=50,
    )
    assert unknown_unscoped.items == []
    assert unknown_unscoped.total == 0


@pytest.mark.asyncio
async def test_owner_page_and_total_match_membership_export_and_retained_scope_policy(
    db_session, annotator, project_admin
):
    actor, _ = annotator
    manager, _ = project_admin
    reviewer_project = await create_project(db_session, owner_id=manager.id)
    annotator_project = await create_project(db_session, owner_id=manager.id)
    reviewer_membership = await create_membership(
        db_session,
        project_id=reviewer_project.id,
        user_id=actor.id,
        role="reviewer",
        assigned_by=manager.id,
    )
    await create_membership(
        db_session,
        project_id=annotator_project.id,
        user_id=actor.id,
        role="annotator",
        assigned_by=manager.id,
    )

    jobs = [
        _job(
            job_id=uuid.uuid4(),
            user_id=actor.id,
            kind="batch_predict",
            project_id=reviewer_project.id,
            payload={},
        ),
        _job(
            job_id=uuid.uuid4(),
            user_id=actor.id,
            kind="export",
            project_id=reviewer_project.id,
            payload={},
        ),
        _job(
            job_id=uuid.uuid4(),
            user_id=manager.id,
            kind="export",
            project_id=reviewer_project.id,
            payload={},
        ),
        _job(
            job_id=uuid.uuid4(),
            user_id=actor.id,
            kind="batch_predict",
            project_id=annotator_project.id,
            payload={},
        ),
        _job(
            job_id=uuid.uuid4(),
            user_id=actor.id,
            kind="export",
            project_id=annotator_project.id,
            payload={},
        ),
        _job(
            job_id=uuid.uuid4(),
            user_id=manager.id,
            kind="batch_predict",
            project_id=reviewer_project.id,
            payload={},
        ),
        _job(
            job_id=uuid.uuid4(),
            user_id=actor.id,
            kind="audit_archive",
            payload={},
        ),
        _job(
            job_id=uuid.uuid4(),
            user_id=actor.id,
            kind="batch_predict",
            payload={},
        ),
    ]
    db_session.add_all(jobs)
    await db_session.flush()

    reviewer_scope = await _assert_matches_legacy_list(
        db_session, actor, project_id=reviewer_project.id
    )
    assert {row.id for row in reviewer_scope.items} == {jobs[0].id, jobs[1].id}
    assert reviewer_scope.total == 2

    annotator_scope = await _assert_matches_legacy_list(
        db_session, actor, project_id=annotator_project.id
    )
    assert [row.id for row in annotator_scope.items] == [jobs[3].id]
    assert annotator_scope.total == 1

    manager_scope = await _assert_matches_legacy_list(
        db_session, manager, project_id=reviewer_project.id
    )
    assert {row.id for row in manager_scope.items} == {jobs[2].id, jobs[5].id}
    assert manager_scope.total == 2

    await db_session.delete(reviewer_membership)
    await db_session.flush()
    revoked = await _assert_matches_legacy_list(
        db_session, actor, project_id=reviewer_project.id
    )
    assert revoked.items == []
    assert revoked.total == 0

    await db_session.execute(delete(Project).where(Project.id == annotator_project.id))
    await db_session.flush()
    hidden_retained_scope = await _assert_matches_legacy_list(
        db_session, actor, project_id=annotator_project.id
    )
    assert hidden_retained_scope.items == []
    assert hidden_retained_scope.total == 0

    hidden_unresolved_scope = await _assert_matches_legacy_list(
        db_session, actor, kind=["batch_predict"]
    )
    assert jobs[7].id not in {row.id for row in hidden_unresolved_scope.items}
    assert hidden_unresolved_scope.total == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_role", "current_change", "expected_scope_jobs"),
    [
        ("employee", {"is_active": False}, {"global_job_id"}),
        ("employee", {"role": "viewer"}, {"global_job_id"}),
        ("employee", {"role": "reviewer"}, {"global_job_id"}),
        (
            "viewer",
            {"role": "employee"},
            {"project_job_id", "global_job_id"},
        ),
    ],
)
async def test_owner_membership_uses_current_identity_but_keeps_preflight_role_snapshot(
    db_session,
    monkeypatch,
    test_engine,
    initial_role,
    current_change,
    expected_scope_jobs,
):
    async with _committed_identity_scope_world(
        test_engine, actor_role=initial_role
    ) as world:
        snapshot = await IdentityAccountLockData(db_session).read_actor_status(
            world["actor_id"]
        )
        assert snapshot is not None and snapshot.is_active
        assert snapshot.role == initial_role

        await _commit_user_change(
            world["sessions"], world["actor_id"], **current_change
        )

        legacy = _build_async_job_query(
            current_user=snapshot,
            status=None,
            kind=None,
            project_id=None,
            search=None,
        )
        legacy_page = (
            legacy.order_by(AsyncJob.created_at.desc(), AsyncJob.id.desc())
            .offset(0)
            .limit(50)
            .execution_options(populate_existing=True)
        )
        legacy_total = select(func.count()).select_from(
            legacy.order_by(None).subquery()
        )
        expected_rows = (await db_session.execute(legacy_page)).scalars().all()
        expected_count = (await db_session.execute(legacy_total)).scalar_one()

        _return_preflight_snapshot(monkeypatch, world["actor_id"], snapshot)
        actual = await SqlAsyncJobReadAdapter(db_session).list_jobs(
            actor_id=world["actor_id"],
            status=None,
            kind=None,
            project_id=None,
            search=None,
            limit=50,
            offset=0,
        )

        expected_ids = {world[name] for name in expected_scope_jobs}
        assert {row.id for row in expected_rows} == expected_ids
        assert {row.id for row in actual.items} == expected_ids
        assert actual.total == expected_count == len(expected_ids)


@pytest.mark.asyncio
async def test_owner_snapshot_manager_branch_survives_same_statement_deactivation(
    db_session, monkeypatch, test_engine
):
    async with _committed_identity_scope_world(test_engine) as world:
        snapshot = await IdentityAccountLockData(db_session).read_actor_status(
            world["manager_id"]
        )
        assert snapshot is not None
        assert snapshot.role == "project_admin" and snapshot.is_active

        await _commit_user_change(
            world["sessions"], world["manager_id"], is_active=False
        )
        legacy = _build_async_job_query(
            current_user=snapshot,
            status=None,
            kind=None,
            project_id=None,
            search=None,
        )
        expected_rows = (
            (
                await db_session.execute(
                    legacy.order_by(AsyncJob.created_at.desc(), AsyncJob.id.desc())
                    .offset(0)
                    .limit(50)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        expected_total = await db_session.scalar(
            select(func.count()).select_from(legacy.order_by(None).subquery())
        )

        _return_preflight_snapshot(monkeypatch, world["manager_id"], snapshot)
        actual = await SqlAsyncJobReadAdapter(db_session).list_jobs(
            actor_id=world["manager_id"],
            status=None,
            kind=None,
            project_id=None,
            search=None,
            limit=50,
            offset=0,
        )

        assert {row.id for row in expected_rows} == {world["manager_job_id"]}
        assert {row.id for row in actual.items} == {world["manager_job_id"]}
        assert actual.total == expected_total == 1


@pytest.mark.asyncio
async def test_owner_page_and_total_keep_independent_identity_snapshots(
    db_session, test_engine
):
    async with _committed_identity_scope_world(test_engine) as world:
        snapshot = await IdentityAccountLockData(db_session).read_actor_status(
            world["actor_id"]
        )
        assert (
            snapshot is not None and snapshot.role == "employee" and snapshot.is_active
        )

        legacy = _build_async_job_query(
            current_user=snapshot,
            status=None,
            kind=None,
            project_id=None,
            search=None,
        )
        legacy_page = (
            legacy.order_by(AsyncJob.created_at.desc(), AsyncJob.id.desc())
            .limit(50)
            .offset(0)
            .execution_options(populate_existing=True)
        )
        legacy_total = select(func.count()).select_from(
            legacy.order_by(None).subquery()
        )
        query = AsyncJobListQuery(
            actor_id=snapshot.id,
            platform_role=snapshot.role,
            statuses=[],
            kinds=[],
            project_id=None,
            search_pattern=None,
            limit=50,
            offset=0,
        )
        owner_data = JobsListQueryPostgresData(db_session, AsyncJob)

        expected_page = (await db_session.execute(legacy_page)).scalars().all()
        actual_page = await owner_data.page(query)
        expected_page_ids = {world["project_job_id"], world["global_job_id"]}
        assert {row.id for row in expected_page} == expected_page_ids
        assert {row.id for row in actual_page} == expected_page_ids

        await _commit_user_change(world["sessions"], world["actor_id"], is_active=False)
        expected_total = (await db_session.execute(legacy_total)).scalar_one()
        actual_total = await owner_data.total(query)
        assert expected_total == actual_total == 1


@pytest.mark.asyncio
async def test_owner_list_preserves_session_flush_cache_and_rollback(
    db_session, annotator
):
    actor, _ = annotator
    adapter = SqlAsyncJobReadAdapter(db_session)

    stale = _job(
        job_id=uuid.uuid4(),
        user_id=actor.id,
        kind="audit_archive",
        status="pending",
        payload={},
    )
    db_session.add(stale)
    await db_session.flush()
    await db_session.execute(
        update(AsyncJob)
        .where(AsyncJob.id == stale.id)
        .values(status="running", progress_pct=61)
        .execution_options(synchronize_session=False)
    )
    assert stale.status == "pending"

    refreshed = await _list_page(adapter, actor, kind=["audit_archive"])
    assert (
        next(row for row in refreshed.items if row.id == stale.id).status == "running"
    )
    assert stale.status == "running"

    pending = _job(
        job_id=uuid.uuid4(),
        user_id=actor.id,
        kind="audit_archive",
        payload={},
    )
    db_session.add(pending)
    default_flush = await _list_page(adapter, actor, kind=["audit_archive"])
    assert pending.id in {row.id for row in default_flush.items}

    no_autoflush_pending = _job(
        job_id=uuid.uuid4(),
        user_id=actor.id,
        kind="audit_archive",
        payload={},
    )
    with db_session.no_autoflush:
        db_session.add(no_autoflush_pending)
        no_autoflush_page = await _list_page(adapter, actor, kind=["audit_archive"])
        assert no_autoflush_pending.id not in {
            row.id for row in no_autoflush_page.items
        }

    await db_session.flush()
    flushed_page = await _list_page(adapter, actor, kind=["audit_archive"])
    assert no_autoflush_pending.id in {row.id for row in flushed_page.items}

    savepoint = await db_session.begin_nested()
    rolled_back = _job(
        job_id=uuid.uuid4(),
        user_id=actor.id,
        kind="audit_archive",
        payload={},
    )
    db_session.add(rolled_back)
    savepoint_page = await _list_page(adapter, actor, kind=["audit_archive"])
    assert rolled_back.id in {row.id for row in savepoint_page.items}
    await savepoint.rollback()
    assert db_session.in_transaction()

    after_rollback = await _list_page(adapter, actor, kind=["audit_archive"])
    assert rolled_back.id not in {row.id for row in after_rollback.items}
