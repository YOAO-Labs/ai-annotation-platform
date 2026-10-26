"""Pure PCD parsing and actual original-reader resource lifecycle regression tests."""

import asyncio
from dataclasses import replace
import hashlib
import io
import struct
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import numpy as np
import pytest

from app.services import sample_evaluation_originals as bounded
from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services.managed_original_reader import BinaryStage
from app.services.original_sources import OriginalSource, TaskRelation
from app.services.point_cloud_quality import originals as service
from tests.test_managed_original_reader import Provider


def pcd(mode="ascii"):
    # A deliberately reordered field layout ensures XYZ is decoded by name.
    header = (
        "VERSION .7\nFIELDS intensity z x y\nSIZE 4 4 4 4\n"
        "TYPE F F F F\nCOUNT 1 1 1 1\nWIDTH 3\nHEIGHT 1\n"
        f"POINTS 3\nDATA {mode}\n"
    ).encode()
    rows = [(9.0, 3.0, 1.0, 2.0), (8.0, -6.0, -4.0, 5.0), (0, 1, float("nan"), 2)]
    payload = (
        b"".join(struct.pack("<ffff", *row) for row in rows)
        if mode == "binary"
        else b"9 3 1 2\n8 -6 -4 5\n0 1 nan 2\n"
    )
    return header + payload


def stage(raw):
    return BinaryStage(io.BytesIO(raw), hashlib.sha256(raw).hexdigest(), len(raw), {})


@pytest.mark.parametrize("mode", ["ascii", "binary"])
@pytest.mark.parametrize(
    "convention,expected",
    [
        (None, [[1, 2, 3], [-4, 5, -6]]),
        ("iso_8855", [[1, 2, 3], [-4, 5, -6]]),
        ("ros_rep103", [[1, 2, 3], [-4, 5, -6]]),
        ("raw", [[1, 2, 3], [-4, 5, -6]]),
        ("apollo", [[2, -1, 3], [5, 4, -6]]),
        ("y_forward", [[2, -1, 3], [5, 4, -6]]),
        ("kitti_camera", [[3, -1, -2], [-6, 4, -5]]),
        ("opencv_camera", [[3, -1, -2], [-6, 4, -5]]),
        ("sustechpoints_demo", [[-2, 1, 3], [-5, -4, -6]]),
    ],
)
def test_real_pcd_field_order_finite_filter_and_iso_axes(mode, convention, expected):
    captured = stage(pcd(mode))
    try:
        points = service.parse_points(captured, convention)
        assert points.dtype == np.float32 and points.shape == (2, 3)
        np.testing.assert_allclose(points, expected)
        assert captured.file.closed is False
    finally:
        captured.close()


@pytest.mark.parametrize(
    "raw",
    [
        b"not a point cloud",
        pcd().replace(b"DATA ascii", b"DATA binary_compressed"),
        pcd().replace(b"FIELDS intensity z x y", b"FIELDS intensity z u v"),
        pcd("binary")[:-1],
        pcd().replace(b"POINTS 3", b"POINTS 99"),
    ],
    ids=[
        "missing_header",
        "compressed",
        "missing_xyz",
        "binary_truncated",
        "ascii_truncated",
    ],
)
def test_invalid_or_unsupported_pcd_never_returns_points(raw):
    captured = stage(raw)
    try:
        with pytest.raises(ValueError):
            service.parse_points(captured, "iso_8855")
    finally:
        captured.close()


@pytest.mark.parametrize("change", ["sha", "size", "bytes", "axis"])
def test_verified_stage_receipt_and_axis_must_match_actual_input(change):
    captured = stage(pcd())
    try:
        if change == "sha":
            captured.sha256 = "f" * 64
        elif change == "size":
            captured.size += 1
        elif change == "bytes":
            captured.file.seek(0)
            captured.file.write(b"tampered")
        with pytest.raises(ValueError if change == "axis" else ContentError):
            service.parse_points(captured, "unknown" if change == "axis" else "raw")
    finally:
        captured.close()


