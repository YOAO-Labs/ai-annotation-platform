"""Caller-transaction and credential-free facts for fixed Native health operations."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference import extract_capabilities
from aap_inference.health import reconcile_pool_capability
from aap_inference.pool_capability import (
    canonicalize_capability,
    capability_fingerprint,
)
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.user import User
from app.services.inference_health_data import (
    HealthBackendFact,
    InferenceHealthData,
    _STAGES,
)
from app.services.ml_backend import MLBackendService
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_CAPS = extract_capabilities(
    {"name": "controlled", "model_version": "one", "supported_prompts": ["bbox"]}
)
_NEXT = extract_capabilities(
    {"name": "controlled", "model_version": "two", "supported_prompts": ["bbox"]}
)


async def _seed(db):
    backend = await MLBackendService(db).create_registry(
        "Native health " + uuid.uuid4().hex, "http://" + uuid.uuid4().hex + ".invalid"
    )
    data = InferenceHealthData(db)
    pool_id = await data.read_member_pool(backend.id)
    pool = await data.load_pool(pool_id, registry_id=backend.id)
    return backend, pool, data


@pytest.mark.parametrize("autoflush", [False, True])
async def test_native_reconciliation_defers_to_original_flush_and_rollback(
    test_engine, autoflush
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False, autoflush=autoflush)
    async with maker() as db:
        backend, pool, data = await _seed(db)
        original = pool.routing_generation
        pending = User(
            id=uuid.uuid4(),
            email=uuid.uuid4().hex + "@example.test",
            name="Pending",
            password_hash="fixture",
            role="employee",
        )
        db.add(pending)
        trace = []

        def observe(conn, cursor, statement, parameters, context, executemany):
            trace.append(" ".join(statement.lower().split()))

        event.listen(test_engine.sync_engine, "before_cursor_execute", observe)
        try:
            await reconcile_pool_capability(data, backend.id, _CAPS)
            assert pool.routing_generation == original + 1
            assert _STAGES in db.sync_session.info
            row = (
                await db.execute(
                    text(
                        "SELECT routing_generation FROM ml_backend_service_pools WHERE id=:id"
                    ),
                    {"id": pool.id},
                )
            ).one()
            assert row.routing_generation == original
            assert not any("apply_reconciliation(" in sql for sql in trace)
            await db.flush([pending])
            assert not any("apply_reconciliation(" in sql for sql in trace)
            assert db.sync_session.info[_STAGES]
            await data.flush()
            assert sum("apply_reconciliation(" in sql for sql in trace) == 1
            assert not any(
                sql.startswith("update ml_backend_service_pools")
                or sql.startswith("update ml_backend_pool_members")
                for sql in trace
            )
            assert pending not in db.new
            assert (
                await db.scalar(
                    select(MLBackendServicePool.routing_generation).where(
                        MLBackendServicePool.id == pool.id
                    )
                )
            ) == original + 1
            assert db.in_transaction()
            async with maker() as observer:
                assert await observer.get(MLBackendServicePool, pool.id) is None
            await db.rollback()
            assert _STAGES not in db.sync_session.info
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", observe)


async def test_native_facts_writes_and_stage_keep_the_caller_transaction(test_engine):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False, autoflush=False)
    async with maker() as db:
        backend, pool, data = await _seed(db)
        registry_id = backend.id
        fact = await data.get_backend(registry_id)
        assert isinstance(fact, HealthBackendFact)
        assert fact.id == registry_id
        assert fact.state == "disconnected"
        assert fact.is_interactive is False
        assert fact.gpu_resource_id is None
        assert fact.last_checked_at is None
        assert fact.health_meta is None
        assert len(fact.endpoint_sha256) == 64
        assert set(fact.endpoint_sha256) <= set("0123456789abcdef")
        assert not hasattr(fact, "url") and not hasattr(fact, "auth_token")
        locked = await data.lock_current_backend(registry_id)
        assert isinstance(locked, HealthBackendFact)
        assert locked.id == registry_id
        assert locked.endpoint_sha256 == fact.endpoint_sha256
        cached = await db.get(MLBackendRegistry, registry_id)
        assert cached is not backend
        observed = await data.clock_timestamp()
        assert (
            await data.write_health(
                registry_id,
                {"state": "error", "last_checked_at": observed, "health_meta": None},
            )
            == 1
        )
        await data.refresh_backend(cached)
        assert cached.state == "error" and cached.health_meta is None
        assert cached.last_checked_at == observed
        assert await data.get_backend(uuid.uuid4()) is None
        await reconcile_pool_capability(data, registry_id, _CAPS)
        await data.flush()
        assert pool.capability_fingerprint == capability_fingerprint(
            canonicalize_capability(_CAPS)
        )
        assert await data.read_membership(registry_id, "absent/GPU") is None
        await data.lock_gpu_resource("controlled/GPU")
        assert await data.try_membership_promotion() is True
        await db.rollback()


async def test_native_fact_reads_leave_caller_orm_values_unpopulated(test_engine):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False, autoflush=False)
    async with maker() as db:
        backend, _pool, data = await _seed(db)
        cached = await db.get(MLBackendRegistry, backend.id)
        url = cached.url
        cached.url = "http://pending.invalid"
        fact = await data.get_backend(backend.id)
        assert isinstance(fact, HealthBackendFact)
        assert fact.id == backend.id
        assert not hasattr(fact, "url") and not hasattr(fact, "auth_token")
        assert cached.url == "http://pending.invalid"
        assert await data.lock_current_backend(backend.id) is not cached
        assert cached.url == "http://pending.invalid"
        assert (
            await db.scalar(
                text("SELECT url FROM ml_backend_registry WHERE id=:id"),
                {"id": backend.id},
            )
        ) == url
        assert db.is_modified(cached)
        await db.rollback()


@pytest.mark.parametrize("autoflush", [False, True])
async def test_repeated_reconciliation_respects_populate_existing_and_autoflush(
    test_engine, autoflush
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False, autoflush=autoflush)
    async with maker() as db:
        backend, pool, data = await _seed(db)
        original = pool.routing_generation
        await reconcile_pool_capability(data, backend.id, _CAPS)
        await reconcile_pool_capability(data, backend.id, _NEXT)
        await db.flush()
        current = await data.load_pool(pool.id, registry_id=backend.id)
        member = await data.load_pool_member(backend.id)
        assert current is pool
        if autoflush:
            assert pool.routing_generation == original + 2
            assert member.traffic_state == "disabled"
            assert pool.capability_fingerprint == capability_fingerprint(
                canonicalize_capability(_CAPS)
            )
        else:
            assert pool.routing_generation == original + 1
            assert member.traffic_state == "active"
            assert pool.capability_fingerprint == capability_fingerprint(
                canonicalize_capability(_NEXT)
            )
        await db.rollback()


async def test_later_caller_edits_are_preserved_by_native_flush(test_engine):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False, autoflush=False)
    async with maker() as db:
        backend, pool, data = await _seed(db)
        await reconcile_pool_capability(data, backend.id, _CAPS)
        await db.flush()
        await reconcile_pool_capability(data, backend.id, _NEXT)
        member = next(iter(db.sync_session.info[_STAGES].values())).member
        pool.name = "Caller name"
        pool.enabled = True
        member.weight = 7
        member.traffic_state = "draining"
        await db.flush()
        assert (
            await data.load_pool(pool.id, registry_id=backend.id)
        ).name == "Caller name"
        assert pool.enabled is True
        assert (await data.load_pool_member(backend.id)).traffic_state == "draining"
        assert member.weight == 7
        await db.rollback()


async def test_savepoint_rollback_drops_only_unflushed_health_intents(test_engine):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False, autoflush=False)
    async with maker() as db:
        backend, pool, data = await _seed(db)
        pool_id, registry_id, original = pool.id, backend.id, pool.routing_generation
        nested = await db.begin_nested()
        await reconcile_pool_capability(data, registry_id, _CAPS)
        assert db.sync_session.info[_STAGES]
        await nested.rollback()
        assert _STAGES not in db.sync_session.info
        await db.flush()
        row = await data.load_pool(pool_id, registry_id=registry_id)
        assert row.routing_generation == original and row.capability_fingerprint is None
        assert db.in_transaction()
        await db.rollback()
