"""Physical routing hooks keep frozen logical owners and real reader lifetimes."""

from contextlib import contextmanager
from dataclasses import replace
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import dataset_assets, dataset_references
from app.services import managed_original_reader as reader
from app.services import original_binary, original_sources, prediction_originals
from app.services import storage_access
from app.services.content_snapshot import ContentError
from tests.test_managed_original_reader import Provider
from tests.test_prediction_originals import source as image_source
from tests.test_training_dataset_originals import encoded


@pytest.fixture
def routing(monkeypatch):
    raw = encoded()
    logical = image_source()
    digest = hashlib.sha256(raw).hexdigest()
    logical = replace(
        logical,
        sha256=digest,
        size_bytes=len(raw),
        ownership_facts=replace(
            logical.ownership_facts, sha256=digest, size_bytes=len(raw)
        ),
    )
    provider = Provider()
    provider.raw = provider.actual = raw
    provider.meta.endpoint_url = "http://physical-b.test:9000"
    provider.version = "verified-destination-version"
    publication_id = uuid.uuid4()
    plan = SimpleNamespace(
        location_id=uuid.uuid4(),
        maintenance_revision=19,
        spec=reader.ObjectSpec(
            provider.meta.endpoint_url,
            "destination-bucket",
            "migration/copied-original.png",
            digest,
            len(raw),
            expected_version_id=provider.version,
            expected_etag=provider.etag,
        ),
        purpose=lambda _label: "migration_read_" + publication_id.hex,
    )
    state = SimpleNamespace(
        source=logical,
        raw=raw,
        provider=provider,
        plan=plan,
        active=False,
        rechecks=[],
        db=SimpleNamespace(commit=AsyncMock()),
    )

    class Permit:
        async def __aenter__(self):
            state.active = True
            return self

        async def __aexit__(self, *_args):
            state.active = False

    state.permit = Permit()

    async def acquire(_db, location_id, purpose, *, expected_revision, **_kwargs):
        assert location_id == plan.location_id != logical.location_id
        assert expected_revision == plan.maintenance_revision
        assert purpose == "migration_read_" + publication_id.hex
        state.active = True
        return state.permit

    @contextmanager
    def client():
        assert state.active
        try:
            yield provider
        finally:
            provider.close()

    async def options(_db, source, **_kwargs):
        assert state.active and source is logical
        return {"client_context": client}

    async def recheck(_db, source):
        state.rechecks.append(source)

    monkeypatch.setattr(original_sources, "read_plan", lambda _source: plan)
    monkeypatch.setattr(original_sources, "read_options", options)
    monkeypatch.setattr(original_sources, "recheck_read_route", recheck)
    monkeypatch.setattr(storage_access, "acquire", acquire)
    return state


def assert_physical_reads(state):
    assert state.provider.closed == 1
    assert all(body.closed for body in state.provider.bodies)
    operations = [
        params for action, params in state.provider.calls if action in {"head", "get"}
    ]
    assert operations
    assert all(
        params["Bucket"] == state.plan.spec.bucket
        and params["Key"] == state.plan.spec.object_key
        and params["VersionId"] == state.provider.version
        for params in operations
    )
    assert state.source.bucket == "saved-bucket"
    assert state.source.endpoint_url == "http://saved.test:9000"


@pytest.mark.parametrize("withdraw_after_read", [False, True])
async def test_download_routes_physical_io_and_rechecks_before_delivery(
    routing, monkeypatch, withdraw_after_read
):
    state = routing
    monkeypatch.setattr(
        original_binary, "_current", AsyncMock(return_value=(state.source, None))
    )
    monkeypatch.setattr(original_binary.access, "check_clock", lambda *_args: None)
    context = original_binary.AuthorizedOriginal(object(), object(), state.source, None)
    stages = []

    async def capture(spec, **options):
        stage = await reader.capture(
            spec, max_bytes=len(state.raw), timeout_seconds=3, **options
        )
        stages.append(stage)
        return stage

    if withdraw_after_read:

        async def recheck(_db, source):
            state.rechecks.append(source)
            if len(state.rechecks) == 2:
                raise ContentError("migration_read_route_unavailable")

        monkeypatch.setattr(original_sources, "recheck_read_route", recheck)
        with pytest.raises(ContentError, match="migration_read_route_unavailable"):
            await original_binary.capture_authorized(state.db, context, capture)
    else:
        stage = await original_binary.capture_authorized(state.db, context, capture)
        try:
            assert stage.file.read() == state.raw
        finally:
            stage.close()
    assert len(state.rechecks) == 2
    assert all(source is state.source for source in state.rechecks)
    assert stages and all(stage.file.closed for stage in stages)
    assert not state.active
    assert_physical_reads(state)


async def test_directory_dedup_resolves_credentials_only_under_target_permit(
    routing, monkeypatch
):
    state = routing
    item = SimpleNamespace(id=state.source.item_id)
    monkeypatch.setattr(
        dataset_assets, "find_verified_content", AsyncMock(return_value=item)
    )
    monkeypatch.setattr(
        original_sources, "resolve_item", AsyncMock(return_value=state.source)
    )
    result = await dataset_references.prepare_copy(
        state.db,
        object(),
        object(),
        SimpleNamespace(id=uuid.uuid4()),
        SimpleNamespace(sha256=state.source.sha256, size=state.source.size_bytes),
    )
    assert result == {"existing_id": item.id}
    assert len(state.rechecks) == 2 and not state.active
    assert_physical_reads(state)


async def test_prediction_freshness_rechecks_initial_plan_after_source_refresh(
    routing, monkeypatch
):
    state = routing

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    async def resolve(*_args, **_kwargs):
        return state.source if not state.rechecks else replace(state.source)

    monkeypatch.setattr(prediction_originals, "AsyncSession", Session)
    monkeypatch.setattr(prediction_originals, "resolve", resolve)
    monkeypatch.setattr(storage_access, "engine", lambda _db: object())
    expected = prediction_originals.source_receipt(state.source)
    # This entry owns the Permit beyond capture, through the adopted image close.
    owned = await prediction_originals.open_snapshot(
        state.db,
        state.source.relation.task_id,
        expected,
        guard=AsyncMock(),
        purpose="prediction_original",
        node_id="test:physical-plan",
    )
    try:
        assert owned.snapshot.sha256 == state.source.sha256
        assert state.active and not owned.snapshot.file.closed
        assert prediction_originals.source_receipt(state.source) == expected
        assert len(state.rechecks) == 3
        assert all(source is state.source for source in state.rechecks)
    finally:
        await owned.close()
    assert owned.snapshot.file.closed and not state.active
    assert_physical_reads(state)
