"""Canonical A/B sources, durable thumbnail candidates and short publication locks."""

import asyncio
from contextlib import contextmanager
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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.dataset_source import DatasetSourceRevision
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.db.models.task import Task
from app.db.models.task_original import TaskOriginal
from app.db.models.thumbnail_output import ThumbnailOutput
from app.services import dataset_storage, image_pyramid_originals as originals
from app.services import (
    managed_original_reader as reader,
    ordinary_thumbnails as service,
)
from app.services import thumbnail_outputs
from app.services import storage as storage_module
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_original_retirement import managed_storage_identity
from app.services.managed_s3_target import Target
from app.services.source_readers.s3 import S3Access
from app.services.storage import storage_service
from app.workers import media
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


@pytest.fixture
async def world(test_engine, monkeypatch):
    schema = "ordinary_thumbnails_" + uuid.uuid4().hex
    tables = (
        "storage_connections",
        "storage_locations",
        "storage_bindings",
        "storage_maintenance_gates",
        "storage_operations",
        "datasets",
        "dataset_items",
        "dataset_assets",
        "dataset_asset_locations",
        "tasks",
        "task_originals",
        "task_dataset_item_links",
        "dataset_source_revisions",
        "thumbnail_outputs",
    )
    async with test_engine.begin() as connection:
        original_schema = await connection.scalar(text("SELECT current_schema()"))
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        for table in tables:
            await connection.execute(
                text(
                    f'CREATE TABLE "{schema}"."{table}" (LIKE "{original_schema}"."{table}" INCLUDING ALL)'
                )
            )
        await connection.execute(
            text(
                f'CREATE TRIGGER trg_thumbnail_output_owner BEFORE UPDATE OR DELETE ON "{schema}".thumbnail_outputs FOR EACH ROW EXECUTE FUNCTION "{original_schema}".guard_thumbnail_output_owner()'
            )
        )
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    value = SimpleNamespace(
        engine=engine,
        factory=async_sessionmaker(engine, expire_on_commit=False),
        objects={},
        calls=[],
        clients=[],
        bodies=[],
        lock_probe=None,
    )
    try:
        raw = io.BytesIO()
        with Image.new("RGB", (32, 16), (30, 60, 90)) as image:
            image.save(raw, format="PNG")
        value.raw = raw.getvalue()
        raw.close()
        value.sha = hashlib.sha256(value.raw).hexdigest()
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

        class OwnedStorage(storage_module.StorageService):
            def __init__(self, **kwargs):
                self.client = Client(value, storage_service.client.meta.endpoint_url)

        monkeypatch.setattr(storage_module, "StorageService", OwnedStorage)
        monkeypatch.setattr(
            reader,
            "create_client",
            lambda **kwargs: Client(value, storage_service.client.meta.endpoint_url),
        )
        monkeypatch.setattr(settings, "image_pyramid_auto_generate", False)
        async with value.factory() as db:
            await ensure_legacy_bindings(db)
            dataset = Dataset(
                id=uuid.uuid4(),
                display_id="ot-" + uuid.uuid4().hex[:12],
                name="Ordinary thumbnails",
                created_by=uuid.uuid4(),
            )
            item = DatasetItem(
                id=uuid.uuid4(),
                dataset_id=dataset.id,
                file_name="saved.png",
                file_path="pending",
                file_type="image",
                width=32,
                height=16,
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
            db.add_all([dataset, item, connection, location])
            await db.flush()
            target = Target(
                location.id,
                2,
                2,
                "https://objects.example.test",
                "saved-b",
                "公司/root/_aap_managed/datasets/",
                purpose="datasets",
            )
            item.file_path = target.key_prefix + "originals/" + uuid.uuid4().hex
            asset = DatasetAsset(
                id=uuid.uuid4(),
                dataset_item_id=item.id,
                media_type="image",
                verification_state="verified",
                checksum_algorithm="sha256",
                checksum_value=value.sha,
                size_bytes=len(value.raw),
            )
            replica = DatasetAssetLocation(
                asset_id=asset.id,
                location_id=location.id,
                bucket=target.bucket,
                object_key=item.file_path,
                storage_ref=dataset_storage.make_ref(target, 2, item.file_path),
                role="managed_original",
                reference_state="present",
                etag='"old-write-etag"',
            )
            task = Task(
                id=uuid.uuid4(),
                project_id=uuid.uuid4(),
                display_id="ot-" + uuid.uuid4().hex[:12],
                file_name=item.file_name,
                file_path=item.file_path,
                file_type="image",
                dataset_item_id=item.id,
            )
            db.add_all([asset, replica, task])
            await db.execute(
                update(StorageBinding)
                .where(StorageBinding.purpose == "media-cache")
                .values(location_id=location.id, bucket=target.bucket, generation=2)
            )
            await db.commit()
            value.dataset_id, value.item_id, value.task_id = (
                dataset.id,
                item.id,
                task.id,
            )
            value.asset_id, value.location_id, value.connection_id = (
                asset.id,
                location.id,
                connection.id,
            )
            value.key = item.file_path
            value.objects[target.bucket, value.key] = value.raw
        yield value
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        assert all(client.closed for client in value.clients)
        assert all(body.closed for body in value.bodies)


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


@pytest.mark.parametrize("kind", ["dataset_item", "task"])
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


async def test_standalone_task_keeps_saved_a_source_and_saves_new_b_output(world):
    async with world.factory() as db:
        task = await db.get(Task, world.task_id)
        identifier = uuid.uuid4()
        key = f"__aap_task_originals__/{task.id}/{identifier}/payload"
        owner = TaskOriginal(
            id=identifier,
            creation_id=identifier,
            task_id=task.id,
            project_id=task.project_id,
            location_id=LEGACY_LOCATION_ID,
            storage_identity=managed_storage_identity(
                storage_service.client.meta.endpoint_url, "saved-old-annotations"
            ),
            bucket="saved-old-annotations",
            object_key=key,
            sha256=world.sha,
            size_bytes=len(world.raw),
            etag='"old-etag"',
            created_at=thumbnail_outputs.now(),
        )
        task.dataset_item_id, task.original_id, task.file_path = None, identifier, key
        db.add(owner)
        await db.commit()
        world.objects[owner.bucket, key] = world.raw
    await run(world, "task")
    row, output = await result(world, "task")
    assert output and output.state == "ready"
    assert output.input_ref["source"]["bucket"] == "saved-old-annotations"
    assert output.storage_ref["bucket"] == "saved-b"
    assert any(
        op == "head" and params["Bucket"] == "saved-old-annotations"
        for op, _, params in world.calls
    )
    assert row.thumbnail_path.startswith("公司/root/_aap_managed/media-cache/")


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
    probe.assert_awaited_once()


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
                    update(DatasetAsset)
                    .where(DatasetAsset.id == world.asset_id)
                    .values(checksum_value="0" * 64)
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
                db.add(
                    DatasetSourceRevision(
                        source_id=uuid.uuid4(),
                        path_digest="1" * 64,
                        run_id=uuid.uuid4(),
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


async def test_source_limit_is_512_mib_before_any_physical_capture(world):
    async with world.factory() as db:
        await db.execute(
            update(DatasetAsset)
            .where(DatasetAsset.id == world.asset_id)
            .values(size_bytes=media.THUMBNAIL_MAX_SOURCE_BYTES + 1)
        )
        await db.commit()
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


@pytest.mark.parametrize(
    "kind,allow,automatic,expected",
    [
        ("dataset_item", True, True, True),
        ("dataset_item", True, False, False),
        ("task", True, True, True),
        ("task", True, False, False),
        ("task", False, True, False),
    ],
)
async def test_existing_large_image_and_allow_pyramid_branches_remain(
    world, monkeypatch, kind, allow, automatic, expected
):
    from app.workers import image_pyramid

    calls = []
    monkeypatch.setattr(settings, "image_pyramid_auto_generate", automatic)
    monkeypatch.setattr(
        image_pyramid,
        "enqueue_image_pyramid",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    if kind == "dataset_item":
        async with world.factory() as db:
            await db.execute(
                update(DatasetItem)
                .where(DatasetItem.id == world.item_id)
                .values(width=8193, height=6145)
            )
            await db.commit()
    if not allow:
        monkeypatch.setattr(
            "app.services.storage.StorageService._logical_image_dimensions",
            lambda image: (8193, 6145),
        )
    probe = AsyncMock(return_value=(8193, 6145))
    await run(world, kind, dimensions=probe, allow_pyramid=allow)
    assert bool(calls) == expected
    assert (await result(world, kind))[1] is None
    assert not any(op == "put" for op, _, _ in world.calls)
    if kind == "task" and allow:
        probe.assert_awaited_once()
    if not allow:
        probe.assert_not_awaited()
    if expected:
        assert calls[0][0][0] == kind
        assert calls[0][1].get("force", False) == (kind == "task")


async def test_linked_task_preview_keeps_read_semantics_on_archived_dataset(world):
    async with world.factory() as db:
        await db.execute(
            update(Dataset)
            .where(Dataset.id == world.dataset_id)
            .values(archived_at=thumbnail_outputs.now(), lifecycle_revision=2)
        )
        await db.commit()
    await run(world, "task")
    assert (await result(world, "task"))[1] is not None
    await run(world, "dataset_item")
    assert (await result(world, "dataset_item"))[1] is None
