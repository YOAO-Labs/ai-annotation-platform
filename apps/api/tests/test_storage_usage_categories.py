"""Listed bytes and saved ownership are distinct from reservations and references."""

from copy import deepcopy
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import delete, event, text

from app.db.models.avatar_upload import AvatarUpload
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.dataset_auxiliary_original import DatasetAuxiliaryOriginal
from app.db.models.image_pyramid import ImagePyramidOutput
from app.db.models.managed_original_creation import ManagedOriginalCreation
from app.db.models.original_cache import OriginalCacheEntry
from app.db.models.prediction_input import PredictionInputLease
from app.db.models.task_original import TaskOriginal
from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.services import storage_usage as svc, storage_usage_categories as categories
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_original_retirement import (
    capture_managed_original_retirements,
    managed_storage_identity,
)
from app.services.storage import storage_service
from tests.test_storage_usage import ready as ready, run_all, seed_success
from tests.test_training_dataset_storage import database_version


PURPOSES = {
    "prediction_inputs": "import",
    "image_pyramid_storage": "media-cache",
    "video_derivative_storage": "media-cache",
    "export_media_storage": "export",
    "training_dataset_storage": "training-dataset",
    "training_run_storage": "training-run",
    "training_preview_storage": "training-preview",
    "sample_evaluation_storage": "sample-evaluation",
    "active_learning_storage": "active-learning",
    "avatar_storage": "avatars",
    "bug_attachment_storage": "bug-reports",
    "comment_attachment_storage": "annotations",
    "guide_asset_storage": "annotations",
    "raster_object_storage": "annotations",
    "export_storage": "export",
    "audit_archive_storage": "audit-archive",
}


def family_case(index, bucket="listed-bucket"):
    family = categories.FAMILIES[index]
    service = import_module("app.services." + family.service)
    data = {
        name: str(uuid.uuid4())
        for name in ("id", "project_id", "owner_id", "annotation_id", "job_id")
    }
    data.update(
        token=uuid.uuid4().hex,
        mime_type="image/png",
        storage_encoding="identity",
        media="image",
        year=2026,
        month=1,
    )
    address = getattr(service, family.method)(
        *[data[name] for name in family.arguments]
    )
    ref = {
        "schema": 1,
        "purpose": PURPOSES[family.service],
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": categories.endpoint(),
        "bucket": bucket,
        "storage_identity": managed_storage_identity(categories.endpoint(), bucket),
    }
    field = (
        "object_key"
        if not family.prefix
        else "key_prefix"
        if family.service in {"prediction_inputs", "export_media_storage"}
        else "prefix"
    )
    ref[field] = address
    if family.service in {
        "training_dataset_storage",
        "training_run_storage",
        "training_preview_storage",
        "sample_evaluation_storage",
        "active_learning_storage",
    }:
        ref.update(origin="new", evidence=None)
    data["storage_ref"] = ref
    relative = (
        "metadata.json"
        if family.service == "video_derivative_storage"
        else f"{uuid.uuid4()}.bin"
        if family.service == "export_media_storage"
        else "part.bin"
    )
    return family, data, address + relative if family.prefix else address


@pytest.mark.parametrize("index", range(len(categories.FAMILIES)))
async def test_saved_family_helpers_classify_actual_listed_size(index):
    family, data, key = family_case(index)
    db = SimpleNamespace(execute=AsyncMock(return_value=[(f"ref:{index}", data)]))
    result = await categories.classify(db, "listed-bucket", [{"key": key, "size": 7}])
    assert result["categories"][family.category] == {"object_count": 1, "size_bytes": 7}
    assert sum(item["object_count"] for item in result["categories"].values()) == 1
    db.execute.assert_awaited_once()