def source(raw):
    task_id, project_id, item_id = (uuid.uuid4() for _ in range(3))
    return OriginalSource(
        owner_kind="dataset_asset",
        content_id=uuid.uuid4(),
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        media_type="point_cloud",
        item_id=item_id,
        relation=TaskRelation(
            task_id,
            project_id,
            None,
            "saved/点云 空格%25.pcd",
            "point_cloud",
            None,
            item_id,
            None,
            None,
            item_id,
            (),
        ),
        bucket="historical-pointclouds",
        object_key="saved/点云 空格%25.pcd",
        endpoint_url="http://originals.test:9000",
        location_id=uuid.uuid4(),
        location_revision=3,
        maintenance_revision=9,
        ownership_facts=("saved-original",),
        item_facts=None,
        replica_facts=None,
        location_facts=None,
    )


@pytest.mark.parametrize("owner", ["dataset_asset", "task_original"])
def test_receipt_keeps_only_safe_content_and_logical_location_identity(owner):
    original = replace(source(pcd()), owner_kind=owner)
    value = service.receipt(original)
    assert value == {
        "schema": 1,
        "owner_kind": owner,
        "content_id": str(original.content_id),
        "sha256": original.sha256,
        "size_bytes": original.size_bytes,
        "location_id": str(original.location_id),
    }
    assert service.validate_receipt(value) == value
    assert (
        not {
            "bucket",
            "object_key",
            "endpoint_url",
            "location_revision",
            "maintenance_revision",
        }
        & value.keys()
    )


@pytest.mark.parametrize(
    "field,invalid",
    [
        ("schema", True),
        ("schema", 2),
        ("owner_kind", "legacy"),
        ("owner_kind", []),
        ("content_id", "not-an-id"),
        ("content_id", "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"),
        ("location_id", None),
        ("location_id", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("sha256", "F" * 64),
        ("sha256", "x" * 64),
        ("sha256", "a" * 63),
        ("sha256", None),
        ("size_bytes", True),
        ("size_bytes", 1.0),
        ("size_bytes", 0),
        ("size_bytes", -1),
        ("size_bytes", service.MAX_BYTES + 1),
    ],
)
def test_strict_receipt_rejects_ambiguous_or_invalid_identity(field, invalid):
    value = service.receipt(source(pcd())) | {field: invalid}
    with pytest.raises(ContentError, match="original_input_unrecorded"):
        service.validate_receipt(value)


@pytest.mark.parametrize("change", ["missing", "private_extra", "none", "list"])
def test_partial_receipt_or_private_locator_is_not_a_legacy_upgrade(change):
    value = service.receipt(source(pcd()))
    if change == "missing":
        value.pop("content_id")
    elif change == "private_extra":
        value["bucket"] = "private-originals"
    else:
        value = None if change == "none" else list(value)
    with pytest.raises(ContentError, match="original_input_unrecorded"):
        service.validate_receipt(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"media_type": "image"},
        {"bucket": None},
        {"location_id": None},
        {"size_bytes": 0},
        {"size_bytes": service.MAX_BYTES + 1},
    ],
)
def test_unsupported_sources_do_not_receive_a_pcd_receipt(changes):
    with pytest.raises(ContentError, match="pointcloud_original_unsupported"):
        service.receipt(replace(source(pcd()), **changes))


def test_exact_declared_byte_budget_is_accepted_without_allocating_a_large_file():
    original = replace(source(pcd()), size_bytes=service.MAX_BYTES)
    assert (
        service.validate_receipt(service.receipt(original))["size_bytes"]
        == 256 * 1024**2
    )


