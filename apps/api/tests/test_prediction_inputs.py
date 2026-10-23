"""Durable input ownership, late writes, isolation and real pipeline integration."""

import asyncio
from contextlib import contextmanager
from contextlib import asynccontextmanager
from datetime import timedelta
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace
import uuid

from PIL import Image
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.prediction_input import PredictionInputLease
from app.services import prediction_inputs as service
from app.services.content_snapshot import ContentError, capture
from app.services.ml_client import PredictionResult
from app.workers import tasks


class MemoryStorage:
    def __init__(self, world):
        self.world = world
        self.client = self
        self.meta = SimpleNamespace(endpoint_url=world.endpoint)
        self.import_bucket = world.bucket
        self.closed = False
        world.storages.append(self)

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        if self.world.put_started:
            self.world.put_started.set()
            if not self.world.put_release.wait(5):
                raise TimeoutError("test gate expired")
        self.world.objects[Bucket, Key] = Body.read() if hasattr(Body, "read") else Body
        self.world.puts.append(Key)
        if self.world.fail_put:
            raise OSError("synthetic failed signed request, must stay private")

    def put_bytes(self, key, content, *, content_type, bucket):
        self.put_object(Bucket=bucket, Key=key, Body=content)

    def generate_download_url(self, key, *, expires_in, bucket, align):
        assert align is False
        self.world.expirations.append(expires_in)
        return f"http://ml-objects.test/{bucket}/{key}?signature=synthetic"

    def rewrite_host_for_ml_backend(self, url):
        return url

    def delete_prefix(self, prefix, *, bucket):
        self.world.deletions.append((bucket, prefix))
        if self.world.fail_delete:
            raise OSError("synthetic private storage failure")
        for identity in list(self.world.objects):
            if identity[0] == bucket and identity[1].startswith(prefix):
                del self.world.objects[identity]

    def close(self):
        self.closed = True


@pytest.fixture
async def input_world(test_engine, monkeypatch):
    world = SimpleNamespace(
        factory=async_sessionmaker(test_engine, expire_on_commit=False),
        owner_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        objects={},
        endpoint="http://owned.test:9000",
        bucket="owned-import",
        storages=[],
        puts=[],
        expirations=[],
        deletions=[],
        fail_put=False,
        fail_delete=False,
        put_started=None,
        put_release=None,
    )
    monkeypatch.setattr(service, "make_storage", lambda: MemoryStorage(world))
    try:
        yield world
    finally:
        if world.put_release:
            world.put_release.set()
        async with world.factory() as db:
            await db.execute(
                delete(PredictionInputLease).where(
                    PredictionInputLease.owner_id == world.owner_id
                )
            )
            await db.commit()


async def open_inputs(world, **kwargs):
    return await service.PredictionInputs.create(
        world.factory,
        owner_id=world.owner_id,
        owner_kind="batch_predict",
        task_id=world.task_id,
        **kwargs,
    )


async def snapshot_image(*, orientation=None):
    content = io.BytesIO()
    image = Image.new("RGB", (48, 32), (23, 42, 61))
    if orientation:
        exif = Image.Exif()
        exif[274] = orientation
        image.save(content, "JPEG", exif=exif)
    else:
        image.save(content, "PNG")
    image.close()
    raw = content.getvalue()

    @contextmanager
    def source():
        yield io.BytesIO(raw), {"size": len(raw)}

    return await capture(source)


async def get_record(world, identity):
    async with world.factory() as db:
        return await db.get(PredictionInputLease, identity)


async def test_original_put_has_independently_committed_intent_and_closes_snapshot(
    input_world,
):
    world = input_world
    inputs = await open_inputs(world)
    snapshot = await snapshot_image()
    world.put_started, world.put_release = threading.Event(), threading.Event()
    pending = asyncio.create_task(inputs.upload_snapshot(snapshot))
    try:
        assert await asyncio.to_thread(world.put_started.wait, 3)
        # A different committed connection sees recovery before PUT can finish.
        record = await get_record(world, inputs.record.id)
        assert record.state == "active" and record.task_id == world.task_id
        assert not world.objects
        world.put_release.set()
        url = await pending
        assert f"prediction-inputs/{record.id}/original/{snapshot.sha256}.png" in url
        assert all(value <= service.URL_SECONDS for value in world.expirations)
    finally:
        world.put_release.set()
        await pending
        await inputs.close()
    assert snapshot.file.closed
    assert not world.objects
    assert await get_record(world, inputs.record.id) is None


