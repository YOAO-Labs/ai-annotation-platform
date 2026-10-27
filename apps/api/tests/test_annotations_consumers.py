"""The three existing attachment families keep logical keys and saved transport."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.comment_attachment_upload import CommentAttachmentUpload
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.guide_asset_upload import GuideAssetUpload
from app.db.models.raster_mask_object import RasterMaskObject
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.services import annotations_storage, storage_access
from app.services import comment_attachment_storage as comments
from app.services import comment_attachment_uploads
from app.services import guide_asset_storage as guides
from app.services import guide_asset_uploads, raster_mask_objects
from app.services import raster_object_storage as raster
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_s3_target import Target
from app.services.sources.s3_reference import S3Access
from tests import test_comment_attachment_storage as comment_facts
from tests import test_guide_asset_storage as guide_facts
from tests import test_raster_object_storage as raster_facts
from tests.test_comment_attachment_uploads import (
    comment as bind_comment,
    discussion as discussion,
    download as download_comment,
    upload as upload_comment,
)
from tests.test_guide_assets import (
    content as guide_content,
    project as project,
    unlink as unlink_guide,
    upload as upload_guide,
)
from tests.test_raster_mask_objects import harness as harness

CASES = [
    (comments, comment_facts.record, comment_facts.RAW, CommentAttachmentUpload),
    (guides, guide_facts.record, guide_facts.RAW, GuideAssetUpload),
    (raster, raster_facts.record, raster_facts.RAW, RasterMaskObject),
]
ROOT = "公司/[a](b)/_aap_managed/annotations/"
ENDPOINT = "https://saved-annotations.example.test"


def target_for(provider, *, secret=b"current", fail_close=False):
    @contextmanager
    def client(**kwargs):
        provider.seen.append(secret)
        try:
            yield provider
        finally:
            provider.closed += 1
            if fail_close:
                raise OSError("private close outcome")

    return Target(
        uuid.uuid4(),
        1,
        1,
        ENDPOINT,
        "saved-b",
        ROOT,
        purpose="annotations",
        _access=SimpleNamespace(client=client),
    )


def provider():
    result = raster_facts.Provider()
    result.meta.endpoint_url = ENDPOINT
    result.seen, result.closed = [], 0
    return result


@pytest.mark.parametrize("family,make_record,raw,model", CASES)
def test_schema2_roundtrip_uses_physical_root_and_keeps_public_logical_key(
    family,
    make_record,
    raw,
    model,
):
    row, physical = make_record(), provider()
    logical = row.storage_ref["object_key"]
    target = target_for(physical)
    row.storage_ref = annotations_storage.make_ref(target, 2, ROOT + logical)
    receipt = family.snapshot(row)
    family.run_io(target, object(), "write", receipt, raw)
    actual = family.run_io(target, object(), "read", receipt)
    assert actual == (raster_facts.RLE if family is raster else raw)
    assert set(physical.objects) == {("saved-b", ROOT + logical)}
    assert physical.closed == 2 and all(body.closed for body in physical.bodies)
    if family is guides:
        assert guides.entry(receipt)["key"] == logical
    elif family is comments:
        assert comment_attachment_uploads.attachment(receipt).storage_key == logical
        assert receipt.expires_at == row.expires_at
    else:
        assert raster.reference(receipt) == raster_facts.portable()
    family.run_io(target, object(), "delete", receipt)
    assert not physical.objects


@pytest.mark.parametrize("family,make_record,raw,model", CASES)
def test_family_rejects_other_logical_suffix_and_unknown_client_close(
    family,
    make_record,
    raw,
    model,
):
    row, physical = make_record(), provider()
    logical = row.storage_ref["object_key"]
    target = target_for(physical, fail_close=True)
    row.storage_ref = annotations_storage.make_ref(target, 2, ROOT + logical)
    with pytest.raises(ValueError) as caught:
        family.run_io(target, object(), "write", row, raw)
    assert caught.value.code.endswith("_unavailable")
    assert caught.value.storage_operation_unsettled
    assert physical.objects[("saved-b", ROOT + logical)] == raw
    assert all(body.closed for body in physical.bodies)
    foreign = guide_facts.KEY if family is not guides else comment_facts.KEY
    row.storage_ref = annotations_storage.make_ref(target, 2, ROOT + foreign)
    before = len(physical.calls)
    with pytest.raises(ValueError):
        family.run_io(target, object(), "read", row)
    assert len(physical.calls) == before


@pytest.mark.parametrize("family,make_record,raw,model", CASES)
async def test_schema1_transport_adapter_does_not_rewrite_receipt(
    monkeypatch,
    family,
    make_record,
    raw,
    model,
):
    row = make_record()
    saved = deepcopy(row.storage_ref)
    capture = []

    async def current(db, ref):
        capture.append(ref)
        return "transport"

    monkeypatch.setattr(annotations_storage, "target_for_ref", current)
    assert await family.io_target(object(), row) == "transport"
    assert row.storage_ref == saved and saved["schema"] == 1
    assert capture == [{**saved, "schema": 2, "key_prefix": ""}]


@pytest.fixture
async def consumer_db(test_engine):
    schema = "annotations_consumers_" + uuid.uuid4().hex
    tables = (
        "storage_connections",
        "storage_locations",
        "storage_bindings",
        "storage_maintenance_gates",
        "storage_operations",
        "system_settings",
        "comment_attachment_uploads",
        "guide_asset_uploads",
        "raster_mask_objects",
    )
    async with test_engine.begin() as connection:
        source = await connection.scalar(text("SELECT current_schema()"))
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
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
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await ensure_legacy_bindings(db)
            await db.commit()
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.mark.parametrize("family,make_record,raw,model", CASES)
async def test_durable_b_allocation_uses_current_credentials_after_switchback(
    consumer_db,
    monkeypatch,
    family,
    make_record,
    raw,
    model,
):
    db, physical = consumer_db, provider()
    connection = StorageConnection(
        id=uuid.uuid4(),
        name="saved B",
        kind="s3",
        scope="global",
        secret_enc=b"original-credentials",
        config={
            "endpoint": ENDPOINT,
            "bucket": "saved-b",
            "base_prefix": "公司/[a](b)",
            "use_ssl": True,
        },
    )
    location = StorageLocation(
        id=uuid.uuid4(),
        name="saved B",
        kind="s3",
        connection_id=connection.id,
        config={},
    )
    db.add(connection)
    await db.flush()
    db.add(location)
    await db.flush()
    binding = await db.get(StorageBinding, "annotations")
    binding.location_id, binding.bucket, binding.generation = location.id, "saved-b", 2
    await db.commit()
    row = make_record()
    row.storage_ref = await family.allocate(db, row)
    saved = deepcopy(row.storage_ref)
    owner = model(**vars(row), state="pending")
    db.add(owner)
    await db.flush()
    async with async_sessionmaker(db.bind)() as other:
        assert await other.get(model, row.id) is None
    await db.commit()
    async with async_sessionmaker(db.bind)() as other:
        assert (await other.get(model, row.id)).storage_ref == saved
    binding.location_id = LEGACY_LOCATION_ID
    binding.bucket, binding.generation = annotations_storage.storage_service.bucket, 3
    connection.secret_enc = b"rotated-credentials"
    connection.revision = location.config_revision = 2
    await db.commit()

    @contextmanager
    def client(access, **kwargs):
        assert not db.in_transaction()
        physical.seen.append(access.secret_enc)
        try:
            yield physical
        finally:
            physical.closed += 1

    monkeypatch.setattr(S3Access, "client", client)

    async def run(operation, *args):
        current = None

        async def acquired(permit):
            nonlocal current
            current = await family.io_target(db, row)
            assert current.config_revision == current.connection_revision == 2
            await db.commit()

        return await storage_access.run(
            db,
            location.id,
            "annotations_consumer_" + uuid.uuid4().hex,
            lambda: family.run_io(current, object(), operation, row, *args),
            on_acquired=acquired,
        )

    await run("write", raw)
    assert await run("read") == (raster_facts.RLE if family is raster else raw)
    await run("delete")
    assert physical.seen == [b"rotated-credentials"] * 3
    assert physical.closed == 3 and not physical.objects
    assert all(body.closed for body in physical.bodies)
    assert row.storage_ref == saved and saved["binding_generation"] == 2
    assert saved["object_key"] == ROOT + make_record().storage_ref["object_key"]
    current = await family.allocate(db, make_record())
    assert current["location_id"] == str(LEGACY_LOCATION_ID)
    assert current["binding_generation"] == 3 and current["key_prefix"] == ""


@pytest.fixture
async def external_b(test_engine, monkeypatch):
    """A committed target permits the real independent maintenance admission."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    physical = provider()
    async with factory() as db:
        await ensure_legacy_bindings(db)
        binding = await db.get(StorageBinding, "annotations")
        original = binding.location_id, binding.bucket, binding.generation
        connection = StorageConnection(
            id=uuid.uuid4(),
            name="consumer B " + uuid.uuid4().hex,
            kind="s3",
            scope="global",
            secret_enc=b"initial",
            config={
                "endpoint": ENDPOINT,
                "bucket": "saved-b",
                "base_prefix": "公司/[a](b)",
                "use_ssl": True,
            },
        )
        db.add(connection)
        await db.flush()
        location = StorageLocation(
            id=uuid.uuid4(),
            name="consumer B " + uuid.uuid4().hex,
            kind="s3",
            connection_id=connection.id,
            config={},
        )
        db.add(location)
        await db.flush()
        binding.location_id, binding.bucket, binding.generation = (
            location.id,
            "saved-b",
            2,
        )
        await db.commit()

    @contextmanager
    def client(access, **kwargs):
        physical.seen.append(access.secret_enc)
        try:
            yield physical
        finally:
            physical.closed += 1

    monkeypatch.setattr(S3Access, "client", client)

    async def switchback(db):
        binding = await db.get(StorageBinding, "annotations", populate_existing=True)
        binding.location_id, binding.bucket, binding.generation = (
            LEGACY_LOCATION_ID,
            annotations_storage.storage_service.bucket,
            3,
        )
        current_connection = await db.get(StorageConnection, connection.id)
        current_location = await db.get(StorageLocation, location.id)
        current_connection.secret_enc = b"rotated"
        current_connection.revision = current_location.config_revision = 2
        await db.commit()

    try:
        yield physical, switchback, factory
    finally:
        for body in physical.bodies:
            if not body.closed:
                body.close()
        physical.objects.clear()
        async with factory() as db:
            binding = await db.get(
                StorageBinding, "annotations", populate_existing=True
            )
            binding.location_id, binding.bucket, binding.generation = original
            await db.execute(
                delete(StorageOperation).where(
                    StorageOperation.location_id == location.id
                )
            )
            await db.execute(
                delete(StorageMaintenanceGate).where(
                    StorageMaintenanceGate.location_id == location.id
                )
            )
            await db.delete(await db.get(StorageLocation, location.id))
            await db.delete(await db.get(StorageConnection, connection.id))
            await db.commit()


