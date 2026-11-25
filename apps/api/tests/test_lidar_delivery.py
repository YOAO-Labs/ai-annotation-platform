"""Frozen delivery orchestration with real PCD inspection and owned-copy receipts."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
from types import SimpleNamespace
import uuid

import pytest

from app.services import auxiliary_originals, original_sources
from app.services import export_media_bundles as bundles
from app.services import export_media_storage as physical
from app.services import export_media_transfer as transfer
from app.services.content_snapshot import ContentError
from app.services.exporting import lidar_delivery, lidar_inputs
from app.services.exporting.lidar_frozen import FrozenLidar
from app.services.exporting.lidar_media_paths import logical_sources
from app.services.exporting.lidar_pcd import PcdInfo, inspect_pcd
from app.services.exporting.lidar_sources import canonical_identity


def pcd(rows=("1 2 3", "nan 5 6", "7 8 9")):
    header = (
        "VERSION .7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\n"
        f"WIDTH {len(rows)}\nHEIGHT 1\nPOINTS {len(rows)}\nDATA ascii\n"
    )
    return (header + "\n".join(rows) + ("\n" if rows else "")).encode()


def forbidden(*args, **kwargs):
    pytest.fail(
        "delivery must not commit, publish, delete owners or use current storage"
    )


@pytest.fixture
def world(monkeypatch):
    project_id, bundle_id, dataset_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    data = {
        "schema": lidar_inputs.CONTRACT,
        "project_id": str(project_id),
        "batch_id": None,
        "project": {"id": str(project_id), "tool_bindings": {}},
        "targets": ["pointmask"],
        "options": {"lidar": {}},
        "tasks": [],
        "items": [],
        "links": [],
        "media": [],
        "datasets": [
            {"id": str(dataset_id), "metadata_": {"axis_convention": "iso_8855"}}
        ],
        "annotations": [],
        "scenes": [],
        "poses": [],
        "tracks": [],
        "intervals": [],
        "calibrations": [],
    }
    storage_ref = physical.make_ref(bundle_id, bucket="saved-output-bucket")
    state = SimpleNamespace(
        data=data,
        bundle_id=bundle_id,
        dataset_id=dataset_id,
        storage_ref=storage_ref,
        snapshot=None,
        source_bytes={},
        source_items={},
        sources={},
        source_failures={},
        copies={},
        receipts={},
        stages=[],
        calls=[],
        resolves=[],
        reads=[],
        guards=[],
        inspections={},
        receipt_overrides={},
        fail_at=None,
        guard_failure=None,
        db=SimpleNamespace(commit=forbidden, add=forbidden, flush=forbidden),
    )

    async def guard(db):
        assert db is state.db
        state.guards.append(db)
        if state.guard_failure is not None:
            raise state.guard_failure

    state.guard = guard

    def source(db, task_id, selector, lock):
        assert db is state.db and not lock
        key = task_id, selector
        if key in state.source_failures:
            raise state.source_failures[key]
        binding, original = state.sources[key]
        state.resolves.append(deepcopy(binding))
        return original

    async def task_source(db, task_id, *, lock, compact_relation):
        assert compact_relation
        return source(db, task_id, "primary_lidar", lock)

    async def link_source(db, task_id, link_id, *, lock, compact_relation):
        assert compact_relation
        return source(db, task_id, link_id, lock)

    async def raw_source(db, task_id, *, lock):
        return source(db, task_id, "raw_lidar", lock)

    async def map_source(db, task_id, *, lock):
        return source(db, task_id, "map", lock)

    async def copy_source(
        db, saved, *, binding, relative, resolve_source, guard, inspect
    ):
        assert db is state.db and saved is state.snapshot
        assert saved.storage_ref["bucket"] == "saved-output-bucket"
        key = binding["source_key"]
        state.calls.append((deepcopy(binding), relative, inspect))
        await guard(db)
        original = await resolve_source(db, binding)
        if state.fail_at == len(state.calls):
            raise ContentError("saved_copy_failed")
        body = state.source_bytes[original.object_key]
        stage = io.BytesIO(body)
        state.stages.append(stage)
        inspection = None if inspect is None else inspect(stage)
        assert not stage.closed and stage.tell() == 0
        if key in state.inspections:
            inspection = state.inspections[key]
        state.copies[relative] = stage.read()
        receipt = transfer.TransferReceipt(
            hashlib.sha256(body).hexdigest(),
            len(body),
            1,
            "https://saved.invalid/" + relative + "?signature=private",
            inspection,
        )
        receipt = state.receipt_overrides.get(key, receipt)
        state.receipts[relative] = receipt
        return receipt

    async def read_copy(db, saved, *, relative, guard, read):
        assert db is state.db and saved is state.snapshot
        await guard(db)
        state.reads.append(relative)
        body = state.copies[relative]
        expected = state.receipts[relative]
        if hashlib.sha256(body).hexdigest() != expected.original_sha:
            raise ContentError("export_media_storage_integrity_mismatch")
        stage = io.BytesIO(body)
        state.stages.append(stage)
        return read(stage)

    monkeypatch.setattr(original_sources, "resolve_task", task_source)
    monkeypatch.setattr(original_sources, "resolve_task_link", link_source)
    monkeypatch.setattr(auxiliary_originals, "resolve_task_raw", raw_source)
    monkeypatch.setattr(auxiliary_originals, "resolve_scene_map", map_source)
    monkeypatch.setattr(transfer, "copy_source", copy_source)
    monkeypatch.setattr(transfer, "read_copy", read_copy)
    yield state
    for stage in state.stages:
        stage.close()


def add_source(world, role, item, relation, payload, *, link_id=None, owner_id=None):
    auxiliary = role in {"raw_lidar", "map"}
    media_type = role if auxiliary else item["file_type"]
    item_id = uuid.UUID(item["id"])
    content_id = owner_id or item_id
    object_key = (
        f"__aap_auxiliary_originals__/{world.dataset_id}/{content_id}/payload"
        if auxiliary
        else item["file_path"]
    )
    checksum = hashlib.sha256(payload).hexdigest()
    source = original_sources.OriginalSource(
        owner_kind="dataset_auxiliary" if auxiliary else "dataset_asset",
        content_id=content_id,
        sha256=checksum,
        size_bytes=len(payload),
        media_type=media_type,
        item_id=item_id,
        relation=relation,
        bucket="saved-original-bucket",
        object_key=object_key,
        endpoint_url=world.storage_ref["endpoint_url"],
        location_id=physical.LEGACY_LOCATION_ID,
        location_revision=1,
        maintenance_revision=1,
        ownership_facts=(
            {"id": owner_id, "kind": role}
            if auxiliary
            else original_sources.AssetFacts(
                content_id, item_id, media_type, checksum, len(payload)
            )
        ),
        item_facts=original_sources.ItemFacts(
            item_id, world.dataset_id, item["file_path"], item["file_type"], None, None
        ),
        replica_facts=None,
        location_facts=None,
    )
    binding = {
        "task_id": str(relation.task_id),
        "role": role,
        "link_id": str(link_id) if link_id else None,
        "source_key": lidar_inputs.source_key(source),
        "source": lidar_inputs.source_value(source, link_id),
    }
    world.data["media"].append(binding)
    world.sources[relation.task_id, link_id or role] = (binding, source)
    world.source_bytes[object_key] = payload


def add_task(world, *, primary="pcd-a", cameras=(), body=None):
    task_id = uuid.uuid4()
    frame = f"frame-{len(world.data['tasks'])}.pcd"
    task = {
        "id": str(task_id),
        "project_id": world.data["project_id"],
        "dataset_item_id": None,
        "display_id": "T-" + str(len(world.data["tasks"])),
        "file_name": frame,
        "file_path": f"frames/{frame}",
        "file_type": "point_cloud",
    }
    world.data["tasks"].append(task)
    roles = ([] if primary is None else [("primary_lidar", primary)]) + list(cameras)
    links, assets = [], []
    for role, name in roles:
        is_pcd = role == "primary_lidar"
        if name not in world.source_items:
            filename = name + (".pcd" if is_pcd else ".jpg")
            item = {
                "id": str(uuid.uuid4()),
                "dataset_id": str(world.dataset_id),
                "file_name": filename,
                "file_path": f"saved-inputs/{filename}",
                "file_type": "point_cloud" if is_pcd else "image",
                "metadata_": {"point_count": 999_999_999},
            }
            world.source_items[name] = item
            world.data["items"].append(item)
        item = world.source_items[name]
        link_id = None if is_pcd else uuid.uuid4()
        if is_pcd:
            task["dataset_item_id"] = item["id"]
        else:
            sensor = role.removeprefix("camera_")
            world.data["links"].append(
                {
                    "id": str(link_id),
                    "task_id": str(task_id),
                    "dataset_item_id": item["id"],
                    "role": role,
                    "sensor_name": sensor,
                }
            )
            links.append(
                original_sources.LinkFact(
                    link_id, task_id, uuid.UUID(item["id"]), role, sensor
                )
            )
        payload = (
            body
            if body is not None and is_pcd
            else pcd()
            if is_pcd
            else b"same-image-content"
        )
        assets.append((role, item, payload, link_id))
    item_id = uuid.UUID(task["dataset_item_id"]) if primary is not None else None
    relation = original_sources.TaskRelation(
        task_id=task_id,
        project_id=uuid.UUID(task["project_id"]),
        batch_id=None,
        file_path=task["file_path"],
        file_type=task["file_type"],
        original_id=None,
        direct_item_id=item_id,
        primary_item_id=None,
        primary_link_id=None,
        item_id=item_id,
        links=tuple(links),
    )
    for role, item, payload, link_id in assets:
        add_source(world, role, item, relation, payload, link_id=link_id)
    return task_id


def bundle(world):
    logical = logical_sources(world.data)
    receipts = {}
    for binding in world.data["media"]:
        key, source = binding["source_key"], binding["source"]
        receipts[key] = {"sha256": source["sha256"], "size_bytes": source["size_bytes"]}
    keys = {
        key: world.storage_ref["key_prefix"] + str(uuid.UUID(key[:32])) + ".bin"
        for key in receipts
    }
    created = datetime.now(timezone.utc)
    return bundles.snapshot(
        {
            "id": world.bundle_id,
            "project_id": uuid.UUID(world.data["project_id"]),
            "job_id": uuid.uuid4(),
            "zip_output_id": uuid.uuid4(),
            "state": "building",
            "storage_ref": world.storage_ref,
            "created_at": created,
            "expires_at": created + timedelta(days=7),
            "input_schema": world.data["schema"],
            "input_snapshot": world.data,
            "input_digest": lidar_inputs.digest(world.data),
            "object_count": len(keys),
            "size_bytes": sum(receipt["size_bytes"] for receipt in receipts.values()),
            "logical_paths": {
                "schema": 1,
                "targets": {
                    target: {path: keys[key] for path, key in paths.items()}
                    for target, paths in logical.items()
                },
            },
            "inventory": {
                "schema": 1,
                "objects": {keys[key]: receipt for key, receipt in receipts.items()},
            },
        }
    )


async def prepare(world, snapshot=None):
    world.snapshot = bundle(world) if snapshot is None else snapshot
    return await lidar_delivery.prepare(world.db, world.snapshot, guard=world.guard)


async def test_dedup_preserves_first_binding_order_and_each_task_receipt(world):
    first = add_task(world, cameras=(("camera_front", "image-a"),))
    second = add_task(world, cameras=(("camera_front", "image-a"),))
    before = deepcopy(world.data)
    value = await prepare(world)
    assert len(world.calls) == 2
    assert {row["task_id"] for row in world.resolves} == {
        row["task_id"] for row in world.data["media"]
    }
    assert [(row[0]["task_id"], row[0]["role"]) for row in world.calls] == [
        (str(first), "primary_lidar"),
        (str(first), "camera_front"),
    ]
    assert world.calls[0][2] is inspect_pcd and world.calls[1][2] is None
    assert value.entry(first, "primary_lidar") == value.entry(second, "primary_lidar")
    assert value.entry(first, "camera_front") == value.entry(second, "camera_front")
    assert value.point_count(first) == value.point_count(str(second)) == 3
    assert world.data == world.snapshot.input_snapshot == before
    assert isinstance(value, FrozenLidar)
    assert value.axis_convention(first) == "iso_8855"
    project_id = uuid.UUID(world.data["project_id"])
    assert value.project.id == project_id
    chunks = value.service(world.db).iter_export_chunks(project_id)
    assert [task.id async for tasks, _, _ in chunks for task in tasks] == [
        first,
        second,
    ]
    assert world.snapshot.object_count == 2
    assert list(world.copies.values()) == [pcd(), b"same-image-content"]


async def test_identical_sha_different_canonical_source_keys_stay_independent(world):
    first, second = add_task(world), add_task(world, primary="pcd-b")
    value = await prepare(world)
    assert len(world.calls) == 2
    a, b = value.entry(first, "primary_lidar"), value.entry(second, "primary_lidar")
    assert a["sha256"] == b["sha256"] and a["size_bytes"] == b["size_bytes"]
    assert a["presigned_url"] != b["presigned_url"]
    assert world.calls[0][1] != world.calls[1][1]


@pytest.mark.parametrize("rows", [(), ("1 2 3", "nan 5 6", "7 inf 9")])
async def test_actual_pcd_count_keeps_zero_and_nonfinite_original_records(world, rows):
    original = pcd(rows)
    task = add_task(world, body=original)
    value = await prepare(world)
    assert value.point_count(task) == len(rows)
    assert next(iter(world.copies.values())) == original
    assert value.point_count(task) != world.data["items"][0]["metadata_"]["point_count"]


async def test_hundred_million_point_budget_counts_each_task_not_unique_source(world):
    tasks = [add_task(world) for _ in range(26)]
    key = world.data["media"][0]["source_key"]
    world.inspections[key] = PcdInfo(4_000_000, "binary", 12, 0, len(pcd()))
    with pytest.raises(ContentError) as error:
        await prepare(world)
    assert error.value.code == "lidar_export_point_budget_exceeded"
    assert len(tasks) == 26 and len(world.calls) == len(world.copies) == 1


async def test_exact_job_point_budget_allows_shared_source_per_frame(
    world, monkeypatch
):
    first, second = add_task(world), add_task(world)
    monkeypatch.setattr(lidar_delivery, "MAX_JOB_POINTS", 6)
    value = await prepare(world)
    assert value.point_count(first) + value.point_count(second) == 6
    assert len(world.calls) == 1


@pytest.mark.parametrize("count", [-1, True, 4_000_001, None])
async def test_invalid_actual_count_never_uses_metadata_fallback(world, count):
    add_task(world)
    key = world.data["media"][0]["source_key"]
    world.inspections[key] = PcdInfo(count, "binary", 12, 0, len(pcd()))
    with pytest.raises(ContentError) as error:
        await prepare(world)
    assert error.value.code == "lidar_export_pcd_inspection_invalid"
    assert len(world.copies) == 1


async def test_missing_pcd_inspection_is_refused(world):
    add_task(world)
    world.inspections[world.data["media"][0]["source_key"]] = None
    with pytest.raises(ContentError, match="lidar_export_pcd_inspection_invalid"):
        await prepare(world)


async def test_entries_are_fresh_and_repr_never_discloses_signed_urls(world):
    task = add_task(world)
    value = await prepare(world)
    a, b = value.entry(task, "primary_lidar"), value.entry(task, "primary_lidar")
    assert a is not b and a == b
    assert a["expires_at"] == world.snapshot.expires_at.isoformat()
    assert value.expires_at is world.snapshot.expires_at
    a["sha256"] = "changed"
    a["presigned_url"] = "changed"
    assert value.entry(task, "primary_lidar") == b
    assert "signature" not in repr(value) and "private" not in repr(value)


async def test_pcd_bytes_reads_saved_copy_and_never_reopens_original(world):
    task = add_task(world)
    value = await prepare(world)
    saved = pcd()
    world.source_bytes.clear()
    before_resolves = deepcopy(world.resolves)
    assert await value.pcd_bytes(task) == saved
    assert world.resolves == before_resolves
    assert world.reads == [world.calls[0][1]]
    assert world.guards


async def test_pcd_bytes_surfaces_saved_copy_integrity_failure(world):
    task = add_task(world)
    value = await prepare(world)
    world.copies[world.calls[0][1]] = b"corrupted-copy"
    with pytest.raises(ContentError, match="integrity_mismatch"):
        await value.pcd_bytes(task)
    assert len(world.resolves) == 1


async def test_pure_coco_has_no_primary_inspection_or_pcd_access(world):
    world.data["targets"] = ["coco-multicamera"]
    task = add_task(world, primary=None, cameras=(("camera_front", "image-a"),))
    value = await prepare(world)
    assert len(world.calls) == 1 and world.calls[0][2] is None
    assert value.entry(task, "camera_front")["size_bytes"] == len(b"same-image-content")
    with pytest.raises(ContentError, match="scope_unknown"):
        value.point_count(task)
    with pytest.raises(ContentError, match="scope_unknown"):
        await value.pcd_bytes(task)
    assert not world.reads


@pytest.mark.parametrize("method", ["entry", "point_count", "pcd_bytes"])
async def test_unknown_task_is_refused_before_any_new_physical_work(world, method):
    add_task(world)
    value = await prepare(world)
    unknown = uuid.uuid4()
    with pytest.raises(ContentError, match="scope_unknown"):
        if method == "entry":
            value.entry(unknown, "primary_lidar")
        elif method == "point_count":
            value.point_count(unknown)
        else:
            await value.pcd_bytes(unknown)
    assert not world.reads and len(world.calls) == 1


@pytest.mark.parametrize("role", ["camera_absent", None, [], "../primary_lidar"])
async def test_unknown_role_is_refused(world, role):
    task = add_task(world)
    value = await prepare(world)
    with pytest.raises(ContentError, match="scope_unknown"):
        value.entry(task, role)


@pytest.mark.parametrize("fault", ["unknown_task", "duplicate", "media_type", "role"])
async def test_binding_scope_is_validated_before_copy(world, fault):
    add_task(world)
    snapshot = bundle(world)
    binding = world.data["media"][0]
    error_type, code = ContentError, "export_media_bundle_invalid"
    if fault == "unknown_task":
        world.data["media"].append({**binding, "task_id": str(uuid.uuid4())})
    elif fault == "duplicate":
        world.data["media"].append(deepcopy(binding))
        error_type, code = ValueError, "lidar_media_binding_conflict"
    elif fault == "media_type":
        binding["source"]["media_type"] = "image"
    else:
        world.data["media"].append({**binding, "role": "arbitrary"})
    with pytest.raises(error_type, match=f"^{code}$"):
        await prepare(world, replace(snapshot, _input_snapshot=world.data))
    assert not world.calls


async def test_unowned_output_path_is_rejected_before_copy(world):
    add_task(world)
    snapshot = bundle(world)
    logical, inventory = snapshot.logical_paths, snapshot.inventory
    paths = logical["targets"]["pointmask"]
    old_key = paths["velodyne/pcd-a.pcd"]
    foreign_key = "other-owner/" + old_key.rsplit("/", 1)[1]
    paths["velodyne/pcd-a.pcd"] = foreign_key
    inventory["objects"][foreign_key] = inventory["objects"].pop(old_key)
    with pytest.raises(physical.ExportMediaStorageError) as error:
        await prepare(
            world, replace(snapshot, _logical_paths=logical, _inventory=inventory)
        )
    assert error.value.code == "export_media_storage_invalid_key"
    assert not world.calls


async def test_midway_failure_preserves_previous_physical_responsibility(world):
    add_task(world, cameras=(("camera_front", "image-a"),))
    world.fail_at = 2
    before = deepcopy(world.data)
    with pytest.raises(ContentError, match="saved_copy_failed"):
        await prepare(world)
    assert len(world.calls) == 2 and len(world.copies) == 1
    assert next(iter(world.copies.values())) == pcd()
    assert world.data == world.snapshot.input_snapshot == before


async def test_guard_revocation_propagates_without_dispatch_or_new_owner(world):
    add_task(world)
    failure = ContentError("actor_revoked")
    world.guard_failure = failure
    with pytest.raises(ContentError) as error:
        await prepare(world)
    assert error.value is failure
    assert not world.copies


@pytest.mark.parametrize("fault", ["sha256", "size"])
async def test_transfer_receipt_must_match_exact_source(world, fault):
    add_task(world)
    key = world.data["media"][0]["source_key"]
    world.receipt_overrides[key] = transfer.TransferReceipt(
        "0" * 64 if fault == "sha256" else hashlib.sha256(pcd()).hexdigest(),
        len(pcd()) + (1 if fault == "size" else 0),
        1,
        "https://saved.invalid/?secret=hidden",
        inspect_pcd(io.BytesIO(pcd())),
    )
    with pytest.raises(ContentError, match="delivery_changed"):
        await prepare(world)
    assert len(world.copies) == 1


async def test_missing_guard_is_rejected_before_physical_work(world):
    add_task(world)
    with pytest.raises(ContentError, match="callbacks_required"):
        await lidar_delivery.prepare(world.db, bundle(world), guard=None)
    assert not world.calls


async def test_one_source_key_cannot_silently_change_canonical_facts(world):
    add_task(world)
    add_task(world)
    snapshot = bundle(world)
    world.data["media"][1]["source"]["object_key"] = "changed-source"
    with pytest.raises(ContentError, match="^export_media_bundle_source_changed$"):
        await prepare(world, replace(snapshot, _input_snapshot=world.data))
    assert not world.calls


async def test_unused_output_responsibility_is_not_invented_or_delivered(world):
    add_task(world)
    snapshot = bundle(world)
    inventory = snapshot.inventory
    objects = inventory["objects"]
    receipt = next(iter(objects.values()))
    objects[world.storage_ref["key_prefix"] + str(uuid.uuid4()) + ".bin"] = receipt
    with pytest.raises(ContentError, match="^export_media_bundle_coverage$"):
        await prepare(
            world,
            replace(
                snapshot,
                _inventory=inventory,
                object_count=snapshot.object_count + 1,
                size_bytes=snapshot.size_bytes + receipt["size_bytes"],
            ),
        )
    assert not world.calls


async def test_saved_copy_read_rechecks_guard_without_any_original_resolution(world):
    task = add_task(world)
    value = await prepare(world)
    failure = ContentError("current_attempt_revoked")
    world.guard_failure = failure
    with pytest.raises(ContentError) as error:
        await value.pcd_bytes(task)
    assert error.value is failure and not world.reads
    assert len(world.resolves) == 1


async def test_mixed_nuscenes_uses_private_logical_slot_and_same_saved_copy(world):
    world.data["schema"] = 1
    world.data["targets"].append("nuscenes")
    task = add_task(world)
    value = await prepare(world)
    logical = world.snapshot.logical_paths["targets"]
    assert set(logical) == {"pointmask", "nuscenes"}
    assert logical["nuscenes"] == {
        f"support/pointclouds/{task}/pcd-a.pcd": logical["pointmask"][
            "velodyne/pcd-a.pcd"
        ]
    }
    assert len(world.calls) == 1
    assert await value.pcd_bytes(task) == pcd()
    assert world.reads == [world.calls[0][1]]


def add_nuscenes_auxiliary(world, task_id, *, map_owner, raw_data=b"r" * 80):
    world.data["targets"] = ["nuscenes"]
    scene_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "owned-nuscenes-scene"))
    if not world.data["scenes"]:
        world.data["scenes"].append(
            {
                "id": scene_id,
                "source_metadata": {
                    "nuscenes_export": {"map": {"filename": "maps/map.png"}}
                },
            }
        )
    selected = [row for row in world.data["media"] if row["task_id"] == str(task_id)]
    items = {row["id"]: row for row in world.data["items"]}
    for binding in selected:
        item = items[binding["source"]["item_id"]]
        item["scene_id"] = scene_id
        item["metadata_"]["nuscenes_export"] = {
            "sample_data": {
                "filename": (
                    f"samples/LIDAR_TOP/{task_id}.pcd.bin"
                    if binding["role"] == "primary_lidar"
                    else f"samples/{binding['role']}/{task_id}.jpg"
                )
            }
        }
    _, primary = world.sources[task_id, "primary_lidar"]
    item = items[str(primary.item_id)]
    add_source(
        world, "raw_lidar", item, primary.relation, raw_data, owner_id=uuid.uuid4()
    )
    add_source(world, "map", item, primary.relation, b"shared-map", owner_id=map_owner)


async def test_nuscenes_shared_map_has_one_copy_and_every_binding_is_fresh(world):
    first = add_task(
        world, primary="pcd-first", cameras=(("camera_front", "image-first"),)
    )
    second = add_task(
        world, primary="pcd-second", cameras=(("camera_front", "image-second"),)
    )
    owner = uuid.uuid4()
    for task_id in (first, second):
        add_nuscenes_auxiliary(world, task_id, map_owner=owner)
    value = await prepare(world)
    maps = [
        (binding, relative)
        for binding, relative, inspect in world.calls
        if binding["role"] == "map"
    ]
    assert len(world.calls) == 7 and len(maps) == 1
    assert value.entry(first, "map") == value.entry(second, "map")
    assert {row["task_id"] for row in world.resolves if row["role"] == "map"} == {
        str(first),
        str(second),
    }
    assert {row["role"] for row in world.resolves} == {
        "primary_lidar",
        "camera_front",
        "raw_lidar",
        "map",
    }
    assert all(
        inspect is None
        for binding, _, inspect in world.calls
        if binding["role"] in {"raw_lidar", "map"}
    )
    logical = world.snapshot.logical_paths["targets"]["nuscenes"]
    assert len(logical) == 7 and "maps/map.png" in logical


async def test_shared_map_second_task_revocation_prevents_copy(world):
    first, second = add_task(world, primary="first"), add_task(world, primary="second")
    owner = uuid.uuid4()
    for task_id in (first, second):
        add_nuscenes_auxiliary(world, task_id, map_owner=owner)
    world.source_failures[second, "map"] = ContentError("map_binding_revoked")
    with pytest.raises(ContentError, match="map_binding_revoked"):
        await prepare(world)
    map_calls = [
        relative for binding, relative, _ in world.calls if binding["role"] == "map"
    ]
    assert not any(relative in world.copies for relative in map_calls)


@pytest.mark.parametrize("targets", [["nuscenes"], ["kitti", "nuscenes"]])
def test_nuscenes_paths_cover_aux_all_cameras_and_private_pcd_in_pure_and_mixed(
    world, targets
):
    task = add_task(world, cameras=(("camera_front", "front"), ("camera_back", "back")))
    add_nuscenes_auxiliary(world, task, map_owner=uuid.uuid4())
    world.data["targets"] = targets
    world.data["options"] = {"lidar": {"kitti_camera_role": "camera_front"}}
    paths = logical_sources(world.data)
    public = paths["nuscenes"]
    assert set(public.values()) == {row["source_key"] for row in world.data["media"]}
    assert len(public) == 5
    assert set(public) == {
        f"support/pointclouds/{task}/pcd-a.pcd",
        f"samples/LIDAR_TOP/{task}.pcd.bin",
        f"samples/camera_front/{task}.jpg",
        f"samples/camera_back/{task}.jpg",
        "maps/map.png",
    }
    if "kitti" in targets:
        assert len(paths["kitti"]) == 2
        rear = next(
            row["source_key"]
            for row in world.data["media"]
            if row["role"] == "camera_back"
        )
        assert rear in public.values() and rear not in paths["kitti"].values()


@pytest.mark.parametrize(
    "path",
    [
        "README.txt",
        "v1.0-aap/sample.json",
        "media_manifest.json",
        "support/map.bin",
        "maps/../escape",
        "maps/a\nbin",
    ],
)
def test_nuscenes_original_paths_cannot_overwrite_generated_files_or_escape(
    world, path
):
    task = add_task(world)
    add_nuscenes_auxiliary(world, task, map_owner=uuid.uuid4())
    world.data["scenes"][0]["source_metadata"]["nuscenes_export"]["map"]["filename"] = (
        path
    )
    with pytest.raises(ValueError, match="lidar_media_path_(collision|invalid)"):
        logical_sources(world.data)


def test_auxiliary_canonical_identity_deduplicates_owner_without_changing_g1_keys():
    for kind in ("dataset_asset", "task_original"):
        source = {
            "owner_kind": kind,
            "item_id": "item",
            "item_facts": {"id": "item"},
            "relation": {"task_id": "task"},
            "sha256": "a" * 64,
        }
        assert canonical_identity(source) == {**source, "relation": None}
    source = {
        "owner_kind": "dataset_auxiliary",
        "item_id": "item-1",
        "item_facts": {"id": "item-1"},
        "relation": {"task_id": "task-1"},
        "ownership_facts": {"id": "same-map"},
        "sha256": "b" * 64,
    }
    other = {
        **source,
        "item_id": "item-2",
        "item_facts": {"id": "item-2"},
        "relation": {"task_id": "task-2"},
    }
    assert canonical_identity(source) == canonical_identity(other)
    assert source["relation"]["task_id"] == "task-1"
    assert canonical_identity(source) != canonical_identity(
        {**other, "ownership_facts": {"id": "different-map"}}
    )


async def test_nuscenes_empty_raw_keeps_delivery_role_and_full_receipt(world):
    task = add_task(world)
    add_nuscenes_auxiliary(world, task, map_owner=uuid.uuid4(), raw_data=b"")
    delivery = await prepare(world)
    receipt = delivery.entry(task, "raw_lidar")
    assert (
        receipt["size_bytes"] == 0
        and receipt["sha256"] == hashlib.sha256(b"").hexdigest()
    )
    assert len(world.calls) == 3
    relative, inspect = next(
        (relative, inspect)
        for binding, relative, inspect in world.calls
        if binding["role"] == "raw_lidar"
    )
    assert inspect is None
    assert world.copies[relative] == b""
