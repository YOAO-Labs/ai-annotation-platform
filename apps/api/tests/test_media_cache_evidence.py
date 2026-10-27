"""Stored cache evidence follows saved targets without contacting providers."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import select

from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.original_cache import OriginalCacheEntry
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.db.models.thumbnail_output import ThumbnailOutput
from app.services import dataset_workbench as workbench
from app.services import original_cache, storage_source_diagnostics as diagnostics
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.source_readers.s3 import S3Access
from app.services.storage import storage_service
from tests.test_storage_source_diagnostics import (
    content_world as content_world,
    evidence_world as evidence_world,
)


async def saved_target(db):
    connection = StorageConnection(
        id=uuid.uuid4(),
        name="evidence-" + uuid.uuid4().hex,
        kind="s3",
        scope="global",
        secret_enc=b"current-credentials",
        config={
            "endpoint": "https://saved-cache.example.test",
            "bucket": "cache-b",
            "base_prefix": "公司/root",
            "use_ssl": True,
        },
    )
    location = StorageLocation(
        id=uuid.uuid4(),
        name="evidence-" + uuid.uuid4().hex,
        kind="s3",
        connection_id=connection.id,
        config={},
    )
    db.add(connection)
    await db.flush()
    db.add(location)
    await db.flush()
    return connection, location


def saved_ref(connection, location, logical):
    endpoint = connection.config["endpoint"]
    root = connection.config["base_prefix"] + "/_aap_managed/media-cache/"
    return {
        "schema": 2,
        "purpose": "media-cache",
        "location_id": str(location.id),
        "config_revision": location.config_revision,
        "binding_generation": 2,
        "endpoint_url": endpoint,
        "bucket": connection.config["bucket"],
        "key_prefix": root,
        "prefix": root + logical,
        "storage_identity": managed_storage_identity(
            endpoint, connection.config["bucket"]
        ),
    }


@pytest.fixture
def no_provider(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Stored evidence must not contact or open a provider")

    monkeypatch.setattr(S3Access, "client", forbidden)
    monkeypatch.setattr(original_cache, "make_storage", forbidden)


@pytest.fixture
async def cache_evidence(db_session, evidence_world, no_provider, request):
    db = db_session
    world, mappings, _ = evidence_world
    connection, location = await saved_target(db)
    source = world[5]
    source.cache_policy = "on_demand"
    world[4].config = {
        **world[4].config,
        "cache_policy": "on_demand",
        "original_cache_quota_bytes": 999999,
    }
    asset = await db.get(DatasetAsset, mappings[0].asset_id)
    now = datetime.now(timezone.utc)
    entry = OriginalCacheEntry(
        id=uuid.uuid4(),
        scope_location_id=source.location_id,
        source_id=source.id,
        asset_id=asset.id,
        sha256=asset.checksum_value,
        size_bytes=asset.size_bytes,
        source_revision=source.config_revision,
        policy_revision=source.policy_revision,
        location_revision=world[4].config_revision,
        location_id=location.id,
        bucket=connection.config["bucket"],
        binding_generation=2,
        storage_identity=managed_storage_identity(
            connection.config["endpoint"], connection.config["bucket"]
        ),
        state="ready",
        uncertain_write=False,
        verified_at=now,
        last_accessed_at=now,
        expires_at=now + timedelta(hours=1),
        writer_until=now,
        retain_until=now + timedelta(days=7),
        next_check_at=now,
    )
    entry.storage_ref = saved_ref(
        connection, location, original_cache.logical_prefix(entry)
    )
    if getattr(request, "param", None) == "legacy":
        entry.storage_ref = None
        entry.location_id, entry.bucket = (
            LEGACY_LOCATION_ID,
            storage_service.media_cache_bucket,
        )
        entry.binding_generation = 1
        entry.storage_identity = managed_storage_identity(
            storage_service.client.meta.endpoint_url, entry.bucket
        )
    locator = DatasetAssetLocation(
        asset_id=asset.id,
        location_id=entry.location_id,
        bucket=entry.bucket,
        object_key=original_cache.object_key(entry),
        role="original_cache",
        reference_state="present",
        verified_at=now,
    )
    db.add_all([entry, locator])
    await db.flush()
    return world, connection, location, entry, locator


@pytest.mark.parametrize("cache_evidence", ["legacy"], indirect=True)
async def test_legacy_null_cache_evidence_ignores_new_b_binding(
    db_session, cache_evidence
):
    db = db_session
    world, connection, location, entry, _ = cache_evidence
    binding = await db.get(StorageBinding, "media-cache")
    binding.location_id, binding.bucket, binding.generation = (
        location.id,
        connection.config["bucket"],
        2,
    )
    await db.flush()
    assert await workbench.available_cache_ids(db, world[3].id, world[2]) == [entry.id]
    assert entry.storage_ref is None and entry.location_id == LEGACY_LOCATION_ID


async def test_b_cache_evidence_survives_current_a_binding_and_credential_rotation(
    db_session, cache_evidence, monkeypatch
):
    db = db_session
    world, connection, location, entry, _ = cache_evidence
    binding = await db.get(StorageBinding, "media-cache")
    assert binding.location_id == LEGACY_LOCATION_ID
    frozen = deepcopy(entry.storage_ref)
    connection.secret_enc = b"rotated-credentials"
    connection.revision = location.config_revision = 2
    await db.flush()
    capture, seen = original_cache.prepare_target, []

    async def transport(*args, **kwargs):
        target = await capture(*args, **kwargs)
        seen.append(target._access.secret_enc)
        return target

    monkeypatch.setattr(original_cache, "prepare_target", transport)
    item_id = await db.scalar(
        select(DatasetAsset.dataset_item_id).where(DatasetAsset.id == entry.asset_id)
    )
    ids = await workbench.available_cache_ids(
        db, world[3].id, world[2], item_ids=[item_id]
    )
    assert ids == [entry.id] and seen == [b"rotated-credentials"]
    obs = workbench.observations(world[3].id, world[2], cache_entry_ids=ids)
    row = (
        (await db.execute(select(obs).where(obs.c.original_cache_entry_id == entry.id)))
        .mappings()
        .one()
    )
    assert (
        row["original_cache_recorded"]
        and row["original_cache_recorded_at"] == entry.verified_at
    )
    evidence = await diagnostics.reference_evidence(db, [world[5].id], world[2])
    assert evidence[world[5].id]["original_cache_recorded_count"] == 1
    assert entry.storage_ref == frozen and entry.location_revision == 1


async def test_cache_candidates_stay_page_bounded_and_skip_disabled_targets(
    db_session, cache_evidence, evidence_world
):
    db = db_session
    world, connection, location, first, _ = cache_evidence
    second_asset = await db.get(DatasetAsset, evidence_world[1][1].asset_id)
    values = {
        column.key: getattr(first, column.key)
        for column in OriginalCacheEntry.__table__.columns
        if column.key != "created_at"
    }
    values.update(
        id=uuid.uuid4(),
        asset_id=second_asset.id,
        sha256=second_asset.checksum_value,
        size_bytes=second_asset.size_bytes,
    )
    second = OriginalCacheEntry(**values)
    second.storage_ref = saved_ref(
        connection, location, original_cache.logical_prefix(second)
    )
    db.add(second)
    db.add(
        DatasetAssetLocation(
            asset_id=second.asset_id,
            location_id=location.id,
            bucket=second.bucket,
            object_key=original_cache.object_key(second),
            role="original_cache",
            reference_state="present",
        )
    )
    await db.flush()
    ids = await workbench.available_cache_ids(db, world[3].id, world[2])
    assert set(ids) == {first.id, second.id}
    assert await workbench.available_cache_ids(
        db, world[3].id, world[2], item_ids=[second_asset.dataset_item_id]
    ) == [second.id]
    location.enabled = False
    await db.flush()
    assert await workbench.available_cache_ids(db, world[3].id, world[2]) == []


@pytest.mark.parametrize(
    "change",
    [
        "disabled_target",
        "target_gate",
        "source_gate",
        "endpoint",
        "root",
        "policy",
        "quota",
        "expired",
        "source_revision",
        "locator",
    ],
)
async def test_cache_evidence_rejects_unavailable_or_stale_saved_addresses(
    db_session, cache_evidence, change
):
    db = db_session
    world, connection, location, entry, locator = cache_evidence
    if change == "disabled_target":
        location.enabled = False
    elif change.endswith("gate"):
        identity = location.id if change == "target_gate" else world[4].id
        gate = await db.get(StorageMaintenanceGate, identity)
        if gate is None:
            gate = StorageMaintenanceGate(location_id=identity)
            db.add(gate)
        gate.state = "stopped"
    elif change in {"endpoint", "root"}:
        connection.config = {
            **connection.config,
            "endpoint"
            if change == "endpoint"
            else "base_prefix": "https://redirected.example.test"
            if change == "endpoint"
            else "redirected",
        }
    elif change == "policy":
        world[5].cache_policy = "preview"
    elif change == "quota":
        world[4].config = {**world[4].config, "original_cache_quota_bytes": 0}
    elif change == "expired":
        # The candidate query compares against the database transaction clock, so
        # expiry must be established before this test's transaction began.
        entry.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    elif change == "source_revision":
        world[5].config_revision += 1
    else:
        locator.reference_state = "missing"
    await db.flush()
    assert await workbench.available_cache_ids(db, world[3].id, world[2]) == []
    evidence = await diagnostics.reference_evidence(db, [world[5].id], world[2])
    assert evidence[world[5].id]["original_cache_recorded_count"] == 0


@pytest.mark.parametrize(
    "mismatch",
    [None, "owner", "path", "input", "receipt", "key", "state", "target", "gate"],
)
async def test_directory_preview_evidence_uses_saved_owner_and_current_metadata(
    db_session, evidence_world, no_provider, mismatch
):
    db = db_session
    world, _, previews = evidence_world
    connection, location = await saved_target(db)
    preview = previews[0]
    now = datetime.now(timezone.utc)
    output_id = uuid.uuid4()
    ref = saved_ref(connection, location, f"thumbnails/objects/{output_id}/")
    output = ThumbnailOutput(
        id=output_id,
        owner_kind="directory_preview",
        owner_id=preview.source_id,
        path_digest=preview.path_digest,
        storage_ref=ref,
        input_ref={
            "directory": {
                "source_id": str(preview.source_id),
                "path_digest": preview.path_digest,
                "source_sha256": preview.source_sha256,
                "spec": preview.spec,
            },
            "encoded": {"sha256": "b" * 64, "size_bytes": 23},
        },
        state="ready",
        sha256="b" * 64,
        size_bytes=23,
        etag="saved",
        ready_at=now,
        writer_until=now,
        next_check_at=now,
    )
    preview.location_id, preview.bucket = location.id, ref["bucket"]
    preview.object_key, preview.sha256, preview.size_bytes = (
        ref["prefix"] + "thumbnail.webp",
        output.sha256,
        output.size_bytes,
    )
    if mismatch == "owner":
        output.owner_id = uuid.uuid4()
    elif mismatch == "path":
        output.path_digest = "c" * 64
    elif mismatch == "input":
        output.input_ref["directory"]["source_sha256"] = "c" * 64
    elif mismatch == "receipt":
        preview.sha256 = "c" * 64
    elif mismatch == "key":
        preview.object_key += "other"
    elif mismatch == "state":
        output.state = "retiring"
    elif mismatch == "target":
        location.enabled = False
    elif mismatch == "gate":
        db.add(StorageMaintenanceGate(location_id=location.id, state="stopped"))
    db.add(output)
    await db.flush()
    preview.thumbnail_output_id = output.id
    connection.revision = location.config_revision = 2
    connection.secret_enc = b"rotated-credentials"
    await db.flush()
    evidence = await diagnostics.reference_evidence(db, [world[5].id], world[2])
    assert evidence[world[5].id]["preview_counts"]["ready"] == int(mismatch is None)
    assert evidence[world[5].id]["preview_counts"]["failed"] == 1
