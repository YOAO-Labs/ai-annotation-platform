"""Pure training-image contracts and actual capture/native-close lifecycles."""

import asyncio
from contextvars import ContextVar
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
from PIL import Image
import pytest

from app.services import storage_access
from app.services import training_dataset_originals as service
from app.services.content_snapshot import ContentError
from app.services.original_sources import OriginalSource, TaskRelation
from app.services.training_dataset_bridge import TrainingDatasetError
from tests.test_managed_original_reader import Provider


def encoded(format="PNG", *, size=(4, 3), orientation=None, mode="RGB", color=None):
    output = io.BytesIO()
    image = Image.new(
        mode, size, color if color is not None else (37 if mode == "L" else "red")
    )
    try:
        options = {"compress_level": 1} if format == "PNG" else {}
        if orientation is not None:
            exif = Image.Exif()
            exif[274] = orientation
            options["exif"] = exif
        image.save(output, format=format, **options)
        return output.getvalue()
    finally:
        image.close()
        output.close()


def original(raw):
    task_id, project_id, item_id = (uuid.uuid4() for _ in range(3))
    return OriginalSource(
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
            "saved/图片 空格%25",
            "image",
            None,
            item_id,
            None,
            None,
            item_id,
            (),
        ),
        bucket="historical-training-source",
        object_key="saved/图片 空格%25",
        endpoint_url="http://originals.test:9000",
        location_id=uuid.uuid4(),
        location_revision=1,
        maintenance_revision=9,
        ownership_facts=("saved-owner",),
        item_facts=None,
        replica_facts=None,
        location_facts=None,
    )


@pytest.mark.parametrize("owner_kind", ["dataset_asset", "task_original"])
def test_receipt_contains_only_safe_content_and_logical_location_identity(owner_kind):
    source = replace(original(encoded()), owner_kind=owner_kind)
    receipt = service.receipt(source)
    assert receipt == {
        "schema": 1,
        "owner_kind": owner_kind,
        "content_id": str(source.content_id),
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "location_id": str(source.location_id),
    }
    assert service.validate_receipt(receipt) == receipt
    assert (
        not {
            "bucket",
            "object_key",
            "endpoint_url",
            "location_revision",
            "maintenance_revision",
        }
        & receipt.keys()
    )


@pytest.mark.parametrize(
    "field,invalid",
    [
        ("schema", True),
        ("schema", 2),
        ("owner_kind", "legacy"),
        ("owner_kind", []),
        ("content_id", "unknown"),
        ("content_id", "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"),
        ("location_id", None),
        ("location_id", "a" * 32),
        ("sha256", "A" * 64),
        ("sha256", "z" * 64),
        ("sha256", "a" * 63),
        ("sha256", None),
        ("size_bytes", True),
        ("size_bytes", 1.0),
        ("size_bytes", 0),
        ("size_bytes", -1),
        ("size_bytes", service.MAX_BYTES + 1),
    ],
)
def test_strict_receipt_rejects_ambiguous_or_invalid_facts(field, invalid):
    receipt = service.receipt(original(encoded())) | {field: invalid}
    with pytest.raises(ContentError, match="original_input_unrecorded"):
        service.validate_receipt(receipt)


@pytest.mark.parametrize("change", ["missing", "private_extra", "none", "list"])
def test_partial_legacy_or_private_receipt_never_becomes_an_original_contract(change):
    receipt = service.receipt(original(encoded()))
    if change == "missing":
        receipt.pop("location_id")
    elif change == "private_extra":
        receipt["bucket"] = "must-remain-private"
    else:
        receipt = None if change == "none" else list(receipt)
    with pytest.raises(ContentError, match="original_input_unrecorded"):
        service.validate_receipt(receipt)


@pytest.mark.parametrize(
    "changes",
    [
        {"media_type": "point_cloud"},
        {"bucket": None},
        {"location_id": None},
        {"size_bytes": 0},
        {"size_bytes": service.MAX_BYTES + 1},
    ],
)
def test_unsupported_originals_have_no_training_receipt(changes):
    with pytest.raises(ContentError, match="original_input_unsupported"):
        service.receipt(replace(original(encoded()), **changes))


def test_exact_256_mebibyte_declared_size_is_valid_without_large_allocation():
    source = replace(original(encoded()), size_bytes=256 * 1024**2)
    assert (
        service.validate_receipt(service.receipt(source))["size_bytes"]
        == service.MAX_BYTES
    )


