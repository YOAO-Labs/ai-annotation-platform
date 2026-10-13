"""Member mutation races over real PostgreSQL with independent connections.

These tests deliberately use separate ``AsyncSession`` objects on the shared
``test_engine`` (never the savepoint-bound ``db_session``) because in-memory or
sequential mocks cannot prove that two concurrent member mutations serialize.
Every committed fixture is removed in ``finally`` by its exact seeded IDs.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from app.services.project_access import (
    ProjectCapability,
    resolve_project_access,
)
from app.services.project_membership import (
    add_member,
    change_role,
    preview_role_change,
    remove_member,
)
from tests.factory import (
    create_batch,
    create_membership,
    create_project,
    create_task,
    create_user,
)

pytestmark = pytest.mark.asyncio


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _add_member_row(
    db: AsyncSession, *, project_id, user_id, role: str, assigned_by
) -> ProjectMember:
    return await create_membership(
        db,
        project_id=project_id,
        user_id=user_id,
        role=role,
        assigned_by=assigned_by,
    )


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    *,
    annotator_role: str = "annotator",
    with_annotation_work: bool = True,
    with_other_project: bool = True,
) -> dict:
    """Create a committed project fixture with a batch + optional work."""

    async with maker() as seed:
        suffix = uuid.uuid4().hex[:8]
        owner = await create_user(
            seed, "project_admin", f"race-owner-{suffix}@test.local", "Owner"
        )
        admin = await create_user(
            seed, "super_admin", f"race-admin-{suffix}@test.local", "Admin"
        )
        member_user = await create_user(
            seed, "employee", f"race-member-{suffix}@test.local", "Member"
        )
        annotator_receiver = await create_user(
            seed, "employee", f"race-anno-{suffix}@test.local", "AnnoRecv"
        )
        reviewer_receiver = await create_user(
            seed, "employee", f"race-qa-{suffix}@test.local", "QaRecv"
        )
        project = await create_project(seed, owner_id=owner.id, name="Race Project")
        member_row = await _add_member_row(
            seed,
            project_id=project.id,
            user_id=member_user.id,
            role=annotator_role,
            assigned_by=owner.id,
        )
        await _add_member_row(
            seed,
            project_id=project.id,
            user_id=annotator_receiver.id,
            role="annotator",
            assigned_by=owner.id,
        )
        await _add_member_row(
            seed,
            project_id=project.id,
            user_id=reviewer_receiver.id,
            role="reviewer",
            assigned_by=owner.id,
        )

        batch = await create_batch(
            seed, project_id=project.id, status="annotating", name="Race Batch"
        )
        batch.annotator_id = member_user.id
        batch.assigned_user_ids = [str(member_user.id)]

        task = None
        assigned_at = None
        if with_annotation_work:
            task = await create_task(seed, project_id=project.id, status="in_progress")
            task.batch_id = batch.id
            task.assignee_id = member_user.id
            task.assignee_is_override = False
            assigned_at = datetime.now(timezone.utc)
            task.assigned_at = assigned_at

        lock = None
        if task is not None:
            lock = TaskLock(
                task_id=task.id,
                user_id=member_user.id,
                expire_at=datetime(2999, 1, 1, tzinfo=timezone.utc),
            )
            seed.add(lock)

        other_project = None
        other_task = None
        if with_other_project:
            other_project = await create_project(
                seed, owner_id=owner.id, name="Other Project"
            )
            other_task = await create_task(
                seed, project_id=other_project.id, status="in_progress"
            )
            other_task.assignee_id = member_user.id

        await seed.commit()
        return {
            "owner": owner.id,
            "admin": admin.id,
            "member_user": member_user.id,
            "annotator_receiver": annotator_receiver.id,
            "reviewer_receiver": reviewer_receiver.id,
            "project": project.id,
            "member_row": member_row.id,
            "batch": batch.id,
            "task": task.id if task else None,
            "assigned_at": assigned_at,
            "lock": lock.id if lock else None,
            "other_project": other_project.id if other_project else None,
            "other_task": other_task.id if other_task else None,
        }


async def _drop(
    maker: async_sessionmaker[AsyncSession],
    fixture: dict,
    extra_user_ids: tuple[uuid.UUID, ...] = (),
) -> None:
    user_ids = [
        fixture["owner"],
        fixture["admin"],
        fixture["member_user"],
        fixture["annotator_receiver"],
        fixture["reviewer_receiver"],
        *extra_user_ids,
    ]
    project_ids = [pid for pid in (fixture["project"], fixture["other_project"]) if pid]
    async with maker() as cleanup:
        # Audit rows are immutable; allow the exact fixture rows to be removed
        # before their actors so the users FK cannot trip the guard on SET NULL.
        await cleanup.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
        await cleanup.execute(delete(AuditLog).where(AuditLog.actor_id.in_(user_ids)))
        await cleanup.execute(delete(TaskLock).where(TaskLock.user_id.in_(user_ids)))
        await cleanup.execute(delete(Task).where(Task.project_id.in_(project_ids)))
        await cleanup.execute(
            delete(TaskBatch).where(TaskBatch.project_id.in_(project_ids))
        )
        await cleanup.execute(
            delete(ProjectMember).where(ProjectMember.project_id.in_(project_ids))
        )
        await cleanup.execute(delete(Project).where(Project.id.in_(project_ids)))
        await cleanup.execute(delete(User).where(User.id.in_(user_ids)))
        await cleanup.commit()


async def _preview(
    db: AsyncSession,
    fixture: dict,
    *,
    target_role: str,
    replacement_annotator_id=None,
    replacement_reviewer_id=None,
):
    project = await db.get(Project, fixture["project"], populate_existing=True)
    actor = await db.get(User, fixture["admin"], populate_existing=True)
    return await preview_role_change(
        db,
        project=project,
        actor=actor,
        member_id=fixture["member_row"],
        target_role=target_role,
        replacement_annotator_id=replacement_annotator_id,
        replacement_reviewer_id=replacement_reviewer_id,
    )


async def _change(
    db: AsyncSession,
    fixture: dict,
    *,
    target_role: str,
    expected_version: int,
    preview_token: str,
    replacement_annotator_id=None,
    replacement_reviewer_id=None,
):
    project = await db.get(Project, fixture["project"], populate_existing=True)
    actor = await db.get(User, fixture["admin"], populate_existing=True)
    return await change_role(
        db,
        project=project,
        actor=actor,
        member_id=fixture["member_row"],
        target_role=target_role,
        expected_version=expected_version,
        preview_token=preview_token,
        reason="race test",
        replacement_annotator_id=replacement_annotator_id,
        replacement_reviewer_id=replacement_reviewer_id,
    )


# ---------------------------------------------------------------------------
# RACE-01: stale preview / same-version concurrency
# ---------------------------------------------------------------------------


async def test_same_version_race_and_busy_membership_rollback(test_engine):
    """Only one same-version mutation may succeed; the loser 409s and rolls back."""

    maker = _maker(test_engine)
    fixture = await _seed(maker)
    holder = maker()
    contender = maker()
    try:
        # A concurrent member mutation holds the membership row lock.
        locked = await holder.scalar(
            select(ProjectMember)
            .where(ProjectMember.id == fixture["member_row"])
            .with_for_update()
        )
        assert locked is not None and locked.version == 1

        preview = await _preview(
            contender,
            fixture,
            target_role="reviewer",
            replacement_annotator_id=fixture["annotator_receiver"],
        )
        with pytest.raises(Exception) as caught:
            await _change(
                contender,
                fixture,
                target_role="reviewer",
                expected_version=1,
                preview_token=preview["preview_token"],
                replacement_annotator_id=fixture["annotator_receiver"],
            )
        detail = getattr(caught.value, "detail", None)
        assert getattr(caught.value, "status_code", None) == 409, caught.value
        assert detail["reason"] == "membership_busy"
        # A busy lock rolls the whole transaction back; no partial handoff remains.
        assert not contender.in_transaction()

        await holder.rollback()

        # A fresh session applies the change for real.
        winner = maker()
        try:
            winner_preview = await _preview(
                winner,
                fixture,
                target_role="reviewer",
                replacement_annotator_id=fixture["annotator_receiver"],
            )
            changed = await _change(
                winner,
                fixture,
                target_role="reviewer",
                expected_version=1,
                preview_token=winner_preview["preview_token"],
                replacement_annotator_id=fixture["annotator_receiver"],
            )
            assert changed.version == 2
            assert changed.role == "reviewer"
        finally:
            await winner.close()

        # The original same-version attempt can no longer succeed.
        with pytest.raises(Exception) as stale:
            await _change(
                contender,
                fixture,
                target_role="reviewer",
                expected_version=1,
                preview_token=preview["preview_token"],
                replacement_annotator_id=fixture["annotator_receiver"],
            )
        assert getattr(stale.value, "status_code", None) == 409
        assert stale.value.detail["reason"] == "stale_member_version"
    finally:
        await holder.close()
        await contender.close()
        await _drop(maker, fixture)


async def test_new_assignment_after_preview_conflicts(test_engine):
    """A task assigned after the preview invalidates the resource snapshot."""

    maker = _maker(test_engine)
    fixture = await _seed(maker, with_annotation_work=False)
    preview_session = maker()
    try:
        preview = await _preview(
            preview_session,
            fixture,
            target_role="reviewer",
            replacement_annotator_id=fixture["annotator_receiver"],
        )
        preview_token = preview["preview_token"]

        # Commit a brand-new assignment to the member on another connection.
        async with maker() as seeding:
            batch = await seeding.get(TaskBatch, fixture["batch"])
            batch.annotator_id = fixture["member_user"]
            batch.assigned_user_ids = [str(fixture["member_user"])]
            new_task = await create_task(
                seeding, project_id=fixture["project"], status="in_progress"
            )
            new_task.batch_id = fixture["batch"]
            new_task.assignee_id = fixture["member_user"]
            new_task.assignee_is_override = False
            await seeding.commit()

        session = maker()
        try:
            with pytest.raises(Exception) as caught:
                await _change(
                    session,
                    fixture,
                    target_role="reviewer",
                    expected_version=1,
                    preview_token=preview_token,
                    replacement_annotator_id=fixture["annotator_receiver"],
                )
            assert getattr(caught.value, "status_code", None) == 409
            assert caught.value.detail["reason"] == "stale_resource_snapshot"
        finally:
            await session.close()
    finally:
        await preview_session.close()
        await _drop(maker, fixture)


async def test_resource_lock_contention_rolls_back_without_partial_write(
    test_engine,
):
    """A busy task row produces a rolled-back 409 and leaves the member intact."""

    maker = _maker(test_engine)
    fixture = await _seed(maker)
    holder = maker()
    contender = maker()
    try:
        await holder.scalar(
            select(Task).where(Task.id == fixture["task"]).with_for_update()
        )
        preview = await _preview(
            contender,
            fixture,
            target_role="reviewer",
            replacement_annotator_id=fixture["annotator_receiver"],
        )
        with pytest.raises(Exception) as caught:
            await _change(
                contender,
                fixture,
                target_role="reviewer",
                expected_version=1,
                preview_token=preview["preview_token"],
                replacement_annotator_id=fixture["annotator_receiver"],
            )
        assert getattr(caught.value, "status_code", None) == 409
        assert caught.value.detail["reason"] == "resource_busy"
        assert not contender.in_transaction()

        await holder.rollback()
        async with maker() as check:
            member = await check.get(ProjectMember, fixture["member_row"])
            task = await check.get(Task, fixture["task"])
            assert member.version == 1
            assert member.role == "annotator"
            assert task.assignee_id == fixture["member_user"]
    finally:
        await holder.close()
        await contender.close()
        await _drop(maker, fixture)


async def test_ownership_change_after_preview_conflicts(test_engine):
    """Transferring the project after a preview invalidates the snapshot token."""

    maker = _maker(test_engine)
    fixture = await _seed(maker, with_annotation_work=False)
    preview_session = maker()
    extra_user_ids: tuple[uuid.UUID, ...] = ()
    try:
        preview = await _preview(preview_session, fixture, target_role="reviewer")
        # A different legitimate manager (platform project_admin) takes ownership
        # on another connection.  The member's own platform role is untouched so
        # only the project-owner change can invalidate the snapshot.
        async with maker() as seeding:
            new_owner = await create_user(
                seeding,
                "project_admin",
                f"new-owner-{uuid.uuid4().hex[:8]}@test.local",
                "NewOwner",
            )
            await seeding.flush()
            project = await seeding.get(Project, fixture["project"])
            project.owner_id = new_owner.id
            await seeding.commit()
            extra_user_ids = (new_owner.id,)

        session = maker()
        try:
            with pytest.raises(Exception) as caught:
                await _change(
                    session,
                    fixture,
                    target_role="reviewer",
                    expected_version=1,
                    preview_token=preview["preview_token"],
                )
            assert getattr(caught.value, "status_code", None) == 409
            assert caught.value.detail["reason"] == "stale_resource_snapshot"
        finally:
            await session.close()
    finally:
        await preview_session.close()
        await _drop(maker, fixture, extra_user_ids=extra_user_ids)


async def test_receiver_role_change_after_preview_conflicts(test_engine):
    """Changing the receiver membership after a preview must conflict."""

    maker = _maker(test_engine)
    fixture = await _seed(maker)
    preview_session = maker()
    try:
        preview = await _preview(
            preview_session,
            fixture,
            target_role="reviewer",
            replacement_annotator_id=fixture["annotator_receiver"],
        )
        async with maker() as seeding:
            receiver_member = await seeding.scalar(
                select(ProjectMember).where(
                    ProjectMember.project_id == fixture["project"],
                    ProjectMember.user_id == fixture["annotator_receiver"],
                )
            )
            receiver_member.role = "viewer"
            receiver_member.version = receiver_member.version + 1
            await seeding.commit()

        session = maker()
        try:
            with pytest.raises(Exception) as caught:
                await _change(
                    session,
                    fixture,
                    target_role="reviewer",
                    expected_version=1,
                    preview_token=preview["preview_token"],
                    replacement_annotator_id=fixture["annotator_receiver"],
                )
            assert getattr(caught.value, "status_code", None) == 409
            assert caught.value.detail["reason"] == "stale_resource_snapshot"
        finally:
            await session.close()
    finally:
        await preview_session.close()
        await _drop(maker, fixture)


# ---------------------------------------------------------------------------
# RACE-02: no cross-project effects, post-revocation authority
# ---------------------------------------------------------------------------


async def test_handoff_is_project_scoped_and_preserves_history(test_engine):
    """Handoff changes only this project and never rewrites terminal attribution."""

    maker = _maker(test_engine)
    fixture = await _seed(maker)
    # A terminal task in the same batch must keep its historical assignee.
    async with maker() as seeding:
        terminal = await create_task(
            seeding, project_id=fixture["project"], status="completed"
        )
        terminal.batch_id = fixture["batch"]
        terminal.assignee_id = fixture["member_user"]
        terminal.assignee_is_override = False
        terminal.assigned_at = datetime.now(timezone.utc)
        # An explicit override task is transferred without losing "override".
        overridden = await create_task(
            seeding, project_id=fixture["project"], status="in_progress"
        )
        overridden.batch_id = fixture["batch"]
        overridden.assignee_id = fixture["member_user"]
        overridden.assignee_is_override = True
        overridden.assigned_at = datetime.now(timezone.utc)
        await seeding.commit()
        terminal_id, overridden_id = terminal.id, overridden.id

    try:
        session = maker()
        try:
            preview = await _preview(
                session,
                fixture,
                target_role="reviewer",
                replacement_annotator_id=fixture["annotator_receiver"],
            )
            assert preview["blockers"] == []
            changed = await _change(
                session,
                fixture,
                target_role="reviewer",
                expected_version=1,
                preview_token=preview["preview_token"],
                replacement_annotator_id=fixture["annotator_receiver"],
            )
            assert changed.version == 2
        finally:
            await session.close()

        async with maker() as check:
            inherited = await check.get(Task, fixture["task"])
            assert inherited.assignee_id == fixture["annotator_receiver"]
            assert inherited.assignee_is_override is False
            assert inherited.assigned_at is not None

            override_task = await check.get(Task, overridden_id)
            assert override_task.assignee_id == fixture["annotator_receiver"]
            assert override_task.assignee_is_override is True

            history = await check.get(Task, terminal_id)
            assert history.assignee_id == fixture["member_user"]
            assert history.assignee_is_override is False

            batch = await check.get(TaskBatch, fixture["batch"])
            assert batch.annotator_id == fixture["annotator_receiver"]
            assert str(fixture["annotator_receiver"]) in batch.assigned_user_ids
            assert str(fixture["member_user"]) not in batch.assigned_user_ids
            assert None not in batch.assigned_user_ids

            # The other project's assignment is untouched.
            other = await check.get(Task, fixture["other_task"])
            assert other.assignee_id == fixture["member_user"]

            lock = await check.get(TaskLock, fixture["lock"])
            assert lock is None  # released only for this project's member
    finally:
        await _drop(maker, fixture)


async def test_post_revocation_access_and_handoff_are_atomic(test_engine):
    """After demotion the member's fresh access loses annotation authority."""

    maker = _maker(test_engine)
    fixture = await _seed(maker)
    try:
        session = maker()
        try:
            preview = await _preview(
                session,
                fixture,
                target_role="viewer",
                replacement_annotator_id=fixture["annotator_receiver"],
            )
            await _change(
                session,
                fixture,
                target_role="viewer",
                expected_version=1,
                preview_token=preview["preview_token"],
                replacement_annotator_id=fixture["annotator_receiver"],
            )
        finally:
            await session.close()

        async with maker() as check:
            member_user = await check.get(User, fixture["member_user"])
            project = await check.get(Project, fixture["project"])
            access = await resolve_project_access(
                check, user=member_user, project=project
            )
            assert access.project_role == "viewer"
            assert ProjectCapability.ANNOTATION_WRITE.value not in access.capabilities
            assert ProjectCapability.REVIEW_WRITE.value not in access.capabilities

            task = await check.get(Task, fixture["task"])
            assert task.assignee_id == fixture["annotator_receiver"]
    finally:
        await _drop(maker, fixture)


