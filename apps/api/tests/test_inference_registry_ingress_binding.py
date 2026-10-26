"""Caller-session binding checks for the fixed Native Registry ingress port."""

from __future__ import annotations

from dataclasses import fields
import uuid


import pytest
from sqlalchemy import event, select, text

from aap_inference.registry_ingress import (
    RegistrySeedFacts,
    ensure_singleton_pool,
    get_or_create_seed,
)
from aap_inference_contract.topology import PoolFacts
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.user import User

from app.services.inference_registry_ingress_data import (
    InferenceRegistryIngressData,
    _STAGES,
)


pytestmark = pytest.mark.asyncio

_MARKER = {
    "managed_by": "screenshot-seed",
    "profile": "screenshots",
    "revision": "screenshots-2026-08-g",
}
_NEW_MARKER = {**_MARKER, "revision": "screenshots-2026-08-h"}
_SEED_FIELDS = {
    "id",
    "name",
    "url",
    "state",
    "is_interactive",
    "source",
    "health_meta",
    "extra_params",
}


async def _capture_sql(db_session):
    connection = (await db_session.connection()).sync_connection
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.lower().split()))

    event.listen(connection, "before_cursor_execute", observe)
    return connection, statements, observe


async def test_seed_by_url_returns_credential_free_persisted_facts(db_session):
    data = InferenceRegistryIngressData(db_session)
    created = await data.create_seed_registry(
        "ingress-binding", "http://identity-map.test", _MARKER
    )
    row = await db_session.get(MLBackendRegistry, created.id)
    db_session.autoflush = False
    row.name = "pending caller value"

    loaded = await data.seed_by_url(row.url)

    assert isinstance(loaded, RegistrySeedFacts)
    assert {field.name for field in fields(loaded)} == _SEED_FIELDS
    assert loaded.id == row.id
    assert loaded.name == "ingress-binding"
    assert loaded.url == "http://identity-map.test"
    assert loaded.source == "seed"
    assert loaded.state == "disconnected"
    assert loaded.is_interactive is False
    assert loaded.health_meta is None
    assert loaded.extra_params == {"seed": _MARKER}
    assert not hasattr(loaded, "auth_method") and not hasattr(loaded, "auth_token")
    assert row.name == "pending caller value"
    persisted_name = await db_session.scalar(
        text("SELECT name FROM public.ml_backend_registry WHERE id=:id"),
        {"id": row.id},
    )
    assert persisted_name == "ingress-binding"


async def test_get_or_create_seed_creates_credential_free_facts_and_pool(db_session):
    data = InferenceRegistryIngressData(db_session)

    created = await get_or_create_seed(
        data,
        "http://new-seed.test",
        "New seed",
        _MARKER,
    )

    assert isinstance(created, RegistrySeedFacts)
    assert created.url == "http://new-seed.test"
    assert created.name == "New seed"
    assert created.source == "seed"
    assert created.extra_params == {"seed": _MARKER}
    assert not hasattr(created, "auth_method") and not hasattr(created, "auth_token")
    pool = await data.singleton_pool(created.id)
    assert isinstance(pool, PoolFacts)
    assert pool.legacy_instance_id == created.id


async def test_native_seed_insert_precedes_pending_unrelated_uow_flush(db_session):
    pending = User(
        id=uuid.uuid4(),
        email=f"registry-ingress-{uuid.uuid4().hex}@example.test",
        name="Pending",
        password_hash="fixture",
        role="employee",
    )
    db_session.add(pending)
    connection, statements, observe = await _capture_sql(db_session)
    try:
        row = await InferenceRegistryIngressData(db_session).create_seed_registry(
            "Native-created",
            "http://native-create.test",
            _MARKER,
        )
    finally:
        event.remove(connection, "before_cursor_execute", observe)

    native_index = next(
        index
        for index, statement in enumerate(statements)
        if "aap_inference_topology_v1.create_seed_registry" in statement
    )
    user_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("insert into users")
    )
    assert native_index < user_index
    assert isinstance(row, RegistrySeedFacts)
    assert row.source == "seed"
    assert row.extra_params == {"seed": _MARKER}
    assert not hasattr(row, "auth_token")
    assert pending not in db_session.new


