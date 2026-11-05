"""Private native ZIP retirement against a coordinator-owned disposable store.

This case waits for the real 15-minute deadline; failures intentionally retain
its exact fixture evidence instead of sweeping storage or forcing retirement.
"""

import asyncio
import os
import sys
import uuid
from contextlib import closing
from datetime import timezone
from pathlib import Path
from zipfile import ZipFile

import pytest
from app.config import settings
from app.db.models.export_output import ExportOutput
from app.services import (
    async_job,
    export_media_retirement,
    export_outputs,
    export_storage,
)
from app.services import fixture_run_ownership as ownership
from app.workers.export import _ExportExecution
from botocore.exceptions import ClientError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests import live_provider_guard
from tests.factory import create_project, create_user
from tests.seed_owner_support import private_seed_owner
from tests.test_asset_storage_cleanup_runtime import (
    _local_s3_client,
    _verified_resource_manifest,
    _verify_bucket_owner,
)

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from fixture_suite_coordinator import verified_fixture_owner_url  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
    or os.environ.get("AAP_FIXTURE_EXPORT_ZIP_LIVE") != "1",
    reason="requires the explicitly enabled real-deadline fixture ZIP verifier",
)


@pytest.mark.asyncio
async def test_native_fixture_retires_only_its_verified_zip_version(
    test_engine, tmp_path
):
    suite_id = uuid.uuid4()
    async with private_seed_owner() as coordinator:
        owner_url = await verified_fixture_owner_url()
        engine = create_async_engine(
            owner_url, hide_parameters=True, poolclass=NullPool
        )
        try:
            # Refuse an owner for any database other than the verifier's database.
            assert (engine.url.host, engine.url.port or 5432, engine.url.database) == (
                test_engine.url.host,
                test_engine.url.port or 5432,
                test_engine.url.database,
            )
            resources = _verified_resource_manifest(test_engine, engine.url)
            bucket = resources["buckets"]["MINIO_EXPORT_BUCKET"]
            assert bucket == settings.minio_export_bucket
            with closing(
                _local_s3_client(
                    settings, settings.minio_access_key, settings.minio_secret_key
                )
            ) as store:
                _verify_bucket_owner(store, bucket, resources["owner"])
                inventory = store.list_object_versions(Bucket=bucket, MaxKeys=1)
                assert not inventory.get("Versions") and not inventory.get(
                    "DeleteMarkers"
                )
                store.put_bucket_versioning(
                    Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
                )
            await coordinator.prepare_suite(suite_id, "screenshot")
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                admission = await ownership.admit(
                    db, suite_id, "zip_version_regression"
                )
                actor = await create_user(
                    db, "super_admin", f"{suite_id}@fixture.test", "ZIP version owner"
                )
                await ownership.reserve(
                    db, suite_id, "user", actor.id, logical_key="actor", published=True
                )
                project = await create_project(
                    db, owner_id=actor.id, type_key="lidar-detection"
                )
                project.data_type = "lidar"
                await db.flush()
                await ownership.reserve(
                    db,
                    suite_id,
                    "project",
                    project.id,
                    logical_key="project",
                    published=True,
                )
                job = await async_job.create_job(
                    db,
                    kind="export",
                    user_id=actor.id,
                    project_id=project.id,
                    payload={"targets": ["native_json"]},
                )
                job_id, project_id = job.id, project.id
                await ownership.settle(db, suite_id, admission)
                await db.commit()

            path = tmp_path / "export.zip"
            with ZipFile(path, "w") as archive:
                archive.writestr("annotations.json", "{}")
            content = path.read_bytes()
            async with (
                engine.connect() as connection,
                AsyncSession(bind=connection, expire_on_commit=False) as db,
            ):
                execution = _ExportExecution(db, connection)
                await execution.acquire(f"job:{job_id}")
                assert await execution.admit_fixture(job_id)
                await execution.begin_fixture(job_id, project_id)
                output = await export_outputs.create(
                    db,
                    project_id=project_id,
                    job_id=job_id,
                    media="image",
                    format="native_json",
                    file_count=1,
                    path=str(path),
                )
                assert output.fixture
                with pytest.raises(DBAPIError) as unproven:
                    async with db.begin_nested():
                        await db.execute(
                            text(
                                "UPDATE public.export_outputs SET state='ready' WHERE id=:id"
                            ),
                            {"id": output.id},
                        )
                assert unproven.value.orig.sqlstate == "23514"
                live_provider_guard.owned_bucket(
                    output.storage_ref["bucket"], purpose="export"
                )
                target = await export_storage.io_target(db, output)
                revision = await export_outputs.write(db, output, str(path))
                row = await export_outputs.publish(db, output, revision)
                await db.commit()
                proof = dict(row.upload_proof)
                assert proof["version_id"] and proof["version_id"] != "null"
                with export_storage.client_context(
                    output, target, owned=True
                ) as client:
                    bucket = output.storage_ref["bucket"]
                    assert (
                        client.get_bucket_versioning(Bucket=bucket)["Status"]
                        == "Enabled"
                    )
                    params = {"Bucket": bucket, "Key": output.storage_ref["object_key"]}
                    response = client.get_object(
                        **params, VersionId=proof["version_id"]
                    )
                    with closing(response["Body"]) as body:
                        assert body.read() == content
                    neighbor = client.put_object(
                        **params, Body=b"independent neighboring version"
                    )
                    neighbor_version = neighbor["VersionId"]
                    assert neighbor_version != proof["version_id"]
                # This test deliberately cancels its own unpublished Job after the
                # output was verified and made ready, rather than inventing completion.
                await async_job.mark_cancelled(db, job_id)
                await db.commit()
                await execution.finish_fixture(job_id)

            async with sessions() as db:
                assert not await ownership.seal(db, suite_id, family="screenshot")
                await db.commit()
                assert not await export_media_retirement.retire_fixture_export(
                    db, run_id=suite_id, project_id=project_id, job_id=job_id
                )
                current = await db.scalar(text("SELECT pg_catalog.clock_timestamp()"))
                await db.commit()
            remaining = (
                output.expires_at - current.astimezone(timezone.utc)
            ).total_seconds()
            await asyncio.sleep(max(0, remaining) + 1)
            async with sessions() as db:
                assert await export_media_retirement.retire_fixture_export(
                    db, run_id=suite_id, project_id=project_id, job_id=job_id
                )
                assert (
                    await db.scalar(
                        select(ExportOutput.id).where(ExportOutput.id == output.id)
                    )
                    is None
                )
                ledger = (
                    (
                        await db.execute(
                            text(
                                "SELECT run_id,project_id,job_id,storage_ref,sha256,"
                                "size_bytes,upload_proof,confirmed_absent_at "
                                "FROM yoao_fixture_v1.fixture_export_zip_versions WHERE output_id=:id"
                            ),
                            {"id": output.id},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert (ledger["run_id"], ledger["project_id"], ledger["job_id"]) == (
                    suite_id,
                    project_id,
                    job_id,
                )
                assert ledger["storage_ref"] == output.storage_ref
                assert (
                    ledger["sha256"],
                    ledger["size_bytes"],
                    ledger["upload_proof"],
                ) == (output.sha256, output.size_bytes, proof)
                assert ledger["confirmed_absent_at"] is not None
                await db.commit()

            with export_storage.client_context(output, target, owned=True) as client:
                with pytest.raises(ClientError) as missing:
                    client.get_object(**params, VersionId=proof["version_id"])
                assert (
                    missing.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
                )
                response = client.get_object(**params, VersionId=neighbor_version)
                with closing(response["Body"]) as body:
                    assert body.read() == b"independent neighboring version"
                # The neighboring version was created by this test, not discovered
                # by listing or adopted from another owner.
                deleted = client.delete_object(**params, VersionId=neighbor_version)
                assert deleted.get("VersionId") == neighbor_version
            response = await coordinator.post(
                "/api/v1/__test/seed/screenshot-cleanup", json={"run_id": str(suite_id)}
            )
            assert response.status_code == 200, response.text
        finally:
            await engine.dispose()