class Permit:
    def __init__(self, world):
        self.world, self.active, self.exits = world, False, []

    async def __aenter__(self):
        self.active = True
        self.world.events.append(("permit_enter",))
        return self

    async def __aexit__(self, _kind, error, _traceback):
        self.exits.append(error)
        if not getattr(error, "storage_operation_unsettled", False):
            self.active = False
        self.world.events.append(("permit_exit",))


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(
        service.original_sources, "read_options", AsyncMock(return_value={})
    )
    raw = encoded()
    source = original(raw)
    provider = Provider()
    objects = {
        (source.bucket, source.object_key): raw,
        ("current-bucket-trap", source.object_key): encoded(color="blue"),
    }
    value = SimpleNamespace(
        raw=raw,
        source=source,
        current=source,
        provider=provider,
        objects=objects,
        sample={
            "task_id": str(source.relation.task_id),
            "source_key": source.object_key,
            "expected_width": 4,
            "expected_height": 3,
            "original": service.receipt(source),
        },
        authority=service.Authority(
            source.relation.project_id, uuid.uuid4(), None, None
        ),
        db=SimpleNamespace(commit=AsyncMock()),
        files=[],
        stages=[],
        images=[],
        permits=[],
        events=[],
        resolves=0,
        checks=0,
        resolve_hook=None,
        check_hook=None,
        stage_hook=None,
        actual_override=None,
        capture_seconds=None,
    )

    def load(raw, *, expected_width=4, expected_height=3):
        value.raw = raw
        value.source = replace(
            source, size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()
        )
        value.current = value.source
        value.objects[source.bucket, source.object_key] = raw
        value.sample.update(
            expected_width=expected_width,
            expected_height=expected_height,
            original=service.receipt(value.source),
        )

    value.load = load

    def select(call):
        def selected(**kwargs):
            payload = objects[kwargs["Bucket"], kwargs["Key"]]
            provider.raw = payload
            provider.actual = (
                payload if value.actual_override is None else value.actual_override
            )
            return call(**kwargs)

        return selected

    provider.head_object = select(provider.head_object)
    provider.get_object = select(provider.get_object)
    monkeypatch.setattr(service.reader, "create_client", lambda **_: provider)
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(2))
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

    async def resolve(*args, **kwargs):
        value.resolves += 1
        result = value.current
        value.events.append(
            ("source", value.resolves, all(file.closed for file in value.files))
        )
        if value.resolve_hook is not None:
            value.resolve_hook()
        return result

    monkeypatch.setattr(
        service.original_sources, "resolve_task", AsyncMock(side_effect=resolve)
    )

    async def check(*args):
        value.checks += 1
        if value.check_hook is not None:
            await value.check_hook()

    monkeypatch.setattr(service, "check", AsyncMock(side_effect=check))
    monkeypatch.setattr(service, "clock", lambda *_: None)

    async def acquire(*args, **kwargs):
        permit = Permit(value)
        value.permits.append(permit)
        return permit

    monkeypatch.setattr(
        service.storage_access, "acquire", AsyncMock(side_effect=acquire)
    )
    yield value
    for image in value.images:
        image.close()
    for file in value.files:
        file.close()
    for body in provider.bodies:
        io.BytesIO.close(body)


async def preflight(world):
    return await service.preflight(world.db, world.authority, world.sample)


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP", "BMP", "TIFF"])
async def test_supported_real_formats_keep_original_bytes_and_borrowed_stage(
    world, format
):
    world.load(encoded(format))
    async with service.media(world.db, world.authority, world.sample) as (
        stage,
        source,
    ):
        assert source == world.source and not isinstance(stage.file, io.BytesIO)
        assert stage.file.fileno() >= 0 and not stage.file.closed
        assert world.provider.closed == 1 and all(
            body.closed for body in world.provider.bodies
        )
        result = await service.physical(service.inspect_image, stage, world.sample)
        assert result == {
            "sha256": hashlib.sha256(world.raw).hexdigest(),
            "size_bytes": len(world.raw),
            "width": 4,
            "height": 3,
        }
        assert not stage.file.closed and service._slots._value == 1
        stage.file.seek(0)
        assert stage.file.read() == world.raw
        assert world.permits[0].exits == []
    assert stage.file.closed and service._slots._value == 2
    assert world.permits[0].exits == [None] and not world.permits[0].active
    for action, params in world.provider.calls:
        if action in {"get", "head"}:
            assert (params["Bucket"], params["Key"]) == (
                source.bucket,
                source.object_key,
            )
    assert world.events[-2:] == [("source", 3, True), ("permit_exit",)]
    service.storage_access.acquire.assert_awaited_once_with(
        world.db,
        source.location_id,
        "training_dataset_original",
        expected_revision=9,
    )


