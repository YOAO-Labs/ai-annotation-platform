"""Real transaction boundaries for managed worker fixture cleanup."""

from contextlib import asynccontextmanager
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.dataset import Dataset
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.project import Project
from app.db.models.user import User
from tests.managed_prediction_worker_fixtures import (
    input_world as input_world,
    managed_worker_world,
)


MODELS = (User, Project, Dataset, MLBackendRegistry)


def _rows(ids, owner_id, label):
    token = uuid.uuid4().hex
    return (
        User(
            id=ids[User],
            email=f"{token}@example.test",
            name=label,
            password_hash="test",
        ),
        Project(
            id=ids[Project],
            display_id=f"p-{token[:16]}",
            name=label,
            type_label="Image",
            type_key="image-det",
            owner_id=owner_id,
        ),
        Dataset(
            id=ids[Dataset],
            display_id=f"d-{token[:16]}",
            name=label,
            created_by=owner_id,
        ),
        MLBackendRegistry(
            id=ids[MLBackendRegistry],
            name=label,
            url=f"http://{token}.example.test",
        ),
    )


def _ids():
    return {model: uuid.uuid4() for model in MODELS}


class _ExitWorld(Exception):
    pass


@pytest.mark.parametrize(
    "boundary", ("root", "savepoint", "released_savepoint", "close", "failed_commit")
)
@pytest.mark.parametrize("exceptional", (False, True), ids=("normal", "exceptional"))
async def test_managed_worker_cleanup_respects_transaction_ownership(
    input_world, test_engine, monkeypatch, boundary, exceptional
):
    """A rolled-back ID belongs to its independent committer, not the fixture."""
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    owner_id = uuid.uuid4()
    preexisting, previous, rolled_back = _ids(), _ids(), _ids()
    outer_user = uuid.uuid4()
    all_ids = [preexisting, previous, rolled_back]
    owner_email = f"{uuid.uuid4().hex}@example.test"
    try:
        async with owner() as db:
            db.add(
                User(id=owner_id, email=owner_email, name="owner", password_hash="test")
            )
            await db.flush()
            db.add_all(_rows(preexisting, owner_id, "preexisting"))
            await db.commit()

        fixture = asynccontextmanager(managed_worker_world.__wrapped__)
        try:
            async with fixture(input_world, test_engine, monkeypatch) as world:
                async with world.factory() as db:
                    for model in MODELS:
                        row = await db.get(model, preexisting[model])
                        row.name = "modified preexisting"
                    db.add_all(_rows(previous, owner_id, "previously committed"))
                    await db.commit()

                    if boundary == "savepoint":
                        db.add(
                            User(
                                id=outer_user,
                                email=f"{outer_user.hex}@example.test",
                                name="outer transaction",
                                password_hash="test",
                            )
                        )
                        await db.flush()
                    if boundary in ("savepoint", "released_savepoint"):
                        nested = await db.begin_nested()
                    db.add_all(_rows(rolled_back, owner_id, "rolled back"))
                    await db.flush()
                    if boundary == "savepoint":
                        await nested.rollback()
                    elif boundary == "released_savepoint":
                        await nested.commit()
                        await db.rollback()
                    elif boundary == "close":
                        await db.close()
                    elif boundary == "failed_commit":
                        await db.execute(
                            text(
                                "CREATE TEMP TABLE managed_owner_commit_guard "
                                "(value integer UNIQUE DEFERRABLE INITIALLY DEFERRED) "
                                "ON COMMIT DROP"
                            )
                        )
                        await db.execute(
                            text(
                                "INSERT INTO managed_owner_commit_guard VALUES (1), (1)"
                            )
                        )
                        with pytest.raises(IntegrityError):
                            await db.commit()
                        await db.rollback()
                    else:
                        await db.rollback()

                    async with owner() as neighbor:
                        neighbor.add_all(_rows(rolled_back, owner_id, "independent"))
                        await neighbor.commit()
                    if boundary == "savepoint":
                        await db.commit()

                if exceptional:
                    raise _ExitWorld()
        except _ExitWorld:
            if not exceptional:
                raise

        async with owner() as observer:
            for model in MODELS:
                assert await observer.get(model, previous[model]) is None
                assert (
                    await observer.get(model, preexisting[model])
                ).name == "modified preexisting"
                assert (
                    await observer.get(model, rolled_back[model])
                ).name == "independent"
            if boundary == "savepoint":
                assert await observer.get(User, outer_user) is None
    finally:
        async with owner() as db:
            for model in (Project, Dataset, MLBackendRegistry, User):
                ids = [group[model] for group in all_ids]
                if model is User:
                    ids.extend((owner_id, outer_user))
                await db.execute(delete(model).where(model.id.in_(ids)))
            await db.commit()