class Permit:
    def __init__(self, world):
        self.world, self.active, self.exits = world, False, []

    async def __aenter__(self):
        self.active = True
        self.world.events.append("permit_enter")
        return self

    async def __aexit__(self, _kind, error, _traceback):
        self.exits.append(error)
        if not getattr(error, "storage_operation_unsettled", False):
            self.active = False
        self.world.events.append("permit_exit")


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(
        service.original_sources, "read_options", AsyncMock(return_value={})
    )
    provider = Provider()
    raw = pcd()
    provider.raw = provider.actual = raw
    original = source(raw)
    value = SimpleNamespace(
        provider=provider,
        source=original,
        db=object(),
        context=object(),
        files=[],
        stages=[],
        permits=[],
        events=[],
        rechecks=0,
        recheck_hook=None,
        stage_hook=None,
        capture_seconds=None,
    )

    def exact(call):
        def checked(**kwargs):
            assert (kwargs["Bucket"], kwargs["Key"]) == (
                original.bucket,
                original.object_key,
            )
            return call(**kwargs)

        return checked

    provider.head_object = exact(provider.head_object)
    provider.get_object = exact(provider.get_object)
    monkeypatch.setattr(service.reader, "create_client", lambda **_: provider)
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(1))
    temporary_file = service.reader.tempfile.TemporaryFile

    def temporary(*args, **kwargs):
        result = temporary_file(*args, **kwargs)
        value.files.append(result)
        return result

    monkeypatch.setattr(service.reader.tempfile, "TemporaryFile", temporary)
    capture = service.reader.capture

    async def captured(*args, **kwargs):
        if value.capture_seconds is not None:
            kwargs.update(
                timeout_seconds=value.capture_seconds,
                transport_timeout_seconds=value.capture_seconds / 2,
            )
        result = await capture(*args, **kwargs)
        value.stages.append(result)
        if value.stage_hook is not None:
            value.stage_hook(result)
        return result

    monkeypatch.setattr(service.reader, "capture", captured)

    async def recheck(*args, **kwargs):
        value.rechecks += 1
        value.events.append(("recheck", all(file.closed for file in value.files)))
        if value.recheck_hook is not None:
            await value.recheck_hook()

    monkeypatch.setattr(service, "recheck", AsyncMock(side_effect=recheck))

    async def acquire(*args, **kwargs):
        permit = Permit(value)
        value.permits.append(permit)
        return permit

    monkeypatch.setattr(
        service.storage_access, "acquire", AsyncMock(side_effect=acquire)
    )
    yield value
    # Unknown-close tests deliberately leave responsibility registered. Retire
    # their actual test-owned files without weakening the assertions above.
    for file in value.files:
        file.close()
    for body in provider.bodies:
        io.BytesIO.close(body)


async def read(world, convention="apollo"):
    return await service.read_points(world.db, world.context, world.source, convention)


@pytest.mark.parametrize("mode", ["ascii", "binary"])
async def test_real_read_uses_exact_saved_bucket_and_closes_before_final_recheck(
    world, mode
):
    raw = pcd(mode)
    world.provider.raw = world.provider.actual = raw
    world.source = replace(
        world.source, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
    )
    points = await read(world)
    np.testing.assert_allclose(points, [[2, -1, 3], [5, 4, -6]])
    assert world.files and all(file.closed for file in world.files)
    assert all(body.closed for body in world.provider.bodies)
    assert world.provider.closed == 1 and service._slots._value == 1
    assert world.permits[0].exits == [None] and not world.permits[0].active
    assert world.events == [
        "permit_enter",
        ("recheck", True),
        ("recheck", True),
        "permit_exit",
    ]
    service.storage_access.acquire.assert_awaited_once_with(
        world.db,
        world.source.location_id,
        "point_cloud_quality_original",
        expected_revision=9,
    )
    actions = [name for name, _ in world.provider.calls]
    assert actions == ["head", "get", "get", "body_close", "head", "client_close"]


@pytest.mark.parametrize("change", ["wrong_sha", "wrong_size", "bad_pcd", "bad_axis"])
async def test_invalid_actual_input_is_unavailable_after_close_and_final_recheck(
    world, change
):
    convention = "apollo"
    if change == "wrong_sha":
        world.provider.actual = b"x" * len(world.provider.raw)
    elif change == "wrong_size":
        world.provider.head_changes = {"ContentLength": world.source.size_bytes + 1}
    elif change == "bad_pcd":
        raw = b"not a PCD"
        world.provider.raw = world.provider.actual = raw
        world.source = replace(
            world.source, size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()
        )
    else:
        convention = "unknown"
    with pytest.raises(service.Unavailable):
        await read(world, convention)
    assert all(file.closed for file in world.files) and world.rechecks == 2
    assert world.provider.closed == 1 and service._slots._value == 1
    assert not world.permits[0].active


