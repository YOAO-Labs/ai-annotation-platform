"""Database responsibility, lease fencing and saved-owner pyramid regressions.

All source fixtures explicitly register canonical assets and present replicas.
Provider I/O uses an in-memory provider. The normal disposable DB fixtures own
migration execution; this module never starts a runtime or creates a database.
"""

from copy import deepcopy
from datetime import timedelta
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
)
from app.db.models.image_pyramid import (
    ImagePyramidAsset,
    ImagePyramidGeneration,
    ImagePyramidOutput,
)
from app.db.models.storage_maintenance import StorageOperation
from app.services import image_pyramid as service
from app.services import image_pyramid_objects as objects
from app.services import image_pyramid_originals as originals
from app.services import image_pyramid_storage as physical
from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.workers import image_pyramid as worker
from tests.test_image_pyramid_storage import Provider
from tests.training_original_fixtures import image_bytes


def _ref(identifier, *, bucket="saved-pyramid-output-cache"):
    endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    return {
        "schema": 1,
        "purpose": "media-cache",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "prefix": physical.prefix_for(identifier),
        "storage_identity": managed_storage_identity(endpoint, bucket),
    }


def _inventory(output, *, width=2, height=2, source_fingerprint="sha256:" + "a" * 64):
    owner = physical.resolve(output)
    payloads = {
        owner.key(
            f"tiles/{level['level']}/{x}/{y}.webp"
        ): f"tile-{level['level']}-{x}-{y}".encode()
        for level in service.pyramid_levels(width, height)
        for y in range(level["rows"])
        for x in range(level["columns"])
    }
    overview = b"verified-overview"
    manifest = service.build_manifest(
        generation=output.generation,
        source_fingerprint=source_fingerprint,
        width=width,
        height=height,
        overview_width=width,
        overview_height=height,
        overview_digest=service.sha256_digest(overview),
    )
    payloads[owner.key("overview.webp")] = overview
    payloads[owner.key("manifest.json")] = service.canonical_manifest_bytes(manifest)
    return {
        "schema": 1,
        "objects": {
            key: {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
            for key, raw in payloads.items()
        },
    }, payloads


@pytest.fixture
async def registered(db_session, super_admin):
    user, _ = super_admin
    await ensure_legacy_bindings(db_session)
    await storage_access.ensure(db_session, LEGACY_LOCATION_ID)
    raw = image_bytes(size=(2, 2))
    digest = hashlib.sha256(raw).hexdigest()
    dataset = Dataset(
        display_id="D-PO-" + uuid.uuid4().hex[:10],
        name="Pyramid output sources " + uuid.uuid4().hex[:10],
        data_type="image",
        created_by=user.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="registered.png",
        file_path="saved/pyramid-input-" + uuid.uuid4().hex + ".png",
        file_type="image",
        file_size=len(raw),
        content_hash=digest,
        width=2,
        height=2,
    )
    db_session.add(item)
    await db_session.flush()
    canonical = DatasetAsset(
        id=uuid.uuid4(),
        dataset_item_id=item.id,
        media_type="image",
        size_bytes=len(raw),
        verification_state="verified",
        checksum_algorithm="sha256",
        checksum_value=digest,
    )
    db_session.add(canonical)
    await db_session.flush()
    replica = DatasetAssetLocation(
        asset_id=canonical.id,
        location_id=LEGACY_LOCATION_ID,
        bucket="saved-pyramid-inputs",
        object_key=item.file_path,
        role="managed_original",
        reference_state="present",
    )
    db_session.add(replica)
    await db_session.flush()
    owner = await service.owner_for_dataset_item(db_session, item)
    delegation = originals.system_authorization("dataset_item", item.id, owner.source)
    return SimpleNamespace(
        dataset=dataset,
        item=item,
        canonical=canonical,
        replica=replica,
        raw=raw,
        owner=owner,
        delegation=delegation,
    )


async def _lineage(db, registered, *, legacy=False):
    asset = ImagePyramidAsset(
        id=uuid.uuid4(),
        dataset_item_id=registered.item.id,
        profile_version=settings.image_pyramid_profile_version,
        building_generation=1,
    )
    db.add(asset)
    await db.flush()
    generation = ImagePyramidGeneration(
        asset_id=asset.id,
        generation=1,
        source_identity="explicit-test-identity",
        status="pending",
        normalization_version=settings.image_pyramid_normalization_version,
        original_ref=None if legacy else originals.receipt(registered.owner.source),
        auth_ref=None if legacy else deepcopy(registered.delegation),
    )
    db.add(generation)
    await db.flush()
    return asset, generation


async def _new_output(db, *, asset_id=None, generation=1, old=False):
    identifier = uuid.uuid4()
    values = {}
    if old:
        values = {
            "created_at": service.utcnow() - timedelta(days=10),
            "updated_at": service.utcnow() - timedelta(days=10),
        }
    row = ImagePyramidOutput(
        id=identifier,
        asset_id=asset_id or uuid.uuid4(),
        generation=generation,
        lease_token=uuid.uuid4(),
        storage_ref=_ref(identifier),
        state="building",
        **values,
    )
    db.add(row)
    await db.flush()
    return row


async def _make_ready(db, row):
    inventory, payloads = _inventory(row)
    row.inventory = inventory
    row.state = "ready"
    await db.flush()
    return payloads


def _publication(registered, asset, generation, output, token):
    inventory, payloads = _inventory(
        output, source_fingerprint="sha256:" + registered.owner.source.sha256
    )
    owner = physical.resolve(output)
    manifest = owner.key("manifest.json")
    overview = owner.key("overview.webp")
    return {
        "asset_id": asset.id,
        "generation_number": generation.generation,
        "lease_token": token,
        "output_id": output.id,
        "inventory": inventory,
        "gate_revision": registered.owner.source.maintenance_revision,
        "source_fingerprint": "sha256:" + registered.owner.source.sha256,
        "width": 2,
        "height": 2,
        "max_level": len(service.pyramid_levels(2, 2)) - 1,
        "manifest_storage_key": manifest,
        "manifest_digest": service.sha256_digest(payloads[manifest]),
        "overview_storage_key": overview,
        "overview_width": 2,
        "overview_height": 2,
        "overview_digest": service.sha256_digest(payloads[overview]),
        "tile_count": service.total_tile_count(2, 2),
        "retained_bytes": sum(len(raw) for raw in payloads.values()),
    }


@pytest.mark.parametrize("column", ["original_ref", "auth_ref"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_generation_inputs_cannot_be_rewritten_or_adopted(
    db_session, registered, column, legacy
):
    asset, generation = await _lineage(db_session, registered, legacy=legacy)
    original = getattr(generation, column)
    changed = (
        {"unexpected": "cannot-adopt"}
        if original is None
        else {**deepcopy(original), "unexpected": "cannot-rewrite"}
    )
    values = {column: changed}
    if legacy:
        values = {
            "original_ref": originals.receipt(registered.owner.source),
            "auth_ref": deepcopy(registered.delegation),
        }
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(ImagePyramidGeneration)
                .where(
                    ImagePyramidGeneration.asset_id == asset.id,
                    ImagePyramidGeneration.generation == generation.generation,
                )
                .values(values)
                .execution_options(synchronize_session=False)
            )
    await db_session.refresh(generation)
    assert getattr(generation, column) == original


@pytest.mark.parametrize(
    "column",
    ["id", "asset_id", "generation", "lease_token", "storage_ref", "created_at"],
)
async def test_output_owner_facts_are_database_immutable(db_session, column):
    row = await _new_output(db_session)
    changed = {
        "id": uuid.uuid4(),
        "asset_id": uuid.uuid4(),
        "generation": 2,
        "lease_token": uuid.uuid4(),
        "storage_ref": _ref(row.id, bucket="another-saved-cache"),
        "created_at": row.created_at - timedelta(days=1),
    }[column]
    values = {column: changed}
    if column == "id":
        values["storage_ref"] = _ref(changed)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(ImagePyramidOutput)
                .where(ImagePyramidOutput.id == row.id)
                .values(values)
                .execution_options(synchronize_session=False)
            )


@pytest.mark.parametrize(
    "initial,target",
    [
        ("building", "deleted"),
        ("ready", "building"),
        ("retiring", "ready"),
        ("deleted", "retiring"),
    ],
)
async def test_output_state_never_moves_backwards_or_skips_retirement(
    db_session, initial, target
):
    row = await _new_output(db_session)
    if initial == "ready":
        await _make_ready(db_session, row)
    elif initial in {"retiring", "deleted"}:
        row.state = "retiring"
        await db_session.flush()
        if initial == "deleted":
            row.state = "deleted"
            await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(ImagePyramidOutput)
                .where(ImagePyramidOutput.id == row.id)
                .values(state=target)
                .execution_options(synchronize_session=False)
            )


