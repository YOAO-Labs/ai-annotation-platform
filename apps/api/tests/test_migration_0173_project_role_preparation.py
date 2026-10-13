"""Verify the 0173 additive preparation on a disposable database.

The preparation must stay additive and default-safe: legacy binaries keep
reading and writing the tables, historical unknowns stay NULL, and the same
historical row survives a 0173 upgrade/downgrade/upgrade round trip.

Revision 0174 is not reversible, so the round trip replays *only* the 0173
migration's own ``upgrade``/``downgrade`` Operations inside a transaction that
is rolled back at the end.  It never calls the production 0174 downgrade and
never infers role data; 0174 conversion is covered separately.
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.project_member import ProjectMember
from app.db.models.user_invitation import UserInvitation
from tests.factory import create_project, create_task, create_user

_API_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_0173 = (
    _API_ROOT / "alembic" / "versions" / "0173_project_role_preparation.py"
)

_PREPARATION_COLUMNS = {
    "project_members": {"version", "updated_at"},
    "user_invitations": {"project_role"},
    "tasks": {
        "annotation_contributor_ids",
        "review_contributor_ids",
        "review_submitter_id",
    },
}


def _load_migration(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _replay(sync_conn, migration, direction: str) -> None:
    context = MigrationContext.configure(sync_conn)
    with Operations.context(context):
        getattr(migration, direction)()


def _columns(sync_conn, table: str) -> set[str]:
    rows = sync_conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :table"
        ),
        {"table": table},
    )
    return {name for (name,) in rows.all()}


def _assert_downgraded(sync_conn, saved: dict) -> None:
    for table, columns in _PREPARATION_COLUMNS.items():
        present = _columns(sync_conn, table)
        assert present.isdisjoint(columns), (
            f"{table} still has preparation columns after 0173 downgrade"
        )
    count = sync_conn.execute(
        text("SELECT count(*) FROM project_members WHERE id = :id"),
        {"id": saved["member"]},
    ).scalar()
    assert count == 1


def _assert_upgraded(sync_conn, saved: dict) -> None:
    for table, columns in _PREPARATION_COLUMNS.items():
        present = _columns(sync_conn, table)
        assert columns.issubset(present), f"{table} missing preparation columns"

    task_evidence = sync_conn.execute(
        text(
            "SELECT annotation_contributor_ids, review_contributor_ids, "
            "review_submitter_id FROM tasks WHERE id = :id"
        ),
        {"id": saved["task"]},
    ).one()
    assert task_evidence == (None, None, None)

    task_column_default = sync_conn.execute(
        text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'tasks' AND column_name = 'annotation_contributor_ids'"
        )
    ).scalar()
    assert task_column_default is None

    version, updated_at = sync_conn.execute(
        text("SELECT version, updated_at FROM project_members WHERE id = :id"),
        {"id": saved["member"]},
    ).one()
    assert version == 1
    assert updated_at is not None

    # The 0173-only round trip leaves the invitation as it was seeded: the
    # project_role column is empty and the original platform role survives.
    invitation = sync_conn.execute(
        text("SELECT role, project_role FROM user_invitations WHERE id = :id"),
        {"id": saved["invitation"]},
    ).one()
    assert invitation.role == "annotator"
    assert invitation.project_role is None

    # Legacy writer: an INSERT that omits the new columns must still succeed,
    # with server defaults applied (NULL stays unknown, not known-empty).
    legacy_member_id = uuid.uuid4()
    sync_conn.execute(
        text(
            "INSERT INTO project_members "
            "(id, project_id, user_id, role, assigned_by) "
            "VALUES (:id, :project_id, :user_id, 'annotator', :assigned_by)"
        ),
        {
            "id": legacy_member_id,
            "project_id": saved["project"],
            "user_id": saved["legacy_user"],
            "assigned_by": saved["owner"],
        },
    )
    version, updated_at = sync_conn.execute(
        text("SELECT version, updated_at FROM project_members WHERE id = :id"),
        {"id": legacy_member_id},
    ).one()
    assert version == 1
    assert updated_at is not None

    constraint = sync_conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conname = 'fk_tasks_review_submitter_id_users'"
        )
    ).scalar()
    assert constraint == 1


def test_0173_roundtrip_is_additive_and_preserves_legacy_rows(
    test_db_url, apply_migrations
):
    migration = _load_migration(_MIGRATION_0173, "migration_0173")
    saved: dict = {}

    async def seed() -> None:
        engine = create_async_engine(test_db_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                owner = await create_user(
                    db,
                    "project_admin",
                    f"migration-owner-{uuid.uuid4()}@test.local",
                    "Owner",
                )
                saved["owner"] = owner.id
                legacy_user = await create_user(
                    db,
                    "annotator",
                    f"migration-legacy-{uuid.uuid4()}@test.local",
                    "Legacy",
                )
                saved["legacy_user"] = legacy_user.id
                project = await create_project(db, owner_id=owner.id)
                saved["project"] = project.id
                member = ProjectMember(
                    project_id=project.id,
                    user_id=owner.id,
                    role="viewer",
                    assigned_by=owner.id,
                )
                db.add(member)
                await db.flush()
                saved["member"] = member.id
                task = await create_task(db, project_id=project.id, status="review")
                task.review_round_id = uuid.uuid4()
                await db.flush()
                saved["task"] = task.id
                invitation = UserInvitation(
                    email=f"migration-invite-{uuid.uuid4()}@test.local",
                    role="annotator",
                    project_id=project.id,
                    project_role=None,
                    token=uuid.uuid4().hex,
                    expires_at=datetime.now(timezone.utc) + timedelta(days=3),
                    invited_by=owner.id,
                )
                db.add(invitation)
                await db.flush()
                saved["invitation"] = invitation.id
                await db.commit()
        finally:
            await engine.dispose()

    async def roundtrip() -> None:
        engine = create_async_engine(test_db_url)
        try:
            async with engine.connect() as conn:
                trans = await conn.begin()
                await conn.run_sync(_replay, migration, "downgrade")
                await conn.run_sync(_assert_downgraded, saved)
                await conn.run_sync(_replay, migration, "upgrade")
                await conn.run_sync(_assert_upgraded, saved)
                # Roll back so the shared test schema and rows are untouched.
                await trans.rollback()
        finally:
            await engine.dispose()

    async def cleanup() -> None:
        engine = create_async_engine(test_db_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                await db.execute(
                    text("DELETE FROM project_members WHERE project_id = :id"),
                    {"id": saved["project"]},
                )
                await db.execute(
                    text("DELETE FROM tasks WHERE id = :id"), {"id": saved["task"]}
                )
                await db.execute(
                    text("DELETE FROM user_invitations WHERE id = :id"),
                    {"id": saved["invitation"]},
                )
                await db.execute(
                    text("DELETE FROM projects WHERE id = :id"),
                    {"id": saved["project"]},
                )
                await db.execute(
                    text("DELETE FROM users WHERE id = ANY(:ids)"),
                    {"ids": [saved["owner"], saved["legacy_user"]]},
                )
                await db.commit()
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed())
        asyncio.run(roundtrip())
    finally:
        asyncio.run(cleanup())
