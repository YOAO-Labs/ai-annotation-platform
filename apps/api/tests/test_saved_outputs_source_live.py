"""Real restricted LOGINs, versioned MinIO, and the shipped registered Source HTTP path."""

import asyncio
from datetime import date, datetime, timedelta, timezone
import gzip
from hashlib import sha256
import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zipfile import ZIP_STORED, ZipFile

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from yoao_assets_source.saved_io import SavedIOClient, SavedIOError

from app.config import settings
from app.db.models.audit_archive_output import AuditArchiveOutput
from app.db.models.export_media_bundle import ExportMediaBundle
from app.services import audit_archive_storage, export_delivery_data, export_media_retirement
from app.services import export_media_storage, export_outputs, storage_access
from app.services.audit_partition_service import _write_candidate
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.managed_original_retirement import managed_storage_identity
from tests.factory import DEFAULT_PASSWORD
from tests.saved_io_live import real_source, registered_sink
from tests.test_identity_avatar_acl_live import runtime
from tests.test_identity_embedded_acl import embedded_http, seeded_embedded_accounts
from tests.test_me_avatar import AVATAR_URL, _bearer, _png_bytes

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_SAVED_OUTPUTS_LIVE") != "1",
        reason="requires the disposable module-data verifier and shared bootstrap MinIO",
    ),
]


async def _login(client, account):
    result = await client.post(
        "/api/v1/auth/login", json={"email": account.email, "password": DEFAULT_PASSWORD}
    )
    assert result.status_code == 200
    return _bearer(result.json()["access_token"])


@pytest.fixture
async def registered_outputs(test_engine, tmp_path, app_module, monkeypatch):
    with registered_sink(test_engine) as sink, real_source(
        test_engine, tmp_path, s3_credentials=sink.credentials
    ) as source:
        monkeypatch.setattr(settings, "assets_source_url", source.origin)
        monkeypatch.setattr(settings, "assets_source_connections_token", source.connections_token)
        monkeypatch.setattr(settings, "connector_encryption_key", "")
        for scope in ("read", "write", "cleanup"):
            monkeypatch.setattr(settings, f"assets_source_saved_io_{scope}_token", source.tokens[scope])
        async with (
            seeded_embedded_accounts(test_engine) as (admin, user),
            runtime(test_engine, "AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL", "identity_embedded_api_consumer") as engine,
            embedded_http(app_module, engine, monkeypatch) as api,
            httpx.AsyncClient(trust_env=False, timeout=20) as http,
        ):
            async with async_sessionmaker(test_engine).begin() as owner:
                await ensure_legacy_bindings(owner)
            admin_headers = await _login(api, admin)
            user_headers = await _login(api, user)
            identifier = uuid4()
            registered = await http.post(
                source.origin + "/v1/connections/create",
                headers=_bearer(source.connections_token),
                json={
                    "operation_id": str(identifier), "actor_id": str(admin.id),
                    "name": f"saved outputs {identifier.hex}", "kind": "s3", "scope": "global",
                    "config": {"endpoint": source.storage_origin, "bucket": sink.bucket, "base_prefix": sink.prefix},
                    "secret": {"access_key": sink.credentials[0], "secret_key": sink.credentials[1]},
                },
            )
            assert registered.status_code == 200
            async with test_engine.connect() as owner:
                location = await owner.scalar(text(
                    "SELECT id FROM public.storage_locations WHERE connection_id=:id"
                ), {"id": identifier})
            assert location is not None
            previous = {}

            async def bind(purpose):
                async with test_engine.connect() as owner:
                    row = (await owner.execute(text(
                        "SELECT location_id, bucket, generation FROM public.storage_bindings WHERE purpose=:purpose"
                    ), {"purpose": purpose})).mappings().one()
                previous[purpose] = dict(row)
                path = f"/api/v1/storage-locations/bindings/{purpose}"
                change = {"expected_generation": row["generation"], "target_location_id": str(location), "target_config_revision": 1}
                preview = await api.post(path + "/change-preview", headers=admin_headers, json=change)
                assert preview.status_code == 200
                facts = preview.json()
                assert facts["can_apply"] and facts["capabilities"]["conditional_write"]
                if purpose == "export":
                    assert facts["capabilities"]["list"] and facts["capabilities"]["list_versions"]
                applied = await api.patch(path, headers=admin_headers, json=change | {"impact_token": facts["impact_token"]})
                assert applied.status_code == 200
                return applied.json()["generation"]

            try:
                yield SimpleNamespace(
                    api=api, http=http, source=source, storage=sink.storage, sink=sink, db=async_sessionmaker(engine, expire_on_commit=False),
                    owner=async_sessionmaker(test_engine, expire_on_commit=False), location=location,
                    admin=admin, user=user, admin_headers=admin_headers, user_headers=user_headers, bind=bind,
                    process_log=tmp_path / "saved-io-source.log",
                )
            finally:
                async with test_engine.begin() as owner:
                    for purpose, row in previous.items():
                        await owner.execute(text(
                            "UPDATE public.storage_bindings SET location_id=:location_id, bucket=:bucket, generation=:generation "
                            "WHERE purpose=:purpose AND location_id=:ours"
                        ), row | {"purpose": purpose, "ours": location})
                    await owner.execute(text("DELETE FROM public.bug_reports WHERE reporter_id=:actor"), {"actor": user.id})
                    await owner.execute(text(
                        "ALTER TABLE public.audit_archive_outputs DISABLE TRIGGER trg_audit_archive_facts"
                    ))
                    await owner.execute(text(
                        "ALTER TABLE public.export_media_bundles DISABLE TRIGGER trg_export_media_bundle"
                    ))
                    for table in ("avatar_uploads", "bug_attachment_uploads", "audit_archive_outputs", "export_outputs", "export_media_bundles"):
                        await owner.execute(text(f"DELETE FROM public.{table} WHERE storage_ref->>'location_id'=:location"), {"location": str(location)})
                    await owner.execute(text(
                        "ALTER TABLE public.export_media_bundles ENABLE TRIGGER trg_export_media_bundle"
                    ))
                    await owner.execute(text(
                        "ALTER TABLE public.audit_archive_outputs ENABLE TRIGGER trg_audit_archive_facts"
                    ))
                    await owner.execute(text(
                        "DELETE FROM yoao_assets_v1.saved_output_writes WHERE facts->'storage_ref'->>'location_id'=:location"
                    ), {"location": str(location)})
                    await owner.execute(text("DELETE FROM public.storage_maintenance_gates WHERE location_id=:id"), {"id": location})
                    await owner.execute(text("DELETE FROM public.storage_location_probes WHERE location_id=:id"), {"id": location})
                    await owner.execute(text("DELETE FROM public.storage_locations WHERE id=:id"), {"id": location})
                    await owner.execute(text("DELETE FROM public.storage_connections WHERE id=:id"), {"id": identifier})


