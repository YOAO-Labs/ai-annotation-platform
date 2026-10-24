"""Pure bundle contract tests plus explicit disposable-DB transaction tests."""

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.export_media_bundle import ExportMediaBundle as Bundle
from app.services import export_media_bundles as bundles
from app.services import export_media_storage as physical
from tests import test_export_media_bundle_models as model_tests

bundle_owned_db = model_tests.owned_db
create_zip = model_tests._zip


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def inputs(*, targets=("kitti", "pointmask", "coco-multicamera"), repeat=True, tasks=2):
    project = uuid.uuid4()
    frozen = {
        "schema": 1,
        "project_id": str(project),
        "targets": list(targets),
        "options": {"lidar": {"kitti_camera_role": "camera_front"}},
        "tasks": [],
        "media": [],
        "private": {"说明": "冻结输入"},
    }
    logical = {
        target: {}
        for target in targets
        if target in bundles.TARGETS
        or (target == "nuscenes" and bundles.TARGETS.intersection(targets))
    }
    for index in range(tasks):
        task = str(uuid.uuid4())
        frozen["tasks"].append({"id": task, "project_id": str(project)})
        roles = []
        if {"kitti", "pointmask", "nuscenes"}.intersection(targets):
            roles.append("primary_lidar")
        roles += ["camera_front"]
        if {"pointmask", "coco-multicamera"}.intersection(targets):
            roles.append("camera_rear")
        for role in roles:
            original = role + ("" if repeat else str(index))
            source = {
                "owner_kind": "dataset_asset",
                "content_id": str(uuid.uuid5(uuid.NAMESPACE_URL, original)),
                "sha256": hashlib.sha256(role.encode()).hexdigest(),
                "size_bytes": len(role),
                "media_type": "point_cloud" if role == "primary_lidar" else "image",
                "bucket": "saved-originals",
                "object_key": original + ".bin",
                "location_id": str(physical.LEGACY_LOCATION_ID),
                "endpoint_url": physical._runtime_endpoint(),
                "relation": {"task_id": task, "links": []},
            }
            key = digest({**source, "relation": None})
            frozen["media"].append(
                {
                    "task_id": task,
                    "role": role,
                    "link_id": None if role == "primary_lidar" else str(uuid.uuid4()),
                    "source_key": key,
                    "source": source,
                }
            )
            for target in logical:
                if target == "kitti" and role not in {"primary_lidar", "camera_front"}:
                    continue
                if target == "coco-multicamera" and role == "primary_lidar":
                    continue
                if target == "nuscenes" and role != "primary_lidar":
                    continue
                logical[target][f"{index}/{role}.bin"] = key
    return project, frozen, logical


class FakeSavepoint:
    def __init__(self):
        self.commit, self.rollback = AsyncMock(), AsyncMock()


class FakeDB:
    def __init__(self):
        self.rows, self.statements, self.savepoints = {}, [], []
        self.no_autoflush = nullcontext()
        self.commit, self.rollback, self.flush = AsyncMock(), AsyncMock(), AsyncMock()
        self.failure = None

    def add(self, row):
        self.rows[row.id] = row

    async def connection(self):
        return self

    async def begin_nested(self):
        savepoint = FakeSavepoint()
        self.savepoints.append(savepoint)
        return savepoint

    def current(self, statement):
        self.statements.append(statement)
        if self.failure:
            raise self.failure
        identifier = statement.compile().params["id_1"]
        return self.rows.get(identifier)

    async def scalar(self, statement):
        return self.current(statement)

    async def execute(self, statement):
        row = self.current(statement)
        data = (
            None
            if row is None
            else {
                column.name: getattr(row, column.name)
                for column in statement.selected_columns
            }
        )
        return SimpleNamespace(
            mappings=lambda: SimpleNamespace(one_or_none=lambda: data)
        )


