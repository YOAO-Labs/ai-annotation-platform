"""Durable input ownership, late writes, isolation and real pipeline integration."""

import asyncio
from contextlib import contextmanager
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from botocore.exceptions import ClientError
from PIL import Image
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.prediction_input import PredictionInputLease
from app.services import prediction_inputs as service
from app.services.content_snapshot import ContentError, capture
from app.services.ml_client import PredictionResult
from app.services.dataset_assets import ensure_legacy_bindings
from app.workers import tasks
from tests.test_export_media_bundle_models import _migration


class MemoryStorage:
    """One real-shaped SDK unit; every new unit gets its own closable client."""

    def __init__(self, world):
        self.world = world
        self.client = self
        self.meta = SimpleNamespace(
            endpoint_url=world.endpoint, events=SimpleNamespace(register=Mock())
        )
        self.import_bucket = world.bucket
        self.closed = False
        world.storages.append(self)

    def get_bucket_versioning(self, *, Bucket):
        assert not self.closed
        self.world.versioning_reads.append(Bucket)
        return {
            "Status": self.world.versioning,
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    @staticmethod
    def seed(world, bucket, key, raw, metadata, *, version=None):
        version = version or str(uuid.uuid4())
        world.versions.setdefault((bucket, key), {})[version] = (
            raw,
            deepcopy(metadata),
        )
        world.current_versions[bucket, key] = version
        world.objects[bucket, key] = raw
        world.metadata[bucket, key] = deepcopy(metadata)
        return version

    def _value(self, bucket, key, version=None):
        current = self.world.current_versions.get((bucket, key), "null")
        version = current if version is None else version
        if version == current and (bucket, key) in self.world.objects:
            return (
                self.world.objects[bucket, key],
                self.world.metadata.get((bucket, key), {}),
                version,
            )
        if version in self.world.versions.get((bucket, key), {}):
            raw, metadata = self.world.versions[bucket, key][version]
            return raw, metadata, version
        raise ClientError({"Error": {"Code": "NoSuchVersion"}}, "HeadObject")

    def _head(self, bucket, key, version=None):
        raw, metadata, version = self._value(bucket, key, version)
        return {
            "ContentLength": len(raw),
            "ETag": '"' + hashlib.sha256(raw).hexdigest() + '"',
            "VersionId": version,
            "Metadata": deepcopy(metadata),
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def put_object(
        self, *, Bucket, Key, Body, ContentLength, IfNoneMatch, Metadata, **kwargs
    ):
        assert not self.closed and IfNoneMatch == "*"
        assert set(Metadata) == {"aap-prediction-lease", "aap-prediction-write"}
        assert (
            str(uuid.UUID(Metadata["aap-prediction-write"]))
            == Metadata["aap-prediction-write"]
        )
        assert Metadata["aap-prediction-lease"] == Key.split("/")[1]
        self.world.puts.append(Key)
        if self.world.put_started:
            self.world.put_started.set()
            if not self.world.put_release.wait(5):
                raise TimeoutError("test gate expired")
        if (Bucket, Key) in self.world.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        raw = Body.read()
        assert len(raw) == ContentLength
        self.seed(self.world, Bucket, Key, raw, Metadata)
        if self.world.fail_put:
            raise OSError("synthetic failed signed request, must stay private")
        return self._head(Bucket, Key)

    def head_object(self, *, Bucket, Key, VersionId=None):
        assert not self.closed
        self.world.heads.append((Bucket, Key))
        self.world.head_versions.append(VersionId)
        return self._head(Bucket, Key, VersionId)

    def get_object(self, *, Bucket, Key, IfMatch, **kwargs):
        assert not self.closed
        version = kwargs["VersionId"]
        assert version not in {None, "null"}
        facts = self._head(Bucket, Key, version)
        assert IfMatch == facts["ETag"]
        raw, _, _ = self._value(Bucket, Key, version)
        body = io.BytesIO(raw)
        self.world.bodies.append(body)
        self.world.reads.append((Bucket, Key))
        self.world.read_versions.append(version)
        return {**facts, "Body": body}

    def list_objects_v2(self, *, Bucket, Prefix, **kwargs):
        assert not self.closed
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "IsTruncated": False,
            "Contents": [
                {"Key": key, "Size": len(raw), "ETag": self._head(bucket, key)["ETag"]}
                for (bucket, key), raw in self.world.objects.items()
                if bucket == Bucket and key.startswith(Prefix)
            ],
        }

    def list_object_versions(self, *, Bucket, Prefix, **kwargs):
        rows = []
        for (bucket, key), raw in self.world.objects.items():
            if bucket != Bucket or not key.startswith(Prefix):
                continue
            for version in self.world.versions.get((bucket, key), {"null": (raw, {})}):
                facts = self._head(bucket, key, version)
                rows.append(
                    {
                        "Key": key,
                        "Size": facts["ContentLength"],
                        "ETag": facts["ETag"],
                        "VersionId": version,
                    }
                )
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "IsTruncated": False,
            "Versions": rows,
            "DeleteMarkers": [],
        }

    def delete_object(self, *, Bucket, Key, VersionId, IfMatch):
        assert not self.closed
        # Current MinIO ignores IfMatch on DELETE; the exact version must fence it.
        self._value(Bucket, Key, VersionId)
        if self.world.fail_delete:
            raise OSError("synthetic private storage failure")
        self.world.deletions.append((Bucket, Key))
        self.world.deleted_versions.append(VersionId)
        versions = self.world.versions.get((Bucket, Key), {})
        versions.pop(VersionId, None)
        if versions:
            current = next(reversed(versions))
            raw, metadata = versions[current]
            self.world.current_versions[Bucket, Key] = current
            self.world.objects[Bucket, Key] = raw
            self.world.metadata[Bucket, Key] = deepcopy(metadata)
        else:
            self.world.versions.pop((Bucket, Key), None)
            self.world.current_versions.pop((Bucket, Key), None)
            self.world.objects.pop((Bucket, Key), None)
            self.world.metadata.pop((Bucket, Key), None)
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def generate_download_url(self, key, *, expires_in, bucket, align, version_id):
        assert not self.closed and align is False
        assert version_id not in {None, "null"}
        self.world.expirations.append(expires_in)
        self.world.signed_versions.append(version_id)
        return f"http://ml-objects.test/{bucket}/{key}?signature=synthetic&versionId={version_id}"

    def rewrite_host_for_ml_backend(self, url):
        return url

    def close(self):
        self.closed = True