@pytest.mark.parametrize("orientation", range(1, 9))
async def test_real_exif_uses_logical_dimensions_without_reencoding(world, orientation):
    expected = (3, 4) if orientation in {5, 6, 7, 8} else (4, 3)
    world.load(
        encoded("JPEG", orientation=orientation),
        expected_width=expected[0],
        expected_height=expected[1],
    )
    media, _source = await preflight(world)
    assert (media["width"], media["height"]) == expected
    assert media["sha256"] == hashlib.sha256(world.raw).hexdigest()
    assert world.stages[0].file.closed and service._slots._value == 2


@pytest.mark.parametrize("orientation", [0, 9])
async def test_invalid_encoded_exif_never_becomes_a_successful_preflight(
    world, orientation
):
    world.load(encoded("JPEG", orientation=orientation))
    with pytest.raises(TrainingDatasetError, match="图片方向信息无效"):
        await preflight(world)
    assert world.stages[0].file.closed and not world.permits[0].active


@pytest.mark.parametrize("field", ["expected_width", "expected_height"])
async def test_logical_dimension_mismatch_is_rejected_after_actual_decode(world, field):
    world.sample[field] += 1
    with pytest.raises(TrainingDatasetError, match="图片尺寸"):
        await preflight(world)
    assert world.stages[0].file.closed and world.resolves == 3


@pytest.mark.filterwarnings("ignore:Image size.*:PIL.Image.DecompressionBombWarning")
@pytest.mark.parametrize(
    "size,accepted", [((10000, 10000), True), ((10001, 10000), False)]
)
async def test_real_encoded_one_hundred_megapixel_boundary(world, size, accepted):
    world.load(
        encoded(size=size, mode="L"), expected_width=size[0], expected_height=size[1]
    )
    if accepted:
        media, _source = await preflight(world)
        assert (media["width"], media["height"]) == size
    else:
        with pytest.raises(TrainingDatasetError, match="图片格式或像素数量"):
            await preflight(world)
    assert all(file.closed for file in world.files) and service._slots._value == 2


@pytest.mark.parametrize("invalid", ["gif", "bytes", "sha", "size"])
async def test_bad_or_unsupported_physical_input_is_closed_without_success(
    world, invalid
):
    if invalid == "gif":
        world.load(encoded("GIF"))
    elif invalid == "bytes":
        world.load(b"not an encoded image")
    elif invalid == "sha":
        world.actual_override = b"x" * len(world.raw)
    else:
        world.provider.head_changes = {"ContentLength": len(world.raw) + 1}
    with pytest.raises((TrainingDatasetError, ContentError, OSError)):
        await preflight(world)
    assert all(file.closed for file in world.files) and service._slots._value == 2
    assert not world.permits[0].active


@pytest.mark.parametrize("invalid", ["legacy", "owner", "relation", "oversize"])
async def test_unproven_or_changed_frozen_source_does_no_raw_io(world, invalid):
    if invalid == "legacy":
        world.sample.pop("original")
    elif invalid == "owner":
        world.current = replace(world.source, content_id=uuid.uuid4())
    elif invalid == "relation":
        world.current = replace(
            world.source,
            relation=replace(world.source.relation, project_id=uuid.uuid4()),
        )
    else:
        world.current = replace(world.source, size_bytes=service.MAX_BYTES + 1)
    with pytest.raises(ContentError):
        await preflight(world)
    assert not world.provider.calls and not world.files and not world.permits
    assert service._slots._value == 2