async def test_inventory_is_published_once_then_retained_through_retirement(db_session):
    row = await _new_output(db_session)
    await _make_ready(db_session, row)
    saved = deepcopy(row.inventory)
    changed = deepcopy(saved)
    first_key = next(iter(changed["objects"]))
    changed["objects"][first_key]["sha256"] = "f" * 64
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(ImagePyramidOutput)
                .where(ImagePyramidOutput.id == row.id)
                .values(inventory=changed)
                .execution_options(synchronize_session=False)
            )
    row.state = "retiring"
    await db_session.flush()
    row.state = "deleted"
    await db_session.flush()
    await db_session.refresh(row)
    assert row.inventory == saved


async def test_unsettled_output_owner_cannot_be_deleted(db_session):
    row = await _new_output(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                delete(ImagePyramidOutput).where(ImagePyramidOutput.id == row.id)
            )


async def test_generation_reference_restricts_even_settled_output_deletion(
    db_session, registered
):
    asset, generation = await _lineage(db_session, registered)
    row = await _new_output(db_session, asset_id=asset.id)
    generation.output_id = row.id
    row.state = "retiring"
    await db_session.flush()
    row.state = "deleted"
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                delete(ImagePyramidOutput).where(ImagePyramidOutput.id == row.id)
            )
    assert await db_session.get(ImagePyramidOutput, row.id) is not None


