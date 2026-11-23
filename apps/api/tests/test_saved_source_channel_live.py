"""Real ordinary API, Source process, scoped PostgreSQL logins and registered S3."""

import asyncio
import os
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yoao_assets_source.saved_io import SavedIOClient, SavedIOError

from app.config import settings
from app.core.security import create_access_token
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.db.models.user import User
from app.deps import get_db
from app.services import storage_access
from app.services.dataset_assets import ensure_legacy_bindings
from tests.saved_io_live import real_source, registered_sink

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable PostgreSQL/MinIO module-data verifier",
    ),
]


async def _deny(engine, sql, parameters=None):
    with pytest.raises(DBAPIError) as failure:
        async with engine.begin() as connection:
            await connection.execute(text(sql), parameters or {})
    assert getattr(failure.value.orig, "sqlstate", None) == "42501"


async def test_saved_source_registered_binding_probe_and_denial(
    test_engine,
    app_module,
    tmp_path,
    monkeypatch,
):
    ordinary_url = make_url(os.environ["DATABASE_URL"])
    source_url = make_url(os.environ["AAP_MODULE_DATA_ASSETS_SOURCE_SAVED_IO_URL"])
    for url in (ordinary_url, source_url):
        assert (url.host, url.port, url.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
    assert source_url.username == "yoao_asset_source_saved_io_client"
    assert ordinary_url.username not in {source_url.username, test_engine.url.username}
    ordinary = create_async_engine(ordinary_url, hide_parameters=True)
    source = create_async_engine(
        source_url.set(drivername="postgresql+asyncpg"), hide_parameters=True
    )
    open_session = async_sessionmaker(ordinary, expire_on_commit=False)
    owner_session = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = uuid4()
    connector_name = f"saved-source-probe-{actor_id.hex}"
    connector_id = target_id = None

    async def ordinary_db():
        async with open_session() as session:
            yield session

    monkeypatch.setitem(app_module.dependency_overrides, get_db, ordinary_db)
    try:
        await _deny(
            ordinary, "SELECT secret_enc FROM public.storage_connections LIMIT 1"
        )
        await _deny(
            ordinary,
            "SELECT yoao_assets_v1.saved_io_connector(NULL::uuid, 1, '[]'::jsonb)",
        )
        await _deny(
            ordinary,
            "SELECT yoao_assets_v1.capture_saved_binding_probe(NULL::uuid, NULL::uuid, true)",
        )
        await _deny(source, "SELECT secret_enc FROM public.storage_connections LIMIT 1")
        await _deny(
            source,
            "SELECT yoao_assets_v1.capture_storage_connection(NULL::uuid, NULL::uuid, 'read')",
        )

        async with owner_session.begin() as db:
            await ensure_legacy_bindings(db)
            db.add(
                User(
                    id=actor_id,
                    email=f"{actor_id.hex}@source.invalid",
                    name="Source Probe",
                    role="super_admin",
                    password_hash="no-interactive-login",
                    is_active=True,
                )
            )
            binding = await db.get(StorageBinding, "avatars")
            before = binding.location_id, binding.generation

        headers = {
            "Authorization": "Bearer "
            + create_access_token(subject=str(actor_id), role="super_admin")
        }
        with registered_sink(test_engine) as sink:
            sink.storage.put_object(
                Bucket=sink.bucket, Key=f"{sink.prefix}/inside.bin", Body=b"inside"
            )
            sink.storage.put_object(
                Bucket=sink.bucket,
                Key=f"{sink.prefix}-adjacent/outside.bin",
                Body=b"outside",
            )
            with real_source(
                test_engine, tmp_path, s3_credentials=sink.credentials
            ) as runtime:
                monkeypatch.setattr(settings, "assets_source_url", runtime.origin)
                monkeypatch.setattr(settings, "assets_source_token", "")
                monkeypatch.setattr(
                    settings,
                    "assets_source_connections_token",
                    runtime.connections_token,
                )
                monkeypatch.setattr(
                    settings, "assets_source_connections_token_file", ""
                )
                for access in ("read", "write", "cleanup"):
                    field = f"assets_source_saved_io_{access}_token"
                    monkeypatch.setattr(settings, field, runtime.tokens[access])
                    monkeypatch.setattr(settings, field + "_file", "")
                monkeypatch.setattr(settings, "connector_encryption_key", "")
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app_module),
                    base_url="http://ordinary-api",
                ) as api:
                    create = await api.post(
                        "/api/v1/storage-connections",
                        headers=headers,
                        json={
                            "name": connector_name,
                            "kind": "s3",
                            "scope": "global",
                            "config": {
                                "endpoint": runtime.storage_origin,
                                "bucket": sink.bucket,
                                "base_prefix": sink.prefix,
                                "use_ssl": runtime.storage_origin.startswith(
                                    "https://"
                                ),
                            },
                            "secret": {
                                "access_key": sink.credentials[0],
                                "secret_key": sink.credentials[1],
                            },
                        },
                    )
                    assert create.status_code == 201, create.text
                    connector_id = UUID(create.json()["id"])
                    assert create.json()["secret_set"] is True
                    assert all(value not in create.text for value in sink.credentials)
                    check = await api.post(
                        f"/api/v1/storage-connections/{connector_id}/test",
                        headers=headers,
                    )
                    assert check.status_code == 200 and check.json()["ok"] is True, (
                        check.text
                    )
                    assert check.json()["sample_count"] == 1

                    async with owner_session() as db:
                        location = await db.scalar(
                            select(StorageLocation).where(
                                StorageLocation.connection_id == connector_id,
                            )
                        )
                        assert location is not None
                        target_id, revision = location.id, location.config_revision
                    path = "/api/v1/storage-locations/bindings/avatars/change-preview"
                    payload = {
                        "expected_generation": before[1],
                        "target_location_id": str(target_id),
                        "target_config_revision": revision,
                    }
                    preview = await api.post(path, headers=headers, json=payload)
                    assert preview.status_code == 200, preview.text
                    assert preview.json()["target"]["bucket"] == sink.bucket
                    assert preview.json()["capabilities"] == {
                        "conditional_write": True,
                        "full_read": True,
                        "delete": True,
                    }
                    assert all(value not in preview.text for value in sink.credentials)
                    stale = await api.post(
                        path,
                        headers=headers,
                        json={
                            **payload,
                            "target_config_revision": revision + 1,
                        },
                    )
                    assert stale.status_code == 409, stale.text

                    with monkeypatch.context() as isolated:
                        isolated.setattr(
                            settings, "assets_source_connections_token", ""
                        )
                        failed = await api.post(
                            f"/api/v1/storage-connections/{connector_id}/test",
                            headers=headers,
                        )
                        assert failed.status_code == 503

                fake_permit = {"operation_id": str(uuid4())}
                for scope in ("read", "cleanup", "cutover"):
                    client = SavedIOClient(runtime.origin, runtime.tokens[scope])
                    with pytest.raises(SavedIOError) as wrong:
                        await asyncio.to_thread(
                            client.command, "binding", "probe", fake_permit
                        )
                    assert wrong.value.status_code == 401 and not wrong.value.unsettled
                writer = SavedIOClient(runtime.origin, runtime.tokens["write"])
                for family, action, subject, status in (
                    ("binding", "probe", fake_permit, 409),
                    ("binding", "probe", {**fake_permit, "bucket": sink.bucket}, 400),
                    ("binding", "sign", fake_permit, 404),
                    ("migration", "probe_cutover_target", fake_permit, 401),
                ):
                    with pytest.raises(SavedIOError) as denied:
                        await asyncio.to_thread(writer.command, family, action, subject)
                    assert (
                        denied.value.status_code == status
                        and not denied.value.unsettled
                    )

                expired_subject = {}

                async def admit_then_expire(permit):
                    async with ordinary.begin() as db:
                        admitted = await db.scalar(
                            text("""
                                SELECT yoao_assets_v1.begin_saved_binding_probe(
                                    :operation, :actor, NULL::uuid, NULL::text,
                                    'avatars', :location, :config, :connection, :generation
                                )
                            """),
                            {
                                "operation": permit.id,
                                "actor": actor_id,
                                "location": target_id,
                                "config": revision,
                                "connection": preview.json()["target"][
                                    "connection_revision"
                                ],
                                "generation": before[1],
                            },
                        )
                        assert admitted is True
                    async with owner_session.begin() as db:
                        expired = await db.scalar(
                            text("""
                                UPDATE yoao_assets_v1.saved_binding_probe
                                SET expires_at = clock_timestamp() - interval '1 second'
                                WHERE operation_id = :operation AND attempt_id IS NULL
                                  AND expires_at > clock_timestamp()
                                RETURNING operation_id
                            """),
                            {"operation": permit.id},
                        )
                        assert expired == permit.id
                    expired_subject["operation_id"] = str(permit.id)

                def probe_expired():
                    try:
                        writer.command("binding", "probe", expired_subject)
                    except SavedIOError as exc:
                        if exc.unsettled:
                            storage_access.mark_unsettled(exc)
                        raise

                async with open_session() as db:
                    with pytest.raises(SavedIOError) as expired:
                        await storage_access.run(
                            db,
                            target_id,
                            "avatar_binding_probe",
                            probe_expired,
                            on_acquired=admit_then_expire,
                        )
                assert expired.value.status_code == 409
                assert expired.value.code == "probe_scope_unproven"
                assert not expired.value.unsettled

                observed = await asyncio.to_thread(
                    sink.storage.list_object_versions,
                    Bucket=sink.bucket,
                    Prefix=sink.prefix.removesuffix("/") + "/_aap_managed/avatars/",
                )
                assert not observed.get("Versions") and not observed.get(
                    "DeleteMarkers"
                )

        log = (tmp_path / "saved-io-source.log").read_text(
            encoding="utf-8", errors="replace"
        )
        if any(
            value in log
            for value in (*sink.credentials, runtime.encryption_key, "X-Amz-Signature=")
        ):
            pytest.fail("Saved Source process logged connector or signature material")

        async with owner_session() as db:
            binding = await db.get(StorageBinding, "avatars")
            assert (binding.location_id, binding.generation) == before
            assert (
                await db.scalar(
                    select(StorageOperation.id).where(
                        StorageOperation.location_id == target_id
                    )
                )
                is None
            )
    finally:
        async with owner_session.begin() as db:
            if target_id is not None:
                await db.execute(
                    delete(StorageMaintenanceGate).where(
                        StorageMaintenanceGate.location_id == target_id
                    )
                )
            await db.execute(
                delete(StorageConnection).where(
                    StorageConnection.name == connector_name
                )
            )
        await ordinary.dispose()
        await source.dispose()
