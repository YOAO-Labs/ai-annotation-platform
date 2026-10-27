"""Standalone Task owners pin A/B addresses without changing frozen evidence."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.db.models.task_original import TaskOriginal
from app.services import annotations_storage, managed_original_creation as creation
from app.services import original_sources as sources, task_originals
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.source_readers.s3 import S3Access
from tests.test_task_originals import (
    allocated,
    original_client as original_client,
    original_db as original_db,
)


async def registered_target(db):
    connection = StorageConnection(
        id=uuid.uuid4(),
        name="Owned annotation test target",
        kind="s3",
        scope="global",
        revision=1,
        config={
            "endpoint": "https://annotations.example.test",
            "bucket": "owned-annotations",
            "base_prefix": "公司/项目",
        },
        secret_enc=b"owned-credential-one",
    )
    location = StorageLocation(
        id=uuid.uuid4(),
        name="Owned annotation location",
        kind="s3",
        enabled=True,
        config_revision=1,
        connection_id=connection.id,
        config=deepcopy(connection.config),
    )
    db.add_all([connection, location])
    await db.flush()
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "annotations")
        .values(
            location_id=location.id, bucket=connection.config["bucket"], generation=2
        )
    )
    await db.commit()
    return connection, location


async def complete(db, task, allocation, client, data=b"original"):
    client.store(allocation["staging_bucket"], allocation["staging_key"], data)
    created = await creation.complete_direct(db, allocation)
    owner = await task_originals.publish(db, task, allocation, created)
    await db.commit()
    return owner


async def test_old_inflight_a_new_b_back_to_a_keep_saved_addresses_and_current_credentials(
    original_db, original_client, monkeypatch
):
    db, client = original_db, original_client
    old_task, old_allocation = await allocated(db, legacy=True)
    old_configuration = deepcopy(old_allocation["configuration"])
    connection, location = await registered_target(db)
    from tests.test_managed_original_creation import Client

    external = Client()
    external.meta = SimpleNamespace(endpoint_url=connection.config["endpoint"])
    opened = []

    @contextmanager
    def current_client(access, **options):
        opened.append(access.secret_enc)
        assert access.endpoint == external.meta.endpoint_url
        try:
            yield external
        finally:
            external.close()

    monkeypatch.setattr(S3Access, "client", current_client)
    old_owner = await complete(db, old_task, old_allocation, client)
    assert old_owner.location_id == LEGACY_LOCATION_ID and old_owner.storage_ref is None
    new_task, new_allocation = await allocated(db)
    new_owner = await complete(db, new_task, new_allocation, client)
    assert new_owner.location_id == location.id
    assert new_owner.storage_ref["schema"] == 2
    assert new_owner.object_key == annotations_storage.managed_prefix(
        "公司/项目"
    ) + task_originals.key_for(new_task.id, new_owner.id)
    assert new_owner.sha256 == old_owner.sha256
    source = await sources.resolve_task(db, new_task.id, lock=True)
    frozen = source
    assert set(vars(source.ownership_facts)) == set(task_originals.FACTS)
    saved_ref = deepcopy(new_owner.storage_ref)
    await db.commit()
    connection.secret_enc = b"owned-credential-two"
    connection.revision = 2
    location.config_revision = 2
    await db.commit()
    assert await sources.resolve_task(db, new_task.id, lock=True) == frozen
    await db.commit()
    target = await sources.read_target(db, frozen)
    assert target.connection_revision == 2
    with target.client() as actual:
        assert (
            actual.objects[new_owner.bucket, new_owner.object_key]["data"]
            == b"original"
        )
    assert opened[-1] == b"owned-credential-two" and new_owner.storage_ref == saved_ref
    from app.services import managed_original_reader as reader, storage_access

    async with await storage_access.acquire(
        db, frozen.location_id, "test_task_original_read"
    ):
        stage = await reader.capture(
            reader.ObjectSpec(
                frozen.endpoint_url,
                frozen.bucket,
                frozen.object_key,
                frozen.sha256,
                frozen.size_bytes,
            ),
            max_bytes=frozen.size_bytes,
            timeout_seconds=30,
            **(await sources.read_options(db, frozen)),
        )
        try:
            assert stage.file.read() == b"original" and stage.sha256 == frozen.sha256
        finally:
            stage.close()
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "annotations")
        .values(
            location_id=LEGACY_LOCATION_ID,
            bucket=annotations_storage.storage_service.bucket,
            generation=3,
        )
    )
    await db.commit()
    back_task, back_allocation = await allocated(db)
    back_owner = await complete(db, back_task, back_allocation, client)
    assert back_owner.location_id == LEGACY_LOCATION_ID
    assert back_owner.storage_ref["binding_generation"] == 3
    assert await sources.resolve_task(db, new_task.id) == frozen
    assert (await creation._read(db, old_owner.id))[
        "configuration"
    ] == old_configuration
    target = await sources.read_target(db, frozen)
    with target.client():
        pass
    assert opened[-1] == b"owned-credential-two"
    assert not client.uploads and not external.uploads
    assert all(body.closed for body in client.bodies + external.bodies)


async def test_saved_reference_is_separately_immutable_and_part_of_idempotency(
    original_db, original_client
):
    db = original_db
    task, allocation = await allocated(db)
    owner = await complete(db, task, allocation, original_client)
    assert len(task_originals.FACTS) == 13
    values = {name: getattr(owner, name) for name in task_originals.FACTS}
    values["storage_ref"] = deepcopy(owner.storage_ref)
    values["storage_ref"]["binding_generation"] += 1
    with pytest.raises(ContentError, match="creation_conflict"):
        await task_originals._persist(db, values)
    with pytest.raises(IntegrityError, match="immutable"):
        async with db.begin_nested():
            await db.execute(
                update(TaskOriginal)
                .where(TaskOriginal.id == owner.id)
                .values(storage_ref=None)
            )
    assert (
        await db.scalar(
            select(TaskOriginal.storage_ref).where(TaskOriginal.id == owner.id)
        )
        == owner.storage_ref
    )


@pytest.mark.parametrize("fallback", ["url", "image"])
def test_rooted_standalone_bypass_refuses_before_physical_client(monkeypatch, fallback):
    from app.services import storage
    from app.workers import tasks

    task = SimpleNamespace(
        id=uuid.uuid4(), original_id=uuid.uuid4(), dataset_item_id=None
    )
    task.file_path = annotations_storage.managed_prefix(
        "owned"
    ) + task_originals.key_for(task.id, task.original_id)

    def forbidden(*args, **kwargs):
        pytest.fail(
            "a rooted standalone original cannot use the legacy physical client"
        )

    monkeypatch.setattr(storage, "StorageService", forbidden)
    monkeypatch.setattr(storage.storage_service, "generate_download_url", forbidden)
    with pytest.raises(ContentError, match="location_unavailable"):
        (storage.resolve_task_url if fallback == "url" else tasks._load_task_image)(
            task
        )


def test_legacy_a_task_fallback_keeps_existing_signed_key(monkeypatch):
    from app.services import storage

    task = SimpleNamespace(
        id=uuid.uuid4(), original_id=uuid.uuid4(), dataset_item_id=None
    )
    task.file_path = task_originals.key_for(task.id, task.original_id)
    calls = []
    monkeypatch.setattr(
        storage.storage_service,
        "generate_download_url",
        lambda key, **options: calls.append((key, options)) or "saved-url",
    )
    assert storage.resolve_task_url(task) == "saved-url"
    assert calls == [
        (task.file_path, {"bucket": storage.storage_service.bucket, "audience": "ml"})
    ]


async def test_reachable_non_image_secondary_fallback_cannot_read_rooted_task(
    monkeypatch,
):
    from app.services import secondary_inference, storage

    task = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        original_id=uuid.uuid4(),
        dataset_item_id=None,
        file_type="point_cloud",
    )
    task.file_path = annotations_storage.managed_prefix(
        "owned"
    ) + task_originals.key_for(task.id, task.original_id)
    monkeypatch.setattr(
        secondary_inference,
        "prepare_secondary_request",
        lambda *args, **kwargs: ({}, {}),
    )

    def forbidden(*args, **kwargs):
        pytest.fail(
            "the reachable non-image bypass must reject before constructing transport"
        )

    monkeypatch.setattr(storage, "StorageService", forbidden)
    with pytest.raises(ContentError, match="location_unavailable"):
        await secondary_inference._run_secondary_inference(
            None,
            annotation=None,
            task=task,
            backend=SimpleNamespace(),
            write_target="attributes",
            write_keys=None,
            label=None,
            model_id=None,
            model_variants=None,
            params=None,
            task_type=None,
            prompt=None,
            class_filter=None,
            pad=0.08,
            user_id=uuid.uuid4(),
        )