async def test_business_cascade_retains_physical_output_responsibility(
    db_session, registered
):
    asset, generation = await _lineage(db_session, registered)
    row = await _new_output(db_session, asset_id=asset.id)
    generation.output_id = row.id
    await db_session.flush()
    output_id, asset_id, item_id = row.id, asset.id, registered.item.id
    await db_session.execute(delete(DatasetItem).where(DatasetItem.id == item_id))
    await db_session.flush()
    assert (
        await db_session.scalar(
            select(ImagePyramidAsset.id).where(ImagePyramidAsset.id == asset_id)
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(ImagePyramidGeneration.asset_id).where(
                ImagePyramidGeneration.asset_id == asset_id
            )
        )
        is None
    )
    retained = await db_session.get(
        ImagePyramidOutput, output_id, populate_existing=True
    )
    assert retained is not None
    assert retained.state == "building"
    assert retained.storage_ref["prefix"] == physical.prefix_for(output_id)


async def _leased(db, registered):
    generation = await service.prepare_generation(
        db, registered.owner, force=True, delegation=registered.delegation
    )
    asset = await service.get_asset_for_owner(db, registered.owner)
    token = await service.acquire_generation_lease(db, asset.id, generation.generation)
    output = await db.get(ImagePyramidOutput, generation.output_id)
    return asset, generation, token, output


async def test_expired_lease_reacquisition_allocates_a_new_uuid_owner(
    db_session, registered
):
    asset, generation, token1, output1 = await _leased(db_session, registered)
    generation.lease_expires_at = service.utcnow() - timedelta(seconds=1)
    await db_session.flush()
    token2 = await service.acquire_generation_lease(
        db_session, asset.id, generation.generation
    )
    output2 = await db_session.get(ImagePyramidOutput, generation.output_id)
    assert token1 != token2
    assert output1.id != output2.id
    assert physical.resolve(output1).prefix != physical.resolve(output2).prefix
    assert output1.lease_token == token1 and output2.lease_token == token2
    assert output1.state == output2.state == "building"
    assert generation.attempts == 2


async def test_stale_publish_and_fail_cannot_damage_a_new_lease(db_session, registered):
    asset, generation, token1, output1 = await _leased(db_session, registered)
    old_publication = _publication(registered, asset, generation, output1, token1)
    generation.lease_expires_at = service.utcnow() - timedelta(seconds=1)
    await db_session.flush()
    token2 = await service.acquire_generation_lease(
        db_session, asset.id, generation.generation
    )
    output2_id = generation.output_id
    assert await service.publish_generation(db_session, **old_publication) is False
    await service.fail_generation(
        db_session,
        asset_id=asset.id,
        generation_number=generation.generation,
        lease_token=token1,
        error_code="late-failure",
    )
    await db_session.refresh(generation)
    assert generation.lease_token == token2
    assert generation.output_id == output2_id
    assert generation.status == "building"
    assert (await db_session.get(ImagePyramidOutput, output1.id)).state == "building"
    assert (await db_session.get(ImagePyramidOutput, output2_id)).state == "building"


async def test_publication_rejects_an_expired_current_lease(db_session, registered):
    asset, generation, token, output = await _leased(db_session, registered)
    publication = _publication(registered, asset, generation, output, token)
    generation.lease_expires_at = service.utcnow() - timedelta(seconds=1)
    await db_session.flush()
    assert await service.publish_generation(db_session, **publication) is False
    assert output.state == "building"
    assert asset.active_generation is None
    assert registered.item.thumbnail_path is None


