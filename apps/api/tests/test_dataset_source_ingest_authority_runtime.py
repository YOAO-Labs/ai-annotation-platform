"""Real Source process: seal the entire ZIP before authority-fenced publication."""

from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
import os
from pathlib import Path
import re
import secrets
import socket
import sys
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.security import create_access_token
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.managed_original_creation import ManagedOriginalCreation
from app.deps import get_db
from app.services.storage import storage_service
from tests.test_asset_storage_cleanup_runtime import (
    _local_s3_client,
    _required_env,
    _verified_resource_manifest,
    _verify_postgres,
)
from tests.test_dataset_upload_native import _cleanup_world, _seed_world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned disposable module-data verifier",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
FUNCTION = "aap_assets_v1.complete_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb)"


def _entries():
    return {
        f"lidar/{index:06d}.pcd": (
            "# .PCD v0.7\nVERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\n"
            "TYPE F F F\nCOUNT 1 1 1\nWIDTH 1\nHEIGHT 1\n"
            "VIEWPOINT 0 0 0 1 0 0 0\nPOINTS 1\nDATA ascii\n"
            f"{index}.0 0.0 0.0\n"
        ).encode()
        for index in range(201)
    }


def _archive(entries):
    out = BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return out.getvalue()


async def _wait_ready(process, origin):
    async with httpx.AsyncClient(trust_env=False) as client:
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            assert process.returncode is None, "Source process exited during startup"
            try:
                if (await client.get(origin + "/health")).status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            await asyncio.sleep(0.1)
    raise AssertionError("Source process did not become healthy")


async def _wait_gate(maker, request):
    deadline = asyncio.get_running_loop().time() + 180
    while asyncio.get_running_loop().time() < deadline:
        if request.done():
            response = await request
            raise AssertionError(f"request ended before gate: {response.status_code} {response.text}")
        async with maker() as db:
            waiting = await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                    "WHERE datname=current_database() AND wait_event='advisory' "
                    "AND query LIKE '%complete_dataset_ingest_batch%')"
                )
            )
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("Source did not reach the native publication gate")


async def _counts(maker, dataset_id):
    async with maker() as db:
        return (
            await db.scalar(select(func.count()).select_from(DatasetItem).where(DatasetItem.dataset_id == dataset_id)),
            await db.scalar(select(func.count()).select_from(Scene).where(Scene.dataset_id == dataset_id)),
            await db.scalar(select(Dataset.file_count).where(Dataset.id == dataset_id)),
        )


async def _physical_proofs(maker, dataset_id, entries, client, bucket):
    async with maker() as db:
        rows = list(
            await db.scalars(
                select(ManagedOriginalCreation).where(
                    ManagedOriginalCreation.dataset_id == dataset_id
                )
            )
        )
    assert len(rows) == len(entries)
    for row in rows:
        assert row.status == "verified" and row.bucket == bucket
        relative = next(name for name in entries if row.logical_path.endswith("/" + name) or row.logical_path == name)
        expected = entries[relative]
        assert row.sha256 == hashlib.sha256(expected).hexdigest()
        assert row.size_bytes == len(expected)
        params = {"Bucket": row.bucket, "Key": row.object_key}
        if row.version_id is not None:
            params["VersionId"] = row.version_id
        response = await asyncio.to_thread(client.get_object, **params)
        try:
            assert await asyncio.to_thread(response["Body"].read) == expected
        finally:
            response["Body"].close()
    return {row.id for row in rows}