async def allocated(db=None, **options):
    db = db or FakeDB()
    project, frozen, logical = inputs(**options)
    receipt = await bundles.allocate(
        db,
        project_id=project,
        job_id=uuid.uuid4(),
        input_snapshot=frozen,
        input_digest=digest(frozen),
        logical_sources=logical,
    )
    return db, receipt, frozen, logical


async def test_allocate_freezes_exact_input_preallocates_deduplicated_keys_and_never_commits(
    monkeypatch,
):
    forbidden = Mock(side_effect=AssertionError("no physical client"))
    monkeypatch.setattr(physical, "create_client", forbidden)
    monkeypatch.setattr(physical.settings, "minio_export_bucket", "allocated-export")
    db, receipt, frozen, logical = await allocated()
    assert receipt.state == "building" and receipt.lease_token is None
    assert receipt.zip_output_id != receipt.id
    assert receipt.expires_at == receipt.created_at + timedelta(hours=168)
    assert receipt.storage_ref["bucket"] == "allocated-export"
    assert receipt.object_count == 3
    assert receipt.size_bytes == sum(
        len(role) for role in ("primary_lidar", "camera_front", "camera_rear")
    )
    objects = bundles.source_objects(receipt, logical)
    assert len(objects) == len(set(objects.values())) == 3
    for target, paths in logical.items():
        assert set(paths) == set(receipt.logical_paths["targets"][target])
        for path, key in paths.items():
            assert receipt.logical_paths["targets"][target][path] == objects[key]
    frozen["private"]["说明"] = "变更"
    for name in ("input_snapshot", "logical_paths", "inventory", "storage_ref"):
        getattr(receipt, name).clear()
        assert getattr(receipt, name)
    assert receipt.input_snapshot["private"]["说明"] == "冻结输入"
    assert bundles.same(db.rows[receipt.id], receipt)
    db.flush.assert_awaited_once()
    db.commit.assert_not_awaited()
    forbidden.assert_not_called()
    with pytest.raises(AttributeError):
        receipt.state = "ready"


async def test_same_bytes_with_different_source_identity_get_distinct_objects_and_recover_without_sha_guess():
    _, receipt, frozen, logical = await allocated(repeat=False)
    objects = bundles.source_objects(receipt, logical)
    assert len(objects) == receipt.object_count == 6
    pairs = [entry for entry in frozen["media"] if entry["role"] == "camera_front"]
    assert pairs[0]["source"]["sha256"] == pairs[1]["source"]["sha256"]
    assert objects[pairs[0]["source_key"]] != objects[pairs[1]["source_key"]]


@pytest.mark.parametrize(
    "targets, count",
    [
        (("kitti",), 2),
        (("pointmask",), 3),
        (("coco-multicamera",), 2),
        (("kitti", "nuscenes"), 2),
        (("coco-multicamera", "nuscenes"), 3),
    ],
)
async def test_required_media_is_specific_to_each_target_including_mixed_targets(
    targets, count
):
    _, receipt, _, logical = await allocated(targets=targets)
    assert receipt.object_count == count
    assert set(receipt.logical_paths["targets"]) == set(targets)
    assert len(bundles.source_objects(receipt, logical)) == count


