"""Exercise local image composition as the real, non-owner application LOGIN."""

import asyncio
from contextlib import AsyncExitStack
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from aap_assets_images import contract as images
from aap_assets_input_manifests import contract as manifests
from aap_assets_inputs import contract as inputs
from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import ProjectDataset
from app.services.accepted_input_manifests import (
    get_input_manifest_capture,
    input_manifest_gateway,
)
from app.services.accepted_inputs import (
    attach_annotation_input_source,
    copy_annotation_input_sources,
    input_gateway,
    lock_input_source,
)
from app.services.assets_images import image_gateway, image_policy, run_image_job
from tests.factory import create_project, create_task
from tests.test_asset_storage_cleanup_runtime import (
    _local_s3_client,
    _required_env,
    _verified_resource_manifest,
    _verify_bucket_owner,
    _verify_postgres,
)
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset
from tests.test_assets_images_execution import _seed_image
from tests.test_assets_images_live import _solid_png, _srgb_profile

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned disposable PostgreSQL and S3 resources",
    ),
]
ROOT = Path(__file__).resolve().parents[3]


async def _apply_lan_grants(test_engine, monkeypatch):
    """Apply the actual deployment script after removing inherited test grants."""
    _verify_postgres(test_engine.url)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    from verify_module_data_boundary import _psql

    await asyncio.to_thread(
        _psql,
        _required_env("AAP_MODULE_DATA_PG_CONTAINER_ID"),
        _required_env("AAP_MODULE_DATA_PG_CONTAINER_NAME"),
        _required_env("AAP_MODULE_DATA_PG_CONTAINER_IDENTITY"),
        username=test_engine.url.username,
        password=test_engine.url.password,
        database=test_engine.url.database,
        sql=(
            "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_assets_v1, "
            "aap_labeling_v1 FROM legacy_runtime;\n"
            + (ROOT / "infra/docker/lan-production-grants.sql").read_text()
        ),
        variables={
            "expected_database": test_engine.url.database,
            "application_role": "legacy_runtime",
            "migration_role": test_engine.url.username,
            "asset_cleanup_role": "asset_cleanup_client",
        },
    )