@pytest.mark.parametrize("phase", ["before_io", "after_close"])
async def test_business_recheck_failure_never_becomes_a_content_skip(world, phase):
    async def revoke():
        if world.rechecks == (1 if phase == "before_io" else 2):
            raise HTTPException(403, {"code": "content_authority_revoked"})

    world.recheck_hook = revoke
    with pytest.raises(HTTPException) as caught:
        await read(world)
    assert caught.value.status_code == 403 and not world.permits[0].active
    assert service._slots._value == 1 and all(file.closed for file in world.files)
    if phase == "before_io":
        assert not world.provider.calls and not world.files


async def test_occupied_admission_never_reads_or_releases_another_owner(world):
    assert service._slots.acquire(blocking=False)
    try:
        with pytest.raises(service.Unavailable, match="snapshot_capacity_exceeded"):
            await read(world)
        assert not world.provider.calls and not world.files
        assert service._slots._value == 0 and not world.permits[0].active
    finally:
        service._slots.release()


async def test_declared_oversize_is_rejected_without_copying_or_allocating(world):
    world.source = replace(world.source, size_bytes=service.MAX_BYTES + 1)
    with pytest.raises(service.Unavailable, match="content_too_large"):
        await read(world)
    assert not world.provider.calls and not world.files and not world.stages
    assert service._slots._value == 1 and not world.permits[0].active


@pytest.mark.parametrize("phase", ["read", "body_close", "client_close"])
@pytest.mark.parametrize("finish", ["cancel", "timeout"])
async def test_physical_capture_drains_before_return_and_releases_its_resources(
    world, phase, finish
):
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        try:
            assert release.wait(5), "test did not release provider thread"
        finally:
            ended.set()

    setattr(world.provider, phase + "_hook", blocked)
    if finish == "timeout":
        world.capture_seconds = 0.025
    pending = asyncio.create_task(read(world))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish == "cancel":
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0.06)
        assert not pending.done() and not ended.is_set()
        assert service._slots._value == 0 and world.permits[0].exits == []
        assert world.files and not world.files[0].closed
    finally:
        release.set()
    with pytest.raises(
        asyncio.CancelledError if finish == "cancel" else service.Unavailable
    ):
        await pending
    assert ended.is_set() and all(file.closed for file in world.files)
    assert world.provider.closed == 1 and all(
        body.closed for body in world.provider.bodies
    )
    assert service._slots._value == 1 and not world.permits[0].active


@pytest.mark.parametrize(
    "finish", ["cancel", "timeout", "late_unknown_cancel", "late_unknown_timeout"]
)
async def test_real_parse_thread_is_drained_before_retiring_the_verified_stage(
    world, monkeypatch, finish
):
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    parse = service.parse_points

    def blocked(*args):
        entered.set()
        try:
            assert release.wait(5), "test did not release PCD decoder"
            points = parse(*args)
            if finish.startswith("late_unknown"):
                raise storage_access.mark_unsettled(OSError("unknown decoder close"))
            return points
        finally:
            ended.set()

    monkeypatch.setattr(service, "parse_points", blocked)
    if finish.endswith("timeout"):
        monkeypatch.setattr(bounded, "THREAD_SECONDS", 0.025)
    pending = asyncio.create_task(read(world))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish.endswith("cancel"):
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0.06)
        assert not pending.done() and not ended.is_set()
        assert not world.stages[0].file.closed and service._slots._value == 0
        assert world.permits[0].exits == []
    finally:
        release.set()
    expected = (
        asyncio.CancelledError
        if finish.endswith("cancel")
        else ContentError
        if finish.startswith("late_unknown")
        else service.Unavailable
    )
    with pytest.raises(expected) as caught:
        await pending
    assert ended.is_set() and world.stages[0].file.closed
    assert service._slots._value == 1
    if finish.startswith("late_unknown"):
        assert caught.value.storage_operation_unsettled
        assert world.permits[0].active
        assert world.permits[0].exits[0].storage_operation_unsettled
    else:
        assert not world.permits[0].active