@pytest.mark.parametrize("phase", ["before_io", "after_decode", "after_close"])
@pytest.mark.parametrize("fact", ["owner", "bucket", "gate"])
async def test_exact_source_and_gate_are_rechecked_after_actual_work(
    world, monkeypatch, phase, fact
):
    def mutate():
        field, value = {
            "owner": ("content_id", uuid.uuid4()),
            "bucket": ("bucket", "current-bucket-trap"),
            "gate": ("maintenance_revision", 10),
        }[fact]
        world.current = replace(world.source, **{field: value})

    if phase == "before_io":

        def resolved():
            if world.resolves == 1:
                mutate()

        world.resolve_hook = resolved
    elif phase == "after_decode":
        inspect = service.inspect_image

        def decoded(*args):
            result = inspect(*args)
            mutate()
            return result

        monkeypatch.setattr(service, "inspect_image", decoded)
    else:
        close = service.close_stage

        async def closed(stage):
            await close(stage)
            mutate()

        monkeypatch.setattr(service, "close_stage", closed)
    with pytest.raises(ContentError, match="content_binding_changed"):
        await preflight(world)
    assert service._slots._value == 2 and all(file.closed for file in world.files)
    assert not world.permits[0].active
    if phase == "before_io":
        assert not world.provider.calls and not world.files


async def test_late_authority_failure_is_not_hidden_by_invalid_image(world):
    world.load(b"invalid image")

    async def check():
        if world.checks == 3:
            raise HTTPException(403, {"code": "content_authority_revoked"})

    world.check_hook = check
    with pytest.raises(HTTPException) as caught:
        await preflight(world)
    assert caught.value.status_code == 403 and world.files[0].closed


async def test_attempt_guard_is_rechecked_only_after_physical_stage_close(world):
    observations = []

    async def guard():
        observations.append(
            bool(world.files) and all(file.closed for file in world.files)
        )
        if observations[-1]:
            raise ContentError("training_attempt_replaced")

    with pytest.raises(ContentError, match="training_attempt_replaced"):
        async with service.media(
            world.db, world.authority, world.sample, guard=guard
        ) as (stage, _):
            await service.physical(service.inspect_image, stage, world.sample)
    assert observations == [False, False, True]
    assert world.stages[0].file.closed and service._slots._value == 2
    assert not world.permits[0].active


async def test_two_slot_capacity_is_owned_until_each_actual_stage_closes(world):
    async with service.media(world.db, world.authority, world.sample):
        assert service._slots._value == 1
        async with service.media(world.db, world.authority, world.sample):
            assert service._slots._value == 0
            calls = len(world.provider.calls)
            with pytest.raises(ContentError, match="snapshot_capacity_exceeded"):
                await preflight(world)
            assert len(world.provider.calls) == calls and service._slots._value == 0
        assert service._slots._value == 1
    assert service._slots._value == 2 and all(file.closed for file in world.files)


@pytest.mark.parametrize("phase", ["read", "body_close", "client_close"])
@pytest.mark.parametrize("finish", ["cancel", "timeout"])
async def test_physical_capture_interruption_drains_real_provider_before_return(
    world, phase, finish
):
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        try:
            assert release.wait(5), "test did not release provider"
        finally:
            ended.set()

    setattr(world.provider, phase + "_hook", blocked)
    if finish == "timeout":
        world.capture_seconds = 0.025
    pending = asyncio.create_task(preflight(world))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish == "cancel":
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0.06)
        assert not pending.done() and not ended.is_set()
        assert not world.files[0].closed and service._slots._value == 1
        assert world.permits[0].exits == []
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError if finish == "cancel" else ContentError):
        await pending
    assert ended.is_set() and all(file.closed for file in world.files)
    assert service._slots._value == 2 and not world.permits[0].active
    assert world.provider.closed == 1 and all(
        body.closed for body in world.provider.bodies
    )


