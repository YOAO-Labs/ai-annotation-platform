"""Logical originals and physical migration routes have separate immutable facts."""

from dataclasses import FrozenInstanceError, replace
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import sys
import uuid

import pytest

from app.services import original_sources as sources
from app.services import managed_original_reclaim_fence as reclaim
from app.services.content_snapshot import ContentError
from app.services.original_read_plan import ReadRoute


def route():
    return ReadRoute(
        publication_id=uuid.UUID(int=1),
        publication_revision=2,
        source_object_id="a" * 64,
        source_location_id=uuid.UUID(int=3),
        source_endpoint_url="http://old.test:9000",
        source_bucket="old-originals",
        source_object_key="logical/original",
        source_version_id="old-version",
        sha256="b" * 64,
        size_bytes=13,
        source_etag='"old-etag"',
        is_latest=False,
        target_location_id=uuid.UUID(int=4),
        target_location_revision=5,
        target_connection_id=uuid.UUID(int=6),
        target_connection_revision=7,
        target_endpoint_url="http://new.test:9000",
        target_bucket="new-originals",
        target_object_key="_aap_migration/copy/object/content",
        target_version_id="copied-version",
        target_etag='"copied-etag"',
        target_content_sha256="c" * 64,
        creation_nonce="original-copy-nonce",
        creation_plan_uuid=str(uuid.UUID(int=8)),
        maintenance_revision=9,
    )


def source(selected=None):
    r = route()
    return sources.OriginalSource(
        "dataset_asset",
        uuid.UUID(int=10),
        r.sha256,
        r.size_bytes,
        "image",
        uuid.UUID(int=11),
        None,
        r.source_bucket,
        r.source_object_key,
        r.source_endpoint_url,
        r.source_location_id,
        1,
        17,
        "historical ownership receipt",
        None,
        None,
        None,
        selected,
    )


def test_physical_plan_keeps_all_logical_original_facts_and_pins_only_target():
    original = source()
    migrated = replace(original, read_route=route())
    plan = sources.read_plan(migrated)
    assert replace(migrated, read_route=None) == original
    assert plan.location_id == migrated.read_route.target_location_id
    assert plan.maintenance_revision == 9
    assert plan.spec.endpoint_url == "http://new.test:9000"
    assert plan.spec.bucket == "new-originals"
    assert plan.spec.expected_version_id == "copied-version"
    assert plan.spec.expected_etag == '"copied-etag"'
    assert plan.purpose("image_read") == route().purpose
    with pytest.raises(FrozenInstanceError):
        plan.spec = None


async def test_unrouted_structural_sources_keep_old_plan_and_add_no_recheck_io():
    original = source()
    structural = SimpleNamespace(
        **{k: v for k, v in vars(original).items() if k != "read_route"}
    )
    plan = sources.read_plan(structural)
    assert plan.location_id == original.location_id
    assert plan.maintenance_revision == original.maintenance_revision
    assert plan.spec.expected_version_id is plan.spec.expected_etag is None
    assert plan.spec.object_key == original.object_key
    assert plan.purpose("image_read") == "image_read"
    await sources.recheck_read_route(object(), structural)


def publication_stub(monkeypatch, **values):
    module = SimpleNamespace(**values)
    monkeypatch.setitem(
        sys.modules, "app.services.storage_migration_read_publications", module
    )
    return module


async def test_logical_stopped_gate_is_captured_before_exact_target_route(monkeypatch):
    original, selected, events = source(), route(), []

    async def gate(*args, **kwargs):
        events.append("source_gate")
        return {"state": "stopped", "revision": 18}

    async def lookup(db, **kwargs):
        events.append("exact_route")
        assert kwargs["endpoint_url"] == original.endpoint_url
        assert kwargs["sha256"] == original.sha256
        assert kwargs["size_bytes"] == original.size_bytes
        return selected

    monkeypatch.setattr(sources, "_gate_state", gate)
    publication_stub(monkeypatch, lookup=lookup)
    result = await sources._read_route(object(), original, True)
    assert events == ["source_gate", "exact_route"]
    assert result.maintenance_revision == 18
    assert result.read_route == selected
    assert result.object_key == original.object_key


async def test_stopped_source_without_exact_route_keeps_existing_rejection(monkeypatch):
    monkeypatch.setattr(
        sources,
        "_gate_state",
        AsyncMock(return_value={"state": "stopped", "revision": 18}),
    )
    publication_stub(monkeypatch, lookup=AsyncMock(return_value=None))
    with pytest.raises(Exception, match="storage_maintenance"):
        await sources._read_route(object(), source(), False)


async def test_recheck_uses_original_frozen_route_and_propagates_revocation(
    monkeypatch,
):
    selected = route()
    service = publication_stub(monkeypatch, recheck=AsyncMock())
    await sources.recheck_read_route(object(), source(selected))
    assert service.recheck.await_args.args[1] == selected
    service.recheck.side_effect = ContentError("migration_read_route_changed")
    with pytest.raises(ContentError, match="migration_read_route_changed"):
        await sources.recheck_read_route(object(), source(selected))


async def test_mapped_read_options_protect_logical_source_and_resolve_current_target(
    monkeypatch,
):
    selected, owned, db, events = route(), object(), object(), []
    original = source(selected)
    engine = object()

    @asynccontextmanager
    async def session(**kwargs):
        assert kwargs["bind"] is engine
        events.append("session")
        yield owned

    async def protect(session, candidate):
        assert session is owned and candidate is original
        assert candidate.bucket == selected.source_bucket
        events.append("protect_logical")

    def client(**kwargs):
        return kwargs

    async def target(session, candidate):
        assert session is owned and candidate is selected
        events.append("resolve_current_target")
        return SimpleNamespace(client=client)

    monkeypatch.setattr(sources, "AsyncSession", session)
    monkeypatch.setattr(sources.storage_access, "engine", lambda candidate: engine)
    monkeypatch.setattr(reclaim, "protect_source", protect)
    publication_stub(monkeypatch, target_for_route=target)
    options = await sources.read_options(db, original, read_timeout=11)
    assert events == ["session", "protect_logical", "resolve_current_target"]
    assert options["client_context"]() == {"read_timeout": 11}