@pytest.fixture
async def db_session(external_b, db_session):
    # The target predates the caller transaction; its metadata cleanup therefore
    # runs after the caller's SAVEPOINT and outer transaction have been released.
    yield db_session


async def test_comment_upload_download_and_existing_expiry_cleanup_follow_b(
    httpx_client,
    super_admin,
    discussion,
    db_session,
    external_b,
    monkeypatch,
):
    physical, switchback, _ = external_b
    annotation = discussion[-1]
    value = await upload_comment(httpx_client, super_admin, annotation)
    key = value["storageKey"]
    identifier = uuid.UUID(key.rsplit("/", 1)[1])
    row = await db_session.get(CommentAttachmentUpload, identifier)
    saved, deadline = deepcopy(row.storage_ref), row.expires_at
    await bind_comment(httpx_client, super_admin, annotation, [value])
    await switchback(db_session)
    response = await download_comment(httpx_client, super_admin, annotation, key)
    assert response.status_code == 200 and response.content == b"general\x00file"
    assert physical.seen[-1] == b"rotated"
    assert row.storage_ref == saved and row.expires_at == deadline
    monkeypatch.setattr(
        comment_attachment_uploads, "now", lambda: deadline + timedelta(seconds=1)
    )
    result = await comment_attachment_uploads.cleanup(
        db_session, identifiers=[identifier]
    )
    assert result["objects"] == 1 and result["failed"] == 0
    assert not physical.objects and physical.seen[-1] == b"rotated"