@pytest.fixture
async def input_world(test_engine, monkeypatch):
    # Keep independent committed ownership out of the business SAVEPOINT, and
    # remove its random schema without bypassing immutable tombstone triggers.
    schema = "prediction_inputs_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        source = await connection.scalar(text("SELECT current_schema()"))
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        for table in (
            "storage_locations",
            "storage_bindings",
            "storage_maintenance_gates",
            "storage_operations",
        ):
            await connection.execute(
                text(
                    f'CREATE TABLE "{schema}"."{table}" '
                    f'(LIKE "{source}"."{table}" INCLUDING ALL)'
                )
            )
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    world = SimpleNamespace(
        factory=async_sessionmaker(engine, expire_on_commit=False),
        owner_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        objects={},
        metadata={},
        versions={},
        current_versions={},
        versioning="Enabled",
        versioning_reads=[],
        read_versions=[],
        head_versions=[],
        deleted_versions=[],
        signed_versions=[],
        endpoint=service._endpoint(),
        bucket=settings.minio_import_bucket,
        storages=[],
        bodies=[],
        puts=[],
        heads=[],
        reads=[],
        expirations=[],
        deletions=[],
        fail_put=False,
        fail_delete=False,
        put_started=None,
        put_release=None,
    )
    try:
        async with engine.begin() as connection:
            for revision in (
                "0212_prediction_input_leases",
                "0238_prediction_input_ownership",
                "0239_prediction_input_versions",
            ):
                await connection.run_sync(
                    lambda sync, name=revision: _migration(sync, name).upgrade()
                )
        async with world.factory() as db:
            await ensure_legacy_bindings(db)
            await db.commit()
        monkeypatch.setattr(service, "make_storage", lambda: MemoryStorage(world))
        yield world
    finally:
        if world.put_release:
            world.put_release.set()
        for body in world.bodies:
            body.close()
        for storage in world.storages:
            storage.close()
        world.objects.clear()
        world.metadata.clear()
        world.versions.clear()
        world.current_versions.clear()
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


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
        entry = next(iter(record.inventory["objects"].values()))
        assert entry["state"] == "planned" and entry["etag"] is None
        assert uuid.UUID(entry["write_nonce"]).version == 4
        assert record.object_count == 1 and record.size_bytes == snapshot.size
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
    assert (await get_record(world, inputs.record.id)).state == "deleted"
    assert world.reads and all(body.closed for body in world.bodies)
    assert all(storage.closed for storage in world.storages)


