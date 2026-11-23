"""Real Source, ordinary API LOGIN, versioned MinIO, and non-current bindings."""

import asyncio
from hashlib import sha256
from io import BytesIO
import json
import os
from uuid import uuid4

from botocore.exceptions import ClientError
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from yoao_assets_source.saved_io import SavedIOClient, SavedIOError

from app.config import settings
from app.db.models.dataset import Dataset
from app.services import (
    managed_original_creation,
    media_cache_storage,
    saved_media_io,
    storage_access,
    thumbnail_outputs,
)
from app.services.dataset_assets import ensure_legacy_bindings
from tests.factory import DEFAULT_PASSWORD, create_project
from tests.saved_io_live import real_source, registered_sink
from tests.test_identity_avatar_acl_live import runtime
from tests.test_identity_embedded_acl import embedded_http, seeded_embedded_accounts
from tests.test_me_avatar import _bearer, _png_bytes

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_TEST_SAVED_MEDIA_IO_LIVE") != "1",
        reason="requires the disposable module-data verifier and shared bootstrap MinIO",
    ),
]


async def test_registered_media_round_trip_and_denial(
    test_engine, tmp_path, app_module, monkeypatch
):
    async with async_sessionmaker(test_engine).begin() as owner:
        await ensure_legacy_bindings(owner)
    connection_id, item_id, dataset_id, output_id = (uuid4() for _ in range(4))
    with (
        registered_sink(test_engine) as sink,
        real_source(
            test_engine,
            tmp_path,
            s3_credentials=sink.credentials,
        ) as source,
    ):
        monkeypatch.setattr(settings, "assets_source_url", source.origin)
        monkeypatch.setattr(
            settings, "assets_source_connections_token", source.connections_token
        )
        monkeypatch.setattr(settings, "connector_encryption_key", "")
        for access in ("read", "write", "cleanup"):
            monkeypatch.setattr(
                settings,
                f"assets_source_saved_io_{access}_token",
                source.tokens[access],
            )
        async with (
            seeded_embedded_accounts(test_engine) as (admin, _),
            runtime(
                test_engine,
                "AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL",
                "identity_embedded_api_consumer",
            ) as engine,
            embedded_http(app_module, engine, monkeypatch) as api,
            httpx.AsyncClient(trust_env=False, timeout=20) as http,
        ):
            owner_db = async_sessionmaker(test_engine, expire_on_commit=False)
            entered = await api.post(
                "/api/v1/auth/login",
                json={"email": admin.email, "password": DEFAULT_PASSWORD},
            )
            assert entered.status_code == 200, entered.text
            headers = _bearer(entered.json()["access_token"])
            registered = await http.post(
                source.origin + "/v1/connections/create",
                headers=_bearer(source.connections_token),
                json={
                    "operation_id": str(connection_id),
                    "actor_id": str(admin.id),
                    "name": f"saved media {connection_id.hex}",
                    "kind": "s3",
                    "scope": "global",
                    "config": {
                        "endpoint": source.storage_origin,
                        "bucket": sink.bucket,
                        "base_prefix": sink.prefix,
                    },
                    "secret": {
                        "access_key": sink.credentials[0],
                        "secret_key": sink.credentials[1],
                    },
                },
            )
            assert registered.status_code == 200, registered.text
            async with owner_db() as owner:
                location = await owner.scalar(
                    text(
                        "SELECT id FROM public.storage_locations WHERE connection_id=:id"
                    ),
                    {"id": connection_id},
                )
            assert location is not None
            previous, project_id = {}, None

            async def restore(purpose):
                async with owner_db.begin() as owner:
                    await owner.execute(
                        text(
                            "UPDATE public.storage_bindings SET location_id=:location_id, bucket=:bucket, generation=:generation "
                            "WHERE purpose=:purpose AND location_id=:ours"
                        ),
                        previous[purpose] | {"purpose": purpose, "ours": location},
                    )

            try:
                for purpose in ("annotations", "import", "datasets", "media-cache"):
                    async with owner_db() as owner:
                        row = (
                            (
                                await owner.execute(
                                    text(
                                        "SELECT location_id, bucket, generation FROM public.storage_bindings WHERE purpose=:purpose"
                                    ),
                                    {"purpose": purpose},
                                )
                            )
                            .mappings()
                            .one()
                        )
                        previous[purpose] = dict(row)
                    route = f"/api/v1/storage-locations/bindings/{purpose}"
                    change = {
                        "expected_generation": row["generation"],
                        "target_location_id": str(location),
                        "target_config_revision": 1,
                    }
                    preview = await api.post(
                        route + "/change-preview", headers=headers, json=change
                    )
                    assert preview.status_code == 200, preview.text
                    capabilities = preview.json()
                    assert (
                        capabilities["can_apply"]
                        and capabilities["capabilities"]["conditional_write"]
                    )
                    applied = await api.patch(
                        route,
                        headers=headers,
                        json=change | {"impact_token": capabilities["impact_token"]},
                    )
                    assert applied.status_code == 200, applied.text

                async with owner_db.begin() as owner:
                    project_id = (await create_project(owner, owner_id=admin.id)).id
                    owner.add(
                        Dataset(
                            id=dataset_id,
                            display_id="D-" + dataset_id.hex[:14],
                            name="Saved media live",
                            created_by=admin.id,
                        )
                    )
                route = f"/api/v1/projects/{project_id}/guide-assets"
                png = _png_bytes()
                uploaded = await api.post(
                    route + "/upload",
                    headers=headers | {"Content-Type": "image/png"},
                    content=png,
                    params={"file_name": "registered.png"},
                )
                assert uploaded.status_code == 200, uploaded.text
                await restore("annotations")
                guide = await api.get(
                    route + "/content",
                    headers=headers,
                    params={"key": uploaded.json()["key"]},
                )
                assert guide.status_code == 200 and guide.content == png

                payload = b"multipart-original\x00" * (512 * 1024)
                content_type = "application/octet-stream"
                async with owner_db() as owner:
                    creation = await managed_original_creation.allocate(
                        owner,
                        dataset_id=dataset_id,
                        requested_by=admin.id,
                        logical_path=f"{item_id}.bin",
                        origin="zip",
                        max_bytes=len(payload),
                        content_type=content_type,
                    )
                    verified = await managed_original_creation.create_stream(
                        owner, creation, BytesIO(payload), content_type=content_type
                    )
                assert verified.sha256 == sha256(
                    payload
                ).hexdigest() and verified.size_bytes == len(payload)
                physical = sink.storage.head_object(
                    Bucket=sink.bucket, Key=creation["object_key"]
                )
                assert (
                    physical["ContentLength"] == len(payload)
                    and "-2" in physical["ETag"]
                )
                await restore("datasets")

                async with owner_db.begin() as owner:
                    ref = await media_cache_storage.allocate(
                        owner, thumbnail_outputs.prefix_for(output_id)
                    )
                    await owner.execute(
                        text(
                            "INSERT INTO public.thumbnail_outputs (id, owner_kind, owner_id, input_ref, storage_ref, writer_until, next_check_at) "
                            "VALUES (:id, 'dataset_thumbnail', :dataset, jsonb_build_object('encoded', "
                            "jsonb_build_object('size_bytes', CAST(:size AS bigint), 'sha256', CAST(:sha AS text))), "
                            "CAST(:ref AS jsonb), now() + interval '1 hour', now() + interval '1 hour')"
                        ),
                        {
                            "id": output_id,
                            "dataset": dataset_id,
                            "size": len(png),
                            "sha": sha256(png).hexdigest(),
                            "ref": json.dumps(ref),
                        },
                    )
                async with async_sessionmaker(engine)() as ordinary:
                    assert not await ordinary.scalar(
                        text(
                            "SELECT has_column_privilege(current_user, 'public.storage_connections', 'secret_enc', 'SELECT')"
                        )
                    )
                    assert not await ordinary.scalar(
                        text(
                            "SELECT has_function_privilege(current_user, 'yoao_assets_v1.media_io_thumbnail(text,uuid,uuid,boolean)', 'EXECUTE')"
                        )
                    )
                    async with await storage_access.acquire(
                        ordinary, location, thumbnail_outputs.purpose(output_id)
                    ) as permit:
                        subject = saved_media_io.subject(output_id, permit)
                        wrong_scope = SavedIOClient(
                            source.origin, source.tokens["read"]
                        )
                        with pytest.raises(SavedIOError) as refused:
                            await asyncio.to_thread(
                                wrong_scope.upload,
                                "media",
                                "thumbnail-write",
                                subject,
                                png,
                                length=len(png),
                            )
                        assert refused.value.status_code == 401
                        target = await media_cache_storage.target_for_ref(ordinary, ref)
                        assert target.is_registered
                        receipt = await asyncio.to_thread(
                            thumbnail_outputs._write,
                            target,
                            ref,
                            png,
                            permit=permit,
                            output_id=output_id,
                        )
                async with owner_db.begin() as owner:
                    await owner.execute(
                        text(
                            "UPDATE public.thumbnail_outputs SET state='ready', sha256=:sha256, size_bytes=:size_bytes, "
                            "etag=:etag, version_id=:version_id, ready_at=now(), writer_until=now()-interval '2 hours' WHERE id=:id"
                        ),
                        receipt | {"id": output_id},
                    )
                await restore("media-cache")
                async with async_sessionmaker(engine)() as ordinary:
                    async with await storage_access.acquire(
                        ordinary, location, thumbnail_outputs.purpose(output_id)
                    ) as permit:
                        subject = saved_media_io.cache_subject(
                            "thumbnail", output_id, "thumbnail.webp", permit
                        )
                        body = await asyncio.to_thread(
                            saved_media_io.read_checked,
                            "cache-download",
                            subject,
                            len(png),
                            sha256(png).hexdigest(),
                        )
                        assert body == png
                        with pytest.raises(SavedIOError):
                            await asyncio.to_thread(
                                saved_media_io.command,
                                "cache-head",
                                subject | {"relative": "foreign.webp"},
                            )
                        with pytest.raises(SavedIOError):
                            await asyncio.to_thread(
                                saved_media_io.command,
                                "cache-head",
                                subject | {"permit": str(uuid4())},
                            )
                protected_key = f"{sink.prefix}/not-managed/foreign.webp"
                sink.storage.put_object(
                    Bucket=sink.bucket, Key=protected_key, Body=b"untouched"
                )
                async with owner_db.begin() as owner:
                    await owner.execute(
                        text(
                            "UPDATE public.thumbnail_outputs SET state='retiring', retired_at=now()-interval '2 hours' WHERE id=:id"
                        ),
                        {"id": output_id},
                    )
                async with async_sessionmaker(engine)() as ordinary:
                    async with await storage_access.acquire(
                        ordinary, location, thumbnail_outputs.purpose(output_id)
                    ) as permit:
                        result = await asyncio.to_thread(
                            saved_media_io.command,
                            "thumbnail-delete",
                            saved_media_io.subject(output_id, permit),
                            access="cleanup",
                        )
                        assert result == {"deleted": 1}
                with pytest.raises(ClientError):
                    sink.storage.head_object(
                        Bucket=sink.bucket, Key=ref["prefix"] + "thumbnail.webp"
                    )
                assert sink.storage.head_object(Bucket=sink.bucket, Key=protected_key)[
                    "ContentLength"
                ] == len(b"untouched")
            finally:
                for purpose in previous:
                    await restore(purpose)
                async with owner_db.begin() as owner:
                    await owner.execute(
                        text("DELETE FROM public.thumbnail_outputs WHERE id=:id"),
                        {"id": output_id},
                    )
                    await owner.execute(
                        text(
                            "DELETE FROM public.managed_original_creations WHERE dataset_id=:id"
                        ),
                        {"id": dataset_id},
                    )
                    if project_id is not None:
                        for table, column in (
                            ("project_guide_assets", "project_id"),
                            ("guide_asset_uploads", "project_id"),
                            ("projects", "id"),
                        ):
                            await owner.execute(
                                text(f"DELETE FROM public.{table} WHERE {column}=:id"),
                                {"id": project_id},
                            )
                    await owner.execute(
                        text("DELETE FROM public.datasets WHERE id=:id"),
                        {"id": dataset_id},
                    )
                    await owner.execute(
                        text(
                            "DELETE FROM public.storage_operations WHERE location_id=:id"
                        ),
                        {"id": location},
                    )
                    await owner.execute(
                        text(
                            "DELETE FROM public.storage_maintenance_gates WHERE location_id=:id"
                        ),
                        {"id": location},
                    )
                    await owner.execute(
                        text(
                            "DELETE FROM public.storage_location_probes WHERE location_id=:id"
                        ),
                        {"id": location},
                    )
                    await owner.execute(
                        text("DELETE FROM public.storage_locations WHERE id=:id"),
                        {"id": location},
                    )
                    await owner.execute(
                        text("DELETE FROM public.storage_connections WHERE id=:id"),
                        {"id": connection_id},
                    )