async def test_expiry_reaper_preserves_a_reacquired_live_lease(db_session, registered):
    asset, generation, _, _ = await _leased(db_session, registered)
    generation.lease_expires_at = service.utcnow() - timedelta(seconds=1)
    await db_session.flush()
    live_token = await service.acquire_generation_lease(
        db_session, asset.id, generation.generation
    )
    await service.fail_generation(
        db_session,
        asset_id=asset.id,
        generation_number=generation.generation,
        lease_token=None,
        error_code="lease_expired",
    )
    await db_session.refresh(generation)
    assert generation.status == "building"
    assert generation.lease_token == live_token
    assert generation.lease_expires_at > service.utcnow()


async def test_allocate_rejects_a_binding_bucket_different_from_current_configuration(
    db_session, registered
):
    binding = await db_session.get(StorageBinding, "media-cache")
    binding.bucket = "registered-but-not-configured-pyramid-cache"
    await db_session.flush()
    with pytest.raises(physical.ImagePyramidStorageError) as caught:
        await physical.allocate(db_session, uuid.uuid4())
    assert caught.value.code == "image_pyramid_storage_binding_unproven"


@pytest.mark.parametrize(
    "tamper",
    [
        "missing-tile",
        "bool-size",
        "foreign-inventory-key",
        "manifest-key",
        "overview-key",
        "manifest-digest",
        "overview-digest",
        "tile-count",
        "retained-bytes",
        "dimensions",
    ],
)
async def test_publication_requires_complete_consistent_output_receipts(
    db_session, registered, tamper
):
    asset, generation, token, output = await _leased(db_session, registered)
    publication = _publication(registered, asset, generation, output, token)
    if tamper == "missing-tile":
        key = next(
            key for key in publication["inventory"]["objects"] if "/tiles/" in key
        )
        del publication["inventory"]["objects"][key]
    elif tamper == "bool-size":
        publication["inventory"]["objects"][publication["overview_storage_key"]][
            "size_bytes"
        ] = True
    elif tamper == "foreign-inventory-key":
        publication["inventory"]["objects"][
            physical.prefix_for(uuid.uuid4()) + "overview.webp"
        ] = {"sha256": "a" * 64, "size_bytes": 1}
    elif tamper in {"manifest-key", "overview-key"}:
        field = (
            "manifest_storage_key"
            if tamper == "manifest-key"
            else "overview_storage_key"
        )
        publication[field] = physical.prefix_for(uuid.uuid4()) + "overview.webp"
    elif tamper in {"manifest-digest", "overview-digest"}:
        publication[tamper.replace("-", "_")] = "sha256:" + "f" * 64
    elif tamper == "tile-count":
        publication["tile_count"] += 1
    elif tamper == "retained-bytes":
        publication["retained_bytes"] += 1
    else:
        publication["width"] = 3
    try:
        async with db_session.begin_nested():
            accepted = await service.publish_generation(db_session, **publication)
    except (
        ContentError,
        physical.ImagePyramidStorageError,
        service.ImagePyramidError,
        IntegrityError,
    ):
        accepted = False
    assert accepted is False, f"Publication accepted inconsistent {tamper}"
    await db_session.refresh(generation)
    await db_session.refresh(output)
    assert generation.status == "building"
    assert output.state == "building"
    assert output.inventory is None


async def test_valid_publication_sets_saved_output_and_thumbnail(
    db_session, registered
):
    asset, generation, token, output = await _leased(db_session, registered)
    publication = _publication(registered, asset, generation, output, token)
    assert await service.publish_generation(db_session, **publication) is True
    await db_session.refresh(output)
    await db_session.refresh(registered.item)
    assert generation.status == "ready"
    assert output.state == "ready"
    assert output.inventory == publication["inventory"]
    assert asset.active_generation == generation.generation
    assert registered.item.thumbnail_path == physical.resolve(output).key(
        "overview.webp"
    )


@pytest.fixture
def same_transaction_gc(monkeypatch):
    """Keep physical-provider tests inside the normal rollback-owned DB session.

    The real durable permit lifecycle is covered by storage_access tests. Here we
    preserve GC's DB operation-barrier checks and execute its on_acquired recheck
    without opening an unrelated connection to uncommitted fixture owners.
    """
    calls = []

    async def admitted(
        db, location_id, purpose, call, *args, on_acquired=None, **kwargs
    ):
        calls.append((location_id, purpose))
        gate = await storage_access.state(db, location_id)
        permit = SimpleNamespace(
            id=uuid.uuid4(), location_id=location_id, revision=gate["revision"]
        )
        if on_acquired is not None:
            await on_acquired(permit)
        return call(*args, **kwargs)

    monkeypatch.setattr(storage_access, "run", admitted)
    return calls