# ---------------------------------------------------------------------------
# RACE-03: delete / rejoin invalidates the member identity
# ---------------------------------------------------------------------------


async def test_delete_rejoin_rejects_stale_member_id(test_engine):
    """A recycled membership never accepts a mutation addressed to the old ID."""

    maker = _maker(test_engine)
    fixture = await _seed(maker, with_annotation_work=False)
    try:
        session = maker()
        try:
            project = await session.get(Project, fixture["project"])
            actor = await session.get(User, fixture["admin"])
            await remove_member(
                session,
                project=project,
                actor=actor,
                member_id=fixture["member_row"],
            )
            # Re-joining the same account creates a brand-new membership row.
            new_member = await add_member(
                session,
                project=project,
                actor=actor,
                target_user_id=fixture["member_user"],
                project_role="annotator",
            )
            assert new_member.id != fixture["member_row"]
            assert new_member.version == 1
            new_member_id = new_member.id
        finally:
            await session.close()

        stale = maker()
        try:
            project = await stale.get(Project, fixture["project"])
            actor = await stale.get(User, fixture["admin"])
            with pytest.raises(Exception) as caught:
                await preview_role_change(
                    stale,
                    project=project,
                    actor=actor,
                    member_id=fixture["member_row"],
                    target_role="reviewer",
                    replacement_annotator_id=None,
                    replacement_reviewer_id=None,
                )
            assert getattr(caught.value, "status_code", None) == 404
            # The new membership is independently usable.
            fresh_preview = await preview_role_change(
                stale,
                project=project,
                actor=actor,
                member_id=new_member_id,
                target_role="reviewer",
                replacement_annotator_id=None,
                replacement_reviewer_id=None,
            )
            assert fresh_preview["current_version"] == 1
        finally:
            await stale.close()
    finally:
        await _drop(maker, fixture)