@pytest.mark.parametrize("grant_profile", ["worktree", "lan"])
async def test_legacy_login_materializes_images_manifests_and_annotation_pins(
    test_engine, tmp_path, monkeypatch, grant_profile
):
    runtime_url = make_url(_required_env("AAP_MODULE_DATA_LEGACY_RUNTIME_URL"))
    assert runtime_url.username == "legacy_runtime"
    assert (runtime_url.host, runtime_url.port, runtime_url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    resource = _verified_resource_manifest(test_engine, runtime_url)
    if grant_profile == "lan":
        await _apply_lan_grants(test_engine, monkeypatch)

    monkeypatch.setattr(settings, "assets_media_url", "")
    monkeypatch.setattr(settings, "minio_public_url", "")
    monkeypatch.setattr(settings, "ml_backend_storage_host", "")
    monkeypatch.setattr(settings, "image_pyramid_srgb_profile", str(_srgb_profile()))
    owner_s3 = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    bucket = resource["buckets"]["MINIO_DATASETS_BUCKET"]
    _verify_bucket_owner(owner_s3, bucket, resource["owner"])
    key = f"legacy-image-login/{uuid4()}.png"
    body = _solid_png((16, 8), (240, 20, 30))
    await asyncio.to_thread(owner_s3.put_object, Bucket=bucket, Key=key, Body=body)

    async with AsyncExitStack() as stack:
        stack.callback(owner_s3.close)
        engine = create_async_engine(
            runtime_url, poolclass=NullPool, hide_parameters=True
        )
        stack.push_async_callback(engine.dispose)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            assert await db.scalar(text("SELECT current_user")) == "legacy_runtime"
            assert not await db.scalar(
                text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
            )
            assert not await db.scalar(
                text("SELECT pg_has_role(current_user, :owner, 'MEMBER')"),
                {"owner": test_engine.url.username},
            )
            for signature in (
                "aap_assets_v1.sweep_accepted_image_inputs(integer)",
                "aap_assets_v1.claim_accepted_input_garbage(uuid,integer,integer)",
                "aap_assets_v1.complete_accepted_input_garbage(uuid,uuid)",
                "aap_assets_v1._lock_accepted_image_input_receipt(uuid,uuid,uuid)",
                "aap_assets_v1._lock_accepted_image_input_pin(uuid,uuid,text)",
            ):
                assert not await db.scalar(
                    text(
                        "SELECT has_function_privilege(current_user,:function,'EXECUTE')"
                    ),
                    {"function": signature},
                ), signature

        # Only fixture creation uses the owner. Every capability call below uses
        # the same LOGIN as the API and ordinary Celery workers.
        owner_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
        async with owner_sessions.begin() as db:
            actor = await _seed_actor(db)
            project = await create_project(db, owner_id=actor)
            dataset = await _seed_dataset(db, actor_id=actor)
            db.add(ProjectDataset(project_id=project.id, dataset_id=dataset))
            item = await _seed_image(
                db,
                actor_id=actor,
                dataset_id=dataset,
                width=16,
                height=8,
                bind_identity=False,
            )
            await db.execute(
                text(
                    "UPDATE dataset_items SET file_path=:key,file_size=:size WHERE id=:item"
                ),
                {"key": key, "size": len(body), "item": item},
            )
            task = await create_task(db, project_id=project.id)
            task.dataset_item_id = item
            task_id, project_id = task.id, project.id
            workbench_job = uuid4()
            db.add(
                AsyncJob(
                    id=workbench_job,
                    kind="batch_predict",
                    status="pending",
                    celery_task_id=str(workbench_job),
                    project_id=project_id,
                    user_id=actor,
                    payload={
                        "execution_scope": "workbench",
                        "task_ids": [str(task_id)],
                        "scope": {"task_ids": [str(task_id)]},
                    },
                    result={},
                )
            )

        async with input_gateway(session_factory=sessions) as gateway:
            request = inputs.FindInputRequest(
                job_id=workbench_job, task_id=task_id, actor_id=actor
            )
            assert (await gateway.find(request)).input is None
            accepted = await gateway.accept(request)
            assert (await gateway.accept(request)).input_id == accepted.input_id
            scope = inputs.InputRequest(
                input_id=accepted.input_id, job_id=workbench_job, task_id=task_id
            )
            reference = await gateway.ensure_reference(scope)
            record = await gateway.record(scope)
            assert (
                record.status == "ready" and record.sha256 == sha256(body).hexdigest()
            )
            assert (await gateway.verify_source(scope)).sha256 == record.sha256
            destination = tmp_path / "accepted-original.png"
            await gateway.download(reference, destination)
            assert destination.read_bytes() == body

        selection = manifests.ManifestSelection(
            predict_mode="overwrite", task_ids=[task_id]
        )
        bulk_job = uuid4()
        capture_request = manifests.CaptureManifestRequest(
            job_id=bulk_job, actor_id=actor, project_id=project_id, selection=selection
        )
        async with sessions.begin() as db:
            job = AsyncJob(
                id=bulk_job,
                kind="batch_predict",
                status="pending",
                celery_task_id=str(bulk_job),
                project_id=project_id,
                user_id=actor,
                payload={
                    "execution_scope": "bulk",
                    "batch_id": None,
                    "accepted_input_manifest_protocol": manifests.MANIFEST_SCHEMA,
                    "accepted_input_manifest_id": str(bulk_job),
                    "input_selection": selection.model_dump(mode="json"),
                    "task_ids": [str(task_id)],
                    "scope": {"task_ids": [str(task_id)]},
                },
                result={},
            )
            db.add(job)
            header = await get_input_manifest_capture()(db).capture(capture_request)
            job.payload = {
                **job.payload,
                "accepted_input_manifest_digest": header.selection_digest,
            }
        manifest_scope = manifests.ManifestScope(
            job_id=bulk_job, actor_id=actor, project_id=project_id
        )
        async with input_manifest_gateway(session_factory=sessions) as gateway:
            sealed = await gateway.ensure_sealed(
                manifest_scope, selection_digest=header.selection_digest
            )
            assert (
                sealed.status == "sealed"
                and sealed.selected_count == sealed.ready_count == 1
            )
            page = await gateway.page(
                manifests.ManifestPageRequest(
                    **manifest_scope.model_dump(),
                    selection_digest=header.selection_digest,
                    sealed_digest=sealed.sealed_digest,
                    page_size=1,
                )
            )
            assert page.members[0].input.sha256 == sha256(body).hexdigest()

        async with sessions.begin() as db:
            assert (
                await lock_input_source(
                    db, input_id=record.input_id, task_id=task_id, project_id=project_id
                )
            ).input_id == record.input_id
            parent = Annotation(
                task_id=task_id,
                project_id=project_id,
                user_id=actor,
                class_name="car",
                geometry={"type": "bbox", "x": 1, "y": 1, "width": 5, "height": 5},
            )
            child_task = await create_task(db, project_id=project_id)
            db.add(parent)
            await db.flush()
            child = Annotation(
                task_id=child_task.id,
                project_id=project_id,
                user_id=actor,
                class_name="car",
                geometry=parent.geometry,
            )
            db.add(child)
            await db.flush()
            assert await attach_annotation_input_source(
                db,
                annotation_id=parent.id,
                input_id=record.input_id,
                sha256=record.sha256,
            )
            assert (
                await copy_annotation_input_sources(
                    db, source_annotation_id=parent.id, target_annotation_id=child.id
                )
                == 1
            )

        async with sessions() as db, image_gateway(db) as gateway:
            work = await gateway.prepare_image_media(
                images.PrepareImageMediaRequest(
                    actor_id=actor,
                    task_id=task_id,
                    operation_id=uuid4(),
                    purpose="thumbnail",
                    policy=image_policy(),
                )
            )
        assert await run_image_job(work.work.job_id, session_factory=sessions)
        async with sessions() as db, image_gateway(db) as gateway:
            reference = await gateway.source_reference(
                images.ReadImageSourceRequest(actor_id=actor, task_id=task_id)
            )
            async with httpx.AsyncClient(trust_env=False) as http:
                response = await http.get(reference.thumbnail_url)
            assert response.status_code == 200
            with Image.open(BytesIO(response.content)) as thumbnail:
                assert thumbnail.format == "WEBP"
                assert thumbnail.getpixel((128, 64))[0] > 220
        # All receipts and referenced users remain until the verifier destroys
        # this test's whole database and versioned buckets.