async def test_same_owner_attempts_and_neighbor_prefixes_are_isolated(input_world):
    world = input_world
    first, second = await open_inputs(world), await open_inputs(world)
    neighbor = (world.bucket, first.prefix.rstrip("/") + "-neighbor/original.jpg")
    world.objects[neighbor] = b"untouched"
    task = SimpleNamespace(id=world.task_id)
    try:
        assert first.upload_crop(task, 0, b"first") != second.upload_crop(
            task, 0, b"second"
        )
        await first.close()
        assert any(key.startswith(second.prefix) for bucket, key in world.objects)
        assert world.objects[neighbor] == b"untouched"
    finally:
        await first.close()
        await second.close()
    assert set(world.objects) == {neighbor}


async def test_delete_failure_is_retained_and_retried_without_private_error(
    input_world, monkeypatch, caplog
):
    world = input_world
    inputs = await open_inputs(world)
    inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"crop")
    world.fail_delete = True
    await inputs.close()
    record = await get_record(world, inputs.record.id)
    assert record.state == "cleanup_pending"
    assert record.error_code == "storage_cleanup_failed"
    assert world.objects and "synthetic private" not in caplog.text
    world.fail_delete = False
    monkeypatch.setattr(
        service, "now", lambda: record.next_cleanup_at + timedelta(seconds=1)
    )
    result = await service.maintain(world.factory)
    assert result["removed"] == 1
    assert not world.objects
    assert await get_record(world, record.id) is None


@pytest.mark.parametrize("lost", [False, True])
async def test_uncertain_or_crashed_put_retains_recovery_for_late_objects(
    input_world, monkeypatch, lost
):
    world = input_world
    inputs = await open_inputs(world)
    task = SimpleNamespace(id=world.task_id)
    if lost:
        inputs.upload_crop(task, 0, b"crop")
        active = await service.maintain(world.factory)
        assert sum(active.values()) == 0 and world.objects
        checked = inputs.record.expires_at + timedelta(seconds=1)
    else:
        world.fail_put = True
        with pytest.raises(ContentError, match="prediction_input_upload_failed"):
            inputs.upload_crop(task, 0, b"crop")
        await inputs.close()
        world.fail_put = False
        record = await get_record(world, inputs.record.id)
        assert record.uncertain_write
        checked = record.next_cleanup_at + timedelta(seconds=1)
    monkeypatch.setattr(service, "now", lambda: checked)
    assert (await service.maintain(world.factory))["retained"] == 1
    assert not world.objects
    # A request admitted before the crash may complete after the first sweep.
    world.objects[world.bucket, inputs.prefix + "crops/late.jpg"] = b"late"
    record = await get_record(world, inputs.record.id)
    checked = record.next_cleanup_at + timedelta(seconds=1)
    assert (await service.maintain(world.factory))["retained"] == 1
    assert not world.objects
    with pytest.raises(ContentError, match="prediction_input_expired"):
        inputs.upload_crop(task, 1, b"forbidden")
    checked = record.retain_until + timedelta(seconds=1)
    assert (await service.maintain(world.factory))["removed"] == 1
    assert await get_record(world, inputs.record.id) is None
    await inputs.close()


@pytest.mark.parametrize("changed", ["endpoint", "bucket"])
async def test_recovery_never_follows_a_changed_storage_server(
    input_world, monkeypatch, changed
):
    world = input_world
    inputs = await open_inputs(world)
    inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"crop")
    original = getattr(world, changed)
    setattr(world, changed, original + "-other")
    checked = inputs.record.expires_at + timedelta(seconds=1)
    monkeypatch.setattr(service, "now", lambda: checked)
    result = await service.maintain(world.factory)
    assert result["deferred"] == 1
    record = await get_record(world, inputs.record.id)
    assert record.error_code == "storage_identity_changed"
    assert not world.deletions and world.objects
    setattr(world, changed, original)
    checked = record.next_cleanup_at + timedelta(seconds=1)
    assert (await service.maintain(world.factory))["retained"] == 1
    assert not world.objects
    await inputs.close()