@pytest.mark.parametrize(
    "fault",
    [
        "digest",
        "source_digest",
        "same_source_changed",
        "unknown_source",
        "missing_source",
        "extra_target",
        "missing_target",
        "foreign_task",
        "wrong_type",
        "bool_size",
        "missing_primary",
        "missing_selected_camera",
        "missing_coco_camera",
        "extra_media",
    ],
)
async def test_invalid_or_incomplete_inputs_are_rejected_before_responsibility(fault):
    db = FakeDB()
    project, frozen, logical = inputs(tasks=1)
    if fault == "source_digest":
        frozen["media"][0]["source_key"] = "f" * 64
    elif fault == "same_source_changed":
        entry = deepcopy(frozen["media"][0])
        entry["source"]["size_bytes"] += 1
        entry["role"], entry["link_id"] = "camera_other", str(uuid.uuid4())
        frozen["media"].append(entry)
    elif fault == "unknown_source":
        logical["kitti"]["unknown.bin"] = "f" * 64
    elif fault == "missing_source":
        logical["kitti"].pop(next(iter(logical["kitti"])))
    elif fault == "extra_target":
        logical["nuscenes"] = {}
    elif fault == "missing_target":
        logical.pop("pointmask")
    elif fault == "foreign_task":
        frozen["media"][0]["task_id"] = str(uuid.uuid4())
    elif fault == "wrong_type":
        frozen["media"][0]["source"]["media_type"] = "image"
    elif fault == "bool_size":
        frozen["media"][0]["source"]["size_bytes"] = True
    elif fault == "missing_primary":
        frozen["media"] = [
            entry for entry in frozen["media"] if entry["role"] != "primary_lidar"
        ]
    elif fault == "missing_selected_camera":
        frozen["media"] = [
            entry for entry in frozen["media"] if entry["role"] != "camera_front"
        ]
    elif fault == "missing_coco_camera":
        frozen["media"] = [
            entry for entry in frozen["media"] if entry["role"] == "primary_lidar"
        ]
    elif fault == "extra_media":
        frozen["targets"], logical = ["kitti"], {"kitti": logical["kitti"]}
    supplied = "0" * 64 if fault == "digest" else digest(frozen)
    with pytest.raises(bundles.ExportMediaBundleError):
        await bundles.allocate(
            db,
            project_id=project,
            job_id=uuid.uuid4(),
            input_snapshot=frozen,
            input_digest=supplied,
            logical_sources=logical,
        )
    assert not db.rows
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "path",
    [
        "../a.bin",
        "/a.bin",
        "x//a.bin",
        "x/./a.bin",
        "x/../a.bin",
        "C:/a.bin",
        "x\\a.bin",
        "x\x00a.bin",
        "x\na.bin",
        "a" * 4097,
        "",
    ],
)
async def test_unsafe_logical_paths_are_rejected(path):
    db = FakeDB()
    project, frozen, logical = inputs(tasks=1)
    key = next(iter(logical["kitti"].values()))
    logical["kitti"][path] = key
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


@pytest.mark.parametrize(
    "limit", ["json", "images", "pcds", "image_bytes", "pcd_bytes", "object_bytes"]
)
async def test_allocation_budgets_apply_to_unique_sources_and_combined_json(
    monkeypatch, limit
):
    project, frozen, logical = inputs()
    if limit == "json":
        monkeypatch.setattr(
            bundles,
            "MAX_JSON_BYTES",
            len(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()) + 1,
        )
    elif limit == "images":
        monkeypatch.setattr(bundles, "MAX_IMAGES", 1)
    elif limit == "pcds":
        monkeypatch.setattr(bundles, "MAX_PCDS", 0)
    elif limit == "image_bytes":
        monkeypatch.setattr(bundles, "MAX_IMAGE_BYTES", 1)
    elif limit == "pcd_bytes":
        monkeypatch.setattr(bundles, "MAX_PCD_BYTES", 1)
    else:
        monkeypatch.setattr(physical, "MAX_OBJECT_BYTES", 1)
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


@pytest.mark.parametrize(
    "value", [float("nan"), {1: "non-string key"}, ("tuple",), "zero\x00byte"]
)
async def test_only_exact_finite_json_is_frozen(value):
    db = FakeDB()
    project, frozen, logical = inputs()
    frozen["private"] = value
    with pytest.raises(bundles.ExportMediaBundleError):
        await bundles.allocate(
            db,
            project_id=project,
            job_id=uuid.uuid4(),
            input_snapshot=frozen,
            input_digest="a" * 64,
            logical_sources=logical,
        )
    assert not db.rows