def _private(world, access):
    return SavedIOClient(world.source.origin, world.source.tokens[access], timeout=30)


def _versions(world, key):
    result = world.storage.list_object_versions(Bucket=world.sink.bucket, Prefix=key)
    return {item["VersionId"] for item in result.get("Versions", ()) if item["Key"] == key}


async def test_avatar_source_public_read_and_exact_retirement(registered_outputs):
    world = registered_outputs
    async with world.db() as db:
        assert not await db.scalar(text(
            "SELECT has_column_privilege(SESSION_USER,'public.storage_connections','secret_enc','SELECT')"
        ))
        assert not await db.scalar(text(
            "SELECT has_table_privilege(SESSION_USER,'yoao_assets_v1.saved_output_writes','SELECT')"
        ))
    await world.bind("avatars")
    one = await world.api.post(
        AVATAR_URL, headers=world.user_headers,
        files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
    )
    assert one.status_code == 200
    token = one.json()["avatar_ref"].split(":", 1)[1]
    async with world.owner() as owner:
        receipt = (await owner.execute(text(
            "SELECT storage_ref, object_version_id FROM public.avatar_uploads WHERE token=:token"
        ), {"token": token})).one()
    key, birth = receipt.storage_ref["object_key"], receipt.object_version_id
    assert birth and _versions(world, key) == {birth}
    other = world.storage.put_object(Bucket=world.sink.bucket, Key=key, Body=b"not the avatar")["VersionId"]
    public = await world.api.get(f"/api/v1/avatars/{token}")
    assert public.status_code == 200 and public.headers["content-type"] == "image/webp"
    assert public.content[8:12] == b"WEBP"
    assert (await world.api.get(f"/api/v1/avatars/{token}", headers={"If-None-Match": public.headers["etag"]})).status_code == 304
    cleared = await world.api.delete(AVATAR_URL, headers=world.user_headers)
    assert cleared.status_code == 200 and cleared.json()["avatar_ref"] is None
    assert (await world.api.get(f"/api/v1/avatars/{token}")).status_code == 404
    assert _versions(world, key) == {other}


