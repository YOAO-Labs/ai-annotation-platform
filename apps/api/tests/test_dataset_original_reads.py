"""Saved B facts and current transport remain separate across credential rotation."""

from contextlib import contextmanager
from dataclasses import asdict
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.dataset_asset import DatasetAssetLocation, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.services import dataset_storage, original_sources as sources
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_s3_target import Target
from tests.test_managed_original_reader import Provider
from tests.test_original_sources import dataset_rows


@pytest.fixture
async def saved_db(test_engine, monkeypatch):
    schema = "dataset_reads_" + uuid.uuid4().hex
    tables = (
        "storage_connections",
        "storage_locations",
        "storage_bindings",
        "storage_maintenance_gates",
        "storage_operations",
        "storage_migration_read_publications",
        "storage_migration_read_routes",
        "dataset_items",
        "dataset_assets",
        "dataset_asset_locations",
        "dataset_auxiliary_originals",
        "tasks",
        "task_dataset_item_links",
        "managed_original_retirements",
    )
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        source = await connection.scalar(text("SELECT current_schema()"))
        for table in tables:
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
    monkeypatch.setattr(
        "app.services.connector_guard.get_allowlist",
        AsyncMock(return_value=["objects.example.test"]),
    )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await ensure_legacy_bindings(db)
            await db.commit()
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


async def saved_item(db):
    item, asset, replica, task = await dataset_rows(db)
    connection = StorageConnection(
        id=uuid.uuid4(),
        name="saved-B-" + uuid.uuid4().hex,
        kind="s3",
        scope="global",
        config={
            "endpoint": "https://objects.example.test",
            "bucket": "saved-originals",
            "base_prefix": "tenant",
            "use_ssl": True,
        },
        secret_enc=b"encrypted-original-credential",
        revision=2,
    )
    db.add(connection)
    location = StorageLocation(
        id=uuid.uuid4(),
        name="saved-B-" + uuid.uuid4().hex,
        kind="s3",
        config={},
        config_revision=2,
        connection_id=connection.id,
        enabled=True,
    )
    db.add(location)
    await db.flush()
    target = Target(
        location.id,
        2,
        2,
        "https://objects.example.test",
        "saved-originals",
        "tenant/_aap_managed/datasets/",
        purpose="datasets",
    )
    key = target.key_prefix + "originals/" + uuid.uuid4().hex
    item.file_path = task.file_path = key
    replica.location_id = location.id
    replica.bucket = target.bucket
    replica.object_key = key
    replica.storage_ref = dataset_storage.make_ref(target, 2, key)
    await db.commit()
    return item, asset, replica, task, location, connection


@pytest.mark.parametrize("bounded", [False, True])
async def test_saved_b_facts_stay_identical_when_current_credentials_rotate(
    saved_db,
    monkeypatch,
    bounded,
):
    db = saved_db
    _item, _asset, replica, task, location, connection = await saved_item(db)

    def forbidden(*_args, **_kwargs):
        pytest.fail("metadata resolution must not access DNS or decrypt credentials")

    monkeypatch.setattr("app.services.connector_guard.assert_host_allowed", forbidden)
    monkeypatch.setattr("app.services.sources.s3_reference.decrypt_secret", forbidden)

    async def resolve():
        if bounded:
            return await sources.CachedResolver(db, lock=True, max_links=8).task(
                task.id
            )
        return await sources.resolve_task(db, task.id, lock=True)

    before = await resolve()
    frozen = asdict(before)
    assert before.endpoint_url == "https://objects.example.test"
    assert before.object_key == replica.object_key
    assert before.location_revision == before.location_facts.config_revision == 2
    connection.secret_enc = b"encrypted-rotated-credential"
    connection.revision += 1
    location.config_revision += 1
    await db.commit()
    after = await resolve()
    assert after == before and asdict(after) == frozen
    await db.commit()
    target = await sources.read_target(db, after)
    assert target.config_revision == location.config_revision == 3
    assert target._access.secret_enc == b"encrypted-rotated-credential"
    assert b"encrypted" not in repr(frozen).encode()
    assert (
        await db.scalar(select(DatasetAssetLocation.storage_ref)) == replica.storage_ref
    )


async def test_public_reader_uses_saved_b_sidecar_and_closes_its_context(
    saved_db,
    monkeypatch,
):
    from app.services import managed_original_reader as reader
    from app.services.sources.s3_reference import S3Access

    db = saved_db
    _item, _asset, _replica, task, _location, _connection = await saved_item(db)
    source = await sources.resolve_task(db, task.id)
    await db.commit()
    provider = Provider()
    provider.meta.endpoint_url = source.endpoint_url
    provider.raw = provider.actual = b"saved"
    contexts = []

    @contextmanager
    def client(access, *, read_timeout=None):
        contexts.append((access.secret_enc, read_timeout))
        try:
            yield provider
        finally:
            provider.close()

    monkeypatch.setattr(S3Access, "client", client)
    monkeypatch.setattr(
        reader, "create_client", lambda **_: pytest.fail("A client used")
    )
    options = await sources.read_options(db, source)
    assert not contexts  # Metadata capture does not open the provider.
    stage = await reader.capture(
        reader.ObjectSpec(
            source.endpoint_url,
            source.bucket,
            source.object_key,
            source.sha256,
            source.size_bytes,
        ),
        max_bytes=32,
        timeout_seconds=5,
        **options,
    )
    try:
        assert stage.file.read() == b"saved"
        assert contexts == [(b"encrypted-original-credential", 30)]
        assert provider.closed == 1
        assert all(
            params["Bucket"] == source.bucket and params["Key"] == source.object_key
            for operation, params in provider.calls
            if operation in {"get", "head"}
        )
    finally:
        stage.close()


@pytest.mark.parametrize("change", ["endpoint", "bucket", "base_prefix", "disabled"])
async def test_saved_b_cannot_follow_a_redirected_or_disabled_location(
    saved_db, change
):
    db = saved_db
    _item, _asset, _replica, task, location, connection = await saved_item(db)
    source = await sources.resolve_task(db, task.id)
    await db.commit()
    if change == "disabled":
        location.enabled = False
    else:
        connection.config = {
            **connection.config,
            change: {
                "endpoint": "https://other.example.test",
                "bucket": "other-bucket",
                "base_prefix": "other",
            }[change],
        }
    await db.commit()
    with pytest.raises(ContentError, match="location_unavailable"):
        await sources.read_target(db, source)
    with pytest.raises(ContentError, match="location_unavailable"):
        await sources.resolve_task(db, task.id)


async def test_legacy_sidecar_does_not_change_old_callback_contract(saved_db):
    db = saved_db
    _item, _asset, _replica, task = await dataset_rows(db)
    await db.commit()
    source = await sources.resolve_task(db, task.id)
    await db.commit()
    assert source.location_id == LEGACY_LOCATION_ID and source.read_route is None
    assert await sources.read_options(db, source) == {}