async def test_reviewer_handoff_moves_assignment_and_releases_claim(test_engine):
    """A reviewer demotion transfers the claim and assignment to the receiver."""

    maker = _maker(test_engine)
    fixture = await _seed(maker, annotator_role="reviewer", with_annotation_work=False)
    try:
        async with maker() as seeding:
            review_task = await create_task(
                seeding, project_id=fixture["project"], status="review"
            )
            review_task.reviewer_id = fixture["member_user"]
            review_task.reviewer_claimed_at = datetime.now(timezone.utc)
            # Known-empty contributor evidence keeps the receiver validatable.
            review_task.annotation_contributor_ids = []
            await seeding.commit()
            review_task_id = review_task.id

        session = maker()
        try:
            preview = await _preview(
                session,
                fixture,
                target_role="annotator",
                replacement_reviewer_id=fixture["reviewer_receiver"],
            )
            assert preview["blockers"] == [], preview["blockers"]
            changed = await _change(
                session,
                fixture,
                target_role="annotator",
                expected_version=1,
                preview_token=preview["preview_token"],
                replacement_reviewer_id=fixture["reviewer_receiver"],
            )
            assert changed.role == "annotator"
        finally:
            await session.close()

        async with maker() as check:
            task = await check.get(Task, review_task_id)
            assert task.reviewer_id == fixture["reviewer_receiver"]
            assert task.reviewer_claimed_at is None
    finally:
        await _drop(maker, fixture)