@pytest.mark.parametrize(
    "service_name,purpose",
    [
        ("avatar_storage", "avatars"),
        ("bug_attachment_storage", "bug-reports"),
        ("audit_archive_storage", "audit-archive"),
    ],
)
@pytest.mark.parametrize("external_endpoint", [False, True])
async def test_prefixed_owner_saved_endpoint_prevents_same_bucket_key_alias(
    external_endpoint,
    service_name,
    purpose,
):
    index = next(
        index
        for index, family in enumerate(categories.FAMILIES)
        if family.service == service_name
    )
    _, data, basename = family_case(index)
    endpoint = (
        "http://other.example:9000" if external_endpoint else categories.endpoint()
    )
    key = f"team/_aap_managed/{purpose}/" + basename
    data["storage_ref"].update(
        schema=2,
        location_id=str(uuid.uuid4()),
        config_revision=2,
        binding_generation=2,
        endpoint_url=endpoint,
        object_key=key,
        storage_identity=managed_storage_identity(endpoint, "listed-bucket"),
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=[(f"ref:{index}", data)]))
    result = await categories.classify(db, "listed-bucket", [{"key": key, "size": 17}])
    expected = "unclassified" if external_endpoint else "other_platform"
    assert result["categories"][expected] == {"object_count": 1, "size_bytes": 17}
    assert sum(item["size_bytes"] for item in result["categories"].values()) == 17
    db.execute.assert_awaited_once()


@pytest.mark.parametrize("bad", ["legacy", "endpoint", "bucket", "identity"])
async def test_old_or_mismatched_pins_keep_listed_bytes_unclassified(bad):
    _, data, key = family_case(0)
    if bad == "legacy":
        data["storage_ref"] = None
    elif bad == "bucket":
        data["storage_ref"]["bucket"] = "other-bucket"
        data["storage_ref"]["storage_identity"] = managed_storage_identity(
            categories.endpoint(), "other-bucket"
        )
    else:
        data["storage_ref"][
            "endpoint_url" if bad == "endpoint" else "storage_identity"
        ] = "http://old.invalid" if bad == "endpoint" else "f" * 64
    db = SimpleNamespace(execute=AsyncMock(return_value=[("ref:0", data)]))
    result = await categories.classify(db, "listed-bucket", [{"key": key, "size": 9}])
    assert result["categories"]["unclassified"] == {"object_count": 1, "size_bytes": 9}


@pytest.mark.parametrize("index", [2, 3])
async def test_restricted_output_prefix_does_not_own_arbitrary_names(index):
    _, data, _ = family_case(index)
    key = (
        data["storage_ref"].get("prefix", data["storage_ref"].get("key_prefix"))
        + "part.bin"
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=[(f"ref:{index}", data)]))
    result = await categories.classify(db, "listed-bucket", [{"key": key, "size": 5}])
    assert result["categories"]["unclassified"]["size_bytes"] == 5


async def test_bad_key_does_not_hide_other_files_of_same_owner():
    _, data, key = family_case(4)
    bad = data["storage_ref"]["prefix"] + "../outside"
    db = SimpleNamespace(execute=AsyncMock(return_value=[("ref:4", data)]))
    result = await categories.classify(
        db, "listed-bucket", [{"key": bad, "size": 3}, {"key": key, "size": 7}]
    )
    assert result["categories"]["training_version"]["size_bytes"] == 7
    assert result["categories"]["unclassified"]["size_bytes"] == 3


async def test_duplicate_claims_collapse_and_cross_category_conflicts_keep_one_object():
    _, data, key = family_case(1)
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=[
                ("managed_original", {"key": key}),
                ("managed_original", {"key": key}),
                ("ref:1", data),
            ]
        )
    )
    result = await categories.classify(db, "listed-bucket", [{"key": key, "size": 11}])
    assert result["conflict_object_count"] == 1
    assert result["categories"]["unclassified"] == {"object_count": 1, "size_bytes": 11}
    assert sum(item["size_bytes"] for item in result["categories"].values()) == 11


async def test_names_alone_do_not_classify_and_lookup_is_one_bounded_sql(db_session):
    objects = [
        {"key": family_case(index)[2], "size": index + 1}
        for index in range(len(categories.FAMILIES))
    ]
    objects.extend(
        {"key": f"{namespace}/{uuid.uuid4()}/{uuid.uuid4()}/payload", "size": 2}
        for namespace in ("__aap_task_originals__", "__aap_auxiliary_originals__")
    )
    await db_session.connection()
    statements = []

    def seen(*args):
        statements.append(args[2])

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", seen)
    try:
        result = await categories.classify(db_session, "listed-bucket", objects)
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", seen)
    assert len(statements) == 1
    assert result["categories"]["unclassified"] == {
        "object_count": len(objects),
        "size_bytes": sum(row["size"] for row in objects),
    }