async def test_scope_and_budgets_fail_before_put_and_duplicate_crop_is_reused(
    input_world, monkeypatch
):
    world = input_world
    inputs = await open_inputs(world)
    task = SimpleNamespace(id=world.task_id)
    monkeypatch.setattr(service, "MAX_OBJECTS", 2)
    monkeypatch.setattr(service, "MAX_TOTAL_BYTES", 6)
    try:
        url = inputs.upload_crop(task, 0, b"one")
        assert inputs.upload_crop(task, 0, b"one") == url
        assert len(world.puts) == 1
        with pytest.raises(ContentError, match="scope_invalid"):
            inputs.upload_crop(SimpleNamespace(id=uuid.uuid4()), 1, b"two")
        inputs.upload_crop(task, 1, b"two")
        with pytest.raises(ContentError, match="budget_exceeded"):
            inputs.upload_crop(task, 2, b"x")
        assert len(world.puts) == 2
    finally:
        await inputs.close()


async def test_failed_put_is_not_cached_as_a_successful_crop(input_world):
    world = input_world
    inputs = await open_inputs(world)
    task = SimpleNamespace(id=world.task_id)
    try:
        world.fail_put = True
        with pytest.raises(ContentError, match="prediction_input_upload_failed"):
            inputs.upload_crop(task, 0, b"crop")
        world.fail_put = False
        assert "prediction-inputs/" in inputs.upload_crop(task, 0, b"crop")
        assert len(world.puts) == 2 and len(inputs._objects) == 1
    finally:
        await inputs.close()
    assert (await get_record(world, inputs.record.id)).uncertain_write


async def test_journal_commit_failure_prevents_upload_and_closes_client(input_world):
    world = input_world

    @asynccontextmanager
    async def unavailable():
        class Database:
            def add(self, record):
                pass

            async def commit(self):
                raise RuntimeError("synthetic commit failure")

        yield Database()

    with pytest.raises(RuntimeError, match="commit failure"):
        await service.PredictionInputs.create(
            unavailable,
            owner_id=world.owner_id,
            task_id=world.task_id,
            owner_kind="batch_predict",
        )
    assert not world.puts and all(storage.closed for storage in world.storages)


@pytest.mark.parametrize("remaining", [1, 17, 30])
async def test_signature_does_not_align_past_lease_expiration(
    input_world, monkeypatch, remaining
):
    world = input_world
    inputs = await open_inputs(world)
    monkeypatch.setattr(
        service, "now", lambda: inputs.record.expires_at - timedelta(seconds=remaining)
    )
    try:
        inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"crop")
        assert world.expirations == [remaining]
    finally:
        await inputs.close()


@pytest.mark.parametrize(
    "outcome", ["success", "read-error", "decode-error", "get-error"]
)
def test_managed_roi_loader_closes_body_and_client(monkeypatch, outcome):
    raw = io.BytesIO()
    with Image.new("RGB", (4, 3)) as image:
        image.save(raw, "PNG")

    class Body(io.BytesIO):
        def read(self, *args):
            if outcome == "read-error":
                raise OSError("synthetic read failure")
            return super().read(*args)

    body = Body(b"invalid" if outcome == "decode-error" else raw.getvalue())
    raw.close()

    class Client:
        closed = False

        def get_object(self, **kwargs):
            if outcome == "get-error":
                raise OSError("synthetic get failure")
            return {"Body": body}

        def close(self):
            self.closed = True

    client = Client()
    storage = SimpleNamespace(
        client=client, datasets_bucket="datasets", bucket="annotations"
    )
    monkeypatch.setattr("app.services.storage.StorageService", lambda: storage)
    task = SimpleNamespace(dataset_item_id=uuid.uuid4(), file_path="managed/image.png")
    if outcome == "success":
        converted = tasks._load_task_image(task)
        assert converted.size == (4, 3)
        converted.close()
    else:
        with pytest.raises(OSError):
            tasks._load_task_image(task)
    assert client.closed
    if outcome != "get-error":
        assert body.closed
    body.close()


def test_dedicated_sdk_sends_one_put_when_server_returns_retryable_failure(monkeypatch):
    from botocore.exceptions import ClientError
    from app.config import settings

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append(self.path)
            content = (
                b"<Error><Code>SlowDown</Code><Message>synthetic</Message></Error>"
            )
            self.send_response(503)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(settings, "minio_endpoint", f"127.0.0.1:{server.server_port}")
    monkeypatch.setattr(settings, "minio_use_ssl", False)
    monkeypatch.setattr(settings, "minio_access_key", "synthetic-access")
    monkeypatch.setattr(settings, "minio_secret_key", "synthetic-secret")
    storage = service.make_storage()
    try:
        with pytest.raises(ClientError):
            storage.client.put_object(
                Bucket="synthetic-import", Key="input.jpg", Body=b"image"
            )
        assert len(received) == 1
    finally:
        storage.client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("orientation", [None, 6])
