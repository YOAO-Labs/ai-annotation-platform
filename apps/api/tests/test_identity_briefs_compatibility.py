"""Native profiles retain captured responses and the caller's uncommitted work."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.project import Project
from app.db.models.user import User
from app.services import identity_briefs_commands as commands_module
from app.services import user_brief
from app.services.identity_gateway import IdentityGateway
from tests.factory import create_membership, create_project, create_user

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/identity_briefs_legacy.json").read_text()
)


@pytest.fixture(autouse=True)
def embedded_selector(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "identity_briefs_database_url", "")
    monkeypatch.setattr(
        commands_module.settings, "identity_briefs_database_url_file", ""
    )
    monkeypatch.setattr(user_brief, "get_identity_gateway", lambda: IdentityGateway())


async def test_native_briefs_match_actual_legacy_response_and_use_one_batch(db_session):
    for item in _FIXTURE["inputs"]:
        db_session.add(
            User(
                id=uuid.UUID(item["id"]),
                name=item["name"],
                email=item["email"],
                role=item["role"],
                is_active=item["is_active"],
                avatar_ref=item["avatar_ref"],
                password_hash="baseline-no-credential",
            )
        )
    await db_session.flush()
    by_key = {item["key"]: uuid.UUID(item["id"]) for item in _FIXTURE["inputs"]}
    project = await create_project(
        db_session, owner_id=by_key["owner"], name="Brief compatibility"
    )
    project.id = uuid.UUID(_FIXTURE["project_id"])
    await db_session.flush()
    for key, role in (
        ("staff", "reviewer"),
        ("historical", "annotator"),
        ("inactive", ""),
    ):
        await create_membership(
            db_session, project_id=project.id, user_id=by_key[key], role=role
        )
    await db_session.flush()
    ids = [str(value).upper() for value in by_key.values()] + [
        None,
        by_key["staff"],
        uuid.uuid4(),
    ]
    queries = []

    def count(*_args):
        queries.append(1)

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", count)
    try:
        assert await user_brief.resolve_briefs(db_session, [None]) == {}
        assert queries == []
        for phase, call in (
            ("global", lambda: user_brief.resolve_briefs(db_session, iter(ids))),
            (
                "project",
                lambda: user_brief.resolve_briefs_with_project_role(
                    db_session, project.id, iter(ids)
                ),
            ),
            (
                "large",
                lambda: user_brief.resolve_briefs(
                    db_session, [by_key["staff"]] + [uuid.uuid4() for _ in range(1200)]
                ),
            ),
        ):
            before = len(queries)
            result = await call()
            assert {
                key: value.model_dump(mode="json") for key, value in result.items()
            } == _FIXTURE["responses"][phase]
            assert len(queries) - before == 1
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", count)


@pytest.mark.parametrize("autoflush", [True, False])
async def test_brief_read_never_commits_or_rolls_back_caller_edits(
    test_engine, autoflush
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    async with maker() as db:
        user = await create_user(
            db, "employee", suffix + "@briefs-transaction.test", "Original"
        )
        project = await create_project(db, owner_id=user.id, name="Original project")
        user_id, project_id = user.id, project.id
        await db.commit()
        project.name = "Caller pending project"
        if autoflush:
            user.name = "Caller pending user"
        flushes = []

        def flushed(*_args):
            flushes.append(1)

        event.listen(db.sync_session, "before_flush", flushed)
        try:
            if autoflush:
                result = await user_brief.resolve_briefs(db, [user_id])
            else:
                with db.no_autoflush:
                    result = await user_brief.resolve_briefs(db, [user_id])
            assert result[str(user_id)].name == (
                "Caller pending user" if autoflush else "Original"
            )
            assert len(flushes) == int(autoflush)
            assert project.name == "Caller pending project"
            assert db.in_transaction()
            async with maker() as observer:
                assert (
                    await observer.scalar(select(User.name).where(User.id == user_id))
                    == "Original"
                )
                assert (
                    await observer.scalar(
                        select(Project.name).where(Project.id == project_id)
                    )
                    == "Original project"
                )
            await db.rollback()
            async with maker() as observer:
                assert (
                    await observer.scalar(select(User.name).where(User.id == user_id))
                    == "Original"
                )
                assert (
                    await observer.scalar(
                        select(Project.name).where(Project.id == project_id)
                    )
                    == "Original project"
                )
        finally:
            event.remove(db.sync_session, "before_flush", flushed)