async def test_aliases_share_one_object_but_reproduced_names_must_match_exactly():
    project, frozen, logical = inputs()
    key = next(iter(logical["kitti"].values()))
    logical["kitti"]["别名/原始文件.pcd"] = key
    db = FakeDB()
    receipt = await bundles.allocate(
        db,
        project_id=project,
        job_id=uuid.uuid4(),
        input_snapshot=frozen,
        input_digest=digest(frozen),
        logical_sources=logical,
    )
    assert len(bundles.source_objects(receipt, logical)) == 3
    logical["kitti"].pop("别名/原始文件.pcd")
    with pytest.raises(bundles.ExportMediaBundleError):
        bundles.source_objects(receipt, logical)


async def test_source_alignment_refuses_one_source_aliasing_different_identical_byte_objects():
    project, frozen, logical = inputs(repeat=False)
    key = logical["kitti"]["0/camera_front.bin"]
    logical["kitti"]["alias.bin"] = key
    receipt = await bundles.allocate(
        FakeDB(),
        project_id=project,
        job_id=uuid.uuid4(),
        input_snapshot=frozen,
        input_digest=digest(frozen),
        logical_sources=logical,
    )
    objects = bundles.source_objects(receipt, logical)
    other = logical["kitti"]["1/camera_front.bin"]
    assert (
        receipt.inventory["objects"][objects[key]]
        == receipt.inventory["objects"][objects[other]]
    )
    paths = receipt.logical_paths
    paths["targets"]["kitti"]["alias.bin"] = objects[other]
    altered = replace(receipt, _logical_paths=paths)
    with pytest.raises(bundles.ExportMediaBundleError):
        bundles.source_objects(altered, logical)


async def test_stored_mapping_and_inventory_are_included_in_combined_json_budget(
    monkeypatch,
):
    project, frozen, logical = inputs()
    size = sum(
        len(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())
        for value in (frozen, logical)
    )
    monkeypatch.setattr(bundles, "MAX_JSON_BYTES", size + 64)
    db = FakeDB()
    with pytest.raises(bundles.ExportMediaBundleError) as failure:
        await bundles.allocate(
            db,
            project_id=project,
            job_id=uuid.uuid4(),
            input_snapshot=frozen,
            input_digest=digest(frozen),
            logical_sources=logical,
        )
    assert failure.value.code == "export_media_bundle_budget"
    assert not db.rows


@pytest.mark.parametrize(
    "fault", ["missing_slot", "missing_pcd", "camera_in_private_slot"]
)
async def test_mixed_nuscenes_private_slot_is_only_canonical_primary_support(fault):
    project, frozen, logical = inputs(targets=("coco-multicamera", "nuscenes"))
    if fault == "missing_slot":
        del logical["nuscenes"]
    elif fault == "missing_pcd":
        frozen["media"] = [
            entry for entry in frozen["media"] if entry["role"] != "primary_lidar"
        ]
    else:
        logical["nuscenes"]["support/camera.bin"] = next(
            iter(logical["coco-multicamera"].values())
        )
    with pytest.raises(bundles.ExportMediaBundleError):
        await bundles.allocate(
            FakeDB(),
            project_id=project,
            job_id=uuid.uuid4(),
            input_snapshot=frozen,
            input_digest=digest(frozen),
            logical_sources=logical,
        )


async def test_claim_is_once_fixed_hour_and_never_commits_or_renews(monkeypatch):
    db, receipt, _, _ = await allocated()
    started = receipt.created_at + timedelta(minutes=1)
    monkeypatch.setattr(bundles, "now", lambda: started)
    claim = await bundles.claim(db, receipt.id)
    assert claim.lease_token and claim.lease_expires_at == started + timedelta(
        seconds=3600
    )
    assert await bundles.claim(db, receipt.id) is None
    monkeypatch.setattr(
        bundles, "now", lambda: claim.lease_expires_at + timedelta(seconds=1)
    )
    assert await bundles.claim(db, receipt.id) is None
    assert db.rows[receipt.id].lease_token == claim.lease_token
    db.commit.assert_not_awaited()
    assert all(query._for_update_arg.nowait for query in db.statements)


async def test_claim_lease_never_outlives_common_expiry(monkeypatch):
    db, receipt, _, _ = await allocated()
    monkeypatch.setattr(
        bundles, "now", lambda: receipt.expires_at - timedelta(seconds=9)
    )
    claim = await bundles.claim(db, receipt.id)
    assert claim.lease_expires_at == receipt.expires_at


