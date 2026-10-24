"""Pure delivery orchestration with independently retained copy receipts."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from types import ModuleType, SimpleNamespace
import sys
import uuid

import pytest

from app.services.content_snapshot import ContentError
from app.services.exporting import image_delivery
from tests.test_image_export_frozen import facts, forbidden


@pytest.fixture
def world(monkeypatch):
    data = facts()
    data["batch_id"] = None
    data["tasks"][0]["batch_id"] = None
    identifier = uuid.uuid4()
    prefix = f"export-media/objects/{identifier}/"
    receipt = SimpleNamespace(
        input_snapshot=data, expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    state = SimpleNamespace(
        data=data,
        receipt=receipt,
        copies={},
        calls=[],
        checks=[],
        guards=0,
        fail_at=None,
        revoke_after_copy=False,
    )
    inputs = ModuleType("app.services.exporting.image_inputs")
    paths = ModuleType("app.services.exporting.image_media_paths")

    async def check_source(db, binding):
        state.checks.append(binding["task_id"])
        if state.revoke_after_copy and state.copies:
            raise ContentError("source_authority_revoked")
        return deepcopy(binding["source"])

    inputs.check_source = check_source
    paths.paths = lambda snapshot: {
        uuid.UUID(row["id"]): "tasks/" + row["id"] + "/same.png"
        for row in snapshot["tasks"]
    }
    paths.archive_paths = lambda snapshot: {
        key: ["images/" + value] for key, value in paths.paths(snapshot).items()
    }
    paths.logical_sources = lambda snapshot: {
        target: {
            paths.paths(snapshot).get(uuid.UUID(row["task_id"]), "unknown"): row[
                "source_key"
            ]
            for row in snapshot["media"]
        }
        for target in snapshot["targets"]
    }
    monkeypatch.setitem(sys.modules, inputs.__name__, inputs)
    monkeypatch.setitem(sys.modules, paths.__name__, paths)
    from app.services import exporting

    monkeypatch.setattr(exporting, "image_inputs", inputs, raising=False)

    def objects(saved, logical):
        assert saved is receipt and logical == paths.logical_sources(data)
        return {row["source_key"]: prefix + row["source_key"] for row in data["media"]}

    class Owner:
        def contains(self, key):
            assert key.startswith(prefix) and len(key[len(prefix) :]) == 64

    Owner.prefix = prefix

    async def copy_source(db, saved, *, binding, relative, resolve_source, guard):
        assert db is state.db and saved is receipt
        state.calls.append((binding["source_key"], relative))
        await guard(db)
        await resolve_source(db, binding)
        if state.fail_at == len(state.calls):
            raise ContentError("copy_failed")
        state.copies[relative] = b"managed"
        await guard(db)
        await resolve_source(db, binding)
        return SimpleNamespace(
            original_sha=hashlib.sha256(b"managed").hexdigest(),
            size=7,
            url="/minio/saved/" + relative + "?signature=private",
        )

    async def guard(db):
        assert db is state.db
        state.guards += 1

    state.guard = guard
    state.db = SimpleNamespace(commit=forbidden, add=forbidden, flush=forbidden)
    monkeypatch.setattr(image_delivery.bundles, "source_objects", objects)
    monkeypatch.setattr(image_delivery.physical, "resolve", lambda saved: Owner())
    monkeypatch.setattr(image_delivery.transfer, "copy_source", copy_source)
    return state


def duplicate(world, *, different=False):
    task = deepcopy(world.data["tasks"][0])
    task["id"] = str(uuid.uuid4())
    binding = deepcopy(world.data["media"][0])
    binding["task_id"] = task["id"]
    binding["source"]["relation"] = {"task_id": task["id"]}
    if different:
        binding["source_key"] = "b" * 64
        binding["source"]["object_key"] = "different-owner/same-sha"
    world.data["tasks"].append(task)
    world.data["media"].append(binding)
    return uuid.UUID(task["id"])


async def test_deduplicates_canonical_owner_checks_every_binding_and_keeps_snapshot(
    world,
):
    second = duplicate(world)
    before = deepcopy(world.data)
    delivery = await image_delivery.prepare(world.db, world.receipt, guard=world.guard)
    assert len(world.calls) == 1 and world.guards == 2
    assert set(world.checks) == {row["id"] for row in world.data["tasks"]}
    assert world.checks.count(str(second)) == 2
    assert world.data == before
    first_entry = delivery.entry(second)
    assert first_entry["expires_at"] == world.receipt.expires_at.isoformat()
    assert (
        first_entry["size_bytes"] == 7
        and first_entry["sha256"] == hashlib.sha256(b"managed").hexdigest()
    )
    first_entry["sha256"] = "wrong"
    assert delivery.entry(second)["sha256"] != "wrong"
    assert delivery.service(world.db).media_paths == delivery.media_paths
    assert "signature" not in repr(delivery)


async def test_same_sha_distinct_owners_copy_separately(world):
    duplicate(world, different=True)
    await image_delivery.prepare(world.db, world.receipt, guard=world.guard)
    assert len(world.calls) == 2 and len(world.copies) == 2


async def test_copy_failure_retains_prior_physical_owner(world):
    duplicate(world, different=True)
    world.fail_at = 2
    with pytest.raises(ContentError, match="copy_failed"):
        await image_delivery.prepare(world.db, world.receipt, guard=world.guard)
    assert len(world.copies) == 1


async def test_duplicate_binding_revocation_after_copy_fails_without_deleting_copy(
    world,
):
    duplicate(world)
    world.revoke_after_copy = True
    with pytest.raises(ContentError, match="source_authority_revoked"):
        await image_delivery.prepare(world.db, world.receipt, guard=world.guard)
    assert len(world.copies) == 1


@pytest.mark.parametrize("change", ["role", "task", "link", "type"])
async def test_unknown_binding_scope_rejected_before_copy(world, change):
    binding = world.data["media"][0]
    if change == "role":
        binding["role"] = "camera_front"
    elif change == "task":
        binding["task_id"] = str(uuid.uuid4())
    elif change == "link":
        binding["link_id"] = str(uuid.uuid4())
    else:
        binding["source"]["media_type"] = "point_cloud"
    with pytest.raises(ContentError, match="image_export_delivery_scope_unknown"):
        await image_delivery.prepare(world.db, world.receipt, guard=world.guard)
    assert not world.calls


async def test_pure_reference_or_empty_media_never_copies_original(world):
    world.data["media"] = []
    delivery = await image_delivery.prepare(world.db, world.receipt, guard=world.guard)
    assert not world.calls and not world.checks
    with pytest.raises(ContentError, match="image_export_delivery_scope_unknown"):
        delivery.entry(world.data["tasks"][0]["id"])
    world.data.update(tasks=[], annotations=[], media=[])
    empty = await image_delivery.prepare(world.db, world.receipt, guard=world.guard)
    assert empty.paths == {} and not world.calls
