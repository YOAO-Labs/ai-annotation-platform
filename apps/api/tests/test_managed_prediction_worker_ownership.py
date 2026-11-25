"""Real transaction boundaries for managed worker fixture cleanup."""

from contextlib import nullcontext
import uuid

import pytest
from sqlalchemy import text
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
from tests.test_prediction_inputs import input_storage_world


MODELS = (User, Project, Dataset, MLBackendRegistry)
DATABASE_OID = text(
    "SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()"
)


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


async def test_failed_managed_database_does_not_seed_the_next_case(managed_database):
    user_id = uuid.uuid4()
    with pytest.raises(_ExitWorld):
        async with managed_database() as first:
            sessions = async_sessionmaker(first)
            async with sessions.begin() as db:
                first_oid = await db.scalar(DATABASE_OID)
                db.add(
                    User(
                        id=user_id,
                        email=f"{user_id}@example.test",
                        name="Failed case",
                        password_hash="test",
                    )
                )
            async with sessions() as observer:
                assert await observer.get(User, user_id) is not None
            raise _ExitWorld()

    async with managed_database() as second, async_sessionmaker(second)() as db:
        assert await db.scalar(DATABASE_OID) != first_oid
        assert await db.get(User, user_id) is None
        assert not await db.scalar(
            text("SELECT EXISTS (SELECT FROM pg_catalog.pg_database WHERE oid=:old)"),
            {"old": first_oid},
        )


@pytest.mark.parametrize(
    "boundary", ("root", "savepoint", "released_savepoint", "close", "failed_commit")
)
@pytest.mark.parametrize("exceptional", (False, True), ids=("normal", "exceptional"))
async def test_managed_worker_cleanup_respects_transaction_ownership(
    input_world, managed_database, monkeypatch, boundary, exceptional
):
    """A case can commit, roll back and reuse IDs without changing a neighbor."""
    neighbor = input_world.factory
    owner_id = uuid.uuid4()
    preexisting, previous, rolled_back = _ids(), _ids(), _ids()
    outer_user = uuid.uuid4()

    async def seed(factory):
        async with factory.begin() as db:
            db.add(
                User(
                    id=owner_id,
                    email=f"{owner_id}@example.test",
                    name="owner",
                    password_hash="test",
                )
            )
            await db.flush()
            db.add_all(_rows(preexisting, owner_id, "preexisting"))
            return await db.scalar(DATABASE_OID)

    neighbor_oid = await seed(neighbor)
    async with neighbor.begin() as db:
        db.add_all(_rows(rolled_back, owner_id, "neighbor"))

    expected = pytest.raises(_ExitWorld) if exceptional else nullcontext()
    with expected, monkeypatch.context() as patch:
        async with managed_database() as engine:
            async with input_storage_world(engine, patch) as inputs:
                world = managed_worker_world.__wrapped__(inputs, patch)
                case_oid = await seed(world.factory)
                assert case_oid != neighbor_oid
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

                    async with world.factory.begin() as independent:
                        independent.add_all(_rows(rolled_back, owner_id, "independent"))
                    if boundary == "savepoint":
                        await db.commit()

                async with world.factory() as observer:
                    for model in MODELS:
                        for ids, label in (
                            (previous, "previously committed"),
                            (preexisting, "modified preexisting"),
                            (rolled_back, "independent"),
                        ):
                            assert (await observer.get(model, ids[model])).name == label
                    kept_outer = await observer.get(User, outer_user)
                    assert (kept_outer is not None) == (boundary == "savepoint")
                if exceptional:
                    raise _ExitWorld()

    async with neighbor() as observer:
        assert not await observer.scalar(
            text("SELECT EXISTS (SELECT FROM pg_catalog.pg_database WHERE oid=:old)"),
            {"old": case_oid},
        )
        for model in MODELS:
            assert await observer.get(model, previous[model]) is None
            assert (await observer.get(model, preexisting[model])).name == "preexisting"
            assert (await observer.get(model, rolled_back[model])).name == "neighbor"
        assert await observer.get(User, outer_user) is None
