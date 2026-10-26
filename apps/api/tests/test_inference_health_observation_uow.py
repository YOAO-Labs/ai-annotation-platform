"""Caller transaction semantics of the sealed health observation owner."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.user import User
from app.services.ml_backend import MLBackendService
from tests.inference_health_observation_helpers import backend_server, select_binding
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _owner_dsn(test_engine) -> str:
    value = test_engine.url.set(drivername="postgresql")
    assert value.database and value.database.endswith(("_test", "_e2e"))
    return value.render_as_string(hide_password=False)


async def _rotate_endpoint(dsn, registry_id, auth_token) -> None:
    connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
    try:
        await connection.execute(
            "UPDATE public.ml_backend_registry SET auth_token=$2 WHERE id=$1",
            registry_id,
            auth_token,
        )
    finally:
        await connection.close()


@pytest.mark.parametrize("autoflush", [False, True])
async def test_pending_endpoint_values_are_observed_only_when_the_caller_autoflushes(
    test_engine, monkeypatch, autoflush
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        await select_binding(stack, monkeypatch, test_engine)
        with backend_server(setup_failed=True) as (origin, calls):
            async with maker(autoflush=autoflush) as caller:
                row = MLBackendRegistry(
                    id=uuid.uuid4(),
                    name="Pending health",
                    url=origin + "/stored",
                    auth_method="token",
                    auth_token="stored-token",
                    extra_params={},
                )
                caller.add(row)
                await caller.flush()
                row.url = origin + "/pending"
                row.auth_token = "pending-token"
                pending = User(
                    id=uuid.uuid4(),
                    email=f"health-uow-{uuid.uuid4().hex}@example.test",
                    name="Pending",
                    password_hash="fixture",
                    role="employee",
                )
                caller.add(pending)

                assert await MLBackendService(caller).check_health(row.id) is True

                observed = "/pending" if autoflush else "/stored"
                observed_token = "pending-token" if autoflush else "stored-token"
                assert calls == [
                    (observed + "/health", "Bearer " + observed_token),
                    (observed + "/setup", "Bearer " + observed_token),
                ]
                assert await caller.get(MLBackendRegistry, row.id) is row
                # Caller edits stay in the original unit of work either way.
                assert row.url == origin + "/pending"
                assert row.auth_token == "pending-token"
                assert row.last_checked_at is not None
                assert (pending in caller.new) is (not autoflush)
                async with maker() as outside:
                    assert await outside.get(MLBackendRegistry, row.id) is None
                    assert await outside.get(User, pending.id) is None
                assert caller.in_transaction()
                await caller.rollback()


@pytest.mark.parametrize("outcome", ["caps", "setup_failed", "plain"])
async def test_observation_does_not_commit_and_only_caps_flush_unrelated_uow(
    test_engine, monkeypatch, outcome
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        await select_binding(stack, monkeypatch, test_engine)
        with backend_server(
            setup_failed=outcome == "setup_failed", plain=outcome == "plain"
        ) as (origin, calls):
            async with maker(autoflush=False) as caller:
                row = MLBackendRegistry(
                    id=uuid.uuid4(),
                    name="Health transaction",
                    url=origin,
                    auth_method="none",
                    extra_params={},
                )
                caller.add(row)
                await caller.flush()
                pending = User(
                    id=uuid.uuid4(),
                    email=f"health-caps-{uuid.uuid4().hex}@example.test",
                    name="Pending",
                    password_hash="fixture",
                    role="employee",
                )
                caller.add(pending)
                assert await MLBackendService(caller).check_health(row.id) is True
                assert row.last_checked_at is not None
                assert (pending in caller.new) is (outcome != "caps")
                assert len(calls) == (1 if outcome == "plain" else 2)
                async with maker() as outside:
                    assert await outside.get(MLBackendRegistry, row.id) is None
                    assert await outside.get(User, pending.id) is None
                await caller.rollback()
                async with maker() as outside:
                    assert await outside.get(MLBackendRegistry, row.id) is None


async def test_loaded_registry_uuid_crosses_closed_wire_and_refreshes_same_instance(
    test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        await select_binding(stack, monkeypatch, test_engine)
        with backend_server() as (origin, calls):
            registry_id = uuid.uuid4()
            async with maker.begin() as seed:
                seed.add(
                    MLBackendRegistry(
                        id=registry_id, name="Loaded registry", url=origin
                    )
                )
            try:
                async with maker() as caller:
                    row = await caller.get(MLBackendRegistry, registry_id)
                    assert await MLBackendService(caller).check_health(row.id) is True
                    assert await caller.get(MLBackendRegistry, registry_id) is row
                    assert row.last_checked_at is not None
                    assert len(calls) == 2
                    async with maker() as outside:
                        assert (
                            await outside.get(MLBackendRegistry, registry_id)
                        ).last_checked_at is None
                    await caller.rollback()
            finally:
                async with maker.begin() as cleanup:
                    await cleanup.execute(
                        text("DELETE FROM public.ml_backend_registry WHERE id=:id"),
                        {"id": registry_id},
                    )


async def test_rotated_endpoint_after_observation_drops_the_stale_write(
    test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        await select_binding(stack, monkeypatch, test_engine)
        registry_id = uuid.uuid4()
        rotation_errors = []

        def rotate():
            try:
                asyncio.run(
                    _rotate_endpoint(
                        _owner_dsn(test_engine), registry_id, "rotated-token"
                    )
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                rotation_errors.append(error)

        with backend_server(on_health=rotate) as (origin, calls):
            async with maker.begin() as seed:
                seed.add(
                    MLBackendRegistry(
                        id=registry_id,
                        name="Rotated health",
                        url=origin,
                        auth_method="token",
                        auth_token="original-token",
                    )
                )
            try:
                async with maker() as caller:
                    assert (
                        await MLBackendService(caller).check_health(registry_id)
                        is False
                    )
                    assert not rotation_errors
                    # The observation still used the sealed pre-rotation snapshot.
                    assert calls == [
                        ("/health", "Bearer original-token"),
                        ("/setup", "Bearer original-token"),
                    ]
                    row = (
                        await caller.execute(
                            text(
                                "SELECT last_checked_at, health_meta "
                                "FROM public.ml_backend_registry WHERE id=:id"
                            ),
                            {"id": registry_id},
                        )
                    ).one()
                    assert row.last_checked_at is None
                    assert row.health_meta is None
                    assert caller.in_transaction()
                    await caller.rollback()
            finally:
                async with maker.begin() as cleanup:
                    await cleanup.execute(
                        text("DELETE FROM public.ml_backend_registry WHERE id=:id"),
                        {"id": registry_id},
                    )