async def test_diagnostics_report_egress_requires_the_committed_report(registered_outputs):
    world = registered_outputs
    await world.bind("bug-reports")
    body = _png_bytes((48, 48))
    upload = await world.api.post(
        "/api/v1/bug_reports/screenshot/upload", params={"file_name": "source.png"},
        headers=world.user_headers | {"Content-Type": "image/png"}, content=body,
    )
    assert upload.status_code == 200
    attachment = upload.json()
    report = await world.api.post(
        "/api/v1/bug_reports", headers=world.user_headers,
        json={"title": "Source-owned attachment", "description": "fixture", "attachments": [attachment]},
    )
    assert report.status_code == 201
    path = f"/api/v1/bug_reports/{report.json()['id']}/attachments/download"
    downloaded = await world.api.get(path, params={"key": attachment["storageKey"]}, headers=world.user_headers)
    assert downloaded.status_code == 200 and downloaded.content == body
    assert downloaded.headers["cache-control"] == "private, no-store"
    async with world.owner() as owner:
        receipt = (await owner.execute(text(
            "SELECT id, storage_ref, object_version_id FROM public.bug_attachment_uploads WHERE owner_id=:owner"
        ), {"owner": world.user.id})).one()
    assert receipt.object_version_id in _versions(world, receipt.storage_ref["object_key"])
    async with world.db() as db:
        permit = await storage_access.acquire(db, world.location, "bug_attachment_read_" + receipt.id.hex)
        async with permit:
            with pytest.raises(SavedIOError) as denied:
                with _private(world, "read").download("outputs", "bug_read", {"operation_id": str(permit.id), "upload_id": str(receipt.id)}):
                    pytest.fail("a permit is not a reporter authorization")
            assert denied.value.status_code == 403


async def test_zip_source_multipart_receipt_and_retained_signer(registered_outputs, tmp_path):
    world = registered_outputs
    await world.bind("export")
    path = tmp_path / "saved.zip"
    with ZipFile(path, "w", ZIP_STORED) as archive:
        archive.writestr("retained.bin", bytes(8 * 1024**2))
    async with world.db() as db:
        item = await export_outputs.create(db, project_id=uuid4(), job_id=uuid4(), media="image", format="native_json", file_count=1, path=path)
        revision = await export_outputs.write(db, item, path)
        await export_outputs.publish(db, item, revision)
        await db.commit()
        saved = await export_delivery_data.zip_receipt(db, item.id)
        birth = saved.upload_proof["version_id"]
        assert birth and "-" in saved.upload_proof["etag"]
        key = saved.storage_ref["object_key"]
        assert _versions(world, key) == {birth}
        other = world.storage.put_object(Bucket=world.sink.bucket, Key=key, Body=b"foreign version")["VersionId"]
        prepared = await export_outputs.prepare_download(db, item, download_name="retained.zip")
        result = await world.http.get(prepared.url)
        assert result.status_code == 200 and sha256(result.content).hexdigest() == item.sha256
        query = parse_qs(urlsplit(prepared.url).query)
        assert query["versionId"] == [birth]
        assert 0 < int(query["X-Amz-Expires"][0]) <= 7 * 24 * 3600
        permit = await storage_access.acquire(db, world.location, "export_read_" + item.id.hex)
        async with permit:
            request = {"operation_id": str(permit.id), "output_id": str(item.id), "download_name": "retained.zip"}
            for extras, status in (({"bucket": world.sink.bucket}, 400), ({"version_id": other}, 400), ({"output_id": str(uuid4())}, 403)):
                with pytest.raises(SavedIOError) as denied:
                    await asyncio.to_thread(_private(world, "read").command, "outputs", "zip_sign", request | extras)
                assert denied.value.status_code == status
            with pytest.raises(SavedIOError) as denied:
                await asyncio.to_thread(_private(world, "write").command, "outputs", "zip_sign", request)
            assert denied.value.status_code == 401
        with pytest.raises(SavedIOError) as replay:
            await asyncio.to_thread(_private(world, "read").command, "outputs", "zip_sign", request)
        assert replay.value.status_code == 403
        assert _versions(world, key) == {birth, other}
        assert not await db.scalar(text(
            "SELECT has_function_privilege(SESSION_USER,'yoao_export_v1.save_source_zip_proof(uuid,uuid,jsonb)','EXECUTE')"
        ))
    log = world.process_log.read_text(encoding="utf-8")
    assert all(value not in log for value in (*world.sink.credentials, "X-Amz-Signature="))