async def test_gc_preserves_unknown_legacy_generations_without_default_bucket_scan(
    db_session, registered, same_transaction_gc
):
    asset, generation = await _lineage(db_session, registered, legacy=True)
    generation.status = "failed"
    generation.updated_at = service.utcnow() - timedelta(days=10)
    asset.building_generation = None
    await db_session.flush()
    provider = Provider()
    provider.objects[
        (
            storage_service.media_cache_bucket,
            f"image-pyramids/{asset.id}/g1/overview.webp",
        )
    ] = b"unknown-old-owner"
    assert await objects.reconcile(db_session, SimpleNamespace(client=provider)) == 0
    assert provider.calls == []
    assert same_transaction_gc == []
    assert (
        await db_session.get(ImagePyramidGeneration, (asset.id, generation.generation))
        is not None
    )


async def test_gc_keeps_unsettled_attempt_owner_and_its_objects(
    db_session, registered, same_transaction_gc
):
    row = await _new_output(db_session, old=True)
    operation = StorageOperation(
        id=uuid.uuid4(),
        location_id=LEGACY_LOCATION_ID,
        revision=1,
        purpose=objects.purpose(row.id),
        node_id="pyramid-test:unsettled",
    )
    db_session.add(operation)
    await db_session.flush()
    provider = Provider()
    provider.objects[
        (row.storage_ref["bucket"], physical.resolve(row).key("overview.webp"))
    ] = b"still-live"
    assert await objects.reconcile(db_session, SimpleNamespace(client=provider)) == 0
    await db_session.refresh(row)
    assert row.state == "building"
    assert provider.calls == []
    assert same_transaction_gc == []
    assert await db_session.get(StorageOperation, operation.id) is not None


async def test_gc_deletes_only_the_saved_bucket_and_uuid_prefix(
    db_session, registered, same_transaction_gc
):
    row = await _new_output(db_session, old=True)
    provider = Provider()
    key = physical.resolve(row).key("overview.webp")
    foreign_key = physical.prefix_for(uuid.uuid4()) + "overview.webp"
    provider.objects[(row.storage_ref["bucket"], key)] = b"owned-attempt"
    provider.objects[(storage_service.media_cache_bucket, key)] = (
        b"same-key-new-current-bucket"
    )
    provider.objects[(row.storage_ref["bucket"], foreign_key)] = b"other-owner"
    assert await objects.reconcile(db_session, SimpleNamespace(client=provider)) == 1
    await db_session.refresh(row)
    assert row.state == "deleted"
    assert (row.storage_ref["bucket"], key) not in provider.objects
    assert (
        provider.objects[(storage_service.media_cache_bucket, key)]
        == b"same-key-new-current-bucket"
    )
    assert provider.objects[(row.storage_ref["bucket"], foreign_key)] == b"other-owner"
    assert same_transaction_gc == [(LEGACY_LOCATION_ID, objects.purpose(row.id))]
    assert all(
        params["Bucket"] == row.storage_ref["bucket"] for _, params in provider.calls
    )


async def test_gc_retains_owner_when_physical_delete_cannot_verify_empty(
    db_session, registered, same_transaction_gc
):
    row = await _new_output(db_session, old=True)
    provider = Provider()
    provider.ignore_delete = True
    key = physical.resolve(row).key("overview.webp")
    provider.objects[(row.storage_ref["bucket"], key)] = b"retained-by-provider"
    assert await objects.reconcile(db_session, SimpleNamespace(client=provider)) == 0
    await db_session.refresh(row)
    assert row.state == "retiring"
    assert provider.objects[(row.storage_ref["bucket"], key)] == b"retained-by-provider"


async def test_worker_rejects_unrecorded_delegation_before_storage_construction(
    monkeypatch,
):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(worker, "create_async_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(worker, "async_sessionmaker", lambda *args, **kwargs: Session)
    monkeypatch.setattr(
        worker,
        "StorageService",
        lambda: pytest.fail("unrecorded delegation must not construct storage"),
    )

    async def reject(db, value):
        assert value is None
        raise ContentError("pyramid_authority_unrecorded", 403)

    monkeypatch.setattr(originals, "delegated", reject)
    await worker._run_image_pyramid(
        "task", str(uuid.uuid4()), force=True, delegation=None
    )
    engine.dispose.assert_awaited_once()
