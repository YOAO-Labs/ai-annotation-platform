"""Saved B facts and current transport remain separate across credential rotation."""

from contextlib import contextmanager
from dataclasses import asdict
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import select

from app.db.models.dataset_asset import DatasetAssetLocation, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.services import dataset_storage, original_sources as sources
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_s3_target import Target
from tests.test_original_sources import dataset_rows
from tests.test_task_originals import current_original_session


@pytest.fixture
async def saved_db(managed_database, monkeypatch):
    monkeypatch.setattr(
        "app.services.connector_guard.get_allowlist",
        AsyncMock(return_value=["objects.example.test"]),
    )
    async with managed_database() as engine:
        async with current_original_session(engine) as db:
            await ensure_legacy_bindings(db)
            await db.commit()
            yield db


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
    await db.flush()
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
    verified_at = replica.verified_at
    await db.delete(replica)
    await db.flush()
    replica = DatasetAssetLocation(
        asset_id=asset.id,
        location_id=location.id,
        bucket=target.bucket,
        object_key=key,
        role="managed_original",
        reference_state="present",
        etag="historical-etag",
        verified_at=verified_at,
        storage_ref=dataset_storage.make_ref(target, 2, key),
    )
    db.add(replica)
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
    monkeypatch.setattr("app.services.source_readers.s3.decrypt_secret", forbidden)

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
    assert target.connection_revision == connection.revision == 3
    assert target.is_registered is True and target._access is None
    assert b"encrypted" not in repr(frozen).encode()
    assert (
        await db.scalar(select(DatasetAssetLocation.storage_ref)) == replica.storage_ref
    )


async def test_public_reader_uses_saved_b_sidecar_and_closes_its_context(
    saved_db,
    monkeypatch,
):
    import io
    from app.services import managed_original_reader as reader, saved_media_io

    db = saved_db
    _item, asset, _replica, task, _location, _connection = await saved_item(db)
    source = await sources.resolve_task(db, task.id)
    await db.commit()
    contexts = []
    closed = []

    class FakeClient:
        @contextmanager
        def download(self, family, action, payload):
            from datetime import datetime, timezone

            contexts.append((family, action, payload))

            class _Stream(io.BytesIO):
                metadata = {
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                    "ContentLength": 5,
                    "ETag": '"saved-etag"',
                    "LastModified": datetime(
                        2026, 1, 1, tzinfo=timezone.utc
                    ).isoformat(),
                }

            stream = _Stream(b"saved")
            try:
                yield stream
            finally:
                stream.close()
                closed.append(stream.closed)

    monkeypatch.setattr(
        saved_media_io, "saved_io_client", lambda *a, **kw: FakeClient()
    )
    monkeypatch.setattr(
        reader, "create_client", lambda **_: pytest.fail("A client used")
    )
    options = await sources.read_options(db, source)
    assert not contexts
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
        assert len(contexts) == 1
        assert contexts[0][0] == "media"
        assert contexts[0][1] == "original-read"
        assert contexts[0][2]["id"] == str(asset.id)
        assert contexts[0][2]["kind"] == "dataset_asset"
        assert closed == [True]
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