@pytest.mark.parametrize("phase", ["body", "client"])
@pytest.mark.parametrize("finish", ["cancel", "timeout"])
async def test_interrupted_capture_keeps_late_unknown_remote_close_registered(
    world, phase, finish
):
    entered, release = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(5), "test did not release interrupted provider read"

    world.provider.read_hook = blocked
    setattr(world.provider, phase + "_close_error", OSError("late unknown close"))
    if finish == "timeout":
        world.capture_seconds = 0.025
    pending = asyncio.create_task(read(world))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish == "cancel":
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0.06)
        assert not pending.done() and service._slots._value == 0
        assert world.permits[0].exits == [] and not world.files[0].closed
    finally:
        release.set()
    with pytest.raises(
        asyncio.CancelledError if finish == "cancel" else ContentError
    ) as caught:
        await pending
    assert caught.value.storage_operation_unsettled
    assert (
        world.permits[0].active
        and world.permits[0].exits[0].storage_operation_unsettled
    )
    assert service._slots._value == 0 and world.files[0].closed
    assert world.provider.closed == 1 and world.rechecks == 1


@pytest.mark.parametrize("phase", ["body", "client", "decoder"])
async def test_unknown_physical_close_propagates_and_retains_durable_responsibility(
    world, monkeypatch, phase
):
    if phase == "decoder":
        parse = service.parse_points

        class Decoder:
            def close(self):
                raise OSError("decoder close was not confirmed")

        def decoded(*args):
            with storage_access.closing_registered(Decoder()):
                return parse(*args)

        monkeypatch.setattr(service, "parse_points", decoded)
    else:
        setattr(world.provider, phase + "_close_error", OSError("unknown remote close"))
    with pytest.raises(OSError if phase == "decoder" else ContentError) as caught:
        await read(world)
    assert caught.value.storage_operation_unsettled
    assert (
        world.permits[0].active
        and world.permits[0].exits[0].storage_operation_unsettled
    )
    assert world.files and all(file.closed for file in world.files)
    assert service._slots._value == (1 if phase == "decoder" else 0)
    assert world.rechecks == 1


@pytest.mark.parametrize(
    "finish", ["cancel", "unknown", "unknown_cancel", "prior_unknown_then_cancel"]
)
async def test_actual_local_close_drains_and_preserves_prior_unknown_markers(
    world, monkeypatch, finish
):
    entered, release = threading.Event(), threading.Event()

    class ClosingFile:
        def __init__(self, file):
            self.file = file

        def __getattr__(self, name):
            return getattr(self.file, name)

        def close(self):
            entered.set()
            assert release.wait(5), "test did not release actual tempfile close"
            if finish.startswith("unknown"):
                raise OSError("local close not confirmed")
            self.file.close()

    def wrap(captured):
        captured.file = ClosingFile(captured.file)

    world.stage_hook = wrap
    if finish == "prior_unknown_then_cancel":
        parse = service.parse_points

        def decoded(*args):
            parse(*args)
            raise storage_access.mark_unsettled(OSError("prior decoder uncertainty"))

        monkeypatch.setattr(service, "parse_points", decoded)
    pending = asyncio.create_task(read(world))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish != "unknown":
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not world.files[0].closed
        assert service._slots._value == 0 and world.permits[0].exits == []
    finally:
        release.set()
    with pytest.raises(
        OSError if finish == "unknown" else asyncio.CancelledError
    ) as caught:
        await pending
    if finish.startswith("unknown"):
        assert not world.files[0].closed and service._slots._value == 0
    else:
        assert world.files[0].closed and service._slots._value == 1
    if finish != "cancel":
        assert caught.value.storage_operation_unsettled
        assert (
            world.permits[0].active
            and world.permits[0].exits[0].storage_operation_unsettled
        )
    else:
        assert not world.permits[0].active