@pytest_asyncio.fixture
async def ordinary_source_stack(test_engine, app_module, monkeypatch, tmp_path):
    source_url = make_url(_required_env("AAP_MODULE_DATA_ASSET_SOURCE_URL"))
    api_url = make_url(_required_env("DATABASE_URL"))
    target = (test_engine.url.host, test_engine.url.port, test_engine.url.database)
    assert source_url.username == "asset_source_client"
    assert (source_url.host, source_url.port, source_url.database) == target
    assert (api_url.host, api_url.port, api_url.database) == target
    assert source_url.database.startswith("aap_wt_") and source_url.database.endswith("_test")
    _verify_postgres(source_url)
    resources = _verified_resource_manifest(test_engine, source_url)
    bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    assert settings.minio_datasets_bucket == bucket
    source_access = _required_env("AAP_MODULE_DATA_ASSET_SOURCE_S3_ACCESS_KEY")
    source_secret = _required_env("AAP_MODULE_DATA_ASSET_SOURCE_S3_SECRET_KEY")
    source_s3 = _local_s3_client(settings, source_access, source_secret)
    api_engine = create_async_engine(api_url, hide_parameters=True)
    source_engine = create_async_engine(source_url, hide_parameters=True)
    for engine, url in ((api_engine, api_url), (source_engine, source_url)):
        async with engine.connect() as db:
            role = (await db.execute(text("SELECT current_user, rolsuper FROM pg_roles WHERE rolname=current_user"))).one()
            assert role.current_user == url.username and not role.rolsuper

    async def ordinary_db():
        async with async_sessionmaker(api_engine, expire_on_commit=False)() as db:
            yield db

    world = await _seed_world(test_engine)
    process = None
    previous = dict(app_module.dependency_overrides)
    try:
        async with world.maker.begin() as db:
            await db.execute(update(Dataset).where(Dataset.id == world.dataset_id).values(data_type="point_cloud", is_temporal=True))
            await db.execute(ProjectDataset.__table__.delete().where(ProjectDataset.dataset_id == world.dataset_id))
        app_module.dependency_overrides[get_db] = ordinary_db
        token = secrets.token_urlsafe(32)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        scheme = "https" if settings.minio_use_ssl else "http"
        origin = f"http://127.0.0.1:{port}"
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "PYTHONPATH": str(ROOT / "apps/assets-source/src"),
            "TMPDIR": _required_env("TMPDIR"),
            "ASSETS_SOURCE_HOST": "127.0.0.1",
            "ASSETS_SOURCE_PORT": str(port),
            "ASSETS_SOURCE_DATABASE_URL": source_url.render_as_string(hide_password=False),
            "ASSETS_SOURCE_TOKEN": token,
            "ASSETS_SOURCE_S3_ENDPOINT": f"{scheme}://{settings.minio_endpoint}",
            "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT": f"{scheme}://{settings.minio_endpoint}",
            "ASSETS_SOURCE_S3_ACCESS_KEY": source_access,
            "ASSETS_SOURCE_S3_SECRET_KEY": source_secret,
            "ASSETS_SOURCE_DATASETS_BUCKET": bucket,
            "ASSETS_SOURCE_IMPORT_BUCKET": resources["buckets"]["MINIO_IMPORT_BUCKET"],
            "ASSETS_SOURCE_MEDIA_CACHE_BUCKET": resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"],
            "ASSETS_SOURCE_SQL_TIMEOUT_SECONDS": "60",
        }
        with (tmp_path / "source.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "aap_assets_source_runtime", cwd=ROOT,
                env=env, stdout=log, stderr=log,
            )
            await _wait_ready(process, origin)
            monkeypatch.setattr(settings, "assets_source_timeout_seconds", 240)
            monkeypatch.setattr(settings, "assets_source_url", origin)
            monkeypatch.setattr(settings, "assets_source_token", token)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module),
                base_url="http://ordinary-api", timeout=240,
            ) as api:
                yield SimpleNamespace(world=world, api=api, source_s3=source_s3, bucket=bucket)
    finally:
        app_module.dependency_overrides.clear()
        app_module.dependency_overrides.update(previous)
        if process is not None and process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=30)
        prefix = f"dataset-sources/{world.dataset_id}/"
        versions = await asyncio.to_thread(
            storage_service.client.list_object_versions, Bucket=bucket, Prefix=prefix
        )
        for item in versions.get("Versions", []) + versions.get("DeleteMarkers", []):
            await asyncio.to_thread(storage_service.client.delete_object, Bucket=bucket, Key=item["Key"], VersionId=item["VersionId"])
        async with world.maker.begin() as db:
            for table in ("dataset_source_ingest_deliveries", "dataset_source_ingest_entries"):
                await db.execute(text(f"DELETE FROM public.{table} WHERE operation_id IN (SELECT operation_id FROM public.dataset_source_ingest_operations WHERE dataset_id=:dataset)"), {"dataset": world.dataset_id})
            await db.execute(text("DELETE FROM public.dataset_source_ingest_operations WHERE dataset_id=:dataset"), {"dataset": world.dataset_id})
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(text("DELETE FROM public.audit_logs WHERE actor_id=ANY(CAST(:ids AS uuid[]))"), {"ids": [world.actor_id, world.other_actor_id]})
            creation_ids = list(await db.scalars(select(ManagedOriginalCreation.id).where(ManagedOriginalCreation.dataset_id == world.dataset_id)))
            await db.execute(text("DELETE FROM public.storage_usage_mutations WHERE owner_id=ANY(CAST(:ids AS uuid[]))"), {"ids": creation_ids})
            await db.execute(text("DELETE FROM public.managed_original_creations WHERE id=ANY(CAST(:ids AS uuid[]))"), {"ids": creation_ids})
        await _cleanup_world(world)
        await api_engine.dispose()
        await source_engine.dispose()