async def create_categories_world(db, actor, bucket):
    await ensure_legacy_bindings(db)
    dataset = Dataset(
        display_id="usage-" + uuid.uuid4().hex[:10],
        name="Usage categories",
        created_by=actor.id,
    )
    db.add(dataset)
    await db.flush()
    for _ in range(2):
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name="original.png",
            file_path="usage/original.png",
            file_type="image",
        )
        db.add(item)
        await db.flush()
        asset = DatasetAsset(
            id=uuid.uuid4(), dataset_item_id=item.id, media_type="image"
        )
        db.add(asset)
        await db.flush()
        db.add(
            DatasetAssetLocation(
                asset_id=asset.id,
                location_id=LEGACY_LOCATION_ID,
                bucket=bucket,
                object_key=item.file_path,
            )
        )
    await db.flush()
    await capture_managed_original_retirements(db, dataset_id=dataset.id)
    version = await database_version(db, actor)
    _, version_data, _ = family_case(4, bucket)
    from app.services.training_dataset_storage import prefix_for

    version.storage_ref = {
        **version_data["storage_ref"],
        "prefix": prefix_for(version.project_id, version.id),
    }
    version.size_bytes = 1000000
    pyramid_id, cache_id = uuid.uuid4(), uuid.uuid4()
    _, pyramid, _ = family_case(1, bucket)
    from app.services.image_pyramid_storage import prefix_for as pyramid_prefix

    pyramid_ref = {**pyramid["storage_ref"], "prefix": pyramid_prefix(pyramid_id)}
    db.add(
        ImagePyramidOutput(
            id=pyramid_id,
            asset_id=uuid.uuid4(),
            generation=1,
            lease_token=uuid.uuid4(),
            storage_ref=pyramid_ref,
            state="building",
        )
    )
    moment = svc.now()
    cache = OriginalCacheEntry(
        id=cache_id,
        scope_location_id=LEGACY_LOCATION_ID,
        source_id=uuid.uuid4(),
        asset_id=uuid.uuid4(),
        sha256="a" * 64,
        size_bytes=999999,
        source_revision=1,
        policy_revision=1,
        location_revision=1,
        location_id=LEGACY_LOCATION_ID,
        bucket=bucket,
        binding_generation=1,
        storage_identity=managed_storage_identity(categories.endpoint(), bucket),
        state="deleting",
        uncertain_write=True,
        last_accessed_at=moment,
        expires_at=moment,
        writer_until=moment,
        retain_until=moment,
        next_check_at=moment,
    )
    db.add(cache)
    _, avatar, avatar_key = family_case(9, bucket)
    db.add(
        AvatarUpload(
            token=avatar["token"],
            owner_id=actor.id,
            storage_ref=avatar["storage_ref"],
            sha256="b" * 64,
            size_bytes=9999,
            state="pending",
        )
    )
    _, inputs, input_key = family_case(0, bucket)
    db.add(
        PredictionInputLease(
            id=uuid.UUID(inputs["id"]),
            owner_id=uuid.uuid4(),
            owner_kind="batch_predict",
            task_id=uuid.uuid4(),
            contract_version=2,
            storage_ref=inputs["storage_ref"],
            maintenance_revision=1,
            inventory={"schema": 1, "objects": {}},
            bucket=bucket,
            storage_identity=managed_storage_identity(categories.endpoint(), bucket),
            state="active",
            uncertain_write=True,
            expires_at=moment + timedelta(hours=2),
            retain_until=moment + timedelta(hours=2, days=7),
            next_cleanup_at=moment + timedelta(hours=2, days=7),
            created_at=moment,
        )
    )
    await db.commit()
    from app.services.original_cache import object_key

    objects = [
        {"key": "usage/original.png", "size": 3},
        {"key": pyramid_prefix(pyramid_id) + "tile.webp", "size": 5},
        {"key": object_key(cache), "size": 7},
        {
            "key": prefix_for(version.project_id, version.id) + "media/original.png",
            "size": 11,
        },
        {
            "key": prefix_for(version.project_id, version.id)
            + "attempts/first/dataset.zip",
            "size": 13,
        },
        {"key": input_key, "size": 17},
        {"key": avatar_key, "size": 19},
        {"key": "unknown/", "size": 23},
    ]
    return dataset, version, objects