async def test_unknown_review_evidence_blocks_review_receiver(test_engine):
    """Legacy unknown contributor evidence makes a review receiver unverifiable."""

    maker = _maker(test_engine)
    fixture = await _seed(maker, annotator_role="reviewer", with_annotation_work=False)
    try:
        async with maker() as seeding:
            review_task = await create_task(
                seeding, project_id=fixture["project"], status="review"
            )
            review_task.reviewer_id = fixture["member_user"]
            review_task.annotation_contributor_ids = None
            await seeding.commit()

        session = maker()
        try:
            preview = await _preview(
                session,
                fixture,
                target_role="annotator",
                replacement_reviewer_id=fixture["reviewer_receiver"],
            )
            assert "review_contributors_unknown" in preview["blockers"]
        finally:
            await session.close()
    finally:
        await _drop(maker, fixture)


async def test_concurrent_duplicate_change_only_one_wins(test_engine):
    """Two connections racing the same CAS: exactly one commits, the other 409s."""

    maker = _maker(test_engine)
    fixture = await _seed(maker)
    first = maker()
    second = maker()
    try:
        preview = await _preview(
            first,
            fixture,
            target_role="reviewer",
            replacement_annotator_id=fixture["annotator_receiver"],
        )

        async def _apply(session: AsyncSession):
            return await _change(
                session,
                fixture,
                target_role="reviewer",
                expected_version=1,
                preview_token=preview["preview_token"],
                replacement_annotator_id=fixture["annotator_receiver"],
            )

        # Start both; whichever acquires the membership lock first wins and the
        # other resolves to a busy/stale conflict rather than a lost update.
        results = await asyncio.gather(
            _apply(first), _apply(second), return_exceptions=True
        )
        succeeded = [r for r in results if isinstance(r, ProjectMember)]
        failed = [r for r in results if isinstance(r, Exception)]
        assert len(succeeded) == 1, results
        assert len(failed) == 1, results
        assert getattr(failed[0], "status_code", None) == 409

        async with maker() as check:
            member = await check.get(ProjectMember, fixture["member_row"])
            assert member.version == 2
            assert member.role == "reviewer"
    finally:
        await first.close()
        await second.close()
        await _drop(maker, fixture)