async def test_media_member_write_replay_and_exact_unpublished_reclamation(registered_outputs):
    world = registered_outputs
    generation = await world.bind("export")
    identifier, member = uuid4(), uuid4()
    current = datetime.now(timezone.utc)
    prefix = f"{world.sink.prefix}/_yoao_managed/export/{export_media_storage.prefix_for(identifier)}"
    key = f"{prefix}{member}.bin"
    body = b"verified immutable export media member"
    async with world.owner.begin() as owner:
        owner.add(ExportMediaBundle(
            id=identifier, project_id=uuid4(), job_id=uuid4(), zip_output_id=uuid4(),
            input_digest="b" * 64, input_snapshot={"schema": 1}, logical_paths={"schema": 1, "targets": {}},
            inventory={"schema": 1, "objects": {key: {"sha256": sha256(body).hexdigest(), "size_bytes": len(body)}}},
            storage_ref={
                "schema": 2, "purpose": "export", "location_id": str(world.location), "config_revision": 1,
                "binding_generation": generation, "endpoint_url": world.source.storage_origin,
                "bucket": world.sink.bucket, "key_prefix": prefix,
                "storage_identity": managed_storage_identity(world.source.storage_origin, world.sink.bucket),
            },
            created_at=current, expires_at=current + timedelta(days=7),
            state="building", object_count=1, size_bytes=len(body),
        ))
    async with world.db() as db:
        assert await export_delivery_data.claim_media(db, identifier, uuid4(), current + timedelta(minutes=5))
        await db.commit()
        permit = await storage_access.acquire(db, world.location, "export_media_" + identifier.hex)
        async with permit:
            request = {"operation_id": str(permit.id), "bundle_id": str(identifier), "member_id": str(member)}
            writer = _private(world, "write")
            receipt = await asyncio.to_thread(writer.upload, "outputs", "member_write", request, body, length=len(body))
            status = await asyncio.to_thread(writer.command, "outputs", "write_status", request | {"kind": "member"})
            assert status == receipt
            birth = receipt["version_id"]
            assert birth and _versions(world, key) == {birth}
            with pytest.raises(SavedIOError) as replay:
                await asyncio.to_thread(writer.upload, "outputs", "member_write", request, body, length=len(body))
            assert replay.value.status_code in (403, 409)
            url = (await asyncio.to_thread(_private(world, "read").command, "outputs", "member_sign", request))["url"]
            other = world.storage.put_object(Bucket=world.sink.bucket, Key=key, Body=b"another version")["VersionId"]
            response = await world.http.get(url)
            assert response.status_code == 200 and response.content == body
            with _private(world, "read").download("outputs", "member_read", request) as copy:
                assert copy.read() == body
            assert _versions(world, key) == {birth, other}
        assert await export_media_retirement._cleanup_one(db, identifier)
        assert _versions(world, key) == {other}


async def test_audit_source_archive_write_and_admin_egress(registered_outputs, tmp_path):
    world = registered_outputs
    await world.bind("audit-archive")
    created = datetime.now(timezone.utc)
    archive_id, job_id = uuid4(), uuid4()
    async with world.owner.begin() as owner:
        await owner.execute(text(
            "INSERT INTO public.async_jobs(id,kind,status,payload,result,user_id) "
            "VALUES(:id,'audit_archive','running','{}','{}',:actor)"
        ), {"id": job_id, "actor": world.admin.id})
        archive = AuditArchiveOutput(
            id=archive_id, job_id=job_id, year=2085, month=1,
            source_schema="public", source_table="audit_logs_y2085m01", parent_oid=1234, source_oid=5678,
            month_start=date(2085, 1, 1), month_end=date(2085, 2, 1),
            format_revision=1, row_count=0, source_size_bytes=0, source_sha256=sha256(b"").hexdigest(),
            created_at=created, state="pending",
        )
        compressed = gzip.compress(audit_archive_storage.encode_meta(archive), mtime=0)
        archive.sha256, archive.size_bytes = sha256(compressed).hexdigest(), len(compressed)
        archive.storage_ref = await audit_archive_storage.allocate(owner, archive)
        owner.add(archive)
    try:
        path = tmp_path / "audit.jsonl.gz"
        path.write_bytes(compressed)
        async with world.db() as db:
            with path.open("rb") as body:
                await _write_candidate(db, audit_archive_storage.snapshot(archive), body)
        async with world.owner.begin() as owner:
            await owner.execute(text(
                "UPDATE public.audit_archive_outputs SET state='ready', published_at=clock_timestamp() WHERE id=:id"
            ), {"id": archive_id})
        async with world.db() as db:
            permit = await storage_access.acquire(db, world.location, "audit_archive_read_" + archive_id.hex)
            async with permit:
                with pytest.raises(SavedIOError) as denied:
                    with _private(world, "read").download("outputs", "archive_read", {"operation_id": str(permit.id), "archive_id": str(archive_id)}):
                        pytest.fail("an unadmitted archive read opened")
                assert denied.value.status_code == 403
        archive_url = "/api/v1/audit-logs/archives/2085/1"
        assert (await world.api.get(archive_url, headers=world.user_headers)).status_code == 403
        page = await world.api.get(archive_url, headers=world.admin_headers, params={"limit": 1})
        assert page.status_code == 200 and page.json()["total"] == 0
        assert len(_versions(world, archive.storage_ref["object_key"])) == 1
    finally:
        async with world.owner.begin() as owner:
            await owner.execute(text("DELETE FROM public.async_jobs WHERE id=:id"), {"id": job_id})
