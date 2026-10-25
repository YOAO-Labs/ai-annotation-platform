from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, make_transient_to_detached

from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services import ai_mask_accept
from app.services.inference_health_data import InferenceHealthData


def _routing_data(instance_id, pool_id):
    return SimpleNamespace(
        routing=SimpleNamespace(
            backend_instance_id=instance_id,
            backend_pool_id=pool_id,
        )
    )


def _registry(registry_id):
    row = MLBackendRegistry(
        id=registry_id,
        name="cached registry",
        url=f"https://{registry_id}.example.test",
    )
    make_transient_to_detached(row)
    return row


def _pool(pool_id):
    row = MLBackendServicePool(
        id=pool_id,
        name="cached pool",
        enabled=False,
    )
    make_transient_to_detached(row)
    return row


@pytest.mark.asyncio
async def test_lineage_keeps_session_get_cache_hits_without_owner_reads(monkeypatch):
    instance_id = uuid.uuid4()
    pool_id = uuid.uuid4()
    registry = _registry(instance_id)
    pool = _pool(pool_id)
    session = Session()
    session.add_all([registry, pool])
    session.expire(registry, ["name"])
    assert not inspect(registry).expired
    db = SimpleNamespace(sync_session=session)

    class UnexpectedOwnerRead:
        def __init__(self, _db):
            pass

        async def get_backend(self, _registry_id):
            raise AssertionError("a Session.get cache hit must avoid an owner read")

        async def get_pool_reference(self, _pool_id):
            raise AssertionError("a Session.get cache hit must avoid an owner read")

    monkeypatch.setattr(ai_mask_accept, "InferenceHealthData", UnexpectedOwnerRead)
    try:
        assert await ai_mask_accept._resolve_routing_lineage(
            db, _routing_data(instance_id, pool_id)
        ) == (instance_id, pool_id)
        assert registry in session.identity_map.values()
        assert pool in session.identity_map.values()
    finally:
        session.close()


@pytest.mark.asyncio
async def test_lineage_reads_independent_primary_keys_in_order(monkeypatch):
    instance_id = uuid.uuid4()
    pool_id = uuid.uuid4()
    db = SimpleNamespace(sync_session=SimpleNamespace(identity_map={}))
    calls = []

    class OwnerReads:
        def __init__(self, _db):
            pass

        async def get_backend(self, registry_id):
            calls.append(("registry", registry_id))
            return None

        async def get_pool_reference(self, requested_pool_id):
            calls.append(("pool", requested_pool_id))
            return SimpleNamespace(id=pool_id)

    monkeypatch.setattr(ai_mask_accept, "InferenceHealthData", OwnerReads)
    assert await ai_mask_accept._resolve_routing_lineage(
        db, _routing_data(instance_id, pool_id)
    ) == (None, pool_id)
    assert calls == [("registry", instance_id), ("pool", pool_id)]


@pytest.mark.asyncio
async def test_lineage_keeps_registry_when_independent_pool_lookup_is_missing(
    monkeypatch,
):
    instance_id = uuid.uuid4()
    pool_id = uuid.uuid4()
    db = SimpleNamespace(sync_session=SimpleNamespace(identity_map={}))
    calls = []

    class OwnerReads:
        def __init__(self, _db):
            pass

        async def get_backend(self, registry_id):
            calls.append(("registry", registry_id))
            return SimpleNamespace(id=instance_id)

        async def get_pool_reference(self, requested_pool_id):
            calls.append(("pool", requested_pool_id))
            return None

    monkeypatch.setattr(ai_mask_accept, "InferenceHealthData", OwnerReads)
    assert await ai_mask_accept._resolve_routing_lineage(
        db, _routing_data(instance_id, pool_id)
    ) == (instance_id, None)
    assert calls == [("registry", instance_id), ("pool", pool_id)]


@pytest.mark.asyncio
async def test_lineage_skips_optional_pool_and_evicts_missing_expired_identity(
    monkeypatch,
):
    instance_id = uuid.uuid4()
    expired_registry = _registry(instance_id)
    session = Session()
    session.add(expired_registry)
    session.expire(expired_registry)
    assert inspect(expired_registry).expired
    calls = []

    class OwnerReads:
        def __init__(self, _db):
            pass

        async def get_backend(self, registry_id):
            calls.append(("registry", registry_id))
            return None

        async def get_pool_reference(self, _pool_id):
            raise AssertionError("a null Pool ID must not be read")

    monkeypatch.setattr(ai_mask_accept, "InferenceHealthData", OwnerReads)
    db = SimpleNamespace(sync_session=session)
    try:
        assert await ai_mask_accept._resolve_routing_lineage(
            db, _routing_data(instance_id, None)
        ) == (None, None)
        assert calls == [("registry", instance_id)]
        assert inspect(expired_registry).deleted
        assert expired_registry not in session.identity_map.values()
    finally:
        session.close()


@pytest.mark.asyncio
async def test_pool_reference_binding_uses_fixed_unlocked_by_id_function():
    pool_id = uuid.uuid4()
    expected = _pool(pool_id)

    class RecordingSession:
        async def scalar(self, statement, params):
            self.sql = str(statement.compile(dialect=postgresql.dialect()))
            self.params = params
            return expected

    db = RecordingSession()
    actual = await InferenceHealthData(db).get_pool_reference(pool_id)
    assert actual is expected
    assert "aap_inference_v1.ai_mask_pool_reference_row" in db.sql
    assert "ml_backend_service_pools" not in db.sql
    assert "FOR UPDATE" not in db.sql
    assert db.params == {"pool_id": pool_id}
    assert tuple(MLBackendServicePool.__table__.columns.keys()) == (
        "id",
        "name",
        "enabled",
        "routing_policy",
        "legacy_instance_id",
        "routing_generation",
        "capability_fingerprint",
        "capability_snapshot",
        "created_at",
        "updated_at",
    )