async def test_same_owner_attempts_and_neighbor_prefixes_are_isolated(input_world):
    world = input_world
    first, second = await open_inputs(world), await open_inputs(world)
    neighbor = (world.bucket, first.prefix.rstrip("/") + "-neighbor/original.jpg")
    world.objects[neighbor] = b"untouched"
    task = SimpleNamespace(id=world.task_id)
    try:
        assert await first.upload_crop(task, 0, b"first") != await second.upload_crop(
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
    await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"crop")
    world.fail_delete = True
    await inputs.close()
    record = await get_record(world, inputs.record.id)
    assert record.state == "cleanup_pending"
    assert record.error_code == "prediction_input_storage_cleanup_failed"
    assert world.objects and "synthetic private" not in caplog.text
    world.fail_delete = False
    monkeypatch.setattr(
        service, "now", lambda: record.next_cleanup_at + timedelta(seconds=1)
    )
    result = await service.maintain(world.factory)
    assert result["removed"] == 1
    assert not world.objects
    assert (await get_record(world, record.id)).state == "deleted"


@pytest.mark.parametrize("lost", [False, True])
async def test_uncertain_or_crashed_put_retains_recovery_for_late_objects(
    input_world, monkeypatch, lost
):
    world = input_world
    inputs = await open_inputs(world)
    task = SimpleNamespace(id=world.task_id)
    if lost:
        await inputs.upload_crop(task, 0, b"crop")
        active = await service.maintain(world.factory)
        assert sum(active.values()) == 0 and world.objects
        checked = inputs.record.expires_at + timedelta(seconds=1)
    else:
        world.fail_put = True
        with pytest.raises(ContentError, match="prediction_input_upload_failed"):
            await inputs.upload_crop(task, 0, b"crop")
        world.fail_put = False
    # A late completion has to be the exact admitted object and write nonce.
    # An arbitrary new key is foreign and must never be swept by prefix.
    identity, raw = next(iter(world.objects.items()))
    ownership = deepcopy(world.metadata[identity])
    if not lost:
        await inputs.close()
        record = await get_record(world, inputs.record.id)
        assert record.uncertain_write
        checked = record.next_cleanup_at + timedelta(seconds=1)
    monkeypatch.setattr(service, "now", lambda: checked)
    assert (await service.maintain(world.factory))["retained"] == 1
    assert not world.objects
    # A later PUT creates a distinct immutable version, even for the same bytes.
    # Only an unpublished planned intent may prove that later candidate by nonce;
    # a verified receipt can never be rewritten to adopt the new generation.
    MemoryStorage.seed(world, *identity, raw, ownership)
    record = await get_record(world, inputs.record.id)
    checked = record.next_cleanup_at + timedelta(seconds=1)
    result = await service.maintain(world.factory)
    assert result["deferred" if lost else "retained"] == 1
    assert bool(world.objects) is lost
    with pytest.raises(ContentError, match="prediction_input_expired"):
        await inputs.upload_crop(task, 1, b"forbidden")
    checked = record.retain_until + timedelta(seconds=1)
    result = await service.maintain(world.factory)
    assert result["deferred" if lost else "removed"] == 1
    tombstone = await get_record(world, inputs.record.id)
    assert tombstone.state == ("cleanup_pending" if lost else "deleted")
    assert tombstone.inventory == record.inventory
    await inputs.close()