async def test_roi_loader_uses_owned_snapshot_and_closes_all_handles(
    input_world, orientation
):
    world = input_world
    snapshot = await snapshot_image(orientation=orientation)
    inputs = await open_inputs(world)
    try:
        await inputs.upload_snapshot(snapshot)
        image = inputs.load_image(SimpleNamespace(id=world.task_id))
        second = inputs.load_image(SimpleNamespace(id=world.task_id))
        assert image.size == (snapshot.width, snapshot.height)
        assert image.getpixel((0, 0)) == second.getpixel((0, 0))
        assert len(world.puts) == 1
    finally:
        await inputs.close()
    assert snapshot.file.closed
    with pytest.raises(ValueError):
        image.getpixel((0, 0))


async def test_cancellation_settles_put_before_snapshot_is_closed(input_world):
    world = input_world
    inputs = await open_inputs(world)
    snapshot = await snapshot_image()
    world.put_started, world.put_release = threading.Event(), threading.Event()
    pending = asyncio.create_task(inputs.upload_snapshot(snapshot))
    try:
        assert await asyncio.to_thread(world.put_started.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not snapshot.file.closed
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not snapshot.file.closed
        assert not inputs.storage.closed
        world.put_release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        world.put_release.set()
        await inputs.close()
    assert snapshot.file.closed and not world.objects
    assert (await get_record(world, inputs.record.id)).uncertain_write


@pytest.mark.parametrize("outcome", ["success", "partial", "cancelled", "root-failure"])
async def test_real_pipeline_cleans_owned_crop_after_each_outcome(
    input_world, monkeypatch, outcome
):
    world = input_world
    task = SimpleNamespace(id=world.task_id)
    image = Image.new("RGB", (1000, 1000), (3, 4, 5))
    monkeypatch.setattr(tasks, "_load_task_image", lambda _: image)
    stages = [
        {"stage": 7, "parent_stage": None, "ml_backend_id": str(uuid.uuid4())},
        {
            "stage": 9,
            "parent_stage": 7,
            "ml_backend_id": str(uuid.uuid4()),
            "roi": {"mode": "crop", "pad": 0},
            "write": {"target": "attributes", "keys": ["color"]},
        },
    ]

    class Root:
        async def predict(self, inputs, context=None):
            if outcome == "root-failure":
                raise RuntimeError("synthetic root failure")
            return [
                PredictionResult(
                    task_id=str(world.task_id),
                    result=[
                        {
                            "type": "rectanglelabels",
                            "value": {
                                "x": 10,
                                "y": 10,
                                "width": 20,
                                "height": 20,
                                "rectanglelabels": ["car"],
                            },
                        }
                    ],
                )
            ]

    class Child:
        async def predict(self, inputs, context=None):
            assert "prediction-inputs/" in inputs[0]["file_path"]
            assert world.objects
            async with world.factory() as db:
                assert await db.scalar(
                    select(PredictionInputLease.id).where(
                        PredictionInputLease.owner_id == world.owner_id
                    )
                )
            if outcome == "partial":
                raise RuntimeError("synthetic downstream failure")
            if outcome == "cancelled":
                raise asyncio.CancelledError()
            return [
                PredictionResult(
                    task_id="0", result=[{"attributes": {"color": "blue"}}]
                )
            ]

    async def run():
        return await tasks._run_task_pipeline(
            task,
            stages,
            [Root(), Child()],
            [None, None],
            resolve_url=lambda _: "http://managed.test/image.jpg",
            input_session_factory=world.factory,
            input_owner_id=world.owner_id,
        )

    try:
        if outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await run()
        elif outcome == "root-failure":
            with pytest.raises(RuntimeError):
                await run()
        else:
            _, extra, _ = await run()
            assert bool(extra["pipeline"].get("partial")) == (outcome == "partial")
        assert not world.objects
        async with world.factory() as db:
            assert not (
                await db.scalars(
                    select(PredictionInputLease.id).where(
                        PredictionInputLease.owner_id == world.owner_id
                    )
                )
            ).all()
        if outcome != "root-failure":
            with pytest.raises(ValueError):
                image.getpixel((0, 0))
    finally:
        image.close()