async def test_guide_upload_logical_entry_read_and_existing_unlink_cleanup_follow_b(
    httpx_client,
    project_admin,
    project,
    db_session,
    external_b,
):
    physical, switchback, _ = external_b
    value = await upload_guide(httpx_client, project_admin, project)
    assert len(value["key"].split("/")) == 4 and not value["key"].startswith(ROOT)
    identifier = uuid.UUID(value["key"].rsplit("/", 1)[1])
    row = await db_session.get(GuideAssetUpload, identifier)
    saved = deepcopy(row.storage_ref)
    await switchback(db_session)
    response = await guide_content(httpx_client, project_admin, project, value["key"])
    assert response.status_code == 200 and response.content == b"image\x00bytes"
    assert physical.seen[-1] == b"rotated" and row.storage_ref == saved
    assert (
        await unlink_guide(httpx_client, project_admin, project, value["key"])
    ).status_code == 200
    assert row.state == "retired"
    assert (await guide_asset_uploads.cleanup(db_session, identifiers=[identifier]))[
        "objects"
    ] == 1
    assert not physical.objects and physical.seen[-1] == b"rotated"


async def test_rle_real_publication_read_and_existing_retirement_follow_b(
    harness,
    external_b,
    monkeypatch,
):
    physical, switchback, factory = external_b
    rle, reference, data = harness.content()
    async with harness.factory() as caller:
        assert (
            await raster_mask_objects.store(
                reference, data, db=caller, storage=harness.blobs
            )
            == reference
        )
        assert await raster_mask_objects.bind_publications(caller, reference) == 1
        await caller.commit()
        row = (await harness.rows())[0]
        saved = deepcopy(row.storage_ref)
        async with factory() as db:
            await switchback(db)
        assert (
            await raster_mask_objects.load(reference, db=caller, storage=harness.blobs)
            == rle
        )
        assert physical.seen[-1] == b"rotated" and row.storage_ref == saved
        cleanup_time = raster_mask_objects.now() + timedelta(days=2)
        monkeypatch.setattr(raster_mask_objects, "now", lambda: cleanup_time)
        assert (await raster_mask_objects.cleanup(caller, storage=harness.blobs))[
            "retired"
        ] == 1
        assert (await raster_mask_objects.cleanup(caller, storage=harness.blobs))[
            "deleted"
        ] == 1
        assert not physical.objects and physical.seen[-1] == b"rotated"