@pytest.mark.parametrize("changed", ["endpoint", "bucket"])
async def test_recovery_uses_saved_bucket_and_refuses_a_changed_storage_server(
    input_world, monkeypatch, changed
):
    world = input_world
    inputs = await open_inputs(world)
    await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"crop")
    saved_bucket = inputs.record.bucket
    original = getattr(world, changed)
    setattr(world, changed, original + "-other")
    checked = inputs.record.expires_at + timedelta(seconds=1)
    monkeypatch.setattr(service, "now", lambda: checked)
    trap = None
    if changed == "bucket":
        key = next(iter(world.objects))[1]
        trap = (world.bucket, key)
        world.objects[trap] = b"wrong-current-bucket"
    result = await service.maintain(world.factory)
    record = await get_record(world, inputs.record.id)
    if changed == "endpoint":
        assert result["deferred"] == 1
        assert record.error_code == "prediction_input_storage_identity_changed"
        assert not world.deletions and world.objects
        setattr(world, changed, original)
        checked = record.next_cleanup_at + timedelta(seconds=1)
        assert (await service.maintain(world.factory))["retained"] == 1
        assert not world.objects
    else:
        assert result["retained"] == 1
        assert set(world.objects) == {trap}
        assert world.objects[trap] == b"wrong-current-bucket"
        assert all(bucket == saved_bucket for bucket, _ in world.deletions)
        setattr(world, changed, original)
    await inputs.close()


async def test_foreign_late_key_is_preserved_with_its_cleanup_responsibility(
    input_world, monkeypatch
):
    world = input_world
    inputs = await open_inputs(world)
    await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"owned")
    foreign = (world.bucket, inputs.prefix + "crops/foreign.jpg")
    world.objects[foreign] = b"foreign late bytes"
    checked = inputs.record.expires_at + timedelta(seconds=1)
    monkeypatch.setattr(service, "now", lambda: checked)
    assert (await service.maintain(world.factory))["deferred"] == 1
    assert world.objects[foreign] == b"foreign late bytes"
    assert not world.deletions
    row = await get_record(world, inputs.record.id)
    assert row.state == "cleanup_pending" and row.deleted_at is None
    assert row.error_code == "prediction_input_unknown_object"


async def test_scope_and_budgets_fail_before_put_and_duplicate_crop_is_reused(
    input_world, monkeypatch
):
    world = input_world
    inputs = await open_inputs(world)
    task = SimpleNamespace(id=world.task_id)
    monkeypatch.setattr(service, "MAX_OBJECTS", 2)
    monkeypatch.setattr(service, "MAX_TOTAL_BYTES", 6)
    try:
        url = await inputs.upload_crop(task, 0, b"one")
        assert await inputs.upload_crop(task, 0, b"one") == url
        assert len(world.puts) == 1
        with pytest.raises(ContentError, match="scope_invalid"):
            await inputs.upload_crop(SimpleNamespace(id=uuid.uuid4()), 1, b"two")
        await inputs.upload_crop(task, 1, b"two")
        with pytest.raises(ContentError, match="budget_exceeded"):
            await inputs.upload_crop(task, 2, b"x")
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
            await inputs.upload_crop(task, 0, b"crop")
        world.fail_put = False
        assert "prediction-inputs/" in await inputs.upload_crop(task, 0, b"crop")
        assert len(world.puts) == 2 and len(inputs._objects) == 1
    finally:
        await inputs.close()
    assert (await get_record(world, inputs.record.id)).uncertain_write


async def test_journal_commit_failure_prevents_upload_and_client_construction(
    input_world, monkeypatch
):
    world = input_world

    @asynccontextmanager
    async def unavailable():
        async with world.factory() as db:

            async def fail_commit():
                raise RuntimeError("synthetic commit failure")

            monkeypatch.setattr(db, "commit", fail_commit)
            yield db

    with pytest.raises(RuntimeError, match="commit failure"):
        await service.PredictionInputs.create(
            unavailable,
            owner_id=world.owner_id,
            task_id=world.task_id,
            owner_kind="batch_predict",
        )
    assert not world.puts and not world.storages
    async with world.factory() as db:
        assert not (
            await db.scalars(
                select(PredictionInputLease.id).where(
                    PredictionInputLease.owner_id == world.owner_id
                )
            )
        ).all()


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
        await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"crop")
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
        assert world.storages and any(not storage.closed for storage in world.storages)
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
            records = list(
                await db.scalars(
                    select(PredictionInputLease).where(
                        PredictionInputLease.owner_id == world.owner_id
                    )
                )
            )
            assert len(records) == 1 and records[0].state == "deleted"
            assert records[0].deleted_at is not None
        if outcome != "root-failure":
            with pytest.raises(ValueError):
                image.getpixel((0, 0))
    finally:
        image.close()