# ---------------------------------------------------------------------------
# Follow-up review regressions
# ---------------------------------------------------------------------------


async def test_pending_only_inherited_reviewer_batch_handoff(test_engine):
    """A batch reviewer default used by pending work is handed off and reassigned."""

    maker = _maker(test_engine)
    fixture = await _seed(maker, annotator_role="reviewer", with_annotation_work=False)
    try:
        async with maker() as seeding:
            batch = await seeding.get(TaskBatch, fixture["batch"])
            batch.reviewer_id = fixture["member_user"]
            batch.assigned_user_ids = [str(fixture["member_user"])]
            pending = await create_task(
                seeding, project_id=fixture["project"], status="pending"
            )
            # No reviewer override: the task inherits the batch default.
            pending.batch_id = fixture["batch"]
            await seeding.commit()
            pending_id = pending.id

        session = maker()
        try:
            preview = await _preview(
                session,
                fixture,
                target_role="annotator",
                replacement_reviewer_id=fixture["reviewer_receiver"],
            )
            assert preview["blockers"] == [], preview["blockers"]
            assert (
                str(fixture["batch"])
                in preview["resource_snapshot"]["batch_reviewer_ids"]
            )
            changed = await _change(
                session,
                fixture,
                target_role="annotator",
                expected_version=1,
                preview_token=preview["preview_token"],
                replacement_reviewer_id=fixture["reviewer_receiver"],
            )
            assert changed.role == "annotator"
        finally:
            await session.close()

        async with maker() as check:
            batch = await check.get(TaskBatch, fixture["batch"])
            assert batch.reviewer_id == fixture["reviewer_receiver"]
            assert str(fixture["reviewer_receiver"]) in batch.assigned_user_ids
            assert str(fixture["member_user"]) not in batch.assigned_user_ids
            pending_task = await check.get(Task, pending_id)
            # Pending work keeps inheriting the (now reassigned) default.
            assert pending_task.reviewer_id is None
    finally:
        await _drop(maker, fixture)