@pytest.mark.parametrize("rotate_key", [False, True], ids=["201-success", "201-late-key-rotation"])
async def test_remote_zip_seals_every_entry_before_authority_fence(ordinary_source_stack, rotate_key):
    stack = ordinary_source_stack
    world = stack.world
    jwt = {"Authorization": f"Bearer {create_access_token(str(world.actor_id), 'super_admin')}"}
    issued = await stack.api.post("/api/v1/me/api-keys", headers=jwt, json={"name": "standalone-ingest", "scopes": ["*"]})
    assert issued.status_code == 201, issued.text
    key = issued.json()
    operation = uuid4()
    entries = _entries()
    headers = {"Authorization": "Bearer " + key["plaintext"], "Idempotency-Key": str(operation)}
    async with world.maker() as db:
        original = await db.scalar(text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"), {"signature": FUNCTION})
    gate_key = secrets.randbits(62)
    gated, replaced = re.subn(
        r"(?im)^\s*BEGIN\s*$",
        f"BEGIN\n    IF p_operation_id = '{operation}'::uuid THEN PERFORM pg_catalog.pg_advisory_xact_lock({gate_key}); END IF;",
        original, count=1,
    )
    assert replaced == 1
    request = None
    async with world.maker() as gate:
        await gate.execute(text("SELECT pg_advisory_lock(:key)"), {"key": gate_key})
        try:
            async with world.maker.begin() as db:
                await db.execute(text(gated))
            request = asyncio.create_task(stack.api.post(
                f"/api/v1/datasets/{world.dataset_id}/items/upload-zip",
                headers=headers, files={"file": ("clouds.zip", _archive(entries), "application/zip")},
            ))
            await _wait_gate(world.maker, request)
            assert await _counts(world.maker, world.dataset_id) == (0, 0, 0)
            proof_ids = await _physical_proofs(world.maker, world.dataset_id, entries, stack.source_s3, stack.bucket)
            if rotate_key:
                rotated = await asyncio.wait_for(stack.api.post(f"/api/v1/me/api-keys/{key['id']}/rotate", headers=jwt), timeout=5)
                assert rotated.status_code == 200, rotated.text
            await gate.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": gate_key})
            response = await asyncio.wait_for(request, timeout=120)
            counts = await _counts(world.maker, world.dataset_id)
            if rotate_key:
                assert response.status_code == 403, response.text
                assert counts == (0, 0, 0)
                assert await _physical_proofs(world.maker, world.dataset_id, entries, stack.source_s3, stack.bucket) == proof_ids
                async with world.maker() as db:
                    assert not await db.scalar(text("SELECT EXISTS(SELECT 1 FROM public.dataset_source_ingest_operations WHERE operation_id=:operation)"), {"operation": operation})
            else:
                assert response.status_code == 200, response.text
                assert response.json()["added"] == 201
                assert counts[0] == counts[2] == 201 and counts[1] > 0
        finally:
            await gate.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": gate_key})
            if request is not None and not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
            async with world.maker.begin() as db:
                await db.execute(text(original))
            async with world.maker() as db:
                restored = await db.scalar(text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"), {"signature": FUNCTION})
                assert restored == original