@pytest.mark.parametrize(
    "finish", ["cancel", "timeout", "late_unknown_cancel", "late_unknown_timeout"]
)
async def test_native_copy_drains_before_stage_directory_and_permit_can_end(
    world, tmp_path, finish
):
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    targets, paths = [], []

    def copy(stage, path):
        with path.open("wb") as target:
            targets.append(target)
            entered.set()
            try:
                assert release.wait(5), "test did not release actual copy"
                stage.file.seek(0)
                target.write(stage.file.read())
                target.flush()
                if finish.startswith("late_unknown"):
                    raise storage_access.mark_unsettled(
                        OSError("unknown copy resource close")
                    )
            finally:
                ended.set()

    async def operation():
        with service.temporary_directory(
            dir=tmp_path, prefix="training-copy-"
        ) as directory:
            path = Path(directory) / "image"
            paths.append(path)
            async with service.media(world.db, world.authority, world.sample) as (
                stage,
                _,
            ):
                await service.physical(
                    copy,
                    stage,
                    path,
                    timeout=0.025 if finish.endswith("timeout") else None,
                )

    pending = asyncio.create_task(operation())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish.endswith("cancel"):
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0.06)
        assert not pending.done() and not ended.is_set()
        assert paths[0].exists() and not targets[0].closed
        assert not world.stages[0].file.closed and service._slots._value == 1
        assert world.permits[0].exits == []
    finally:
        release.set()
    with pytest.raises(
        asyncio.CancelledError if finish.endswith("cancel") else TimeoutError
    ) as caught:
        await pending
    assert ended.is_set() and targets[0].closed and not paths[0].parent.exists()
    assert world.stages[0].file.closed
    assert service._slots._value == (1 if finish.startswith("late_unknown") else 2)
    if finish.startswith("late_unknown"):
        assert caught.value.storage_operation_unsettled
        assert (
            world.permits[0].active
            and world.permits[0].exits[0].storage_operation_unsettled
        )
    else:
        assert not world.permits[0].active


async def test_physical_work_preserves_context_and_returns_a_real_file_result(tmp_path):
    context = ContextVar("training_original_test")
    token = context.set("captured training input")
    path = tmp_path / "native-result"

    def write(*, destination):
        destination.write_text(context.get())
        return threading.get_ident(), destination.read_text()

    try:
        thread, result = await service.physical(write, destination=path)
        assert thread != threading.get_ident() and result == "captured training input"
    finally:
        context.reset(token)


@pytest.mark.parametrize(
    "finish", ["cancel", "timeout", "late_unknown_cancel", "late_unknown_timeout"]
)
async def test_interrupted_real_decode_keeps_stage_owned_until_native_exit(
    world, monkeypatch, finish
):
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    inspect = service.inspect_image
    physical = service.physical

    def blocked(*args):
        entered.set()
        try:
            assert release.wait(5), "test did not release image decoder"
            result = inspect(*args)
            if finish.startswith("late_unknown"):
                raise storage_access.mark_unsettled(OSError("late decoder uncertainty"))
            return result
        finally:
            ended.set()

    async def bounded(*args, **kwargs):
        if finish.endswith("timeout"):
            kwargs["timeout"] = 0.025
        return await physical(*args, **kwargs)

    monkeypatch.setattr(service, "inspect_image", blocked)
    monkeypatch.setattr(service, "physical", bounded)
    pending = asyncio.create_task(preflight(world))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish.endswith("cancel"):
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0.06)
        assert not pending.done() and not ended.is_set()
        assert not world.stages[0].file.closed and service._slots._value == 1
        assert world.permits[0].exits == []
    finally:
        release.set()
    with pytest.raises(
        asyncio.CancelledError if finish.endswith("cancel") else TimeoutError
    ) as caught:
        await pending
    assert ended.is_set() and world.stages[0].file.closed
    assert service._slots._value == (1 if finish.startswith("late_unknown") else 2)
    if finish.startswith("late_unknown"):
        assert caught.value.storage_operation_unsettled
        assert (
            world.permits[0].active
            and world.permits[0].exits[0].storage_operation_unsettled
        )
    else:
        assert not world.permits[0].active


@pytest.mark.parametrize(
    "phase", ["body", "client", "image", "missing_body", "constructor"]
)
async def test_unknown_close_never_becomes_success_or_releases_unsettled_responsibility(
    world, monkeypatch, phase
):
    if phase in {"body", "client"}:
        setattr(world.provider, phase + "_close_error", OSError("unknown remote close"))
    elif phase == "missing_body":
        world.provider.get_changes = {"Body": None}
    elif phase == "constructor":

        def unknown(**kwargs):
            raise OSError("unknown provider constructor")

        monkeypatch.setattr(service.reader, "create_client", unknown)
    else:
        opened = Image.open

        class UnknownImage:
            def __init__(self, image):
                self.image = image

            def __getattr__(self, name):
                return getattr(self.image, name)

            def close(self):
                raise OSError("image close not confirmed")

        def open_image(*args, **kwargs):
            image = opened(*args, **kwargs)
            world.images.append(image)
            return UnknownImage(image)

        monkeypatch.setattr(service.Image, "open", open_image)
    with pytest.raises(OSError if phase == "image" else ContentError) as caught:
        await preflight(world)
    assert caught.value.storage_operation_unsettled
    assert (
        world.permits[0].active
        and world.permits[0].exits[0].storage_operation_unsettled
    )
    assert all(file.closed for file in world.files)
    # Unknown decoder ownership quarantines full consumer capacity, even when
    # its borrowed capture stage was independently closed successfully.
    assert service._slots._value == 1
    if phase == "image":
        with pytest.raises(OSError) as second:
            await preflight(world)
        assert second.value.storage_operation_unsettled
        assert service._slots._value == 0 and all(file.closed for file in world.files)
        calls = len(world.provider.calls)
        with pytest.raises(ContentError, match="snapshot_capacity_exceeded"):
            await preflight(world)
        assert len(world.provider.calls) == calls and service._slots._value == 0
        assert all(permit.active for permit in world.permits[:2])
        assert not world.permits[2].active