@pytest.mark.parametrize(
    "fault", ["missing", "expired", "invalid_digest", "invalid_lease"]
)
async def test_missing_or_invalid_unclaimed_owner_is_not_a_duplicate(
    monkeypatch, fault
):
    db, receipt, _, _ = await allocated()
    row = db.rows[receipt.id]
    if fault == "missing":
        del db.rows[receipt.id]
    elif fault == "expired":
        monkeypatch.setattr(bundles, "now", lambda: receipt.expires_at)
    elif fault == "invalid_digest":
        row.input_digest = "e" * 64
    else:
        row.lease_token = uuid.uuid4()
    with pytest.raises(bundles.ExportMediaBundleError):
        await bundles.claim(db, receipt.id)


async def test_check_claim_reads_only_small_fence_columns_and_no_document_copies(
    monkeypatch,
):
    db, receipt, _, _ = await allocated()
    claim = await bundles.claim(db, receipt.id)
    forbidden = Mock(side_effect=AssertionError("per-object full input scan"))
    monkeypatch.setattr(bundles, "snapshot", forbidden)
    current = await bundles.check_claim(db, claim, lock=True)
    assert isinstance(current, bundles.BundleFence)
    assert current.lease_token == claim.lease_token
    assert not {"input_snapshot", "inventory", "logical_paths"}.intersection(
        db.statements[-1].selected_columns.keys()
    )
    db.commit.assert_not_awaited()
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "fault",
    [
        "token",
        "lease_time",
        "expired",
        "state",
        "digest",
        "scope",
        "storage",
        "missing",
    ],
)
async def test_check_claim_refuses_lost_identity_state_and_deadline(monkeypatch, fault):
    db, receipt, _, _ = await allocated()
    claim = await bundles.claim(db, receipt.id)
    row = db.rows[receipt.id]
    if fault == "token":
        row.lease_token = uuid.uuid4()
    elif fault == "lease_time":
        row.lease_expires_at += timedelta(seconds=1)
    elif fault == "expired":
        monkeypatch.setattr(bundles, "now", lambda: claim.lease_expires_at)
    elif fault == "state":
        row.state, row.retired_at = "retiring", bundles.now()
    elif fault == "digest":
        row.input_digest = "d" * 64
    elif fault == "scope":
        row.job_id = uuid.uuid4()
    elif fault == "storage":
        row.storage_ref = physical.make_ref(row.id, bucket="restored-bucket")
    else:
        del db.rows[receipt.id]
    with pytest.raises(bundles.ExportMediaBundleError):
        await bundles.check_claim(db, claim)


@pytest.mark.parametrize("sqlstate", ["55P03", "40P01", "40001"])
async def test_nowait_conflict_is_explicit_and_does_not_rollback_or_commit_caller(
    sqlstate,
):
    db, receipt, _, _ = await allocated()
    failure = RuntimeError("row locked")
    failure.sqlstate = sqlstate
    db.failure = DBAPIError("SELECT", {}, failure)
    with pytest.raises(bundles.ExportMediaBundleError) as caught:
        await bundles.claim(db, receipt.id)
    assert caught.value.code == "export_media_bundle_busy"
    db.savepoints[-1].rollback.assert_awaited_once()
    db.rollback.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_publish_joins_transaction_after_full_same_and_cannot_publish_twice():
    db, receipt, _, _ = await allocated()
    claim = await bundles.claim(db, receipt.id)
    row = await bundles.publish(db, claim)
    assert row.state == "ready" and row.published_at is not None
    assert (
        row.lease_token == claim.lease_token
        and row.lease_expires_at == claim.lease_expires_at
    )
    assert bundles.same(row, claim)
    db.commit.assert_not_awaited()
    with pytest.raises(bundles.ExportMediaBundleError):
        await bundles.publish(db, claim)


