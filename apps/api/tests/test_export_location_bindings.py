"""Exact schema2 export ownership, current credentials and retained in-flight pairs."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import boto3
from botocore.config import Config
import pytest
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from app.db.models.storage_connection import StorageConnection
from app.services import (
    connector_guard,
    export_media_bundles as bundles,
    export_media_storage as media,
    export_outputs,
    export_storage as storage,
    export_storage_target as targets,
    storage_location,
)
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.content_snapshot import ContentError
from app.services.managed_original_retirement import managed_storage_identity
from app.services.managed_s3_target import Target
from tests.test_avatar_storage_target import Objects, connection_target
from tests.test_export_media_bundles import inputs, digest
from tests.test_export_storage import reference, record, migration_route, RAW, KEY

LOCATION = uuid.UUID(int=551)
ENDPOINT = "https://saved-export.example.test"
ROOT = "team/图片/[a](b)/_aap_managed/export/"


def external_reference(**changes):
    return {
        **reference(),
        "schema": 2,
        "location_id": str(LOCATION),
        "config_revision": 3,
        "binding_generation": 2,
        "endpoint_url": ENDPOINT,
        "object_key": ROOT + KEY,
        "storage_identity": managed_storage_identity(ENDPOINT, "exports-original"),
        **changes,
    }


def external_media_ref(identifier):
    ref = external_reference()
    ref.pop("object_key")
    ref["key_prefix"] = ROOT + media.prefix_for(identifier)
    return ref


def test_two_owner_refs_accept_literal_unicode_prefix_and_copy_facts():
    ref = external_reference()
    owner = storage.resolve(record(storage_ref=ref))
    assert owner.object_key == ROOT + KEY
    ref["object_key"] = "changed"
    assert owner.ref["object_key"] == ROOT + KEY
    identifier = uuid.uuid4()
    ref = external_media_ref(identifier)
    assert (
        media.resolve_ref(ref, expected_prefix=media.prefix_for(identifier)).prefix
        == ref["key_prefix"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("binding_generation", 1),
        ("config_revision", 1.5),
        ("location_id", "bad"),
        ("object_key", "../_aap_managed/export/" + KEY),
        ("object_key", "/_aap_managed/export/" + KEY),
        ("object_key", "team//_aap_managed/export/" + KEY),
        ("object_key", "team/_aap_managed/avatars/" + KEY),
        (
            "object_key",
            ROOT + "image/" + str(uuid.uuid4()) + "/" + KEY.partition("/")[2],
        ),
    ],
)
def test_schema2_rejects_changed_physical_and_logical_owner(field, value):
    with pytest.raises(storage.ExportStorageError):
        storage.resolve(record(storage_ref=external_reference(**{field: value})))


def test_maximum_root_leaves_exact_space_for_zip():
    namespace = targets.MANAGED_NAMESPACE
    base = "a" * (904 - len(namespace) - 1)
    root = targets.managed_prefix(base)
    assert len(root.encode()) == 904 and len((root + KEY).encode()) == 1024
    assert (
        storage.resolve_ref(external_reference(object_key=root + KEY)).object_key
        == root + KEY
    )
    with pytest.raises(storage.ExportStorageError):
        targets.managed_prefix(base + "x")


@pytest.mark.parametrize("kind", ["zip", "media"])
async def test_rotation_uses_current_credentials_and_never_reinterprets_address(
    monkeypatch, kind
):
    location, connection = connection_target()
    location.id, location.config_revision = LOCATION, 9
    connection.revision, connection.secret_enc = 10, b"rotated-encrypted-secret"
    connection.config.update(
        endpoint=ENDPOINT, bucket="exports-original", base_prefix="team/图片/[a](b)"
    )

    async def get(model, _identifier, **_kwargs):
        return location if model is StorageLocation else connection

    db = SimpleNamespace(get=AsyncMock(side_effect=get))
    monkeypatch.setattr(connector_guard, "get_allowlist", AsyncMock(return_value=[]))
    if kind == "zip":
        row, service = record(storage_ref=external_reference()), storage
    else:
        identifier = uuid.uuid4()
        created = datetime.now(timezone.utc)
        row = SimpleNamespace(
            id=identifier,
            created_at=created,
            expires_at=created + timedelta(days=7),
            storage_ref=external_media_ref(identifier),
        )
        service = media
    saved = deepcopy(row.storage_ref)
    target = await service.io_target(db, row)
    assert (
        target._access.secret_enc == b"rotated-encrypted-secret"
        and row.storage_ref == saved
    )
    connection.config = {**connection.config, "base_prefix": "changed"}
    with pytest.raises((storage.ExportStorageError, media.ExportMediaStorageError)):
        await service.io_target(db, row)


@pytest.mark.parametrize("operation", ["verify", "delete", "sign", "write"])
def test_external_zip_has_no_global_client_fallback(operation, tmp_path):
    row = record(storage_ref=external_reference())
    path = tmp_path / "candidate.zip"
    path.write_bytes(RAW)
    kwargs = (
        {"download_name": "export.zip", "now": row.created_at}
        if operation == "sign"
        else {}
    )
    with pytest.raises(storage.ExportStorageError):
        getattr(storage, operation)(
            row, *([path] if operation == "write" else []), **kwargs
        )


def test_external_zip_and_media_sign_their_actual_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "minio_public_url", "/storage-proxy")
    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id="actual-access",
        aws_secret_access_key="actual-secret",
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )

    class Access:
        @contextmanager
        def client(self):
            yield client

    target = Target(
        LOCATION,
        9,
        10,
        ENDPOINT,
        "exports-original",
        ROOT,
        purpose="export",
        _access=Access(),
    )
    now = datetime.now(timezone.utc)
    row = record(
        storage_ref=external_reference(),
        created_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
    )
    try:
        url = storage.sign(row, download_name="export.zip", now=now, target=target)
        assert (
            url.startswith(ENDPOINT + "/exports-original/")
            and "storage-proxy" not in url
        )
        identifier, relative = uuid.uuid4(), str(uuid.uuid4()) + ".bin"
        ref = external_media_ref(identifier)
        item = SimpleNamespace(
            id=identifier,
            created_at=row.created_at,
            expires_at=row.expires_at,
            storage_ref=ref,
            inventory={
                "schema": 1,
                "objects": {
                    ref["key_prefix"] + relative: {
                        "sha256": hashlib.sha256(RAW).hexdigest(),
                        "size_bytes": len(RAW),
                    }
                },
            },
        )
        media_url = media.signed_url(client, item, relative, now=now)
        assert (
            media_url.startswith(ENDPOINT + "/exports-original/")
            and "storage-proxy" not in media_url
        )
        assert "X-Amz-Expires=3600" in url and "X-Amz-Expires=3600" in media_url
    finally:
        client.close()


@pytest.mark.parametrize("missing_permission", [None, "list", "versions"])
def test_export_probe_checks_cleanup_listing_and_leaves_no_objects(missing_permission):
    objects = Objects("version")

    def listing(**kwargs):
        assert kwargs["MaxKeys"] == 1 and kwargs["Prefix"].startswith(ROOT)
        if missing_permission == "list":
            raise PermissionError("denied")
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def versions(**kwargs):
        if missing_permission == "versions":
            raise PermissionError("denied")
        return listing(**kwargs)

    objects.list_objects_v2, objects.list_object_versions = listing, versions
    target = Target(
        LOCATION,
        1,
        1,
        ENDPOINT,
        "exports-original",
        ROOT,
        purpose="export",
        _access=objects,
    )
    if missing_permission:
        with pytest.raises(PermissionError):
            targets.probe_target(target)
    else:
        assert targets.probe_target(target)["list_versions"]
    assert not objects.objects


@pytest.fixture
async def external_db(db_session, super_admin, monkeypatch):
    await ensure_legacy_bindings(db_session)
    monkeypatch.setattr(connector_guard, "get_allowlist", AsyncMock(return_value=[]))
    connection = StorageConnection(
        id=uuid.uuid4(),
        name="export target",
        kind="s3",
        scope="global",
        config={
            "endpoint": ENDPOINT,
            "bucket": "exports-original",
            "base_prefix": "team/图片/[a](b)",
            "use_ssl": True,
        },
        secret_enc=b"encrypted-captured-only",
        created_by=super_admin[0].id,
    )
    db_session.add(connection)
    await db_session.flush()
    location = await storage_location.register_connection(db_session, connection)
    binding = await db_session.get(StorageBinding, "export")
    binding.location_id, binding.bucket, binding.generation = (
        location.id,
        "exports-original",
        2,
    )
    await db_session.flush()
    return db_session, location, connection, binding


async def test_media_and_paired_zip_retain_saved_target_across_later_switch(
    external_db, tmp_path
):
    db, location, _, binding = external_db
    project_id, frozen, logical = inputs(tasks=1)
    job_id = uuid.uuid4()
    before = await bundles.allocate(
        db,
        project_id=project_id,
        job_id=job_id,
        input_snapshot=frozen,
        input_digest=digest(frozen),
        logical_sources=logical,
    )
    assert before.storage_ref["location_id"] == str(location.id)
    assert all(key.startswith(ROOT) for key in before.inventory["objects"])
    await db.commit()
    claimed = await bundles.claim(db, before.id)
    binding = await db.get(StorageBinding, "export")
    binding.location_id, binding.bucket, binding.generation = (
        LEGACY_LOCATION_ID,
        storage.storage_service.export_bucket,
        3,
    )
    await db.commit()
    path = tmp_path / "after-switch.zip"
    path.write_bytes(RAW)
    paired = await export_outputs.create(
        db,
        project_id=project_id,
        job_id=job_id,
        media="image",
        format="pointmask",
        file_count=1,
        path=path,
        media_bundle_id=before.id,
    )
    assert paired.storage_ref["location_id"] == str(location.id)
    assert paired.storage_ref["object_key"].startswith(ROOT + "image/")
    assert (
        paired.expires_at == before.expires_at
        and paired.created_at == before.created_at
    )
    new_id = uuid.uuid4()
    following = await storage.allocate(
        db,
        SimpleNamespace(
            id=new_id, media="image", project_id=project_id, job_id=uuid.uuid4()
        ),
    )
    assert (
        following["location_id"] == str(LEGACY_LOCATION_ID)
        and following["binding_generation"] == 3
    )
    assert await storage_location.location_is_referenced(db, location.id)
    assert claimed.storage_ref == before.storage_ref


@pytest.mark.parametrize("kind", ["zip", "media"])
async def test_schema2_saved_reference_is_immutable(external_db, kind):
    db, location, _, _ = external_db
    if kind == "zip":
        values = vars(
            record(storage_ref=external_reference(location_id=str(location.id)))
        )
        row = ExportOutput(**values, state="pending")
    else:
        project_id, frozen, logical = inputs(tasks=1)
        receipt = await bundles.allocate(
            db,
            project_id=project_id,
            job_id=uuid.uuid4(),
            input_snapshot=frozen,
            input_digest=digest(frozen),
            logical_sources=logical,
        )
        row = await db.get(ExportMediaBundle, receipt.id)
    db.add(row)
    await db.flush()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            row.storage_ref = {**row.storage_ref, "binding_generation": 99}
            await db.flush()


async def test_media_inventory_prefix_is_literal_even_when_it_contains_regex(
    external_db,
):
    db, location, _, _ = external_db
    project_id, frozen, logical = inputs(tasks=1)
    original = await bundles.allocate(
        db,
        project_id=project_id,
        job_id=uuid.uuid4(),
        input_snapshot=frozen,
        input_digest=digest(frozen),
        logical_sources=logical,
    )
    row = await db.get(ExportMediaBundle, original.id)
    values = {
        column.name: deepcopy(getattr(row, column.name))
        for column in ExportMediaBundle.__table__.columns
    }
    values.update(id=uuid.uuid4(), zip_output_id=uuid.uuid4())
    correct = ROOT + media.prefix_for(values["id"])
    values["storage_ref"] = {**values["storage_ref"], "key_prefix": correct}
    values["inventory"] = {
        "schema": 1,
        "objects": {
            key.replace(
                ROOT + media.prefix_for(original.id), correct.replace("[a](b)", "ab")
            ): receipt
            for key, receipt in values["inventory"]["objects"].items()
        },
    }
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(ExportMediaBundle(**values))
            await db.flush()


async def test_prepared_download_rechecks_first_deadline_after_gate_wait(monkeypatch):
    now = datetime.now(timezone.utc)
    row = record(
        storage_ref=external_reference(),
        created_at=now - timedelta(days=1),
        expires_at=now + timedelta(seconds=1),
    )
    receipt = export_outputs.snapshot(row)
    prepared = export_outputs.PreparedDownload(
        receipt, "export.zip", "https://private.invalid/signed", 3
    )
    current = [now]

    async def gate(*args, **kwargs):
        current[0] = receipt.expires_at

    monkeypatch.setattr(export_outputs.storage_access, "check", gate)
    monkeypatch.setattr(export_outputs, "now", lambda: current[0])
    with pytest.raises(storage.ExportStorageError) as caught:
        await export_outputs.prepared_download(
            object(), receipt, download_name="export.zip", prepared=prepared
        )
    assert caught.value.code == "export_storage_expired"


@pytest.fixture
def routed_zip(monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    row = record(
        state="ready", created_at=started, expires_at=started + timedelta(days=7)
    )
    receipt = export_outputs.snapshot(row)
    route = migration_route(row)
    url = "https://migration-target.invalid/exact-signed-version"
    client = SimpleNamespace(generate_presigned_url=Mock(return_value=url))
    resolve = AsyncMock(return_value=route)

    async def run(db, captured, purpose, callback, *, on_acquired):
        assert captured == route
        assert purpose == export_outputs.read_purpose(row.id)
        await on_acquired(SimpleNamespace(revision=route.maintenance_revision))
        return callback(client)

    execute = AsyncMock(side_effect=run)
    recheck, gate = AsyncMock(), AsyncMock()
    monkeypatch.setattr(export_outputs.artifact_reads, "resolve", resolve)
    monkeypatch.setattr(export_outputs.artifact_reads, "run", execute)
    monkeypatch.setattr(export_outputs.read_publications, "recheck", recheck)
    monkeypatch.setattr(export_outputs.storage_access, "check", gate)
    direct = AsyncMock(side_effect=AssertionError("source target must not open"))
    monkeypatch.setattr(storage, "io_target", direct)
    monkeypatch.setattr(
        storage, "sign", Mock(side_effect=AssertionError("source must not sign"))
    )
    return SimpleNamespace(
        row=row,
        receipt=receipt,
        route=route,
        url=url,
        resolve=resolve,
        run=execute,
        recheck=recheck,
        gate=gate,
        direct=direct,
    )


async def test_mapped_legacy_zip_prepares_and_rechecks_actual_target(routed_zip):
    case, db = routed_zip, object()
    saved = deepcopy(case.receipt.storage_ref)
    prepared = await export_outputs.prepare_download(
        db,
        case.receipt,
        download_name="export.zip",
        expected_revision=case.route.maintenance_revision,
    )
    assert prepared.read_route == case.route
    assert prepared.gate_revision == case.route.maintenance_revision
    assert (
        await export_outputs.prepared_download(
            db, case.receipt, download_name="export.zip", prepared=prepared
        )
        == case.url
    )
    case.resolve.assert_awaited_once_with(
        db,
        saved,
        saved["object_key"],
        case.receipt.sha256,
        case.receipt.size_bytes,
    )
    case.recheck.assert_awaited_once_with(db, case.route, lock=True)
    # Publication recheck locks and validates the actual target Gate itself.
    case.gate.assert_not_awaited()
    case.direct.assert_not_awaited()
    assert case.receipt.storage_ref == saved


async def test_mapped_download_refuses_stale_gate_before_signing(routed_zip):
    case = routed_zip
    with pytest.raises(storage.ExportStorageError):
        await export_outputs.prepare_download(
            object(),
            case.receipt,
            download_name="export.zip",
            expected_revision=case.route.maintenance_revision - 1,
        )
    case.run.assert_not_awaited()


async def test_mapped_legacy_download_requires_prepared_target(routed_zip):
    with pytest.raises(storage.ExportStorageError) as caught:
        await export_outputs.prepared_download(
            object(), routed_zip.receipt, download_name="export.zip", prepared=None
        )
    assert caught.value.code == "export_storage_invalid_receipt"
    routed_zip.run.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_object_key", "different.zip"),
        ("sha256", "f" * 64),
        ("source_location_id", uuid.UUID(int=999)),
        ("maintenance_revision", 99),
    ],
)
async def test_prepared_route_must_match_saved_receipt(routed_zip, field, value):
    case = routed_zip
    prepared = export_outputs.PreparedDownload(
        case.receipt,
        "export.zip",
        case.url,
        case.route.maintenance_revision,
        replace(case.route, **{field: value}),
    )
    with pytest.raises(storage.ExportStorageError) as caught:
        await export_outputs.prepared_download(
            object(), case.receipt, download_name="export.zip", prepared=prepared
        )
    assert caught.value.code == "export_storage_invalid_receipt"
    case.recheck.assert_not_awaited()
    case.gate.assert_not_awaited()


async def test_prepared_route_withdrawal_prevents_url_delivery(routed_zip):
    case = routed_zip
    prepared = await export_outputs.prepare_download(
        object(), case.receipt, download_name="export.zip"
    )
    case.recheck.side_effect = ContentError("read_publication_changed")
    with pytest.raises(ContentError, match="read_publication_changed"):
        await export_outputs.prepared_download(
            object(), case.receipt, download_name="export.zip", prepared=prepared
        )
    case.gate.assert_not_awaited()


@pytest.mark.parametrize("failure", [None, "route", "expiry", "authority"])
async def test_mapped_cache_keeps_final_business_and_route_checks(
    routed_zip, monkeypatch, failure
):
    case = routed_zip
    artifact = SimpleNamespace(
        id=uuid.uuid4(),
        output_id=case.receipt.id,
        project_id=case.receipt.project_id,
        cache_key="cache-key",
        batch_id=None,
        object_key=case.receipt.storage_ref["object_key"],
        size_bytes=case.receipt.size_bytes,
        file_count=case.receipt.file_count,
        format=case.receipt.format,
        expires_at=case.receipt.expires_at,
    )
    db = SimpleNamespace(scalar=AsyncMock(return_value=artifact))
    monkeypatch.setattr(export_outputs, "_locked", AsyncMock(return_value=case.row))
    hit = export_outputs.CacheHit(
        artifact.id,
        artifact.cache_key,
        None,
        case.receipt,
        case.route.maintenance_revision,
        case.route,
    )
    prepared = await export_outputs.prepare_download(
        db, case.receipt, download_name="export.zip"
    )

    async def authority():
        if failure == "expiry":
            monkeypatch.setattr(export_outputs, "now", lambda: case.receipt.expires_at)
        if failure == "authority":
            raise ContentError("export_output_authority_changed", 403)

    guard = AsyncMock(side_effect=authority)
    if failure == "route":
        prepared = replace(
            prepared, read_route=replace(case.route, publication_revision=2)
        )

    async def sign():
        return await export_outputs.sign_cached(
            db,
            hit,
            project_id=case.receipt.project_id,
            download_name="export.zip",
            before_sign=guard,
            prepared=prepared,
        )

    if failure is not None:
        with pytest.raises(ValueError):
            await sign()
    else:
        assert await sign() == case.url
        guard.assert_awaited_once()
    if failure == "route":
        guard.assert_not_awaited()
    assert all(call.kwargs == {"lock": True} for call in case.recheck.await_args_list)


async def test_unmapped_legacy_zip_keeps_original_prepare_contract(routed_zip):
    case = routed_zip
    case.resolve.return_value = None
    assert (
        await export_outputs.prepare_download(
            object(), case.receipt, download_name="export.zip"
        )
        is None
    )
    case.run.assert_not_awaited()
    case.direct.assert_not_awaited()


async def test_legacy_download_without_prepared_target_still_checks_source_gate(
    routed_zip,
):
    from fastapi import HTTPException

    case = routed_zip
    case.resolve.return_value = None
    case.gate.side_effect = HTTPException(409, {"code": "storage_maintenance"})
    with pytest.raises(HTTPException):
        await export_outputs.prepared_download(
            object(), case.receipt, download_name="export.zip", prepared=None
        )
    assert case.gate.await_args.args[1] == LEGACY_LOCATION_ID
    assert case.gate.await_args.kwargs == {"lock": True}
