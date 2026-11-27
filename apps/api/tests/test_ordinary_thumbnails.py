"""Canonical A/B sources, durable thumbnail candidates and short publication locks."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import hashlib
import io
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from PIL import Image
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.dataset_source import (
    DatasetScanEntry,
    DatasetScanRun,
    DatasetSource,
    DatasetSourceRevision,
)
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.db.models.managed_original_creation import ManagedOriginalCreation
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.thumbnail_output import ThumbnailOutput
from app.services import dataset_storage, image_pyramid_originals as originals
from app.services import (
    managed_original_reader as reader,
    ordinary_thumbnails as service,
)
from app.services import managed_original_creation, task_originals
from app.services.labeling_e2e_fixture_writes import LabelingE2EFixtureWrites
from app.services.workspace_e2e_fixtures import create_fixture_project
from app.services import thumbnail_outputs
from app.services import storage as storage_module
from dataclasses import replace

from app.services import managed_s3_target
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.managed_s3_target import Target
from app.services.source_readers.s3 import S3Access, ScopedS3Access
from app.services.storage import storage_service
from app.workers import media
from tests.managed_original_client_support import Client as OriginalClient, stage_client
from tests.test_managed_original_reader import error


class Client:
    def __init__(self, world, endpoint):
        self.world = world
        self.meta = SimpleNamespace(endpoint_url=endpoint)
        self.closed = False
        world.clients.append(self)

    def _metadata(self, data):
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "ContentLength": len(data),
            "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
            "LastModified": datetime(2026, 10, 25, tzinfo=timezone.utc),
            "ContentType": "image/webp",
        }

    def head_object(self, **params):
        self.world.calls.append(("head", self.meta.endpoint_url, params))
        if self.world.lock_probe:
            self.world.lock_probe()
        return self._metadata(self.world.objects[params["Bucket"], params["Key"]])

    def get_object(self, **params):
        self.world.calls.append(("get", self.meta.endpoint_url, params))
        data = self.world.objects[params["Bucket"], params["Key"]]
        metadata = self._metadata(data)
        if "IfMatch" in params and params["IfMatch"] != metadata["ETag"]:
            raise error()
        body = io.BytesIO(data)
        self.world.bodies.append(body)
        return {**metadata, "Body": body}

    def put_object(self, **params):
        self.world.calls.append(
            (
                "put",
                self.meta.endpoint_url,
                {k: v for k, v in params.items() if k != "Body"},
            )
        )
        if self.world.lock_probe:
            self.world.lock_probe()
        assert params["IfNoneMatch"] == "*"
        pair = params["Bucket"], params["Key"]
        if pair in self.world.objects:
            raise error()
        self.world.objects[pair] = params["Body"]
        return self._metadata(params["Body"])

    def close(self):
        self.closed = True


@asynccontextmanager
async def native_world(
    managed_database, monkeypatch, *, standalone=False, oversized=False, unsettled=False
):
    """Use native public owners in one guarded, test-owned PostgreSQL database."""
    value = SimpleNamespace(
        ready=False,
        clients=[],
        bodies=[],
        objects={},
        calls=[],
        lock_probe=None,
        active=set(),
        original_client=None,
        original_keys=set(),
        tasks=set(),
        threads=set(),
        thread_lock=threading.Lock(),
    )

    async def before_drop(engine):
        assert value.ready, "fixture setup did not complete"
        assert not value.active, "thumbnail execution is still running"
        assert all(task.done() for task in value.tasks), (
            "background task is still running"
        )
        with value.thread_lock:
            assert not value.threads, "physical thread is still running"
        assert all(client.closed for client in value.clients)
        assert all(body.closed for body in value.bodies)
        if value.original_client is not None:
            assert value.original_client.closed
            assert not value.original_client.uploads
            assert all(body.closed for body in value.original_client.bodies)
            assert set(value.original_client.objects) <= value.original_keys
            assert not value.original_client.versions
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            open_operations = list(await db.scalars(select(StorageOperation.purpose)))
            incomplete = list(
                await db.scalars(
                    select(ThumbnailOutput).where(
                        (ThumbnailOutput.state == "building")
                        | ThumbnailOutput.uncertain_write.is_(True)
                    )
                )
            )
            if unsettled:
                assert len(incomplete) == 1 and incomplete[0].state == "failed"
                assert open_operations == [thumbnail_outputs.purpose(incomplete[0].id)]
            else:
                assert not incomplete
                assert not open_operations
            assert not await db.scalar(
                select(ManagedOriginalCreation.id)
                .where(
                    (ManagedOriginalCreation.status != "verified")
                    | ManagedOriginalCreation.settled_at.is_(None)
                )
                .limit(1)
            )
            outputs = list(await db.scalars(select(ThumbnailOutput)))
            known_objects = {
                (value.source_bucket, value.source_key),
                ("saved-b", value.key),
            }
            for output in outputs:
                assert (output.owner_kind, output.owner_id) in {
                    ("task_thumbnail", value.task_id),
                    ("dataset_thumbnail", value.item_id),
                }
                reference = thumbnail_outputs.owner(output)
                known_objects.add((reference.bucket, reference.key("thumbnail.webp")))
            assert set(value.objects) <= known_objects
            assert not await db.scalar(text("SELECT id FROM async_jobs LIMIT 1"))
            assert not await db.scalar(
                text("SELECT id FROM yoao_fixture_v1.fixture_runs LIMIT 1")
            )
            assert not await db.scalar(
                text("SELECT job_id FROM asset_image_jobs LIMIT 1")
            )
            assert not await db.scalar(
                text("SELECT job_id FROM asset_media_jobs LIMIT 1")
            )

    async with managed_database(before_drop=before_drop) as engine:
        value.engine = engine
        value.factory = async_sessionmaker(engine, expire_on_commit=False)
        loop = asyncio.get_running_loop()
        submit = loop.run_in_executor
        create_task = asyncio.create_task

        def tracked_submit(executor, function, *args):
            token = object()
            with value.thread_lock:
                value.threads.add(token)

            def execute():
                try:
                    return function(*args)
                finally:
                    with value.thread_lock:
                        value.threads.remove(token)

            try:
                return submit(executor, execute)
            except BaseException:
                with value.thread_lock:
                    value.threads.remove(token)
                raise

        def tracked_task(coro, *args, **kwargs):
            task = create_task(coro, *args, **kwargs)
            value.tasks.add(task)
            return task

        monkeypatch.setattr(loop, "run_in_executor", tracked_submit)
        monkeypatch.setattr(asyncio, "create_task", tracked_task)
        raw = io.BytesIO()
        with Image.new("RGB", (32, 16), (30, 60, 90)) as image:
            image.save(raw, format="PNG")
        value.raw = raw.getvalue()
        raw.close()
        value.sha = hashlib.sha256(value.raw).hexdigest()
        endpoint = storage_service.client.meta.endpoint_url
        provider = OriginalClient()
        value.original_client = provider
        stage_client(monkeypatch, provider)
        monkeypatch.setattr(
            managed_original_creation, "create_client", lambda **_: provider
        )
        monkeypatch.setattr(storage_service, "bucket", "saved-old-annotations")
        monkeypatch.setattr(
            "app.services.connector_guard.get_allowlist",
            AsyncMock(return_value=["objects.example.test"]),
        )

        @contextmanager
        def client(access, **kwargs):
            instance = Client(value, access.endpoint)
            try:
                yield instance
            finally:
                instance.close()

        monkeypatch.setattr(S3Access, "client", client)
        monkeypatch.setattr(ScopedS3Access, "client", client)
        original_target_for = managed_s3_target.target_for

        async def local_target_for(db, location, **kwargs):
            origin = await original_target_for(db, location, **kwargs)
            if origin.is_registered:
                return replace(
                    origin,
                    _access=ScopedS3Access(
                        {"endpoint": origin.endpoint_url, "bucket": origin.bucket},
                        {
                            "endpoint": origin.endpoint_url,
                            "access_key": "test",
                            "secret_key": "test",
                        },
                        (),
                    ),
                )
            return origin

        monkeypatch.setattr(managed_s3_target, "target_for", local_target_for)
        monkeypatch.setattr(
            thumbnail_outputs.source, "registered", lambda target: False
        )

        class OwnedStorage(storage_module.StorageService):
            def __init__(self, **kwargs):
                self.client = Client(value, endpoint)

        monkeypatch.setattr(storage_module, "StorageService", OwnedStorage)
        monkeypatch.setattr(
            reader, "create_client", lambda **kwargs: Client(value, endpoint)
        )
        monkeypatch.setattr(settings, "image_pyramid_auto_generate", False)
        from app.workers import image_pyramid

        def no_broker(*args, **kwargs):
            raise AssertionError("native thumbnail fixture must not admit broker work")

        monkeypatch.setattr(image_pyramid, "enqueue_image_pyramid", no_broker)
        generate = service.generate

        async def tracked_generate(*args, **kwargs):
            operation = asyncio.current_task()
            value.active.add(operation)
            try:
                return await generate(*args, **kwargs)
            finally:
                value.active.remove(operation)

        monkeypatch.setattr(service, "generate", tracked_generate)
        async with value.factory() as db:
            actor = User(
                id=uuid.uuid4(),
                email=uuid.uuid4().hex + "@example.test",
                name="Thumbnail fixture owner",
                password_hash="unused",
                role="project_admin",
                is_active=True,
            )
            db.add(actor)
            await db.flush()
            project = await create_fixture_project(db, owner_id=actor.id)
            await ensure_legacy_bindings(db)
            dataset = Dataset(
                id=uuid.uuid4(),
                display_id="ot-" + uuid.uuid4().hex[:12],
                name="Ordinary thumbnails",
                created_by=actor.id,
            )
            connection = StorageConnection(
                id=uuid.uuid4(),
                name="B-" + uuid.uuid4().hex,
                kind="s3",
                scope="global",
                config={
                    "endpoint": "https://objects.example.test",
                    "bucket": "saved-b",
                    "base_prefix": "公司/root",
                    "use_ssl": True,
                },
                secret_enc=b"encrypted-current-credentials",
                revision=2,
            )
            location = StorageLocation(
                id=uuid.uuid4(),
                name="B-" + uuid.uuid4().hex,
                kind="s3",
                config={},
                config_revision=2,
                connection_id=connection.id,
            )
            target = Target(
                location.id,
                2,
                2,
                "https://objects.example.test",
                "saved-b",
                "公司/root/_aap_managed/datasets/",
                purpose="datasets",
            )
            item = DatasetItem(
                id=uuid.uuid4(),
                dataset_id=dataset.id,
                file_name="saved.png",
                file_path=target.key_prefix + "originals/" + uuid.uuid4().hex,
                file_type="image",
                width=32,
                height=16,
            )
            db.add_all([dataset, connection])
            await db.flush()
            db.add_all([item, location])
            await db.flush()
            asset = DatasetAsset(
                id=uuid.uuid4(),
                dataset_item_id=item.id,
                media_type="image",
                verification_state="verified",
                checksum_algorithm="sha256",
                checksum_value=value.sha,
                size_bytes=(
                    media.THUMBNAIL_MAX_SOURCE_BYTES + 1
                    if oversized
                    else len(value.raw)
                ),
            )
            db.add(asset)
            await db.flush()
            db.add(
                DatasetAssetLocation(
                    asset_id=asset.id,
                    location_id=location.id,
                    bucket=target.bucket,
                    object_key=item.file_path,
                    storage_ref=dataset_storage.make_ref(target, 2, item.file_path),
                    role="managed_original",
                    reference_state="present",
                    etag='"old-write-etag"',
                )
            )
            await db.execute(
                update(StorageBinding)
                .where(StorageBinding.purpose == "media-cache")
                .values(location_id=location.id, bucket=target.bucket, generation=2)
            )
            task_id = uuid.uuid4()
            if not standalone:
                await LabelingE2EFixtureWrites(db).insert_tasks(
                    [
                        Task(
                            id=task_id,
                            project_id=project.id,
                            display_id="ot-" + uuid.uuid4().hex[:12],
                            file_name=item.file_name,
                            file_path=item.file_path,
                            file_type="image",
                            dataset_item_id=item.id,
                        )
                    ]
                )
            await db.commit()
            value.dataset_id, value.item_id, value.task_id = (
                dataset.id,
                item.id,
                task_id,
            )
            value.asset_id, value.location_id, value.connection_id = (
                asset.id,
                location.id,
                connection.id,
            )
            value.key = item.file_path
            value.objects[target.bucket, value.key] = value.raw
            value.source_bucket, value.source_key = target.bucket, value.key
            if standalone:
                allocation = await managed_original_creation.allocate_task(
                    db,
                    project_id=project.id,
                    task_id=task_id,
                    logical_path="saved.png",
                    max_bytes=len(value.raw),
                    content_type="image/png",
                    requested_by=actor.id,
                    authority={},
                )
                assert await db.scalar(
                    text(
                        "SELECT yoao_labeling_v1.create_uploading_task("
                        ":task_id, :project_id, :file_name, "
                        ":file_path, :file_type, :actor_id)"
                    ),
                    {
                        "task_id": task_id,
                        "project_id": project.id,
                        "file_name": "saved.png",
                        "file_path": allocation["object_key"],
                        "file_type": "image",
                        "actor_id": actor.id,
                    },
                )
                await db.commit()
                value.original_keys = {
                    (allocation["staging_bucket"], allocation["staging_key"]),
                    (allocation["bucket"], allocation["object_key"]),
                }
                provider.store(
                    allocation["staging_bucket"], allocation["staging_key"], value.raw
                )
                created = await managed_original_creation.complete_direct(
                    db, allocation
                )
                task = await db.get(Task, task_id)
                await task_originals.publish(
                    db, task, allocation, created, actor_id=actor.id
                )
                await db.commit()
                value.source_bucket, value.source_key = (
                    allocation["bucket"],
                    allocation["object_key"],
                )
                value.objects[allocation["bucket"], allocation["object_key"]] = (
                    value.raw
                )
        value.ready = True
        try:
            yield value
        finally:
            if not value.active and all(task.done() for task in value.tasks):
                with value.thread_lock:
                    if not value.threads:
                        provider.close()


@pytest.fixture
async def world(managed_database, monkeypatch, request):
    mode = getattr(request, "param", "native")
    async with native_world(
        managed_database,
        monkeypatch,
        standalone=mode == "standalone",
        oversized=mode == "oversized",
        unsettled=mode == "unsettled",
    ) as value:
        yield value


async def run(
    world,
    kind="dataset_item",
    *,
    encode=media._thumbnail_from_path,
    dimensions=None,
    allow_pyramid=False,
):
    async with world.factory() as db:
        await service.generate(
            db,
            kind,
            world.item_id if kind == "dataset_item" else world.task_id,
            encode=encode,
            dimensions_probe=dimensions or AsyncMock(return_value=(32, 16)),
            max_source_bytes=media.THUMBNAIL_MAX_SOURCE_BYTES,
            allow_pyramid=allow_pyramid,
        )


async def result(world, kind="dataset_item"):
    async with world.factory() as db:
        model, identifier = (
            (DatasetItem, world.item_id)
            if kind == "dataset_item"
            else (Task, world.task_id)
        )
        row = await db.get(model, identifier)
        output = (
            await db.get(ThumbnailOutput, row.thumbnail_output_id)
            if row.thumbnail_output_id
            else None
        )
        return row, output


@pytest.mark.parametrize(
    "kind,world",
    [("dataset_item", "native"), ("task", "native")],
    indirect=["world"],
)
async def test_worker_entries_read_saved_b_and_publish_independent_b_thumbnail(
    world, monkeypatch, kind
):
    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.create_async_engine",
        lambda *args, **kwargs: world.engine,
    )
    if kind == "dataset_item":
        await media._generate_thumbnail(str(world.item_id))
    else:
        await media._generate_task_thumbnail(str(world.task_id), allow_pyramid=False)
    row, output = await result(world, kind)
    assert output is not None and output.state == "ready" and not output.uncertain_write
    assert output.owner_id == row.id and output.owner_kind == (
        "dataset_thumbnail" if kind == "dataset_item" else "task_thumbnail"
    )
    assert row.thumbnail_path == output.storage_ref["prefix"] + "thumbnail.webp"
    assert output.storage_ref["location_id"] == str(world.location_id)
    assert row.blurhash
    encoded = world.objects["saved-b", row.thumbnail_path]
    with Image.open(io.BytesIO(encoded)) as image:
        assert image.format == "WEBP" and image.size == (256, 128)
    assert all(
        endpoint == "https://objects.example.test" for _, endpoint, _ in world.calls
    )
    async with world.factory() as db:
        source = await originals.pin(db, kind, row.id)
        assert output.input_ref["source"] == thumbnail_outputs.source_facts(source)
        if kind == "dataset_item":
            url = await thumbnail_outputs.thumbnail_url(
                db, kind, row.id, row.thumbnail_path
            )
            assert url.startswith(f"/api/v1/media-cache/thumbnail/{output.id}/")
        assert not await db.scalar(select(StorageOperation.id))


@pytest.mark.parametrize("world", ["standalone"], indirect=True)
async def test_standalone_task_keeps_saved_a_source_and_saves_new_b_output(world):
    await run(world, "task")
    row, output = await result(world, "task")
    assert output and output.state == "ready"
    assert output.input_ref["source"]["bucket"] == "saved-old-annotations"
    assert output.storage_ref["bucket"] == "saved-b"
    assert output.input_ref["source"]["owner_kind"] == "task_original"
    assert output.input_ref["source"]["content_id"] == str(row.original_id)
    assert output.input_ref["source"]["sha256"] == world.sha
    assert any(
        op == "head" and params["Bucket"] == "saved-old-annotations"
        for op, _, params in world.calls
    )
    assert row.thumbnail_path.startswith("公司/root/_aap_managed/media-cache/")
    with Image.open(io.BytesIO(world.objects["saved-b", row.thumbnail_path])) as image:
        assert image.format == "WEBP" and image.size == (256, 128)


async def test_missing_dataset_dimensions_commit_then_repin_actual_facts(world):
    async with world.factory() as db:
        await db.execute(
            update(DatasetItem)
            .where(DatasetItem.id == world.item_id)
            .values(width=None, height=None)
        )
        await db.commit()
    probe = AsyncMock(return_value=(32, 16))
    await run(world, dimensions=probe)
    row, output = await result(world)
    assert (row.width, row.height) == (32, 16)
    assert output is not None
    assert output.input_ref["source"]["item_facts"]["width"] == 32
    assert output.input_ref["source"]["item_facts"]["height"] == 16


@pytest.mark.parametrize("world", ["unsettled"], indirect=True)
async def test_unsettled_output_failure_propagates_without_linking(world, monkeypatch):
    from app.services import storage_access

    failure = storage_access.mark_unsettled(OSError("output close unconfirmed"))

    def uncertain_write(*_args, **_kwargs):
        raise ExceptionGroup("output", [failure])

    monkeypatch.setattr(thumbnail_outputs, "_write", uncertain_write)

    with pytest.raises(ExceptionGroup) as rejected:
        await run(world)

    assert storage_access.failure_unsettled(rejected.value)
    row, output = await result(world)
    assert row.thumbnail_output_id is None
    assert output is None
    async with world.factory() as db:
        retained = await db.scalar(select(ThumbnailOutput))
        assert retained.state == "failed" and retained.uncertain_write is True
        assert await db.scalar(
            select(StorageOperation.id).where(
                StorageOperation.purpose == thumbnail_outputs.purpose(retained.id)
            )
        )


async def test_regeneration_retires_saved_previous_owner_without_deleting_bytes(world):
    await run(world)
    row, first = await result(world)
    old_path = row.thumbnail_path
    await run(world)
    row, second = await result(world)
    assert second and second.id != first.id
    async with world.factory() as db:
        previous = await db.get(ThumbnailOutput, first.id)
        assert previous.state == "retiring" and previous.retired_at is not None
    assert world.objects["saved-b", old_path]
    assert world.objects["saved-b", row.thumbnail_path]


@pytest.mark.parametrize("race", ["source", "directory", "archive", "busy", "retiring"])
async def test_final_source_and_business_race_leaves_durable_unattached_candidate(
    world, monkeypatch, race
):
    create = thumbnail_outputs.create
    blocker = world.factory()

    async def changed(*args, **kwargs):
        output = await create(*args, **kwargs)
        async with world.factory() as db:
            if race == "source":
                await db.execute(
                    update(DatasetAssetLocation)
                    .where(DatasetAssetLocation.asset_id == world.asset_id)
                    .values(reference_state="changed")
                )
            elif race == "archive":
                await db.execute(
                    update(Dataset)
                    .where(Dataset.id == world.dataset_id)
                    .values(archived_at=thumbnail_outputs.now(), lifecycle_revision=2)
                )
            elif race == "retiring":
                await db.execute(
                    update(ThumbnailOutput)
                    .where(ThumbnailOutput.id == output.id)
                    .values(state="retiring", retired_at=thumbnail_outputs.now())
                )
            elif race == "directory":
                directory = DatasetSource(
                    id=uuid.uuid4(),
                    dataset_id=world.dataset_id,
                    location_id=world.location_id,
                    name="Accepted directory",
                    config={},
                    ownership_mode="managed_copy",
                )
                db.add(directory)
                await db.flush()
                scan = DatasetScanRun(
                    id=uuid.uuid4(), source_id=directory.id, snapshot={}
                )
                db.add(scan)
                await db.flush()
                db.add(
                    DatasetScanEntry(
                        run_id=scan.id,
                        path_digest="1" * 64,
                        relative_path="saved.png",
                        kind="file",
                        state="observed",
                        observed={},
                    )
                )
                await db.flush()
                db.add(
                    DatasetSourceRevision(
                        source_id=directory.id,
                        path_digest="1" * 64,
                        run_id=scan.id,
                        revision_number=1,
                        item_id_at_accept=world.item_id,
                        asset_id_at_accept=world.asset_id,
                        dataset_item_id=world.item_id,
                        asset_id=world.asset_id,
                        content_sha256=world.sha,
                        ownership_mode="managed_copy",
                        owns_item=True,
                    )
                )
            await db.commit()
        if race == "busy":
            await blocker.execute(
                select(DatasetItem.id)
                .where(DatasetItem.id == world.item_id)
                .with_for_update()
            )
        return output

    monkeypatch.setattr(thumbnail_outputs, "create", changed)
    try:
        await asyncio.wait_for(run(world), 5)
    finally:
        await blocker.close()
    row, output = await result(world)
    assert output is None and row.thumbnail_path is None
    async with world.factory() as db:
        candidates = list(await db.scalars(select(ThumbnailOutput)))
        assert len(candidates) == 1
        assert candidates[0].state == ("retiring" if race == "retiring" else "ready")
        assert world.objects[
            "saved-b", thumbnail_outputs.owner(candidates[0]).key("thumbnail.webp")
        ]


@pytest.mark.parametrize("world", ["oversized"], indirect=True)
async def test_source_limit_is_512_mib_before_any_physical_capture(world):
    await run(world)
    row, output = await result(world)
    assert (
        output is None
        and row.metadata_["thumbnail_error"] == "thumbnail_source_too_large"
    )
    assert not world.calls


async def test_stopped_saved_source_gate_blocks_before_object_io(world):
    async with world.factory() as db:
        db.add(
            StorageMaintenanceGate(
                location_id=world.location_id, state="stopped", revision=1
            )
        )
        await db.commit()
    await run(world)
    assert (await result(world))[1] is None
    assert not world.calls


async def test_capture_and_output_io_never_hold_source_or_business_row_locks(world):
    loop = asyncio.get_running_loop()
    checked = []

    async def available():
        async with world.factory() as db:
            await db.execute(
                select(DatasetItem.id)
                .where(DatasetItem.id == world.item_id)
                .with_for_update(nowait=True)
            )
            await db.execute(
                select(DatasetAsset.id)
                .where(DatasetAsset.id == world.asset_id)
                .with_for_update(nowait=True)
            )
        checked.append(True)

    world.lock_probe = lambda: asyncio.run_coroutine_threadsafe(
        available(), loop
    ).result(3)
    await run(world)
    assert (await result(world))[1] is not None
    assert len(checked) >= 3


async def test_cancelled_encoding_drains_thread_before_source_close_or_output_allocation(
    world,
):
    started, release = threading.Event(), threading.Event()
    paths = []

    def encode(path):
        paths.append(path)
        started.set()
        assert release.wait(5)
        assert path.exists()
        return media._thumbnail_from_path(path)

    pending = asyncio.create_task(run(world, encode=encode))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        pending.cancel()
        await asyncio.sleep(0.02)
        assert not pending.done() and paths[0].exists()
        async with world.factory() as db:
            assert await db.scalar(select(StorageOperation.id))
            assert not await db.scalar(select(ThumbnailOutput.id))
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not paths[0].exists()
    async with world.factory() as db:
        assert not await db.scalar(select(StorageOperation.id))
        assert not await db.scalar(select(ThumbnailOutput.id))


@pytest.mark.parametrize("world", ["native"], indirect=True)
async def test_linked_task_preview_keeps_read_semantics_on_archived_dataset(world):
    async with world.factory() as db:
        await db.execute(
            update(Dataset)
            .where(Dataset.id == world.dataset_id)
            .values(archived_at=thumbnail_outputs.now(), lifecycle_revision=2)
        )
        await db.commit()
    await run(world, "task")
    row, output = await result(world, "task")
    assert output is not None and output.input_ref["source"]["bucket"] == "saved-b"
    with Image.open(io.BytesIO(world.objects["saved-b", row.thumbnail_path])) as image:
        assert image.format == "WEBP" and image.size == (256, 128)
    await run(world, "dataset_item")
    assert (await result(world, "dataset_item"))[1] is None
