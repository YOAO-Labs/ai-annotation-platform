"""Canonical video proof and physical lifetime, without business authority fakes."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from billiard.exceptions import SoftTimeLimitExceeded
import pytest

from app.services import original_sources, storage_access, task_originals
from app.services import video_prediction_originals as videos
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from tests.test_managed_original_reader import Provider

RAW = b"\x00\x00\x00\x18ftypisomverified-video"
ENDPOINT = "http://originals.test:9000"
PROBE = {
    "probe_contract": 1,
    "video": {
        "duration_ms": 1200,
        "fps": 2.5,
        "frame_count": 3,
        "width": 96,
        "height": 64,
        "codec": "h264",
    },
}


def source(origin="direct"):
    task_id, project_id, item_id, content_id, link_id = (uuid.uuid4() for _ in range(5))
    sha = hashlib.sha256(RAW).hexdigest()
    task_original = origin == "task"
    key = (
        task_originals.key_for(task_id, content_id)
        if task_original
        else "saved/video.bin"
    )
    links = (
        (original_sources.LinkFact(link_id, task_id, item_id, "primary_lidar", None),)
        if origin == "primary"
        else ()
    )
    relation = original_sources.TaskRelation(
        task_id,
        project_id,
        None,
        key,
        "video",
        content_id if task_original else None,
        item_id if origin == "direct" else None,
        item_id if links else None,
        link_id if links else None,
        None if task_original else item_id,
        links,
    )
    location = original_sources.LocationFacts(
        LEGACY_LOCATION_ID,
        "s3",
        True,
        1,
        "originals.test:9000",
        False,
        None,
    )
    asset = original_sources.AssetFacts(content_id, item_id, "video", sha, len(RAW))
    # Deliberately incorrect metadata dimensions are identity facts, never the
    # trusted dimensions handed to video inference.
    item = original_sources.ItemFacts(item_id, uuid.uuid4(), key, "video", 1, 2)
    replica = original_sources.ReplicaFacts(
        content_id,
        LEGACY_LOCATION_ID,
        "saved-annotations",
        key,
        "original",
        "present",
        None,
        None,
        None,
    )
    original = task_originals._Snapshot(
        content_id,
        content_id,
        task_id,
        project_id,
        LEGACY_LOCATION_ID,
        managed_storage_identity(ENDPOINT, "saved-annotations"),
        "saved-annotations",
        key,
        sha,
        len(RAW),
        '"creation-etag"',
        None,
        datetime.now(timezone.utc),
    )
    return original_sources.OriginalSource(
        "task_original" if task_original else "dataset_asset",
        content_id,
        sha,
        len(RAW),
        "video",
        None if task_original else item_id,
        relation,
        "saved-annotations",
        key,
        ENDPOINT,
        LEGACY_LOCATION_ID,
        1,
        7,
        original if task_original else asset,
        None if task_original else item,
        None if task_original else replica,
        location,
    )


@pytest.mark.parametrize("origin", ["direct", "primary", "task"])
def test_source_receipt_preserves_canonical_owner_relation_and_saved_locator(origin):
    original = source(origin)
    receipt = videos.source_receipt(original)
    assert receipt["schema"] == 3 and receipt["media_type"] == "video"
    assert receipt["bucket"] == "saved-annotations"
    assert receipt["facts"]["relation"]["task_id"] == str(original.relation.task_id)
    assert "version" not in receipt["facts"]["relation"]
    restored = videos.normalize_binding(receipt)
    restored["facts"]["relation"]["file_path"] = "changed"
    assert receipt["facts"]["relation"]["file_path"] == original.object_key


@pytest.mark.parametrize(
    "field,value",
    [
        ("size_bytes", 0),
        ("size_bytes", True),
        ("size_bytes", videos.MAX_BYTES + 1),
        ("media_type", "image"),
        ("owner_kind", "dataset_auxiliary"),
    ],
)
def test_source_budget_and_media_type_are_checked_before_physical_work(field, value):
    with pytest.raises(ContentError, match="prediction_video_original_unsupported"):
        videos.source_receipt(replace(source(), **{field: value}))


@pytest.mark.parametrize(
    "change",
    [
        "schema",
        "bool",
        "uuid",
        "uuid_object",
        "digest",
        "extra",
        "relation",
        "nested_bool",
    ],
)
def test_normalization_does_not_upgrade_or_repair_untrusted_proof(change):
    value = videos.source_receipt(source())
    if change == "schema":
        value["schema"] = 1
    elif change == "bool":
        value["location_revision"] = True
    elif change == "uuid":
        value["content_id"] = value["content_id"].replace("-", "")
    elif change == "uuid_object":
        value["content_id"] = uuid.UUID(value["content_id"])
    elif change == "digest":
        value["facts_digest"] = "0" * 64
    elif change == "extra":
        value["url"] = "private"
    elif change == "relation":
        value["facts"]["relation"]["task_id"] = "bad"
        value["facts_digest"] = videos.common.digest(value["facts"])
    else:
        value["facts"]["item_facts"]["width"] = True
        value["facts_digest"] = videos.common.digest(value["facts"])
    with pytest.raises(ContentError, match="prediction_video_original_invalid"):
        videos.normalize_binding(value)


def test_proof_budget_and_actual_probe_types_remain_bounded():
    value = videos.source_receipt(source())
    value["facts"]["large"] = "x" * videos.MAX_PROOF_BYTES
    with pytest.raises(ContentError, match="prediction_proof_budget_exceeded"):
        videos.normalize_binding(value)
    for name, member in (
        ("frame_count", True),
        ("frame_count", 1_000_001),
        ("fps", False),
    ):
        probe = videos.common.bounded_proof(PROBE)
        probe["video"][name] = member
        with pytest.raises(ContentError, match="prediction_video_probe_invalid"):
            videos.normalize_probe(probe)


@pytest.mark.asyncio
async def test_resolver_is_bounded_and_task_version_remains_with_business_owner(
    monkeypatch,
):
    original = source("primary")
    constructors = []

    class Resolver:
        def __init__(self, db, **kwargs):
            constructors.append(kwargs)

        async def task(self, task_id):
            assert task_id == original.relation.task_id
            return original

    monkeypatch.setattr(original_sources, "CachedResolver", Resolver)
    task = SimpleNamespace(id=original.relation.task_id, version=1)
    first = await videos.managed_binding(object(), task, lock=True)
    task.version += 1
    assert await videos.managed_binding(object(), task, lock=True) == first
    assert constructors == [{"lock": True, "max_links": videos.MAX_LINKS}] * 2


@pytest.fixture
def world(monkeypatch):
    original, sessions = source(), []
    permit = SimpleNamespace(__aexit__=AsyncMock())

    class Session:
        def __init__(self, **kwargs):
            sessions.append(self)

        async def close(self):
            sessions.remove(self)

    monkeypatch.setattr(videos, "AsyncSession", Session)
    monkeypatch.setattr(storage_access, "engine", lambda db: object())
    monkeypatch.setattr(storage_access, "acquire", AsyncMock(return_value=permit))
    monkeypatch.setattr(videos, "resolve", AsyncMock(return_value=original))
    monkeypatch.setattr(videos, "_slots", threading.BoundedSemaphore(1))
    return SimpleNamespace(
        source=original, sessions=sessions, permit=permit, guard=AsyncMock()
    )


async def open_source(world):
    return await videos.open_snapshot(
        object(),
        world.source.relation.task_id,
        videos.source_receipt(world.source),
        guard=world.guard,
        purpose="prediction_input_video",
        node_id="worker:video@test",
    )


def stage(raw=RAW, *, release=lambda: None, file=None):
    if file is None:
        file = tempfile.TemporaryFile("w+b")
        file.write(raw)
        file.seek(0)
    return videos.reader.BinaryStage(
        file,
        hashlib.sha256(raw).hexdigest(),
        len(raw),
        {},
        _release=release,
        _retain_on_close_error=True,
    )


@pytest.mark.asyncio
async def test_full_read_uses_saved_bucket_conditionals_and_same_fd_probe(
    world, monkeypatch
):
    provider = Provider()
    provider.raw = provider.actual = RAW
    provider.version = "observed-source-version"
    monkeypatch.setattr(videos.reader, "create_client", lambda **kwargs: provider)
    inspected = []

    def inspect(file):
        assert not world.sessions
        assert not file.closed and file.read() == RAW
        file.seek(0)
        inspected.append(file)
        return PROBE

    monkeypatch.setattr(videos.video_probe, "inspect", inspect)
    owned = await open_source(world)
    try:
        assert owned.stage.file is inspected[0] and owned.snapshot is owned.stage
        assert owned.probe == PROBE and not owned.closed and not owned.settled
        detached = owned.probe
        detached["video"]["width"] = 1
        assert owned.probe["video"]["width"] == 96
        calls = [params for name, params in provider.calls if name in {"head", "get"}]
        assert all(params["Bucket"] == "saved-annotations" for params in calls)
        assert all(params["Key"] == world.source.object_key for params in calls)
        assert all(
            params["VersionId"] == provider.version
            for name, params in provider.calls
            if name == "get"
        )
        assert provider.closed == 1 and all(body.closed for body in provider.bodies)
        assert world.guard.await_count == 4 and not world.sessions
        world.permit.__aexit__.assert_not_awaited()
        assert not videos._slots.acquire(False)
    finally:
        await owned.aclose()
    assert owned.closed and owned.settled and not owned.unsettled
    assert owned.stage.file.closed
    world.permit.__aexit__.assert_awaited_once()
    await owned.aclose()
    world.permit.__aexit__.assert_awaited_once()
    assert videos._slots.acquire(False)
    videos._slots.release()


@pytest.mark.asyncio
async def test_full_sha_mismatch_never_reaches_probe_or_transfers_stage(
    world, monkeypatch
):
    provider = Provider()
    provider.raw = RAW
    provider.actual = RAW[:-1] + b"X"
    monkeypatch.setattr(videos.reader, "create_client", lambda **kwargs: provider)
    inspect = AsyncMock()
    monkeypatch.setattr(videos.video_probe, "inspect", inspect)
    with pytest.raises(ContentError, match="content_mismatch"):
        await open_source(world)
    assert provider.closed == 1 and all(body.closed for body in provider.bodies)
    inspect.assert_not_called()
    world.permit.__aexit__.assert_awaited_once()
    assert videos._slots.acquire(False)
    videos._slots.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "relation", "guard"])
async def test_post_read_freshness_failure_closes_source_without_probe(
    world, monkeypatch, change
):
    captured = []

    def capture(*args, **kwargs):
        assert not world.sessions
        result = stage(release=args[6])
        captured.append(result)
        return result

    monkeypatch.setattr(videos.reader, "_capture", capture)
    inspect = AsyncMock(side_effect=AssertionError("probe must not start"))
    monkeypatch.setattr(videos.video_probe, "inspect", inspect)
    changed = world.source
    if change == "owner":
        new_id = uuid.uuid4()
        changed = replace(
            changed,
            content_id=new_id,
            ownership_facts=replace(changed.ownership_facts, id=new_id),
            replica_facts=replace(changed.replica_facts, asset_id=new_id),
        )
    elif change == "relation":
        changed = replace(
            changed, relation=replace(changed.relation, batch_id=uuid.uuid4())
        )
    if change == "guard":
        world.guard.side_effect = [None, None, ContentError("prediction_scope_changed")]
    else:
        videos.resolve.side_effect = [world.source, world.source, changed]
    with pytest.raises(
        ContentError, match="prediction_scope_changed|content_binding_changed"
    ):
        await open_source(world)
    assert captured[0].file.closed and not world.sessions
    world.permit.__aexit__.assert_awaited_once()
    inspect.assert_not_called()


async def wait_started(event):
    for _ in range(1000):
        if event.is_set():
            return
        await asyncio.sleep(0.001)
    pytest.fail("physical operation did not start")


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["capture", "probe"])
async def test_repeated_cancellation_drains_late_physical_work_before_release(
    world, monkeypatch, boundary
):
    started, release = threading.Event(), threading.Event()
    captured = []

    def capture(*args, **kwargs):
        if boundary == "capture":
            started.set()
            assert release.wait(3)
        result = stage(release=args[6])
        captured.append(result)
        return result

    def inspect(file):
        started.set()
        assert release.wait(3)
        assert not file.closed
        return PROBE

    monkeypatch.setattr(videos.reader, "_capture", capture)
    monkeypatch.setattr(videos.video_probe, "inspect", inspect)
    pending = asyncio.create_task(open_source(world))
    try:
        await wait_started(started)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        assert not videos._slots.acquire(False)
        world.permit.__aexit__.assert_not_awaited()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert captured[0].file.closed
        world.permit.__aexit__.assert_awaited_once()
        assert not world.sessions
    finally:
        release.set()
        if not pending.done():
            try:
                await videos.common.drain(pending)
            except BaseException:
                pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_type", [SoftTimeLimitExceeded, KeyboardInterrupt, RuntimeError]
)
@pytest.mark.parametrize("boundary", ["body", "client", "constructor"])
async def test_unknown_remote_handle_retains_permit_and_slot(
    world, monkeypatch, failure_type, boundary
):
    provider = Provider()
    provider.raw = provider.actual = RAW
    failure = failure_type("physical end unconfirmed")
    if boundary == "constructor":

        def constructor(**kwargs):
            raise failure
    else:
        setattr(provider, boundary + "_close_error", failure)

        def constructor(**kwargs):
            return provider

    monkeypatch.setattr(videos.reader, "create_client", constructor)
    with pytest.raises(BaseException) as caught:
        await open_source(world)
    assert caught.value.storage_operation_unsettled
    world.permit.__aexit__.assert_not_awaited()
    assert not world.sessions and not videos._slots.acquire(False)


@pytest.mark.asyncio
async def test_unknown_probe_process_preserves_descriptor_and_source_admission(
    world, monkeypatch
):
    captured = []

    def capture(*args, **kwargs):
        result = stage(release=args[6])
        captured.append(result)
        return result

    def inspect(file):
        raise storage_access.mark_unsettled(
            SoftTimeLimitExceeded("probe close unknown")
        )

    monkeypatch.setattr(videos.reader, "_capture", capture)
    monkeypatch.setattr(videos.video_probe, "inspect", inspect)
    try:
        with pytest.raises(SoftTimeLimitExceeded) as caught:
            await open_source(world)
        assert caught.value.storage_operation_unsettled
        assert not captured[0].file.closed and not captured[0]._closed
        world.permit.__aexit__.assert_not_awaited()
        assert not videos._slots.acquire(False)
    finally:
        # This fake raises no real child; release its local test descriptor only.
        if captured:
            captured[0].file.close()


@pytest.mark.asyncio
async def test_unknown_close_stays_unknown_even_after_binary_stage_no_op(monkeypatch):
    permit = SimpleNamespace(__aexit__=AsyncMock())
    monkeypatch.setattr(videos, "_slots", threading.BoundedSemaphore(1))
    admission = videos._Admission()
    calls = []

    class UnknownFile:
        def close(self):
            calls.append(True)
            raise OSError("unknown physical close")

    binary = stage(file=UnknownFile(), release=admission.release)
    owned = videos.SourceInput(binary, PROBE, permit, admission)
    with pytest.raises(OSError) as caught:
        await owned.aclose()
    assert caught.value.storage_operation_unsettled and binary._closed
    with pytest.raises(ContentError, match="storage_operation_unsettled"):
        await owned.aclose()
    assert calls == [True] and owned.unsettled
    assert not owned.closed and not owned.settled
    assert not videos._slots.acquire(False)
    permit.__aexit__.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_cancelled_close_waits_for_actual_descriptor_end(monkeypatch, unknown):
    monkeypatch.setattr(videos, "_slots", threading.BoundedSemaphore(1))
    admission = videos._Admission()
    started, release = threading.Event(), threading.Event()
    permit = SimpleNamespace(__aexit__=AsyncMock())

    def close():
        started.set()
        assert release.wait(3)
        if unknown:
            raise OSError("descriptor end unconfirmed")

    owned = videos.SourceInput(
        stage(file=SimpleNamespace(close=close)), PROBE, permit, admission
    )
    pending = asyncio.create_task(owned.aclose())
    try:
        await wait_started(started)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not owned.closed and not owned.settled
        permit.__aexit__.assert_not_awaited()
        assert not videos._slots.acquire(False)
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await pending
        assert (
            bool(getattr(caught.value, "storage_operation_unsettled", False)) is unknown
        )
        assert owned.unsettled is unknown and owned.settled is not unknown
        if unknown:
            permit.__aexit__.assert_not_awaited()
        else:
            permit.__aexit__.assert_awaited_once()
            assert owned.closed
    finally:
        release.set()
        if not pending.done():
            try:
                await videos.common.drain(pending)
            except BaseException:
                pass


@pytest.mark.asyncio
async def test_unknown_permit_settlement_is_sticky_after_known_file_close(monkeypatch):
    monkeypatch.setattr(videos, "_slots", threading.BoundedSemaphore(1))
    admission = videos._Admission()
    permit = SimpleNamespace(
        __aexit__=AsyncMock(side_effect=RuntimeError("settlement unknown"))
    )
    owned = videos.SourceInput(stage(), PROBE, permit, admission)
    with pytest.raises(RuntimeError) as caught:
        await owned.aclose()
    assert caught.value.storage_operation_unsettled
    assert owned.closed and owned.unsettled and not owned.settled
    assert owned.stage.file.closed
    with pytest.raises(ContentError, match="storage_operation_unsettled"):
        await owned.aclose()
    permit.__aexit__.assert_awaited_once()
    assert not videos._slots.acquire(False)


@pytest.mark.asyncio
async def test_foreign_unknown_input_close_cannot_release_original(monkeypatch):
    monkeypatch.setattr(videos, "_slots", threading.BoundedSemaphore(1))
    admission = videos._Admission()
    file = SimpleNamespace(
        close=lambda: pytest.fail("unknown consumer still owns source")
    )
    permit = SimpleNamespace(__aexit__=AsyncMock())
    owned = videos.SourceInput(stage(file=file), PROBE, permit, admission)
    failure = storage_access.mark_unsettled(RuntimeError("target close unknown"))
    with pytest.raises(ContentError, match="storage_operation_unsettled"):
        await owned.aclose(failure)
    assert owned.unsettled and not owned.closed and not owned.settled
    permit.__aexit__.assert_not_awaited()
    assert not videos._slots.acquire(False)