async def test_singleton_pool_returns_immutable_pool_facts(db_session):
    data = InferenceRegistryIngressData(db_session)
    seed = await data.create_seed_registry(
        "Pool facts", "http://pool-facts.test", _MARKER
    )

    assert await data.singleton_pool(seed.id) is None
    created = await ensure_singleton_pool(data, seed)
    loaded = await data.singleton_pool(seed.id)

    assert isinstance(created, PoolFacts)
    assert isinstance(loaded, PoolFacts)
    assert loaded == created
    assert loaded.id is not None
    assert loaded.name == "Pool facts"
    assert loaded.enabled is False
    assert loaded.routing_policy == "smooth_weighted_round_robin"
    assert loaded.legacy_instance_id == seed.id
    assert loaded.routing_generation == 1
    assert loaded.capability_fingerprint is None
    assert loaded.capability_snapshot is None


async def test_seed_marker_preserves_pending_and_later_extra_param_edits(db_session):
    data = InferenceRegistryIngressData(db_session)
    created = await data.create_seed_registry(
        "ingress-binding", "http://seed-marker-binding.test", _MARKER
    )
    row = await db_session.get(MLBackendRegistry, created.id)
    await ensure_singleton_pool(data, created)

    db_session.autoflush = False
    row.extra_params = {
        "persisted": "kept",
        "caller_before": "before marker",
        "seed": _MARKER,
    }
    updated = await get_or_create_seed(
        data,
        row.url,
        "ingress-binding",
        _NEW_MARKER,
    )
    assert updated.id == row.id
    row.extra_params["caller_after"] = "after marker"

    await data.flush()

    stored = await db_session.scalar(
        select(MLBackendRegistry.extra_params).where(MLBackendRegistry.id == row.id)
    )
    assert stored == {
        "persisted": "kept",
        "caller_before": "before marker",
        "caller_after": "after marker",
        "seed": _NEW_MARKER,
    }


async def test_equal_seed_marker_does_not_call_native_tag(db_session):
    data = InferenceRegistryIngressData(db_session)
    created = await data.create_seed_registry(
        "ingress-binding", "http://equal-seed-marker.test", _MARKER
    )
    row = await db_session.get(MLBackendRegistry, created.id)
    await ensure_singleton_pool(data, created)
    db_session.autoflush = False
    connection, statements, observe = await _capture_sql(db_session)
    try:
        await get_or_create_seed(data, row.url, "ingress-binding", _MARKER)
        await data.flush()
    finally:
        event.remove(connection, "before_cursor_execute", observe)

    assert not any(
        "aap_inference_registry_ingress_v1.tag_seed_registry" in statement
        for statement in statements
    )
    assert row.extra_params == {"seed": _MARKER}


async def test_registry_refresh_discards_a_staged_seed_marker(db_session):
    data = InferenceRegistryIngressData(db_session)
    created = await data.create_seed_registry(
        "ingress-binding", "http://refresh-invalidation.test", _MARKER
    )
    row = await db_session.get(MLBackendRegistry, created.id)
    await ensure_singleton_pool(data, created)

    db_session.autoflush = False
    await get_or_create_seed(data, row.url, "ingress-binding", _NEW_MARKER)
    assert _STAGES in db_session.sync_session.info

    await db_session.refresh(row)
    assert _STAGES not in db_session.sync_session.info

    connection, statements, observe = await _capture_sql(db_session)
    try:
        await data.flush()
    finally:
        event.remove(connection, "before_cursor_execute", observe)

    assert not any(
        "aap_inference_registry_ingress_v1.tag_seed_registry" in statement
        for statement in statements
    )
    assert row.extra_params == {"seed": _MARKER}