@pytest.mark.parametrize("fault", ["input", "mapping", "inventory"])
async def test_final_publication_refuses_changed_complete_documents(fault):
    db, receipt, _, _ = await allocated()
    claim = await bundles.claim(db, receipt.id)
    row = db.rows[receipt.id]
    if fault == "input":
        row.input_snapshot["private"]["说明"] = "变更"
    elif fault == "mapping":
        paths = row.logical_paths["targets"]["kitti"]
        paths["other-name.bin"] = paths.pop(next(iter(paths)))
    else:
        next(iter(row.inventory["objects"].values()))["size_bytes"] += 1
    with pytest.raises(bundles.ExportMediaBundleError):
        await bundles.publish(db, claim)
    assert row.state == "building" and row.published_at is None


async def test_publication_rechecks_clock_after_complete_json_comparison(monkeypatch):
    db, receipt, _, _ = await allocated()
    claim = await bundles.claim(db, receipt.id)
    original = bundles.same

    def expires_during_comparison(row, expected):
        result = original(row, expected)
        monkeypatch.setattr(bundles, "now", lambda: claim.lease_expires_at)
        return result

    monkeypatch.setattr(bundles, "same", expires_during_comparison)
    with pytest.raises(bundles.ExportMediaBundleError):
        await bundles.publish(db, claim)
    assert db.rows[receipt.id].state == "building"


async def test_database_allocation_and_claim_are_caller_owned_transactions(
    bundle_owned_db,
):
    db = bundle_owned_db
    _, receipt, _, _ = await allocated(db)
    identifier = receipt.id
    await db.rollback()
    assert await db.get(Bundle, identifier) is None
    _, receipt, _, _ = await allocated(db)
    await db.commit()
    first = await bundles.claim(db, receipt.id)
    await db.rollback()
    second = await bundles.claim(db, receipt.id)
    assert first.lease_token != second.lease_token
    await db.commit()
    assert await bundles.claim(db, receipt.id) is None


async def test_database_claim_nowait_conflict_preserves_caller_transaction(
    bundle_owned_db,
):
    db = bundle_owned_db
    _, receipt, _, _ = await allocated(db)
    await db.commit()
    first = await bundles.claim(db, receipt.id)
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as other:
        with pytest.raises(bundles.ExportMediaBundleError) as failure:
            await bundles.claim(other, receipt.id)
        assert failure.value.code == "export_media_bundle_busy"
        assert await other.scalar(text("SELECT 1")) == 1
        await db.commit()
        assert await bundles.claim(other, receipt.id) is None
    assert first.lease_token


async def test_database_final_publication_rolls_back_and_commits_with_exact_zip_pair(
    bundle_owned_db,
):
    db = bundle_owned_db
    _, receipt, _, _ = await allocated(db)
    await db.commit()
    claim = await bundles.claim(db, receipt.id)
    await db.commit()
    row = await db.get(Bundle, receipt.id)
    output = await create_zip(db, row)
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
    output = await db.get(type(output), receipt.zip_output_id)
    await bundles.publish(db, claim)
    output.state = "ready"
    await db.commit()
    assert row.state == "ready" and row.published_at is not None
    assert output.state == "ready" and output.expires_at == row.expires_at


def modern_nuscenes_inputs():
    from app.services.exporting.lidar_sources import canonical_identity

    project, frozen, logical = inputs(targets=("nuscenes",))
    frozen["schema"] = 2
    logical = {"nuscenes": {}}
    for entry in list(frozen["media"]):
        logical["nuscenes"][entry["task_id"] + "/" + entry["role"]] = entry[
            "source_key"
        ]
    for index, task in enumerate(frozen["tasks"]):
        for role in ("raw_lidar", "map"):
            owner = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    role + (str(index) if role == "raw_lidar" else ""),
                )
            )
            source = {
                "owner_kind": "dataset_auxiliary",
                "content_id": owner,
                "media_type": role,
                "sha256": hashlib.sha256(role.encode()).hexdigest(),
                "size_bytes": len(role),
                "bucket": "saved-auxiliary",
                "object_key": owner,
                "item_id": str(uuid.uuid4()),
                "item_facts": {"id": str(uuid.uuid4())},
                "ownership_facts": {"id": owner, "kind": role},
                "relation": {"task_id": task["id"]},
            }
            key = digest(canonical_identity(source))
            frozen["media"].append(
                {
                    "task_id": task["id"],
                    "role": role,
                    "link_id": None,
                    "source_key": key,
                    "source": source,
                }
            )
            logical["nuscenes"][task["id"] + "/" + role + ".bin"] = key
    return project, frozen, logical