async def test_pending_inherited_reviewer_batch_conflicts_on_contributor(test_engine):
    """A pending task inheriting the batch reviewer default disqualifies a
    receiver who contributed to it — reviewer responsibility covers pre-review
    work, not only tasks already in review."""

    maker = _maker(test_engine)
    fixture = await _seed(maker, annotator_role="reviewer", with_annotation_work=False)
    try:
        async with maker() as seeding:
            batch = await seeding.get(TaskBatch, fixture["batch"])
            batch.reviewer_id = fixture["member_user"]
            pending = await create_task(
                seeding, project_id=fixture["project"], status="pending"
            )
            pending.batch_id = fixture["batch"]
            # The proposed receiver annotated this pending work earlier.
            pending.annotation_contributor_ids = [str(fixture["reviewer_receiver"])]
            await seeding.commit()

        session = maker()
        try:
            preview = await _preview(
                session,
                fixture,
                target_role="annotator",
                replacement_reviewer_id=fixture["reviewer_receiver"],
            )
            assert "replacement_reviewer_contributor" in preview["blockers"]
        finally:
            await session.close()
    finally:
        await _drop(maker, fixture)


async def test_lock_bounding_ignores_terminal_history_and_expired_locks(test_engine):
    """Only unfinished dependencies and live locks are locked/released."""

    maker = _maker(test_engine)
    fixture = await _seed(maker)
    holder = maker()
    try:
        async with maker() as seeding:
            terminal = await create_task(
                seeding, project_id=fixture["project"], status="completed"
            )
            terminal.batch_id = fixture["batch"]
            terminal.assignee_id = fixture["member_user"]
            terminal.assignee_is_override = False
            seeding.add(
                TaskLock(
                    task_id=terminal.id,
                    user_id=fixture["member_user"],
                    expire_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
                )
            )
            await seeding.commit()
            terminal_id = terminal.id

        # An external writer holds terminal history; the bounded mutation must
        # not need that row.
        await holder.scalar(
            select(Task).where(Task.id == terminal_id).with_for_update()
        )

        session = maker()
        try:
            preview = await _preview(
                session,
                fixture,
                target_role="reviewer",
                replacement_annotator_id=fixture["annotator_receiver"],
            )
            assert "active_locks" not in preview["blockers"]
            changed = await _change(
                session,
                fixture,
                target_role="reviewer",
                expected_version=1,
                preview_token=preview["preview_token"],
                replacement_annotator_id=fixture["annotator_receiver"],
            )
            assert changed.version == 2
        finally:
            await session.close()
        await holder.rollback()

        async with maker() as check:
            history = await check.get(Task, terminal_id)
            assert history.assignee_id == fixture["member_user"]
            expired = await check.scalar(
                select(TaskLock).where(TaskLock.task_id == terminal_id)
            )
            assert expired is not None
    finally:
        await holder.close()
        await _drop(maker, fixture)


