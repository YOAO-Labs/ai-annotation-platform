"""Frozen delivery orchestration with real PCD inspection and owned-copy receipts."""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import uuid

import pytest

from app.services.content_snapshot import ContentError
from app.services.exporting.lidar_media_paths import logical_sources
from app.services.exporting.lidar_pcd import PcdInfo, inspect_pcd


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


@dataclass(frozen=True)
class Receipt:
    original_sha: str
    size: int
    gate_revision: int
    url: str = field(repr=False)
    inspection: object = field(repr=False)


@pytest.fixture
def world(monkeypatch):
    project_id, bundle_id = uuid.uuid4(), uuid.uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    data = {
        "schema": 1,
        "project_id": str(project_id),
        "batch_id": None,
        "project": {"id": str(project_id)},
        "targets": ["pointmask"],
        "options": {"lidar": {}},
        "tasks": [],
        "items": [],
        "links": [],
        "media": [],
        "datasets": [],
        "annotations": [],
    }
    prefix = f"lidar-export-media/objects/{bundle_id}/"
    snapshot = SimpleNamespace(
        id=bundle_id,
        project_id=project_id,
        input_snapshot=data,
        expires_at=expires_at,
        storage_ref={"key_prefix": prefix, "bucket": "saved-output-bucket"},
    )
    state = SimpleNamespace(
        data=data,
        snapshot=snapshot,
        prefix=prefix,
        source_bytes={},
        source_items={},
        copies={},
        receipts={},
        stages=[],
        calls=[],
        resolves=[],
        reads=[],
        guards=[],
        object_calls=[],
        inspections={},
        receipt_overrides={},
        fail_at=None,
        guard_failure=None,
        objects_override=None,
        db=SimpleNamespace(commit=forbidden, add=forbidden, flush=forbidden),
    )

    class Frozen:
        def __init__(self, facts):
            self.project = SimpleNamespace(**deepcopy(facts["project"]))
            self.tasks = [
                SimpleNamespace(**{**deepcopy(row), "id": uuid.UUID(row["id"])})
                for row in facts["tasks"]
            ]
            self._frozen_facts = deepcopy(facts)

        def axis_convention(self, task_id):
            assert uuid.UUID(str(task_id)) in {task.id for task in self.tasks}
            return "iso_8855"

        def service(self, db):
            assert db is state.db
            return SimpleNamespace(project=self.project, tasks=self.tasks)

    state.Frozen = Frozen

    async def guard(db):
        assert db is state.db
        state.guards.append(db)
        if state.guard_failure is not None:
            raise state.guard_failure

    state.guard = guard

    async def check_source(db, binding, *, lock=False):
        assert db is state.db and not lock
        state.resolves.append(deepcopy(binding))
        return SimpleNamespace(**deepcopy(binding["source"]))

    def source_objects(saved, logical):
        assert saved is snapshot
        assert logical == logical_sources(data)
        state.object_calls.append(deepcopy(logical))
        if state.objects_override is not None:
            return state.objects_override
        return {
            key: prefix + str(uuid.UUID(key[:32])) + ".bin"
            for key in {binding["source_key"] for binding in data["media"]}
        }

    class Owner:
        prefix = state.prefix

        def contains(self, key):
            if not isinstance(key, str) or not key.startswith(self.prefix):
                raise ContentError("export_media_storage_invalid_key")
            if "/" in key[len(self.prefix) :] or ".." in key:
                raise ContentError("export_media_storage_invalid_key")
            return True

    def resolve(saved):
        assert (
            saved is snapshot and saved.storage_ref["bucket"] == "saved-output-bucket"
        )
        return Owner()

    async def copy_source(db, saved, **kwargs):
        assert db is state.db and saved is snapshot
        assert kwargs["resolve_source"] is check_source
        assert kwargs["guard"] is guard
        binding = kwargs["binding"]
        key = binding["source_key"]
        state.calls.append((deepcopy(binding), kwargs["relative"], kwargs["inspect"]))
        await kwargs["guard"](db)
        await kwargs["resolve_source"](db, binding)
        if state.fail_at == len(state.calls):
            raise ContentError("saved_copy_failed")
        body = state.source_bytes[key]
        stage = io.BytesIO(body)
        state.stages.append(stage)
        inspect = kwargs["inspect"]
        inspection = None if inspect is None else inspect(stage)
        assert not stage.closed and stage.tell() == 0
        if key in state.inspections:
            inspection = state.inspections[key]
        stage.seek(0)
        state.copies[kwargs["relative"]] = stage.read()
        receipt = Receipt(
            hashlib.sha256(body).hexdigest(),
            len(body),
            1,
            "https://saved.invalid/" + kwargs["relative"] + "?signature=private",
            inspection,
        )
        receipt = state.receipt_overrides.get(key, receipt)
        state.receipts[kwargs["relative"]] = receipt
        return receipt

    async def read_copy(db, saved, *, relative, guard, read):
        assert db is state.db and saved is snapshot
        await guard(db)
        state.reads.append(relative)
        body = state.copies[relative]
        expected = state.receipts[relative]
        if hashlib.sha256(body).hexdigest() != expected.original_sha:
            raise ContentError("export_media_storage_integrity_mismatch")
        stage = io.BytesIO(body)
        state.stages.append(stage)
        return read(stage)

    modules = {}
    for suffix in (
        "services.export_media_bundles",
        "services.export_media_storage",
        "services.export_media_transfer",
        "services.exporting.lidar_inputs",
        "services.exporting.lidar_frozen",
    ):
        name = "app." + suffix
        module = ModuleType(name)
        modules[name] = module
        monkeypatch.setitem(sys.modules, name, module)
        parent_name, attribute = name.rsplit(".", 1)
        parent = __import__(parent_name, fromlist=[attribute])
        monkeypatch.setattr(parent, attribute, module, raising=False)
    modules["app.services.export_media_bundles"].source_objects = source_objects
    modules["app.services.export_media_storage"].resolve = resolve
    modules["app.services.export_media_transfer"].copy_source = copy_source
    modules["app.services.export_media_transfer"].read_copy = read_copy
    modules["app.services.exporting.lidar_inputs"].check_source = check_source
    modules["app.services.exporting.lidar_frozen"].FrozenLidar = Frozen
    path = Path(__file__).parents[1] / "app/services/exporting/lidar_delivery.py"
    spec = importlib.util.spec_from_file_location("lidar_delivery_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state.delivery = module
    yield state
    for stage in state.stages:
        stage.close()


def add_task(world, *, primary="pcd-a", cameras=(), body=None):
    task_id = uuid.uuid4()
    task = {
        "id": str(task_id),
        "project_id": world.data["project_id"],
        "dataset_item_id": None,
        "display_id": "T-" + str(len(world.data["tasks"])),
        "file_path": f"frames/frame-{len(world.data['tasks'])}.pcd",
    }
    world.data["tasks"].append(task)
    roles = ([] if primary is None else [("primary_lidar", primary)]) + list(cameras)
    for role, name in roles:
        is_pcd = role == "primary_lidar"
        if name not in world.source_items:
            item = {
                "id": str(uuid.uuid4()),
                "file_path": f"saved-inputs/{name}.pcd"
                if is_pcd
                else f"saved-inputs/{name}.jpg",
                "metadata_": {"point_count": 999_999_999},
            }
            world.source_items[name] = item
            world.data["items"].append(item)
        item = world.source_items[name]
        link_id = str(uuid.uuid4()) if role != "primary_lidar" else None
        if link_id:
            world.data["links"].append(
                {
                    "id": link_id,
                    "task_id": str(task_id),
                    "dataset_item_id": item["id"],
                    "role": role,
                    "sensor_name": role.removeprefix("camera_"),
                }
            )
        payload = (
            body
            if body is not None and is_pcd
            else pcd()
            if is_pcd
            else b"same-image-content"
        )
        source = {
            "media_type": "point_cloud" if is_pcd else "image",
            "item_id": item["id"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "object_key": "canonical-input/" + name,
            "relation": {
                "task_id": str(task_id),
                "links": [link_id] if link_id else [],
            },
        }
        identity = {**source, "relation": None}
        source_key = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()
        ).hexdigest()
        binding = {
            "task_id": str(task_id),
            "role": role,
            "link_id": link_id,
            "source_key": source_key,
            "source": source,
        }
        world.data["media"].append(binding)
        world.source_bytes[source_key] = payload
    return task_id


async def prepare(world):
    return await world.delivery.prepare(world.db, world.snapshot, guard=world.guard)


async def test_dedup_preserves_first_binding_order_and_each_task_receipt(world):
    first = add_task(world, cameras=(("camera_front", "image-a"),))
    second = add_task(world, cameras=(("camera_front", "image-a"),))
    before = deepcopy(world.data)
    value = await prepare(world)
    assert len(world.calls) == len(world.resolves) == 2
    assert [(row[0]["task_id"], row[0]["role"]) for row in world.calls] == [
        (str(first), "primary_lidar"),
        (str(first), "camera_front"),
    ]
    assert world.calls[0][2] is inspect_pcd and world.calls[1][2] is None
    assert value.entry(first, "primary_lidar") == value.entry(second, "primary_lidar")
    assert value.entry(first, "camera_front") == value.entry(second, "camera_front")
    assert value.point_count(first) == value.point_count(str(second)) == 3
    assert world.data == before
    assert isinstance(value, world.Frozen)
    assert value.axis_convention(first) == "iso_8855"
    assert value.service(world.db).project.id == world.data["project_id"]
    assert len(world.object_calls) == 1


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
    monkeypatch.setattr(world.delivery, "MAX_JOB_POINTS", 6)
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
async def test_binding_scope_is_validated_before_copy(world, fault, monkeypatch):
    add_task(world)
    binding = world.data["media"][0]
    if fault == "unknown_task":
        binding["task_id"] = str(uuid.uuid4())
    elif fault == "duplicate":
        world.data["media"].append(deepcopy(binding))
    elif fault == "media_type":
        binding["source"]["media_type"] = "image"
    else:
        binding["role"] = "arbitrary"
    monkeypatch.setattr(world.delivery, "logical_sources", lambda data: {})
    # Isolate this layer's scope checks from the bundle's own stricter validator.
    monkeypatch.setattr(
        world.delivery.bundles,
        "source_objects",
        lambda *args: {
            binding["source_key"]: world.prefix + str(uuid.uuid4()) + ".bin"
        },
    )
    with pytest.raises(ContentError, match="scope_unknown"):
        await prepare(world)
    assert not world.calls


async def test_unowned_output_path_is_rejected_before_copy(world):
    add_task(world)
    key = world.data["media"][0]["source_key"]
    world.objects_override = {key: "other-owner/unregistered.bin"}
    with pytest.raises(ContentError, match="invalid_key"):
        await prepare(world)
    assert not world.calls


async def test_midway_failure_preserves_previous_physical_responsibility(world):
    add_task(world, cameras=(("camera_front", "image-a"),))
    world.fail_at = 2
    before = deepcopy(world.data)
    with pytest.raises(ContentError, match="saved_copy_failed"):
        await prepare(world)
    assert len(world.calls) == 2 and len(world.copies) == 1
    assert next(iter(world.copies.values())) == pcd()
    assert world.data == before


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
    world.receipt_overrides[key] = Receipt(
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
        await world.delivery.prepare(world.db, world.snapshot, guard=None)
    assert not world.calls


async def test_one_source_key_cannot_silently_change_canonical_facts(world):
    add_task(world)
    add_task(world)
    world.data["media"][1]["source"]["object_key"] = "changed-source"
    with pytest.raises(ContentError, match="delivery_changed"):
        await prepare(world)
    assert not world.calls


async def test_unused_output_responsibility_is_not_invented_or_delivered(world):
    add_task(world)
    key = world.data["media"][0]["source_key"]
    world.objects_override = {
        key: world.prefix + str(uuid.uuid4()) + ".bin",
        "0" * 64: world.prefix + str(uuid.uuid4()) + ".bin",
    }
    with pytest.raises(ContentError, match="delivery_changed"):
        await prepare(world)
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
    world.data["targets"].append("nuscenes")
    task = add_task(world)
    value = await prepare(world)
    logical = world.object_calls[0]
    assert set(logical) == {"pointmask", "nuscenes"}
    assert all(path.startswith("support/pointclouds/") for path in logical["nuscenes"])
    assert len(world.calls) == 1
    assert await value.pcd_bytes(task) == pcd()
    assert world.reads == [world.calls[0][1]]
