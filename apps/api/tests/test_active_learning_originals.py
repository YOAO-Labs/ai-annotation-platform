"""Pure frozen-owner, real-image and physical-drain checks for AL inputs."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
from PIL import Image
import pytest

from app.services import active_learning as learning
from app.services import active_learning_originals as service
from app.services import managed_original_reclaim_fence
from app.services import sample_evaluation_originals as bounded
from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.managed_original_reader import BinaryStage
from app.services.original_sources import OriginalSource, TaskRelation
from tests.sample_evaluation_fixtures import Objects
from tests.test_managed_original_reader import Provider
from tests.test_sample_evaluation_originals import image_bytes


def stage(raw):
    return BinaryStage(io.BytesIO(raw), hashlib.sha256(raw).hexdigest(), len(raw), {})


def snapshot(**changes):
    return {"file_type": "image", "expected_width": 4, "expected_height": 3, **changes}


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"])
def test_real_formats_preserve_raw_receipt_and_produce_metadata_free_rgb_png(format):
    raw = image_bytes(format)
    captured = stage(raw)
    try:
        png, media = service.normalize(captured, snapshot())
        assert media["sha256"] == hashlib.sha256(raw).hexdigest()
        assert media["size_bytes"] == len(raw)
        assert media["derived_sha256"] == hashlib.sha256(png).hexdigest()
        assert media["preprocessing"]["raw_sha256"] == media["sha256"]
        with Image.open(io.BytesIO(png)) as image:
            assert image.format == "PNG" and image.mode == "RGB"
            assert image.size == (4, 3) and image.info == {}
            image.load()
    finally:
        captured.close()


@pytest.mark.parametrize("orientation", range(1, 9))
def test_real_exif_uses_logical_dimensions_and_removes_orientation_metadata(
    orientation,
):
    raw = image_bytes("JPEG", orientation=orientation)
    captured = stage(raw)
    expected = (3, 4) if orientation in {5, 6, 7, 8} else (4, 3)
    try:
        png, media = service.normalize(
            captured, snapshot(expected_width=expected[0], expected_height=expected[1])
        )
        assert (media["width"], media["height"]) == expected
        assert media["preprocessing"]["exif_orientation"] == orientation
        with Image.open(io.BytesIO(png)) as image:
            assert image.size == expected and not image.getexif()
    finally:
        captured.close()


@pytest.mark.parametrize("orientation", [0, 9])
def test_invalid_exif_is_not_normalized(orientation):
    captured = stage(image_bytes("JPEG", orientation=orientation))
    try:
        with pytest.raises(ValueError, match="orientation is invalid"):
            service.normalize(captured, snapshot())
    finally:
        captured.close()


@pytest.mark.parametrize("format", ["GIF", "TIFF"])
def test_multiframe_images_do_not_become_frozen_input(format):
    captured = stage(image_bytes(format, frames=2))
    try:
        with pytest.raises(ValueError, match="pixel/frame budget"):
            service.normalize(captured, snapshot())
    finally:
        captured.close()


@pytest.mark.parametrize("size,accepted", [((4000, 4000), True), ((4001, 4000), False)])
def test_sixteen_megapixel_boundary_uses_real_pillow(size, accepted):
    captured = stage(image_bytes(size=size))
    try:
        if accepted:
            png, media = service.normalize(
                captured, snapshot(expected_width=size[0], expected_height=size[1])
            )
            assert (media["width"], media["height"]) == size and png.startswith(
                b"\x89PNG"
            )
        else:
            with pytest.raises(ValueError, match="pixel/frame budget"):
                service.normalize(captured, snapshot())
    finally:
        captured.close()


def test_stage_bytes_and_expected_logical_dimensions_cannot_be_substituted():
    captured = stage(image_bytes())
    try:
        captured.sha256 = "f" * 64
        with pytest.raises(ContentError, match="content_changed"):
            service.normalize(captured, snapshot())
    finally:
        captured.close()
    captured = stage(image_bytes("JPEG", orientation=6))
    try:
        with pytest.raises(ValueError, match="logical dimensions differ"):
            service.normalize(captured, snapshot())
    finally:
        captured.close()


class Permit:
    def __init__(self, world, purpose, revision):
        self.world, self.purpose, self.revision = world, purpose, revision
        self.entered, self.exits = False, []

    async def __aenter__(self):
        self.entered = True
        self.world.events.append(("enter", self.purpose))
        return self

    async def __aexit__(self, _kind, error, _traceback):
        self.exits.append(error)
        self.world.events.append(
            ("exit", self.purpose, all(item.file.closed for item in self.world.stages))
        )


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(
        service.original_sources, "read_options", AsyncMock(return_value={})
    )
    monkeypatch.setattr(managed_original_reclaim_fence, "protect_source", AsyncMock())
    provider = Provider()
    raw = image_bytes()
    provider.raw = provider.actual = raw
    task_id, project_id, item_id, location_id = (uuid.uuid4() for _ in range(4))
    source = OriginalSource(
        owner_kind="dataset_asset",
        content_id=uuid.uuid4(),
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        media_type="image",
        item_id=item_id,
        relation=TaskRelation(
            task_id,
            project_id,
            None,
            "saved/current.png",
            "image",
            None,
            item_id,
            None,
            None,
            item_id,
            (),
        ),
        bucket="saved-originals",
        object_key="saved/current.png",
        endpoint_url=provider.meta.endpoint_url,
        location_id=location_id,
        location_revision=1,
        maintenance_revision=9,
        ownership_facts=("saved-owner-fact",),
        item_facts=None,
        replica_facts=None,
        location_facts=None,
    )
    current = snapshot(
        task_id=str(task_id),
        project_id=str(project_id),
        batch=None,
        dataset_item_id=str(item_id),
        source_key=source.object_key,
        original=service.receipt(source),
    )
    authority = service.Authority(project_id, uuid.uuid4(), None, None)
    value = SimpleNamespace(
        provider=provider,
        raw=raw,
        source=source,
        current=source,
        snapshot=current,
        authority=authority,
        db=SimpleNamespace(commit=AsyncMock()),
        events=[],
        stages=[],
        permits=[],
    )
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(service.reader, "create_client", lambda **_: provider)
    monkeypatch.setattr(
        service.original_sources,
        "resolve_task",
        AsyncMock(side_effect=lambda *_a, **_k: value.current),
    )

    async def checked(db, _authority, *, commit=True, guard=None):
        if guard is not None:
            await guard()
        if commit:
            await db.commit()
        value.events.append(("authority", commit))

    monkeypatch.setattr(service, "check", AsyncMock(side_effect=checked))
    monkeypatch.setattr(service.access, "check_clock", lambda *_: None)

    async def acquire(_db, _location, purpose, **kwargs):
        permit = Permit(value, purpose, kwargs.get("expected_revision", 9))
        value.permits.append(permit)
        return permit

    monkeypatch.setattr(
        service.storage_access, "acquire", AsyncMock(side_effect=acquire)
    )
    monkeypatch.setattr(service.storage_access, "check", AsyncMock())
    capture = service.reader.capture

    async def captured(*args, **kwargs):
        result = await capture(*args, **kwargs)
        value.stages.append(result)
        return result

    monkeypatch.setattr(service.reader, "capture", captured)
    return value


def test_receipt_contains_only_content_and_logical_location_identity(world):
    value = service.receipt(world.source)
    assert value == {
        "schema": 1,
        "owner_kind": "dataset_asset",
        "content_id": str(world.source.content_id),
        "sha256": hashlib.sha256(world.raw).hexdigest(),
        "size_bytes": len(world.raw),
        "location_id": str(world.source.location_id),
    }
    assert service.validate_receipt(value) == value
    assert not {"bucket", "object_key", "endpoint_url", "maintenance_revision"} & set(
        value
    )


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "extra",
        "schema",
        "owner",
        "id",
        "location",
        "sha",
        "size_bool",
        "size_zero",
        "size_over",
    ],
)
def test_receipt_validation_rejects_partial_or_unsafe_facts(world, change):
    value = {**world.snapshot["original"]}
    if change == "missing":
        value.pop("location_id")
    elif change == "extra":
        value["bucket"] = "must-stay-private"
    else:
        field, invalid = {
            "schema": ("schema", True),
            "owner": ("owner_kind", "legacy"),
            "id": ("content_id", "unknown"),
            "location": ("location_id", "unknown"),
            "sha": ("sha256", "F" * 64),
            "size_bool": ("size_bytes", True),
            "size_zero": ("size_bytes", 0),
            "size_over": ("size_bytes", service.detection.MAX_RAW_BYTES + 1),
        }[change]
        value[field] = invalid
    with pytest.raises(ContentError, match="original_input_unrecorded"):
        service.validate_receipt(value)


async def test_read_owns_stage_and_quota_through_real_close_and_final_recheck(world):
    guard = AsyncMock()
    async with service.input_image(
        world.db, world.authority, world.snapshot, guard=guard
    ) as (png, media, source):
        assert png.startswith(b"\x89PNG") and source == world.source
        assert media["sha256"] == world.source.sha256
        assert service._slots._value == 0 and not world.stages[0].file.closed
        assert world.permits[0].exits == []
    assert world.stages[0].file.closed and service._slots._value == 1
    assert world.permits[0].exits == [None]
    assert world.events[-1] == ("exit", "active_learning_original", True)
    assert guard.await_count == 4
    service.storage_access.acquire.assert_awaited_once_with(
        world.db,
        world.source.location_id,
        "active_learning_original",
        expected_revision=9,
    )


@pytest.mark.parametrize("change", ["legacy", "different_owner", "oversize"])
async def test_unknown_or_wrong_original_is_rejected_before_admission_or_object_io(
    world, change
):
    if change == "legacy":
        world.snapshot.pop("original")
    elif change == "different_owner":
        world.current = replace(world.source, content_id=uuid.uuid4())
    else:
        world.current = replace(
            world.source, size_bytes=service.detection.MAX_RAW_BYTES + 1
        )
    with pytest.raises(ContentError):
        await service.read_input(world.db, world.authority, world.snapshot)
    assert not world.provider.calls and not world.permits and not world.stages
    assert service._slots._value == 1


async def test_exact_sixteen_mebibytes_are_normalized_and_closed(world):
    raw = world.raw + b"\0" * (service.detection.MAX_RAW_BYTES - len(world.raw))
    world.provider.raw = world.provider.actual = raw
    world.current = replace(
        world.source, size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()
    )
    world.snapshot["original"] = service.receipt(world.current)
    _png, media, source = await service.read_input(
        world.db, world.authority, world.snapshot
    )
    assert (
        media["size_bytes"] == service.detection.MAX_RAW_BYTES
        and source == world.current
    )
    assert world.stages[0].file.closed and service._slots._value == 1


@pytest.mark.parametrize("phase", ["before_io", "after_decode", "after_close"])
@pytest.mark.parametrize("fact", ["gate", "bucket", "owner"])
async def test_every_operation_rechecks_exact_source_and_gate(
    world, monkeypatch, phase, fact
):
    def mutate():
        field, value = {
            "gate": ("maintenance_revision", 10),
            "bucket": ("bucket", "same-key-trap"),
            "owner": ("content_id", uuid.uuid4()),
        }[fact]
        world.current = replace(world.source, **{field: value})

    if phase == "before_io":
        resolve = service.original_sources.resolve_task

        async def resolved(*args, **kwargs):
            value = await resolve(*args, **kwargs)
            if resolve.await_count == 1:
                mutate()
            return value

        monkeypatch.setattr(
            service.original_sources, "resolve_task", AsyncMock(side_effect=resolved)
        )
    elif phase == "after_decode":
        run = service.bounded_thread

        async def decoded(*args, **kwargs):
            value = await run(*args, **kwargs)
            mutate()
            return value

        monkeypatch.setattr(service, "bounded_thread", decoded)
    else:
        close = service.close_stage

        async def closed(captured):
            await close(captured)
            mutate()

        monkeypatch.setattr(service, "close_stage", closed)
    with pytest.raises(ContentError, match="content_binding_changed"):
        await service.read_input(world.db, world.authority, world.snapshot)
    assert service._slots._value == 1
    if phase == "before_io":
        assert not world.provider.calls and not world.stages
    else:
        assert world.stages[0].file.closed
    assert len(world.permits[0].exits) == 1


@pytest.mark.parametrize("finish", ["cancel", "timeout", "late_unsettled"])
async def test_interrupted_normalization_drains_real_thread_before_closing_stage(
    world, monkeypatch, finish
):
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    normalize = service.normalize

    def blocked(*args):
        entered.set()
        try:
            assert release.wait(5), "test did not release normalization"
            if finish == "late_unsettled":
                raise storage_access.mark_unsettled(OSError("unknown decoder close"))
            return normalize(*args)
        finally:
            ended.set()

    monkeypatch.setattr(service, "normalize", blocked)
    if finish == "timeout":
        monkeypatch.setattr(bounded, "THREAD_SECONDS", 0.03)
    pending = asyncio.create_task(
        service.read_input(world.db, world.authority, world.snapshot)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish != "timeout":
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0.08)
        assert not pending.done() and not ended.is_set()
        assert not world.stages[0].file.closed and service._slots._value == 0
        assert world.permits[0].exits == []
    finally:
        release.set()
    with pytest.raises(
        ContentError if finish == "timeout" else asyncio.CancelledError
    ) as caught:
        await pending
    if finish == "late_unsettled":
        assert caught.value.storage_operation_unsettled
        assert world.permits[0].exits[0].storage_operation_unsettled
    assert ended.is_set() and world.stages[0].file.closed and service._slots._value == 1


@pytest.mark.parametrize(
    "finish", ["cancel", "unknown", "unknown_cancel", "unsettled_then_cancel"]
)
async def test_physical_tempfile_close_keeps_permit_and_quota_until_settled(
    world, monkeypatch, finish
):
    entered, release = threading.Event(), threading.Event()
    captured = []

    class ClosingFile:
        def __init__(self, file):
            self.file = file

        @property
        def closed(self):
            return self.file.closed

        def close(self):
            entered.set()
            assert release.wait(5), "test did not release tempfile close"
            if finish.startswith("unknown"):
                raise OSError("unknown actual tempfile close")
            self.file.close()

    async def operation():
        async with service.input_image(world.db, world.authority, world.snapshot):
            file = world.stages[0].file
            captured.append(file)
            world.stages[0].file = ClosingFile(file)
            if finish == "unsettled_then_cancel":
                raise storage_access.mark_unsettled(
                    OSError("prior unresolved operation")
                )

    pending = asyncio.create_task(operation())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish != "unknown":
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not captured[0].closed
        assert service._slots._value == 0 and world.permits[0].exits == []
    finally:
        release.set()
    try:
        with pytest.raises(
            OSError if finish == "unknown" else asyncio.CancelledError
        ) as caught:
            await pending
        if finish.startswith("unknown"):
            assert not captured[0].closed and service._slots._value == 0
            assert caught.value.storage_operation_unsettled
            assert world.permits[0].exits[0].storage_operation_unsettled
        else:
            assert captured[0].closed and service._slots._value == 1
            if finish == "unsettled_then_cancel":
                assert caught.value.storage_operation_unsettled
                assert world.permits[0].exits[0].storage_operation_unsettled
    finally:
        captured[0].close()


@pytest.fixture
def output(world, monkeypatch):
    monkeypatch.setattr(
        learning.storage_io.artifact_reads, "resolve", AsyncMock(return_value=None)
    )
    round = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=world.authority.project_id,
        creator_id=world.authority.actor_id,
        spec={"original_contract": 1},
        authorization={},
    )
    captured = stage(world.raw)
    try:
        png, media = service.normalize(captured, world.snapshot)
    finally:
        captured.close()
    sample = {"ordinal": 0, "snapshot": world.snapshot, **media}
    round.spec["samples"] = [sample]
    objects = Objects()
    monkeypatch.setattr(learning.storage, "client", objects)
    endpoint = objects.meta.endpoint_url
    round.storage_ref = {
        "schema": 1,
        "purpose": "active-learning",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": "saved-output",
        "prefix": learning.owners.prefix_for(round.project_id, round.id),
        "storage_identity": managed_storage_identity(endpoint, "saved-output"),
        "origin": "new",
        "evidence": None,
    }
    owner = learning.owners.resolve(round)

    @contextmanager
    def client():
        yield objects

    target = SimpleNamespace(
        client=client, bucket=owner.bucket, location_id=owner.location_id
    )
    # This SQL-free fixture owns the physical transport and permits; durable
    # write-accounting transactions are exercised by the accounting suites.
    monkeypatch.setattr(learning.storage_io, "prepare", AsyncMock(return_value=target))
    monkeypatch.setattr(learning.write_accounting, "recorder", lambda *_a, **_k: None)
    monkeypatch.setattr(service, "delegated", AsyncMock(return_value=world.authority))
    return SimpleNamespace(
        world=world, round=round, sample=sample, owner=owner, objects=objects, png=png
    )


async def test_retained_png_hit_uses_its_owner_and_never_resolves_original(output):
    value = output
    learning._store_png(value.round, value.sample, value.owner, value.png)
    png = await learning.prepare_image(value.world.db, value.round, value.sample)
    assert (
        png == value.png and not value.world.provider.calls and not value.world.stages
    )
    service.original_sources.resolve_task.assert_not_awaited()
    assert [p.purpose for p in value.world.permits] == ["active_learning_media"]
    assert value.world.permits[0].exits == [None]


@pytest.mark.parametrize("state", ["legacy_missing", "bad_cached"])
async def test_legacy_missing_or_bad_retained_png_never_falls_back_to_raw(
    output, state
):
    value = output
    if state == "legacy_missing":
        value.round.spec.pop("original_contract")
    else:
        learning._store_png(value.round, value.sample, value.owner, value.png)
        key = learning.media_descriptor(value.round, value.sample)["key"]
        value.objects.objects[key]["raw"] = b"tampered"
    with pytest.raises(ContentError if state == "legacy_missing" else ValueError):
        await learning.prepare_image(value.world.db, value.round, value.sample)
    assert not value.world.provider.calls and not value.world.stages
    service.original_sources.resolve_task.assert_not_awaited()
    assert len(value.world.permits[0].exits) == 1


async def test_missing_png_rebuild_uses_verified_input_and_closes_before_return(output):
    value = output
    assert (
        await learning.prepare_image(value.world.db, value.round, value.sample)
        == value.png
    )
    assert learning.read_retained_png(value.round, value.sample) == value.png
    assert value.world.stages[0].file.closed and service._slots._value == 1
    exits = [event for event in value.world.events if event[0] == "exit"]
    assert exits == [
        ("exit", "active_learning_original", True),
        ("exit", "active_learning_media", True),
    ]


@pytest.mark.parametrize("finish", ["cancel", "timeout", "guard", "unsettled"])
async def test_real_png_publication_thread_is_drained_and_late_result_is_not_returned(
    output, monkeypatch, finish
):
    value = output
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    store = learning._store_png

    def blocked(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(5), "test did not release PNG store"
            store(*args, **kwargs)
            if finish == "unsettled":
                raise storage_access.mark_unsettled(OSError("unknown output close"))
        finally:
            ended.set()

    async def guard():
        if finish == "guard" and ended.is_set():
            raise RuntimeError("claim fenced")

    monkeypatch.setattr(learning, "_store_png", blocked)
    if finish == "timeout":
        monkeypatch.setattr(bounded, "THREAD_SECONDS", 0.03)
    pending = asyncio.create_task(
        learning.prepare_image(value.world.db, value.round, value.sample, guard=guard)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish == "cancel":
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0.08)
        assert not pending.done() and not ended.is_set()
        assert all(not p.exits for p in value.world.permits)
        assert not value.world.stages[0].file.closed and service._slots._value == 0
    finally:
        release.set()
    error = {
        "cancel": asyncio.CancelledError,
        "timeout": ContentError,
        "guard": RuntimeError,
        "unsettled": OSError,
    }[finish]
    with pytest.raises(error) as caught:
        await pending
    assert (
        ended.is_set()
        and value.world.stages[0].file.closed
        and service._slots._value == 1
    )
    assert learning.read_retained_png(value.round, value.sample) == value.png
    if finish == "unsettled":
        assert caught.value.storage_operation_unsettled
        assert all(p.exits[0].storage_operation_unsettled for p in value.world.permits)
    assert all(len(p.exits) == 1 for p in value.world.permits)


async def test_check_can_revalidate_without_committing_a_business_transaction(
    monkeypatch,
):
    db = SimpleNamespace(commit=AsyncMock())
    proof = object()
    key = ("opaque-hash", ("*",), None)
    context = service.Authority(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), key, proof)
    actor = SimpleNamespace(id=context.actor_id)
    monkeypatch.setattr(
        service.access, "actor_and_key", AsyncMock(return_value=(actor, key))
    )
    training = AsyncMock()
    monkeypatch.setattr(service, "assert_training_authority", training)
    clock = []
    monkeypatch.setattr(service.access, "check_clock", lambda *args: clock.append(args))
    guard = AsyncMock()
    await service.check(db, context, commit=False, guard=guard)
    db.commit.assert_not_awaited()
    guard.assert_awaited_once()
    assert training.await_args.kwargs["required_scopes"] == service.SCOPES
    assert clock == [(proof, key)]


def test_background_key_fingerprint_covers_hash_scopes_and_exact_expiration():
    expires = datetime(2026, 10, 24, tzinfo=timezone.utc)
    key = ("one-hash", ("datasets:read", "datasets:write", "annotations:read"), expires)
    assert service.key_fingerprint(key) != service.key_fingerprint(None)
    for changed in [
        ("new-hash", key[1], expires),
        (key[0], ("*",), expires),
        (key[0], key[1], None),
    ]:
        assert service.key_fingerprint(changed) != service.key_fingerprint(key)


async def test_delegated_new_round_rejects_same_id_rotated_key_before_original_lookup(
    monkeypatch,
):
    actor_id, key_id = uuid.uuid4(), uuid.uuid4()
    key = ("original-hash", ("*",), None)
    round = SimpleNamespace(
        creator_id=actor_id,
        project_id=uuid.uuid4(),
        spec={"original_contract": 1},
        authorization={
            "original_contract": 1,
            "api_key_id": str(key_id),
            "api_key_fingerprint": service.key_fingerprint(key),
        },
    )
    monkeypatch.setattr(
        service,
        "current_actor_key",
        AsyncMock(return_value=(object(), ("rotated-hash", ("*",), None))),
    )
    check = AsyncMock()
    monkeypatch.setattr(service, "check", check)
    with pytest.raises(HTTPException) as caught:
        await service.delegated(object(), round)
    assert caught.value.status_code == 403
    check.assert_not_awaited()


@pytest.mark.parametrize("phase", ["cache_hit", "published_readback"])
async def test_actual_output_body_close_failure_keeps_all_active_permits(
    output, monkeypatch, phase
):
    value = output
    if phase == "cache_hit":
        learning._store_png(value.round, value.sample, value.owner, value.png)
    bodies = []
    read = value.objects.get_object

    class UnknownClose:
        def __init__(self, body):
            self.body = body

        def read(self, size):
            return self.body.read(size)

        def close(self):
            raise OSError("output Body close could not be confirmed")

    def get(**kwargs):
        result = read(**kwargs)
        bodies.append(result["Body"])
        result["Body"] = UnknownClose(result["Body"])
        return result

    monkeypatch.setattr(value.objects, "get_object", get)
    try:
        with pytest.raises(OSError) as caught:
            await learning.prepare_image(value.world.db, value.round, value.sample)
        assert caught.value.storage_operation_unsettled
        assert bodies and all(not body.closed for body in bodies)
        assert all(
            permit.exits[0].storage_operation_unsettled
            for permit in value.world.permits
        )
        assert service._slots._value == 1
        if phase == "cache_hit":
            assert not value.world.stages and not value.world.provider.calls
        else:
            assert value.world.stages[0].file.closed
            assert len(value.world.permits) == 2
    finally:
        for body in bodies:
            body.close()