async def test_stale_actor_object_cannot_authorize_after_deactivation(test_engine):
    """A stale in-memory actor cannot authorize once the locked row is inactive."""

    maker = _maker(test_engine)
    fixture = await _seed(maker, with_annotation_work=False)
    try:
        stale_session = maker()
        try:
            stale_actor = await stale_session.get(User, fixture["owner"])
            assert stale_actor.is_active is True
            async with maker() as deactivate:
                owner = await deactivate.get(User, fixture["owner"])
                owner.is_active = False
                await deactivate.commit()

            session = maker()
            try:
                project = await session.get(Project, fixture["project"])
                with pytest.raises(Exception) as caught:
                    await change_role(
                        session,
                        project=project,
                        actor=stale_actor,
                        member_id=fixture["member_row"],
                        target_role="reviewer",
                        expected_version=1,
                        preview_token="irrelevant",
                        reason="stale actor",
                        replacement_annotator_id=None,
                        replacement_reviewer_id=None,
                    )
                assert getattr(caught.value, "status_code", None) == 401
            finally:
                await session.close()
        finally:
            await stale_session.close()
    finally:
        await _drop(maker, fixture)


async def test_preview_and_apply_agree_on_target_platform_incompatibility(
    test_engine,
):
    """A platform viewer cannot be promoted and preview reports it as a blocker."""

    maker = _maker(test_engine)
    fixture = await _seed(maker, with_annotation_work=False)
    viewer_id = None
    try:
        async with maker() as seeding:
            viewer = await create_user(
                seeding,
                "viewer",
                f"align-view-{uuid.uuid4().hex[:8]}@test.local",
                "View",
            )
            viewer_member = ProjectMember(
                project_id=fixture["project"],
                user_id=viewer.id,
                role="viewer",
                assigned_by=fixture["owner"],
            )
            seeding.add(viewer_member)
            await seeding.commit()
            viewer_id = viewer.id
            viewer_member_id = viewer_member.id

        session = maker()
        try:
            project = await session.get(Project, fixture["project"])
            actor = await session.get(User, fixture["admin"])
            preview = await preview_role_change(
                session,
                project=project,
                actor=actor,
                member_id=viewer_member_id,
                target_role="annotator",
                replacement_annotator_id=None,
                replacement_reviewer_id=None,
            )
            assert "target_role_incompatible" in preview["blockers"]

            with pytest.raises(Exception) as caught:
                await change_role(
                    session,
                    project=project,
                    actor=actor,
                    member_id=viewer_member_id,
                    target_role="annotator",
                    expected_version=preview["current_version"],
                    preview_token=preview["preview_token"],
                    reason="align",
                    replacement_annotator_id=None,
                    replacement_reviewer_id=None,
                )
            assert getattr(caught.value, "status_code", None) == 409
            assert "target_role_incompatible" in [
                entry["code"] for entry in caught.value.detail["blockers"]
            ]
        finally:
            await session.close()
    finally:
        await _drop(maker, fixture, extra_user_ids=(viewer_id,) if viewer_id else ())