@pytest.mark.parametrize("kind", ["task", "dataset_auxiliary"])
async def test_restored_current_original_locator_is_independent_of_creation(
    ready, kind, monkeypatch
):
    db, actor = ready
    await ensure_legacy_bindings(db)
    identifier, business_id, project_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    namespace = (
        "__aap_task_originals__" if kind == "task" else "__aap_auxiliary_originals__"
    )
    key = f"{namespace}/{business_id}/{identifier}/payload"
    old_bucket, bucket = "before-restore", storage_service.bucket
    moment = svc.now()
    creation = ManagedOriginalCreation(
        id=identifier,
        owner_kind=kind,
        task_id=business_id if kind == "task" else None,
        project_id=project_id if kind == "task" else None,
        dataset_id=business_id if kind != "task" else None,
        location_id=LEGACY_LOCATION_ID,
        storage_identity=managed_storage_identity(categories.endpoint(), old_bucket),
        bucket=old_bucket,
        object_key=key,
        logical_path="original.bin",
        origin="direct" if kind == "task" else "zip",
        staging_bucket=old_bucket if kind == "task" else None,
        staging_key="staging/" + str(identifier) if kind == "task" else None,
        max_bytes=1024,
        timeout_seconds=60,
        content_type="application/octet-stream",
        requested_by=actor.id,
        authority={"auxiliary": {"kind": "raw_lidar", "source_contract": {}}},
        configuration={"endpoint": categories.endpoint()},
        input_fingerprint="c" * 64,
        maintenance_revision=1,
        status="verified",
        operation_id=uuid.uuid4(),
        prepared={},
        sha256="d" * 64,
        md5="a" * 32,
        size_bytes=999,
        receipt={},
        etag="source-etag",
        verification={},
        claimed_at=moment,
        frozen_at=moment,
        write_started_at=moment,
        acknowledged_at=moment,
        readback_at=moment,
        settled_at=moment,
        completed_at=moment,
    )
    db.add(creation)
    await db.flush()
    model = TaskOriginal if kind == "task" else DatasetAuxiliaryOriginal
    owner = model(
        id=identifier,
        creation_id=identifier,
        location_id=LEGACY_LOCATION_ID,
        storage_identity=creation.storage_identity,
        bucket=old_bucket,
        object_key=key,
        sha256="d" * 64,
        size_bytes=999,
        etag="source-etag",
        created_at=moment,
        **(
            {"task_id": business_id, "project_id": project_id}
            if kind == "task"
            else {
                "dataset_id": business_id,
                "kind": "raw_lidar",
                "source_contract": {},
            }
        ),
    )
    db.add(owner)
    await db.commit()
    result = await categories.classify(db, old_bucket, [{"key": key, "size": 31}])
    assert result["categories"]["managed_original"] == {
        "object_count": 1,
        "size_bytes": 31,
    }
    if kind == "task":
        staging = await categories.classify(
            db, old_bucket, [{"key": creation.staging_key, "size": 7}]
        )
        assert staging["categories"]["temporary"] == {
            "object_count": 1,
            "size_bytes": 7,
        }
    # Use the existing offline restore gate without changing immutable creation history.
    await db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    owner.bucket = bucket
    owner.storage_identity = managed_storage_identity(categories.endpoint(), bucket)
    await db.commit()
    result = await categories.classify(db, bucket, [{"key": key, "size": 31}])
    assert result["categories"]["managed_original"] == {
        "object_count": 1,
        "size_bytes": 31,
    }
    assert creation.bucket == old_bucket
    monkeypatch.setattr(categories, "endpoint", lambda: "http://different.invalid")
    result = await categories.classify(db, bucket, [{"key": key, "size": 31}])
    assert result["categories"]["unclassified"]["size_bytes"] == 31


