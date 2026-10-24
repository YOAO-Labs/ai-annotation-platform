"""Whole-video input ownership on real SQL, with only provider transport controlled."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.db.models.prediction_input import PredictionInputLease as Lease
from app.db.models.storage_maintenance import StorageOperation
from app.services import prediction_inputs as owner
from app.services.content_snapshot import ContentError
from app.services.exporting import video_probe
from app.services.managed_original_reader import BinaryStage
from tests.test_export_media_bundle_models import _migration
from tests.test_prediction_input_owner import Storage, lease, snapshot
from tests.test_prediction_input_owner import world as image_world  # noqa: F401


class VideoStorage(Storage):
    def _head(self, bucket, key, version=None):
        return {
            **super()._head(bucket, key, version),
            "ContentType": "application/octet-stream",
        }

    def put_object(self, **params):
        assert params["ContentType"] == "application/octet-stream"
        assert not isinstance(params["Body"], (bytes, io.BytesIO))
        chunks = []
        while block := params["Body"].read(owner.CHUNK_BYTES):
            chunks.append(block)
        return super().put_object(**{**params, "Body": io.BytesIO(b"".join(chunks))})


@pytest.fixture
async def world(image_world, monkeypatch):  # noqa: F811
    engine = image_world.factory.kw["bind"]
    async with engine.begin() as connection:
        for name in (
            "0240_secondary_prediction_input_owner",
            "0241_video_prediction_input_owner",
        ):
            await connection.run_sync(
                lambda sync, name=name: _migration(sync, name).upgrade()
            )
    monkeypatch.setattr(owner, "make_storage", lambda: VideoStorage(image_world))
    return image_world


def probe():
    return {
        "probe_contract": 1,
        "video": {
            "width": 32,
            "height": 24,
            "fps": 2.0,
            "frame_count": 3,
            "duration_ms": 1500,
            "codec": "mpeg4",
        },
    }


def entry(size=64, *, state="planned"):
    return {
        "sha256": "a" * 64,
        "size_bytes": size,
        "media_type": "application/octet-stream",
        "state": state,
        "etag": None if state == "planned" else '"etag"',
        "version_id": None if state == "planned" else "known-version",
        "write_nonce": str(uuid.uuid4()),
        "container": "avi",
        "probe": probe(),
    }


def stage(raw):
    file = tempfile.TemporaryFile("w+b")
    file.write(raw)
    file.seek(0)
    return BinaryStage(file, hashlib.sha256(raw).hexdigest(), len(raw), {})


@pytest.fixture
def real_video(tmp_path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("Actual ffmpeg/ffprobe are required for this descriptor contract")
    path = tmp_path / "misleading-image.png"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=32x24:r=2",
            "-frames:v",
            "3",
            "-c:v",
            "mpeg4",
            "-f",
            "avi",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    current = stage(path.read_bytes())
    try:
        yield current, video_probe.inspect(current.file)
    finally:
        current.close()
        path.unlink(missing_ok=True)


async def create(world, kind="video_batch_predict", **kwargs):
    return await owner.PredictionInputs.create(
        world.factory,
        owner_id=uuid.uuid4(),
        owner_kind=kind,
        task_id=uuid.uuid4(),
        **kwargs,
    )


@pytest.mark.parametrize("kind", sorted(owner.VIDEO_KINDS | owner.IMAGE_KINDS))
async def test_owner_kind_selects_exact_contract_and_original_deadlines(world, kind):
    inputs = await create(world, kind)
    assert inputs.record.contract_version == (3 if kind in owner.VIDEO_KINDS else 2)
    assert inputs.record.expires_at == inputs.record.created_at + timedelta(hours=2)
    assert inputs.record.retain_until == inputs.record.expires_at + timedelta(days=7)
    await inputs.close()
    assert (await lease(world, inputs.record.id)).state == "deleted"


async def test_real_probe_same_descriptor_fixed_version_and_cleanup_after_source_close(
    world, real_video
):
    source, receipt = real_video
    source.file.seek(7)
    inputs = await create(world, source_ref={"binding": "frozen", "probe": receipt})
    url = await inputs.upload_video(source, probe=receipt)
    assert source.file.tell() == 7 and not source.file.closed
    saved = inputs.video_receipt
    assert saved["relative"] == f"video/{source.sha256}.avi"
    assert saved["container"] == "avi" and saved["probe"] == receipt
    assert saved["state"] == "verified" and saved["version_id"] not in {None, "null"}
    assert world.signed_versions == [saved["version_id"]]
    assert saved["relative"] in url and ".png" not in url
    source.close()
    await inputs.check_live()
    async with world.factory() as db:
        await owner.check_metadata(db, inputs)
    detached = inputs.video_receipt
    detached["probe"]["video"]["width"] = 999
    assert inputs.video_receipt == saved
    before = deepcopy((await lease(world, inputs.record.id)).inventory)
    await inputs.close()
    row = await lease(world, inputs.record.id)
    assert row.state == "deleted" and row.inventory == before
    assert not world.objects and not world.versions[next(iter(world.versions))]
    assert all(body.closed for body in world.bodies)
    assert all(client.closed for client in world.clients)


@pytest.mark.parametrize(
    "container,header",
    [
        ("mov", b"\0\0\0\x18ftypisom\0\0\0\0"),
        ("matroska", b"\x1aE\xdf\xa3"),
        ("avi", b"RIFF\0\0\0\0AVI "),
        ("asf", bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")),
        ("flv", b"FLV\x01"),
    ],
)
async def test_container_is_descriptor_magic_not_task_filename(
    world, container, header
):
    source = stage(header + b"controlled transport boundary")
    inputs = await create(world)
    try:
        await inputs.upload_video(source, probe=probe())
        assert inputs.video_receipt["container"] == container
        assert inputs.video_receipt["relative"].endswith(
            "." + owner._VIDEO_EXTENSIONS[container]
        )
    finally:
        await inputs.close()
        source.close()


@pytest.mark.parametrize(
    "change",
    [
        "wrong_type",
        "unsupported_magic",
        "size",
        "sha",
        "closed",
        "frame_count",
        "probe_extra",
        "probe_boolean",
    ],
)
async def test_invalid_stage_or_probe_never_plans_or_sends_object(world, change):
    inputs = await create(world)
    source = stage(b"RIFF\0\0\0\0AVI payload")
    value, given = source, probe()
    if change == "wrong_type":
        value = SimpleNamespace(**vars(source))
    elif change == "unsupported_magic":
        source.file.seek(0)
        source.file.write(b"not a container")
    elif change == "size":
        source.size += 1
    elif change == "sha":
        source.sha256 = "wrong"
    elif change == "closed":
        source.close()
    elif change == "frame_count":
        given["video"]["frame_count"] = 1_000_001
    elif change == "probe_extra":
        given["container"] = "avi"
    else:
        given["probe_contract"] = True
    try:
        with pytest.raises(ContentError):
            await inputs.upload_video(value, probe=given)
        assert (
            not world.puts and (await lease(world, inputs.record.id)).object_count == 0
        )
    finally:
        await inputs.close()
        source.close()


async def test_probe_and_single_object_are_immutable_without_second_put(
    world, real_video
):
    source, given = real_video
    inputs = await create(world)
    try:
        await inputs.upload_video(source, probe=given)
        await inputs.upload_video(source, probe=given)
        assert len(world.puts) == 1
        changed = deepcopy(given)
        changed["video"]["width"] += 1
        with pytest.raises(ContentError):
            await inputs.upload_video(source, probe=changed)
        other = stage(b"RIFF\0\0\0\0AVI another source")
        try:
            with pytest.raises(ContentError, match="budget_exceeded"):
                await inputs.upload_video(other, probe=probe())
        finally:
            other.close()
        assert len(world.puts) == 1
    finally:
        await inputs.close()


async def test_image_video_methods_do_not_cross_contracts(world, real_video):
    source, given = real_video
    video, image = await create(world), await create(world, "predict_test")
    pixels = snapshot()
    try:
        with pytest.raises(ContentError, match="video_contract_required"):
            await image.upload_video(source, probe=given)
        with pytest.raises(ContentError, match="image_contract_required"):
            await video.upload_snapshot(pixels)
        with pytest.raises(ContentError, match="image_contract_required"):
            await video.adopt_snapshot(pixels)
        with pytest.raises(ContentError, match="image_contract_required"):
            await video.upload_crop(
                SimpleNamespace(id=video.record.task_id), 0, b"crop"
            )
        with pytest.raises(ContentError, match="image_contract_required"):
            video.load_image(SimpleNamespace(id=video.record.task_id))
        assert not world.puts
    finally:
        await video.close()
        await image.close()
        pixels.close()


def test_video_cap_and_inventory_shape_never_enlarge_image_limits():
    key = "video/" + "a" * 64 + ".avi"
    assert (
        owner._entry(key, entry(owner.MAX_VIDEO_BYTES), contract_version=3)[
            "size_bytes"
        ]
        == 5 * 1024**3
    )
    with pytest.raises(ContentError):
        owner._entry(key, entry(owner.MAX_VIDEO_BYTES + 1), contract_version=3)
    with pytest.raises(ContentError):
        owner._entry(key, entry(), contract_version=2)
    image = {
        key: value
        for key, value in entry(owner.MAX_BYTES + 1).items()
        if key not in {"container", "probe"}
    }
    image["media_type"] = "image/png"
    with pytest.raises(ContentError):
        owner._entry("original/" + "a" * 64 + ".png", image)
    for field, value in [
        ("version_id", None),
        ("container", "mp4"),
        ("media_type", "video/mp4"),
    ]:
        invalid = entry(state="verified")
        invalid[field] = value
        with pytest.raises(ContentError):
            owner._entry(key, invalid, contract_version=3)


@pytest.mark.parametrize(
    "failure", ["disabled_versioning", "bad_readback", "unknown_close"]
)
async def test_video_storage_failure_retains_exact_responsibility(
    world, real_video, failure
):
    source, given = real_video
    inputs = await create(world)
    if failure == "disabled_versioning":
        world.versioning = "Suspended"
    elif failure == "bad_readback":
        world.corrupt_read = True
    else:
        world.fail_close = True
    with pytest.raises(ContentError):
        await inputs.upload_video(source, probe=given)
    if failure == "unknown_close":
        with pytest.raises(BaseException) as caught:
            await inputs.close()
        assert getattr(caught.value, "storage_operation_unsettled", False)
        async with world.factory() as db:
            assert await db.scalar(
                select(StorageOperation.id).where(
                    StorageOperation.purpose == owner.purpose(inputs.record)
                )
            )
    else:
        await inputs.close()
    row = await lease(world, inputs.record.id)
    assert row.inventory["objects"] and row.object_count == 1
    if failure != "disabled_versioning":
        assert row.state == "cleanup_pending" and world.objects


async def test_repeated_cancellation_waits_for_real_video_put_and_preserves_borrowed_stage(
    world, real_video
):
    source, given = real_video
    inputs = await create(world)
    world.started, world.release = threading.Event(), threading.Event()
    pending = asyncio.create_task(inputs.upload_video(source, probe=given))
    try:
        assert await asyncio.to_thread(world.started.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not source.file.closed
    finally:
        world.release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert all(client.closed for client in world.clients)
    await inputs.close()
    assert not source.file.closed
    assert (await lease(world, inputs.record.id)).state == "deleted"


@pytest.mark.parametrize(
    "change", ["kind", "size", "count", "probe", "nonce", "null_version"]
)
async def test_database_guards_video_budget_identity_and_receipts(world, change):
    inputs = await create(world)
    key, value = "video/" + "a" * 64 + ".avi", entry(owner.MAX_VIDEO_BYTES)
    async with world.factory() as db:
        await db.execute(
            update(Lease)
            .where(Lease.id == inputs.record.id)
            .values(
                inventory={"schema": 1, "objects": {key: value}},
                object_count=1,
                size_bytes=value["size_bytes"],
            )
        )
        await db.commit()
    async with world.factory() as db:
        row = await db.get(Lease, inputs.record.id)
        if change == "kind":
            row.owner_kind = "predict_test"
        elif change == "size":
            bad = deepcopy(value)
            bad["size_bytes"] += 1
            row.inventory = {"schema": 1, "objects": {key: bad}}
            row.size_bytes += 1
        elif change == "count":
            second = deepcopy(value)
            second["sha256"] = "b" * 64
            second["size_bytes"] = 1
            row.inventory = {
                "schema": 1,
                "objects": {key: value, "video/" + "b" * 64 + ".avi": second},
            }
            row.object_count = 2
            row.size_bytes += 1
        else:
            bad = deepcopy(value)
            if change == "probe":
                bad["probe"]["video"]["width"] = 999
            elif change == "nonce":
                bad["write_nonce"] = str(uuid.uuid4())
            else:
                bad.update(state="verified", etag='"known"', version_id=None)
            row.inventory = {"schema": 1, "objects": {key: bad}}
        with pytest.raises(DBAPIError):
            await db.commit()
        await db.rollback()
    await inputs.close()


async def test_v3_deleted_tombstone_blocks_downgrade_and_clean_graph_roundtrips(world):
    engine = world.factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: _migration(
                sync, "0241_video_prediction_input_owner"
            ).downgrade()
        )
        await connection.run_sync(
            lambda sync: _migration(sync, "0241_video_prediction_input_owner").upgrade()
        )
    inputs = await create(world)
    await inputs.close()
    assert (await lease(world, inputs.record.id)).state == "deleted"
    async with engine.connect() as connection:
        with pytest.raises(DBAPIError):
            await connection.run_sync(
                lambda sync: _migration(
                    sync, "0241_video_prediction_input_owner"
                ).downgrade()
            )
        await connection.rollback()
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM prediction_input_leases WHERE contract_version=3 AND state='deleted'"
                )
            )
            == 1
        )


async def test_video_upload_hash_and_transport_never_request_an_unbounded_source_read(
    world, real_video
):
    source, given = real_video
    borrowed = source.file
    reads = []

    class BoundedFile:
        def __getattr__(self, name):
            return getattr(borrowed, name)

        def read(self, size=-1):
            assert 0 < size <= owner.CHUNK_BYTES
            reads.append(size)
            return borrowed.read(size)

    source.file = BoundedFile()
    inputs = await create(world)
    try:
        await inputs.upload_video(source, probe=given)
        assert reads and not borrowed.closed
    finally:
        await inputs.close()
        source.file = borrowed