async def test_materialized_history_is_prelocked_with_nowait(test_engine):
    """A NULL-inheriting terminal row is a locked write target, not free history."""

    maker = _maker(test_engine)
    fixture = await _seed(maker)
    holder = maker()
    try:
        async with maker() as seeding:
            null_history = await create_task(
                seeding, project_id=fixture["project"], status="completed"
            )
            null_history.batch_id = fixture["batch"]
            # assignee_id stays NULL: the effective assignee is the batch default.
            await seeding.commit()
            null_history_id = null_history.id

        await holder.scalar(
            select(Task).where(Task.id == null_history_id).with_for_update()
        )
        session = maker()
        try:
            preview = await _preview(
                session,
                fixture,
                target_role="reviewer",
                replacement_annotator_id=fixture["annotator_receiver"],
            )
            assert (
                str(null_history_id)
                in preview["resource_snapshot"]["materialization_annotation_task_ids"]
            )
            with pytest.raises(Exception) as caught:
                await _change(
                    session,
                    fixture,
                    target_role="reviewer",
                    expected_version=1,
                    preview_token=preview["preview_token"],
                    replacement_annotator_id=fixture["annotator_receiver"],
                )
            assert getattr(caught.value, "status_code", None) == 409
            assert caught.value.detail["reason"] == "resource_busy"
            assert not session.in_transaction()
        finally:
            await session.close()
        await holder.rollback()

        session = maker()
        try:
            preview = await _preview(
                session,
                fixture,
                target_role="reviewer",
                replacement_annotator_id=fixture["annotator_receiver"],
            )
            changed = await _change(
                session,
                fixture,
                target_role="reviewer",
                expected_version=1,
                preview_token=preview["preview_token"],
                replacement_annotator_id=fixture["annotator_receiver"],
            )
            assert changed.version == 2
        finally:
            await session.close()

        async with maker() as check:
            materialized = await check.get(Task, null_history_id)
            assert materialized.assignee_id == fixture["member_user"]
            assert materialized.assignee_is_override is False
    finally:
        await holder.close()
        await _drop(maker, fixture)