async def test_unknown_real_directory_cleanup_retains_consumer_capacity(
    world, monkeypatch, tmp_path
):
    temporary = tempfile.TemporaryDirectory
    directories = []

    class UnknownCleanup:
        def __init__(self, **kwargs):
            self.actual = temporary(**kwargs)
            self.name = self.actual.name
            directories.append(Path(self.name))

        def cleanup(self):
            self.actual.cleanup()
            raise OSError("temporary directory cleanup was not confirmed")

    monkeypatch.setattr(service.tempfile, "TemporaryDirectory", UnknownCleanup)

    def copy(stage, path):
        stage.file.seek(0)
        with path.open("wb") as target:
            target.write(stage.file.read())
        assert path.read_bytes() == world.raw

    with pytest.raises(OSError) as caught:
        async with service.media(world.db, world.authority, world.sample) as (stage, _):
            with service.temporary_directory(dir=tmp_path) as directory:
                await service.physical(copy, stage, Path(directory) / "original")
    assert caught.value.storage_operation_unsettled
    assert not directories[0].exists() and world.stages[0].file.closed
    assert service._slots._value == 1
    assert world.permits[0].active
    assert world.permits[0].exits[0].storage_operation_unsettled


@pytest.mark.parametrize("phase", ["body", "client"])
@pytest.mark.parametrize("finish", ["cancel", "timeout"])
async def test_interrupted_capture_preserves_late_unknown_remote_close(
    world, phase, finish
):
    entered, release = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(5), "test did not release provider read"

    world.provider.read_hook = blocked
    setattr(world.provider, phase + "_close_error", OSError("late unknown close"))
    if finish == "timeout":
        world.capture_seconds = 0.025
    pending = asyncio.create_task(preflight(world))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish == "cancel":
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0.06)
        assert not pending.done() and service._slots._value == 1
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
    assert world.files[0].closed and service._slots._value == 1


@pytest.mark.parametrize(
    "finish", ["cancel", "unknown", "unknown_cancel", "prior_unknown_then_cancel"]
)
async def test_actual_stage_close_drains_and_preserves_prior_uncertainty(world, finish):
    entered, release = threading.Event(), threading.Event()

    class ClosingFile:
        def __init__(self, file):
            self.file = file

        def __getattr__(self, name):
            return getattr(self.file, name)

        def close(self):
            entered.set()
            assert release.wait(5), "test did not release local close"
            if finish.startswith("unknown"):
                raise OSError("unknown temporary file close")
            self.file.close()

    def wrap(stage):
        stage.file = ClosingFile(stage.file)

    world.stage_hook = wrap

    async def operation():
        async with service.media(world.db, world.authority, world.sample) as (stage, _):
            await service.physical(service.inspect_image, stage, world.sample)
            if finish == "prior_unknown_then_cancel":
                raise storage_access.mark_unsettled(OSError("prior image uncertainty"))

    pending = asyncio.create_task(operation())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish != "unknown":
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not world.files[0].closed
        assert service._slots._value == 1 and world.permits[0].exits == []
    finally:
        release.set()
    with pytest.raises(
        OSError if finish == "unknown" else asyncio.CancelledError
    ) as caught:
        await pending
    if finish.startswith("unknown"):
        assert not world.files[0].closed and service._slots._value == 1
    else:
        assert world.files[0].closed
        assert service._slots._value == (2 if finish == "cancel" else 1)
    if finish != "cancel":
        assert caught.value.storage_operation_unsettled
        assert (
            world.permits[0].active
            and world.permits[0].exits[0].storage_operation_unsettled
        )
    else:
        assert not world.permits[0].active
