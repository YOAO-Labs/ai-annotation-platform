"""Image bundle coverage and budgets, with caller-owned DB lifecycle checks."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models.export_media_bundle import ExportMediaBundle as Bundle
from app.db.models.export_output import ExportOutput
from app.services import export_media_bundles as bundles
from app.services import export_media_retirement as retirement
from app.services import export_media_storage as physical
from app.services.exporting.lidar_sources import canonical_identity
from app.services.reference_keys import reference_key
from tests import test_export_media_bundle_models as models
from tests import test_export_media_retirement as retirement_tests
from tests.test_export_media_bundles import FakeDB, digest

owned_db = models.owned_db
gc_db = retirement_tests.gc_db


def image_inputs(*, managed=2, references=1, repeat=True, targets=("coco",)):
    project = uuid.uuid4()
    frozen = {
        "schema": 3,
        "project_id": str(project),
        "batch_id": None,
        "task_ids": None,
        "targets": list(targets),
        "options": {},
        "project": {"id": str(project), "data_type": "image"},
        "tasks": [],
        "references": {},
        "media": [],
    }
    logical = {target: {} for target in targets}
    shared = uuid.uuid4()
    for index in range(managed):
        task_id = str(uuid.uuid4())
        content = str(shared if repeat else uuid.uuid4())
        source = {
            "owner_kind": "dataset_asset",
            "content_id": content,
            "sha256": "a" * 64,
            "size_bytes": 11,
            "media_type": "image",
            "item_id": str(shared),
            "relation": {"task_id": task_id, "project_id": str(project), "links": []},
            "bucket": "saved-originals",
            "object_key": f"owned/{content}/image.png",
            "endpoint_url": physical._runtime_endpoint(),
            "location_id": str(physical.LEGACY_LOCATION_ID),
            "location_revision": 1,
            "maintenance_revision": 1,
            "ownership_facts": {"id": content, "sha256": "a" * 64},
            "item_facts": {"id": str(shared), "file_type": "image"},
            "replica_facts": {"asset_id": content, "role": "managed_original"},
            "location_facts": {"id": str(physical.LEGACY_LOCATION_ID), "enabled": True},
        }
        frozen["tasks"].append(
            {
                "id": task_id,
                "project_id": str(project),
                "dataset_item_id": str(shared),
                "file_path": source["object_key"],
                "file_type": "image",
            }
        )
        key = digest(canonical_identity(source))
        frozen["media"].append(
            {
                "task_id": task_id,
                "role": "image",
                "link_id": None,
                "source_key": key,
                "source": source,
            }
        )
        for paths in logical.values():
            paths[f"images/{task_id}/image-{index}.png"] = key
    for _ in range(references):
        task_id, item_id = str(uuid.uuid4()), uuid.uuid4()
        frozen["tasks"].append(
            {
                "id": task_id,
                "project_id": str(project),
                "dataset_item_id": str(item_id),
                "file_path": reference_key(item_id, "b" * 64),
                "file_type": "image",
            }
        )
        frozen["references"][task_id] = {
            "dataset_id": str(uuid.uuid4()),
            "asset_id": str(uuid.uuid4()),
            "sha256": "b" * 64,
            "size": 7,
            "width": 1,
            "height": 1,
            "run_id": str(uuid.uuid4()),
            "accepted_revision_id": str(uuid.uuid4()),
            "source_id": str(uuid.uuid4()),
            "source_revision": 1,
            "location_id": str(uuid.uuid4()),
            "location_revision": 1,
            "maintenance_revision": 1,
            "connection_id": None,
            "connection_revision": None,
        }
    return project, frozen, logical


async def allocate(db=None, **options):
    project, frozen, logical = image_inputs(**options)
    db = db or FakeDB()
    receipt = await bundles.allocate(
        db,
        project_id=project,
        job_id=uuid.uuid4(),
        input_snapshot=frozen,
        input_digest=digest(frozen),
        logical_sources=logical,
    )
    return db, receipt, frozen, logical


async def test_image_targets_deduplicate_full_identity_and_exclude_reference_bytes(
    monkeypatch,
):
    forbidden = Mock(side_effect=AssertionError("no original or provider I/O"))
    monkeypatch.setattr(physical, "create_client", forbidden)
    db, receipt, frozen, logical = await allocate(
        targets=tuple(sorted(bundles.IMAGE_TARGETS))
    )
    assert len(frozen["tasks"]) == 3 and len(frozen["media"]) == 2
    assert receipt.object_count == 1 and receipt.size_bytes == 11
    assert set(receipt.logical_paths["targets"]) == bundles.IMAGE_TARGETS
    assert len(bundles.source_objects(receipt, logical)) == 1
    assert all(len(paths) == 2 for paths in receipt.logical_paths["targets"].values())
    assert bundles.same(db.rows[receipt.id], receipt)
    assert receipt.input_snapshot["references"] == frozen["references"]
    forbidden.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("references", [0, 2])
async def test_empty_and_reference_only_bundles_keep_zero_inventory_and_one_shot_lifecycle(
    references,
):
    db, receipt, frozen, logical = await allocate(
        managed=0, references=references, targets=("coco", "aap_json")
    )
    assert len(frozen["tasks"]) == references
    assert receipt.inventory == {"schema": 1, "objects": {}}
    assert receipt.object_count == receipt.size_bytes == 0
    assert bundles.source_objects(receipt, logical) == {}
    assert receipt.logical_paths == {
        "schema": 1,
        "targets": {"coco": {}, "aap_json": {}},
    }
    claim = await bundles.claim(db, receipt.id)
    assert claim.lease_token is not None
    assert await bundles.claim(db, receipt.id) is None
    row = await bundles.publish(db, claim)
    assert bundles.snapshot(row).state == "ready"
    assert row.expires_at == receipt.created_at + timedelta(days=7)
    assert not await retirement.attempt_ended(retirement_tests.PolicyDB(), row)
    assert await retirement.attempt_ended(
        retirement_tests.PolicyDB(), row, current=row.expires_at
    )
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("owner_kind", ["dataset_asset", "task_original"])
async def test_registered_managed_owner_kinds_keep_distinct_identity_despite_equal_sha(
    owner_kind,
):
    project, frozen, logical = image_inputs(managed=2, references=0, repeat=False)
    for entry in frozen["media"]:
        previous = entry["source_key"]
        entry["source"]["owner_kind"] = owner_kind
        key = entry["source_key"] = digest(canonical_identity(entry["source"]))
        for paths in logical.values():
            for path in paths:
                if paths[path] == previous:
                    paths[path] = key
    receipt = await bundles.allocate(
        FakeDB(),
        project_id=project,
        job_id=uuid.uuid4(),
        input_snapshot=frozen,
        input_digest=digest(frozen),
        logical_sources=logical,
    )
    assert receipt.object_count == 2 and receipt.size_bytes == 22
    assert len(set(bundles.source_objects(receipt, logical).values())) == 2


@pytest.mark.parametrize(
    "fault",
    [
        "foreign_project",
        "lidar_project",
        "foreign_task",
        "duplicate_task",
        "unknown_target",
        "mixed_target",
        "duplicate_target",
        "missing_reference",
        "foreign_reference",
        "fake_reference_path",
        "reference_item",
        "reference_sha",
        "reference_size",
        "reference_bool_size",
        "empty_reference",
        "reference_dict",
        "reference_copy",
        "missing_managed",
        "duplicate_managed",
        "foreign_media",
        "lidar_role",
        "camera_role",
        "link",
        "unowned",
        "malformed_owner",
        "auxiliary",
        "foreign_relation",
        "wrong_type",
        "empty_image",
        "image_bool_size",
        "oversized_image",
        "source_key",
        "extra_entry",
        "missing_target",
        "reference_logical",
        "missing_logical",
    ],
)
async def test_image_input_coverage_and_declared_receipts_fail_before_responsibility(
    fault,
):
    project, frozen, logical = image_inputs(managed=1, references=1)
    entry, task = frozen["media"][0], frozen["tasks"][0]
    reference_id = next(iter(frozen["references"]))
    binding = frozen["references"][reference_id]
    if fault == "foreign_project":
        frozen["project"]["id"] = str(uuid.uuid4())
    elif fault == "lidar_project":
        frozen["project"]["data_type"] = "lidar"
    elif fault == "foreign_task":
        task["project_id"] = str(uuid.uuid4())
    elif fault == "duplicate_task":
        frozen["tasks"].append(deepcopy(task))
    elif fault == "unknown_target":
        frozen["targets"] = ["voc"]
    elif fault == "mixed_target":
        frozen["targets"].append("pointmask")
    elif fault == "duplicate_target":
        frozen["targets"].append("coco")
    elif fault == "missing_reference":
        frozen["references"].clear()
    elif fault == "foreign_reference":
        frozen["references"][str(uuid.uuid4())] = frozen["references"].pop(reference_id)
    elif fault == "fake_reference_path":
        frozen["tasks"][1]["file_path"] = "managed/path.png"
    elif fault == "reference_item":
        frozen["tasks"][1]["dataset_item_id"] = str(uuid.uuid4())
    elif fault == "reference_sha":
        binding["sha256"] = "c" * 64
    elif fault == "reference_size":
        binding["size"] = bundles.MAX_REFERENCE_BYTES + 1
    elif fault == "reference_bool_size":
        binding["size"] = True
    elif fault == "empty_reference":
        binding["size"] = 0
    elif fault == "reference_dict":
        frozen["references"][reference_id] = []
    elif fault == "reference_copy":
        entry["task_id"] = reference_id
    elif fault == "missing_managed":
        frozen["media"].clear()
    elif fault == "duplicate_managed":
        frozen["media"].append(deepcopy(entry))
    elif fault == "foreign_media":
        entry["task_id"] = str(uuid.uuid4())
    elif fault in {"lidar_role", "camera_role"}:
        entry["role"] = "primary_lidar" if fault == "lidar_role" else "camera_front"
    elif fault == "link":
        entry["link_id"] = str(uuid.uuid4())
    elif fault in {"unowned", "auxiliary"}:
        entry["source"]["owner_kind"] = (
            "unknown" if fault == "unowned" else "dataset_auxiliary"
        )
    elif fault == "malformed_owner":
        entry["source"]["owner_kind"] = []
    elif fault == "foreign_relation":
        entry["source"]["relation"]["task_id"] = str(uuid.uuid4())
    elif fault == "wrong_type":
        entry["source"]["media_type"] = "point_cloud"
    elif fault in {"empty_image", "image_bool_size", "oversized_image"}:
        entry["source"]["size_bytes"] = {
            "empty_image": 0,
            "image_bool_size": True,
            "oversized_image": physical.MAX_OBJECT_BYTES + 1,
        }[fault]
    elif fault == "source_key":
        entry["source_key"] = "d" * 64
    elif fault == "extra_entry":
        entry["unsafe"] = True
    elif fault == "missing_target":
        logical.clear()
    elif fault == "reference_logical":
        logical["coco"]["reference.png"] = "b" * 64
    elif fault == "missing_logical":
        logical["coco"].clear()
    db = FakeDB()
    with pytest.raises(bundles.ExportMediaBundleError):
        await bundles.allocate(
            db,
            project_id=project,
            job_id=uuid.uuid4(),
            input_snapshot=frozen,
            input_digest=digest(frozen),
            logical_sources=logical,
        )
    assert not db.rows
    db.flush.assert_not_awaited()


@pytest.mark.parametrize(
    "field",
    [
        "content_id",
        "bucket",
        "object_key",
        "ownership_facts",
        "replica_facts",
        "location_facts",
    ],
)
async def test_image_key_commits_full_canonical_identity_not_only_content_sha(field):
    project, frozen, logical = image_inputs(managed=1, references=0)
    source = frozen["media"][0]["source"]
    if field == "content_id":
        source[field] = str(uuid.uuid4())
    elif isinstance(source[field], dict):
        source[field]["changed"] = True
    else:
        source[field] += "changed"
    with pytest.raises(bundles.ExportMediaBundleError, match="source_changed"):
        await bundles.allocate(
            FakeDB(),
            project_id=project,
            job_id=uuid.uuid4(),
            input_snapshot=frozen,
            input_digest=digest(frozen),
            logical_sources=logical,
        )


async def test_all_tasks_and_each_reference_count_toward_limits_while_managed_copies_deduplicate(
    monkeypatch,
):
    project, frozen, logical = image_inputs(managed=2, references=2)
    # Two aliases of one managed original plus two separately embedded declarations.
    monkeypatch.setattr(bundles, "MAX_IMAGE_BYTES", 11 + 7 + 7)
    receipt = await bundles.allocate(
        FakeDB(),
        project_id=project,
        job_id=uuid.uuid4(),
        input_snapshot=frozen,
        input_digest=digest(frozen),
        logical_sources=logical,
    )
    assert receipt.object_count == 1 and receipt.size_bytes == 11
    monkeypatch.setattr(bundles, "MAX_IMAGE_BYTES", 11 + 7 + 7 - 1)
    with pytest.raises(bundles.ExportMediaBundleError, match="budget"):
        await bundles.allocate(
            FakeDB(),
            project_id=project,
            job_id=uuid.uuid4(),
            input_snapshot=frozen,
            input_digest=digest(frozen),
            logical_sources=logical,
        )
    monkeypatch.setattr(bundles, "MAX_IMAGE_BYTES", 20 * 1024**3)
    monkeypatch.setattr(bundles, "MAX_IMAGES", 3)
    with pytest.raises(bundles.ExportMediaBundleError, match="budget"):
        await bundles.allocate(
            FakeDB(),
            project_id=project,
            job_id=uuid.uuid4(),
            input_snapshot=frozen,
            input_digest=digest(frozen),
            logical_sources=logical,
        )


@pytest.mark.parametrize("references", [0, 2])
async def test_reference_only_scope_still_enforces_task_and_total_declared_byte_budgets(
    monkeypatch, references
):
    project, frozen, logical = image_inputs(managed=0, references=references)
    monkeypatch.setattr(bundles, "MAX_IMAGES", max(0, references - 1))
    if references:
        with pytest.raises(bundles.ExportMediaBundleError, match="budget"):
            await bundles.allocate(
                FakeDB(),
                project_id=project,
                job_id=uuid.uuid4(),
                input_snapshot=frozen,
                input_digest=digest(frozen),
                logical_sources=logical,
            )
        monkeypatch.setattr(bundles, "MAX_IMAGES", 20_000)
        monkeypatch.setattr(bundles, "MAX_IMAGE_BYTES", 13)
        with pytest.raises(bundles.ExportMediaBundleError, match="budget"):
            await bundles.allocate(
                FakeDB(),
                project_id=project,
                job_id=uuid.uuid4(),
                input_snapshot=frozen,
                input_digest=digest(frozen),
                logical_sources=logical,
            )
    else:
        receipt = await bundles.allocate(
            FakeDB(),
            project_id=project,
            job_id=uuid.uuid4(),
            input_snapshot=frozen,
            input_digest=digest(frozen),
            logical_sources=logical,
        )
        assert receipt.object_count == 0


async def test_frozen_reference_json_and_documents_share_bounded_budget(monkeypatch):
    project, frozen, logical = image_inputs(managed=0, references=1)
    encoded = bundles._json_info(frozen)[1]
    monkeypatch.setattr(bundles, "MAX_JSON_BYTES", encoded + 1)
    with pytest.raises(bundles.ExportMediaBundleError, match="budget"):
        await bundles.allocate(
            FakeDB(),
            project_id=project,
            job_id=uuid.uuid4(),
            input_snapshot=frozen,
            input_digest=digest(frozen),
            logical_sources=logical,
        )


@pytest.mark.parametrize("managed,references", [(0, 0), (0, 2), (1, 1)])
async def test_database_image_scopes_publish_exact_zip_pair_atomically(
    gc_db, managed, references, monkeypatch
):
    db = gc_db
    _, receipt, _, _ = await allocate(db, managed=managed, references=references)
    await db.commit()
    claim = await bundles.claim(db, receipt.id)
    await db.commit()
    row = await db.get(Bundle, receipt.id)
    output = await models._zip(db, row, created_at=row.created_at, format="coco")
    await db.commit()
    await bundles.publish(db, claim)
    output.state = "ready"
    await db.flush()
    await db.rollback()
    row = await db.scalar(
        select(Bundle)
        .where(Bundle.id == receipt.id)
        .execution_options(populate_existing=True)
    )
    assert row.state == "building" and row.published_at is None
    output = await db.get(ExportOutput, receipt.zip_output_id)
    await bundles.publish(db, claim)
    output.state = "ready"
    await db.commit()
    assert row.object_count == managed and row.size_bytes == managed * 11
    assert output.media_bundle_id == row.id and output.expires_at == row.expires_at
    assert row.input_snapshot == receipt.input_snapshot
    client = retirement_tests.objects(monkeypatch, row)
    assert (await retirement.cleanup(db))["deleted"] == 0
    assert not client.calls
    # Even an explicit retirement cannot bypass the permanent publication latch.
    row.state, row.retired_at = "retiring", bundles.now()
    await db.commit()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            row.state = "deleted"
            await db.flush()


@pytest.mark.parametrize("fault", ["zip_id", "job", "deadline"])
async def test_database_reference_only_pair_cannot_change_reserved_identity_or_expiry(
    gc_db, fault
):
    db = gc_db
    _, receipt, _, _ = await allocate(db, managed=0, references=1)
    await db.commit()
    row = await db.get(Bundle, receipt.id)
    values = {"created_at": row.created_at, "format": "coco"}
    if fault == "zip_id":
        values["id"] = uuid.uuid4()
    elif fault == "job":
        values["job_id"] = uuid.uuid4()
    else:
        values["expires_at"] = row.expires_at + timedelta(seconds=1)
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await models._zip(db, row, **values)
            await models._constraints(db)


@pytest.mark.parametrize("references", [0, 2])
async def test_database_unpublished_empty_bundle_retires_without_deleting_pending_zip(
    gc_db, references, monkeypatch
):
    db = gc_db
    _, receipt, _, _ = await allocate(db, managed=0, references=references)
    await db.commit()
    row = await db.get(Bundle, receipt.id)
    output = await models._zip(db, row, created_at=row.created_at, format="coco")
    await db.commit()
    client = retirement_tests.objects(monkeypatch, row)
    assert (await retirement.cleanup(db))["deleted"] == 1
    restored = await db.get(Bundle, row.id)
    assert restored.state == "deleted"
    assert restored.inventory == {"schema": 1, "objects": {}}
    assert restored.input_snapshot == receipt.input_snapshot
    assert (await db.get(ExportOutput, output.id)).state == "pending"
    assert not any(method == "delete" for method, _ in client.calls)
    assert client.close_count == 1