async def test_modern_nuscenes_allocates_all_sources_and_deduplicates_shared_map():
    project, frozen, logical = modern_nuscenes_inputs()
    receipt = await bundles.allocate(
        FakeDB(),
        project_id=project,
        job_id=uuid.uuid4(),
        input_snapshot=frozen,
        input_digest=digest(frozen),
        logical_sources=logical,
    )
    # Shared image and main PCD, two independent raw originals and one map.
    assert receipt.object_count == 5
    keys = bundles.source_objects(receipt, logical)
    maps = [row for row in frozen["media"] if row["role"] == "map"]
    assert len({row["source_key"] for row in maps}) == 1
    assert all(keys[row["source_key"]] in receipt.inventory["objects"] for row in maps)
    assert receipt.input_snapshot["schema"] == 2


@pytest.mark.parametrize(
    "damage",
    ["raw", "map", "camera", "main_pcd", "owner", "kind", "total_count", "total_bytes"],
)
async def test_modern_nuscenes_rejects_partial_or_unowned_sources_and_counts_aux_budget(
    monkeypatch, damage
):
    project, frozen, logical = modern_nuscenes_inputs()
    if damage in {"raw", "map", "camera", "main_pcd"}:
        role = {
            "raw": "raw_lidar",
            "map": "map",
            "camera": "camera_front",
            "main_pcd": "primary_lidar",
        }[damage]
        key = next(row["source_key"] for row in frozen["media"] if row["role"] == role)
        logical["nuscenes"] = {
            path: value for path, value in logical["nuscenes"].items() if value != key
        }
    elif damage in {"owner", "kind"}:
        source = next(row["source"] for row in frozen["media"] if row["role"] == "map")
        if damage == "owner":
            source["owner_kind"] = "dataset_asset"
        else:
            source["ownership_facts"]["kind"] = "raw_lidar"
    elif damage == "total_count":
        monkeypatch.setattr(physical, "MAX_OBJECTS", 4)
    else:
        monkeypatch.setattr(physical, "MAX_TOTAL_BYTES", 1)
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


@pytest.mark.parametrize("role", ["raw_lidar", "map", "camera_front", "primary_lidar"])
async def test_only_raw_lidar_may_have_an_empty_delivery_original(role):
    from app.services.exporting.lidar_sources import canonical_identity

    project, frozen, logical = modern_nuscenes_inputs()
    for entry in frozen["media"]:
        if entry["role"] != role:
            continue
        previous = entry["source_key"]
        entry["source"].update(size_bytes=0, sha256=hashlib.sha256(b"").hexdigest())
        entry["source_key"] = digest(canonical_identity(entry["source"]))
        for paths in logical.values():
            for path, key in list(paths.items()):
                if key == previous:
                    paths[path] = entry["source_key"]
    options = dict(
        project_id=project,
        job_id=uuid.uuid4(),
        input_snapshot=frozen,
        input_digest=digest(frozen),
        logical_sources=logical,
    )
    if role == "raw_lidar":
        receipt = await bundles.allocate(FakeDB(), **options)
        assert receipt.object_count == 5
        assert (
            sum(
                facts["size_bytes"] == 0
                for facts in receipt.inventory["objects"].values()
            )
            == 2
        )
    else:
        with pytest.raises(bundles.ExportMediaBundleError):
            await bundles.allocate(FakeDB(), **options)