async def test_real_saved_owners_retirements_and_training_prefix_use_physical_rows(
    ready,
):
    db, actor = ready
    bucket = storage_service.datasets_bucket
    dataset, version, objects = await create_categories_world(db, actor, bucket)
    result = await categories.classify(db, bucket, objects)
    assert {
        name: item["size_bytes"] for name, item in result["categories"].items()
    } == {
        "managed_original": 3,
        "preview_cache": 12,
        "training_version": 24,
        "temporary": 17,
        "other_platform": 19,
        "unclassified": 23,
    }
    # Business deletion and an outstanding deletion intent do not erase listed bytes.
    db.add(
        TrainingStorageDeletion(
            bucket=bucket,
            prefix=version.storage_ref["prefix"],
            storage_ref=deepcopy(version.storage_ref),
        )
    )
    await db.delete(version)
    await db.execute(delete(DatasetItem).where(DatasetItem.dataset_id == dataset.id))
    await db.commit()
    assert await categories.classify(db, bucket, objects) == result


@pytest.mark.parametrize("failure", ["listing", "classification"])
async def test_inventory_publishes_coherent_categories_and_keeps_success_on_failure(
    ready, monkeypatch, failure
):
    db, actor = ready
    _, _, objects = await create_categories_world(db, actor, storage_service.bucket)
    from app.workers import storage_usage as worker

    def page(name, token):
        entries = (
            objects[4:]
            if token
            else objects[:4]
            if name == storage_service.bucket
            else []
        )
        return {
            "objects": entries,
            "count": len(entries),
            "bytes": sum(row["size"] for row in entries),
            "token": "next" if name == storage_service.bucket and not token else None,
        }

    monkeypatch.setattr(worker, "list_page", page)
    job, _ = await svc.start(db, actor)
    await db.commit()
    await run_all(db, job)
    response = await svc.read_usage(db)
    before = response.breakdown.model_dump()
    assert job.status == "completed" and response.total_size_bytes == 98
    assert before["sampled_size_bytes"] == 98 and before["coverage"] == "partial"
    assert before["measured_bucket_count"] == before["bucket_count"] == 9
    if failure == "listing":
        monkeypatch.setattr(
            worker,
            "list_page",
            lambda *args: (_ for _ in ()).throw(OSError("listing failed")),
        )
    else:
        monkeypatch.setattr(
            categories,
            "classify",
            AsyncMock(side_effect=RuntimeError("classification failed")),
        )
    job, _ = await svc.start(db, actor)
    await db.commit()
    await run_all(db, job)
    after = (await svc.read_usage(db)).breakdown.model_dump()
    assert job.status == "failed" and after["has_stale_data"]
    assert {k: v for k, v in after.items() if k != "has_stale_data"} == {
        k: v for k, v in before.items() if k != "has_stale_data"
    }


async def test_old_checkpoint_does_not_publish_partial_classification(
    ready, monkeypatch
):
    db, actor = ready
    from app.db.models.storage_usage import StorageUsageInventory
    from app.workers import storage_usage as worker

    job, _ = await svc.start(db, actor)
    await db.commit()
    inventory = await db.get(StorageUsageInventory, "platform")
    inventory.checkpoint = {
        "count": 1,
        "bytes": 5,
        "pages": 1,
        "token": "old",
        "started_at": svc.now().isoformat(),
    }
    await db.commit()
    monkeypatch.setattr(
        worker,
        "list_page",
        lambda *args: {"count": 0, "bytes": 0, "objects": [], "token": None},
    )
    await run_all(db, job)
    result = (await svc.read_usage(db)).breakdown
    assert result.measured_bucket_count == 8 and result.total_size_bytes is None


async def test_old_snapshot_totals_survive_without_classification(ready):
    db, _ = ready
    row = await seed_success(db)
    assert (await svc.read_usage(db)).breakdown.coverage == "none"
    value = categories.empty()
    value["categories"]["other_platform"] = {"object_count": 7, "size_bytes": 123}
    row.category_totals = categories.stamp(value, 7, 123, row.measured_at)
    await db.commit()
    assert (await svc.read_usage(db)).breakdown.measured_bucket_count == 1
    row.measured_at = svc.now()
    await db.commit()
    assert (await svc.read_usage(db)).breakdown.coverage == "none"
