from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

pytestmark = pytest.mark.asyncio


async def _create_project(test_engine) -> tuple[UUID, tuple[UUID, UUID]]:
    from app.db.models.project import Project
    from app.db.models.user import User

    owner_id, successor_id = uuid4(), uuid4()
    project_id = uuid4()
    suffix = uuid4().hex
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as session:
        session.add_all(
            [
                User(
                    id=owner_id,
                    email=f"workspace-owner-{suffix}@example.test",
                    name="Workspace owner",
                    password_hash="unused-test-hash",
                ),
                User(
                    id=successor_id,
                    email=f"workspace-successor-{suffix}@example.test",
                    name="Workspace successor",
                    password_hash="unused-test-hash",
                ),
            ]
        )
        await session.flush()
        session.add(
            Project(
                id=project_id,
                display_id=f"ws-{suffix[:16]}",
                name="Workspace lock fixture",
                type_label="Image",
                type_key="image",
                owner_id=owner_id,
            )
        )
        await session.commit()
    return project_id, (owner_id, successor_id)


async def _delete_project_fixture(
    test_engine, project_id: UUID, user_ids: tuple[UUID, UUID]
):
    from app.db.models.project import Project
    from app.db.models.user import User

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as session:
        project = await session.get(Project, project_id)
        if project is not None:
            await session.delete(project)
        for user_id in user_ids:
            user = await session.get(User, user_id)
            if user is not None:
                await session.delete(user)


async def _lock_project_reference(session, project_id: UUID | None) -> UUID | None:
    return await session.scalar(
        text("SELECT aap_workspace_v1.lock_project_reference(:project_id)"),
        {"project_id": project_id},
    )


def _sqlstate(error: DBAPIError) -> str | None:
    pending = [error.orig]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        state = getattr(current, "sqlstate", None)
        if state:
            return state
        for nested in (
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
        ):
            if nested is not None:
                pending.append(nested)
    return None


async def test_project_reference_returns_identity_or_null_for_missing_row(test_engine):
    project_id, user_ids = await _create_project(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    try:
        async with maker() as session:
            assert await _lock_project_reference(session, project_id) == project_id
            assert await _lock_project_reference(session, uuid4()) is None
            assert await _lock_project_reference(session, None) is None
    finally:
        await _delete_project_fixture(test_engine, project_id, user_ids)


@pytest.mark.parametrize("finish_transaction", ["commit", "rollback"])
async def test_project_reference_blocks_exclusive_project_lock_and_delete_until_transaction_end(
    test_engine, finish_transaction: str
):
    project_id, user_ids = await _create_project(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    locker, contender = maker(), maker()
    try:
        await locker.begin()
        assert await _lock_project_reference(locker, project_id) == project_id

        await contender.begin()
        with pytest.raises(DBAPIError) as owner_change:
            await contender.execute(
                text(
                    "SELECT id FROM public.projects "
                    "WHERE id = :project_id FOR UPDATE NOWAIT"
                ),
                {"project_id": project_id},
            )
        assert _sqlstate(owner_change.value) == "55P03"
        await contender.rollback()

        await contender.begin()
        await contender.execute(text("SET LOCAL lock_timeout = '250ms'"))
        with pytest.raises(DBAPIError) as deletion:
            await contender.execute(
                text("DELETE FROM public.projects WHERE id = :project_id"),
                {"project_id": project_id},
            )
        assert _sqlstate(deletion.value) == "55P03"
        await contender.rollback()

        if finish_transaction == "commit":
            await locker.commit()
        else:
            await locker.rollback()

        await contender.begin()
        assert (
            await contender.scalar(
                text(
                    "SELECT id FROM public.projects "
                    "WHERE id = :project_id FOR UPDATE NOWAIT"
                ),
                {"project_id": project_id},
            )
            == project_id
        )
        await contender.rollback()

        await contender.begin()
        await contender.execute(
            text("DELETE FROM public.projects WHERE id = :project_id"),
            {"project_id": project_id},
        )
        await contender.commit()
    finally:
        await locker.close()
        await contender.close()
        await _delete_project_fixture(test_engine, project_id, user_ids)


async def test_project_counter_update_is_compatible_with_key_share(test_engine):
    project_id, user_ids = await _create_project(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    locker, updater = maker(), maker()
    try:
        await locker.begin()
        assert await _lock_project_reference(locker, project_id) == project_id

        await updater.begin()
        await updater.execute(text("SET LOCAL lock_timeout = '500ms'"))
        result = await updater.scalar(
            text(
                "UPDATE public.projects "
                "SET total_tasks = total_tasks + 1 "
                "WHERE id = :project_id RETURNING total_tasks"
            ),
            {"project_id": project_id},
        )
        assert result == 1
        await updater.commit()
        await locker.commit()

        async with maker() as verify:
            assert (
                await verify.scalar(
                    text(
                        "SELECT total_tasks FROM public.projects WHERE id = :project_id"
                    ),
                    {"project_id": project_id},
                )
                == 1
            )
    finally:
        await locker.close()
        await updater.close()
        await _delete_project_fixture(test_engine, project_id, user_ids)


async def test_project_reference_ignores_temporary_projects_table(test_engine):
    project_id, user_ids = await _create_project(test_engine)
    fake_project_id = uuid4()
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            await session.execute(
                text("CREATE TEMP TABLE projects (id uuid PRIMARY KEY) ON COMMIT DROP")
            )
            await session.execute(
                text("INSERT INTO pg_temp.projects (id) VALUES (:project_id)"),
                {"project_id": fake_project_id},
            )
            assert await _lock_project_reference(session, project_id) == project_id
            assert await _lock_project_reference(session, fake_project_id) is None
    finally:
        await _delete_project_fixture(test_engine, project_id, user_ids)
