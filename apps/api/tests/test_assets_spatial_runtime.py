"""Real restricted Catalog runtime and public spatial consumer qualification."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_assets_catalog.spatial_contract import (
    ItemFactsPage,
    LayoutMutationResult,
    LayoutPage,
    PosePage,
    PreparationMutationResult,
    decode_spatial_reply,
)
from tests.test_asset_storage_cleanup_runtime import (
    _container_database_url,
    _docker,
    _local_s3_client,
    _remove_worker_image,
    _required_env,
    _stop_remove_container,
    _verified_resource_manifest,
    _verify_bucket_owner,
    _verify_postgres,
    _wait_healthy,
)
from tests.test_assets_catalog_runtime import (
    _catalog_origin,
    _lost_response_proxy,
    _seed_actor,
    _start_catalog,
    _wait_public_ready,
    _write_environment,
)
from tests.test_initial_task_creation_contract import (
    _accept,
    _add_job,
    _seed_uncommitted,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned resources from the module-data verifier",
    ),
]
ROOT = Path(__file__).resolve().parents[3]


async def _cleanup_spatial_fixture(engine, actor_id, operation_id):
    async with engine.begin() as owner:
        for statement, parameters in (
            (
                "DELETE FROM dataset_spatial_operations WHERE actor_id=:actor OR operation_id=:operation",
                {"actor": actor_id, "operation": operation_id},
            ),
            (
                "DELETE FROM task_creation_operations WHERE actor_id=:actor",
                {"actor": actor_id},
            ),
            (
                "DELETE FROM task_append_operations WHERE dataset_id IN (SELECT id FROM datasets WHERE created_by=:actor)",
                {"actor": actor_id},
            ),
            (
                "DELETE FROM tasks WHERE project_id IN (SELECT id FROM projects WHERE owner_id=:actor)",
                {"actor": actor_id},
            ),
            ("DELETE FROM projects WHERE owner_id=:actor", {"actor": actor_id}),
            ("DELETE FROM async_jobs WHERE user_id=:actor", {"actor": actor_id}),
            ("DELETE FROM datasets WHERE created_by=:actor", {"actor": actor_id}),
            # Retain actors referenced by immutable audit evidence until database teardown.
            (
                "DELETE FROM users WHERE id=:actor AND NOT EXISTS "
                "(SELECT 1 FROM audit_logs WHERE actor_id=:actor)",
                {"actor": actor_id},
            ),
        ):
            await owner.execute(text(statement), parameters)


async def test_spatial_container_complete_reads_replay_and_accepted_source_preparation(
    test_engine,
    app_module,
    monkeypatch,
):
    from app.config import settings
    from app.deps import get_db
    from app.db.models.dataset import Dataset, DatasetItem, Scene
    from app.db.models.task import Task
    from app.db.models.task_dataset_item_link import TaskDatasetItemLink

    catalog_url = make_url(_required_env("AAP_MODULE_DATA_ASSET_CATALOG_URL"))
    assert catalog_url.username == "asset_catalog_client"
    assert catalog_url.host == "127.0.0.1"
    assert catalog_url.database == test_engine.url.database
    _verify_postgres(catalog_url)
    resources = _verified_resource_manifest(test_engine, catalog_url)
    datasets = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    cache = resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"]
    owner_s3 = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    for bucket in (datasets, cache):
        _verify_bucket_owner(owner_s3, bucket, resources["owner"])

    identity, service_token = secrets.token_hex(12), secrets.token_urlsafe(32)
    image = f"aap-assets-spatial-acceptance:{identity}"
    actor_id = container = None
    operation_id, patch_id, calibration_patch_id = uuid4(), uuid4(), uuid4()
    objects = []
    previous_db = app_module.dependency_overrides.get(get_db)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def gateway_database():
        async with maker() as session:
            yield session

    try:
        build = await asyncio.to_thread(
            subprocess.run,
            [
                "docker",
                "build",
                "--file",
                "apps/assets-catalog/Dockerfile",
                "--tag",
                image,
                "--label",
                "org.aap.acceptance=module-data-boundary",
                "--label",
                f"org.aap.acceptance-id={identity}",
                ".",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        assert build.returncode == 0, "spatial Catalog image build failed"
        actor_id, token = await _seed_actor(test_engine)
        async with maker() as owner:
            dataset = Dataset(
                id=uuid4(),
                display_id=f"D-SP-{identity[:10]}",
                name=f"spatial-{identity}",
                data_type="point_cloud",
                created_by=actor_id,
            )
            owner.add(dataset)
            await owner.flush()
            scene = Scene(
                id=uuid4(),
                display_id=f"S-SP-{identity[:10]}",
                dataset_id=dataset.id,
                name="initial",
                created_by=actor_id,
                source_metadata={"keep": True},
            )
            owner.add(scene)
            await owner.flush()
            scene_id = scene.id
            paged_dataset = Dataset(
                id=uuid4(),
                display_id=f"D-SP-P-{identity[:10]}",
                name=f"spatial-pages-{identity}",
                data_type="point_cloud",
                file_count=1050,
                created_by=actor_id,
            )
            owner.add(paged_dataset)
            await owner.flush()
            paged_dataset_id = paged_dataset.id
            await owner.execute(
                text("""
                    WITH scenes AS (
                        INSERT INTO scenes
                            (id,display_id,dataset_id,name,source_metadata,created_by)
                        SELECT gen_random_uuid(),'SCN-'||nextval('public.display_seq_scenes')::text,
                            CAST(:dataset AS uuid),'paged-'||i,
                            jsonb_build_object('opaque',repeat('x',17000)),CAST(:actor AS uuid)
                        FROM generate_series(0,1049) i RETURNING id
                    )
                    INSERT INTO dataset_items
                        (id,dataset_id,file_name,file_path,file_type,scene_id,frame_index)
                    SELECT id,CAST(:dataset AS uuid),id::text||'.pcd',
                        'paged/'||id::text||'.pcd','point_cloud',id,0 FROM scenes
                """),
                {"dataset": paged_dataset_id, "actor": actor_id},
            )
            await owner.execute(
                text("""
                INSERT INTO scene_frame_poses
                    (id,scene_id,frame_index,timestamp_us,ego_translation,ego_rotation,source_metadata)
                SELECT gen_random_uuid(),CAST(:scene AS uuid),i,-i::bigint,
                    '[0.0,0.0,0.0]'::jsonb,'[1.0,0.0,0.0,0.0]'::jsonb,
                    jsonb_build_object('opaque',repeat('x',17000))
                FROM generate_series(0,1049) i
            """),
                {"scene": scene_id},
            )
            preparation = await _seed_uncommitted(
                owner, owner_id=actor_id, item_count=2, mode="pointcloud"
            )
            camera = DatasetItem(
                id=uuid4(),
                dataset_id=preparation.dataset_id,
                file_name="000000.jpg",
                file_path=f"{preparation.items[0].file_path.rsplit('/lidar/', 1)[0]}/camera/front/000000.jpg",
                file_type="image",
                width=17,
                height=19,
                thumbnail_path="thumbnails/keep.jpg",
                metadata_={"opaque": {"keep": True}, "video": {"keep": "value"}},
            )
            calibration = DatasetItem(
                id=uuid4(),
                dataset_id=preparation.dataset_id,
                file_name="front.json",
                file_path=f"{preparation.items[0].file_path.rsplit('/lidar/', 1)[0]}/calib/camera/front.json",
                file_type="other",
            )
            owner.add_all((camera, calibration))
            preparation_dataset = await owner.get(Dataset, preparation.dataset_id)
            assert preparation_dataset is not None
            preparation_dataset.file_count += 2
            await owner.flush()
            camera_id, calibration_key = camera.id, calibration.file_path
            job_id = await _add_job(owner, preparation, operation_id)
            accepted = await _accept(
                owner,
                operation_id=operation_id,
                actor_id=actor_id,
                dataset_id=preparation.dataset_id,
                project_id=preparation.project_id,
                association_id=preparation.association_id,
                job_id=job_id,
                mode="pointcloud",
            )
            assert accepted["ok"] is True
            await owner.commit()

        calibration_document = {
            "extrinsic": [1 if i % 5 == 0 else 0 for i in range(16)],
            "intrinsic": [1 if i % 4 == 0 else 0 for i in range(9)],
            "ignored_vendor_flag": True,
        }
        calibration_bytes = json.dumps(calibration_document).encode()
        owner_s3.put_object(
            Bucket=datasets, Key=calibration_key, Body=calibration_bytes
        )
        objects.append((datasets, calibration_key))
        app_module.dependency_overrides[get_db] = gateway_database
        monkeypatch.setattr(settings, "assets_catalog_token", service_token)
        monkeypatch.setattr(settings, "assets_catalog_timeout_seconds", 30)
        scheme = "https" if settings.minio_use_ssl else "http"
        values = {
            "ASSETS_CATALOG_DATABASE_URL": _container_database_url(catalog_url),
            "ASSETS_CATALOG_TOKEN": service_token,
            "ASSETS_CATALOG_S3_PUBLIC_ENDPOINT": f"{scheme}://{settings.minio_endpoint}",
            "ASSETS_CATALOG_S3_ENDPOINT": f"{scheme}://host.docker.internal:{settings.minio_endpoint.rsplit(':', 1)[1]}",
            "ASSETS_CATALOG_S3_REGION": "us-east-1",
            "ASSETS_CATALOG_DATASETS_BUCKET": datasets,
            "ASSETS_CATALOG_MEDIA_CACHE_BUCKET": cache,
            "ASSETS_CATALOG_S3_ACCESS_KEY": _required_env(
                "AAP_MODULE_DATA_ASSET_CATALOG_S3_ACCESS_KEY"
            ),
            "ASSETS_CATALOG_S3_SECRET_KEY": _required_env(
                "AAP_MODULE_DATA_ASSET_CATALOG_S3_SECRET_KEY"
            ),
        }
        with tempfile.TemporaryDirectory(
            prefix="spatial-runtime-", dir=os.environ["TMPDIR"]
        ) as temporary:
            environment = Path(temporary) / "catalog.env"
            _write_environment(environment, values)
            container = await asyncio.to_thread(
                _start_catalog, image, identity, environment
            )
            await asyncio.to_thread(
                _docker,
                "update",
                "--cpus",
                "0.5",
                "--memory",
                "256m",
                "--memory-swap",
                "256m",
                container,
            )
            origin = _catalog_origin(container, image, identity)
            await _wait_healthy(container)
            await _wait_public_ready(origin)
            monkeypatch.setattr(settings, "assets_catalog_url", origin)
            private_headers = {"Authorization": f"Bearer {service_token}"}
            previous_etag = owner_s3.head_object(Bucket=datasets, Key=calibration_key)[
                "ETag"
            ]
            owner_s3.put_object(
                Bucket=datasets, Key=calibration_key, Body=b'{"changed":true}'
            )
            await asyncio.to_thread(
                _docker,
                "exec",
                container,
                "python",
                "-c",
                "import sys; from aap_assets_catalog.config import load_config; "
                "from aap_assets_catalog.object_signer import CatalogObjectSigner,SourceObjectChanged; "
                "reader=CatalogObjectSigner(load_config());\n"
                "try: reader.get_calibration_source(sys.argv[1],expected_etag=sys.argv[2])\n"
                "except SourceObjectChanged: pass\n"
                "else: raise AssertionError('changed calibration source was accepted')",
                calibration_key,
                previous_etag,
            )
            owner_s3.put_object(
                Bucket=datasets, Key=calibration_key, Body=calibration_bytes
            )
            async with httpx.AsyncClient(
                base_url=origin, trust_env=False, timeout=30
            ) as direct:
                denied = await direct.post(
                    "/v1/spatial/get_spatial_scene",
                    json={"actor_id": str(actor_id), "scene_id": str(scene_id)},
                )
                assert denied.status_code == 401
                invalid = await direct.post(
                    "/v1/spatial/get_spatial_scene",
                    headers=private_headers,
                    json={
                        "actor_id": str(actor_id),
                        "scene_id": str(scene_id),
                        "role": "super_admin",
                    },
                )
                assert invalid.status_code == 422
                cursor, digest, count, pages = None, None, 0, 0
                while True:
                    response = await direct.post(
                        "/v1/spatial/read_spatial_poses",
                        headers=private_headers,
                        json={
                            "actor_id": str(actor_id),
                            "scene_id": str(scene_id),
                            "after_frame_index": cursor,
                            "page_size": 200,
                        },
                    )
                    assert response.status_code == 200
                    page = decode_spatial_reply(response.json(), PosePage)
                    assert page.after_frame_index == cursor and page.total == 1050
                    assert digest is None or page.snapshot_digest == digest
                    digest, pages = page.snapshot_digest, pages + 1
                    assert [pose.frame_index for pose in page.poses] == list(
                        range(count, count + len(page.poses))
                    )
                    count += len(page.poses)
                    if page.complete:
                        break
                    cursor = page.next_frame_index
                assert count == 1050 and pages == 6
                scene_page_evidence = {}
                for operation, response_model in (
                    ("read_spatial_layout", LayoutPage),
                    ("read_spatial_items", ItemFactsPage),
                ):
                    cursor, digest, count, page_count, byte_count = None, None, 0, 0, 0
                    while True:
                        command = {
                            "dataset_id": str(paged_dataset_id),
                            "after_item_id": cursor,
                            "page_size": 200,
                        }
                        if operation == "read_spatial_layout":
                            command["actor_id"] = str(actor_id)
                        response = await direct.post(
                            f"/v1/spatial/{operation}",
                            headers=private_headers,
                            json=command,
                        )
                        assert response.status_code == 200
                        assert len(response.content) < 16 * 1024 * 1024
                        page = decode_spatial_reply(response.json(), response_model)
                        assert page.total == 1050
                        assert digest is None or page.snapshot_digest == digest
                        assert {entry.id for entry in page.scenes} == {
                            item.scene_id for item in page.items
                        }
                        digest = page.snapshot_digest
                        count += len(page.items)
                        page_count += 1
                        byte_count += len(response.content)
                        if page.complete:
                            break
                        cursor = str(page.next_item_id)
                    assert count == 1050 and page_count == 6
                    assert byte_count > 16 * 1024 * 1024
                    scene_page_evidence[operation] = {
                        "count": count,
                        "pages": page_count,
                        "bytes": byte_count,
                    }
                backfilled = await direct.post(
                    "/v1/spatial/backfill_scenes",
                    headers=private_headers,
                    json={
                        "operation_id": str(uuid4()),
                        "actor_id": str(actor_id),
                        "dataset_id": str(paged_dataset_id),
                        "dry_run": True,
                    },
                )
                assert backfilled.status_code == 200
                layout_result = decode_spatial_reply(
                    backfilled.json(), LayoutMutationResult
                )
                assert layout_result.created_scenes == 0 and layout_result.dry_run

            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module),
                base_url="http://gateway",
                timeout=60,
            ) as api:
                start = time.monotonic()
                trajectory = await api.get(
                    f"/api/v1/scenes/{scene_id}/trajectory", headers=headers
                )
                assert trajectory.status_code == 200
                assert len(trajectory.content) > 16 * 1024 * 1024
                assert [
                    pose["frame_index"] for pose in trajectory.json()["poses"]
                ] == list(range(1050))
                trajectory_seconds, trajectory_bytes = (
                    time.monotonic() - start,
                    len(trajectory.content),
                )
                body = {
                    "name": "committed",
                    "source_metadata": {"keep": True, "new": "value"},
                }
                async with _lost_response_proxy(origin) as proxy:
                    monkeypatch.setattr(settings, "assets_catalog_url", proxy)
                    lost = await api.patch(
                        f"/api/v1/scenes/{scene_id}",
                        json=body,
                        headers={**headers, "Idempotency-Key": str(patch_id)},
                    )
                    assert lost.status_code in {502, 503}
                monkeypatch.setattr(settings, "assets_catalog_url", origin)
                replay = await api.patch(
                    f"/api/v1/scenes/{scene_id}",
                    json=body,
                    headers={**headers, "Idempotency-Key": str(patch_id)},
                )
                assert (
                    replay.status_code == 200 and replay.json()["name"] == "committed"
                )
                assert replay.headers["Idempotency-Key"] == str(patch_id)

                async with maker() as owner:
                    await owner.execute(
                        text("UPDATE users SET is_active=false WHERE id=:actor"),
                        {"actor": actor_id},
                    )
                    await owner.commit()
                async with httpx.AsyncClient(
                    base_url=origin, trust_env=False, timeout=30
                ) as direct:
                    prepared_response = await direct.post(
                        "/v1/spatial/prepare_pointcloud",
                        headers=private_headers,
                        json={"creation_operation_id": str(operation_id)},
                    )
                    assert prepared_response.status_code == 200
                    prepared = decode_spatial_reply(
                        prepared_response.json(), PreparationMutationResult
                    )
                    assert (
                        prepared.creation_operation_id == operation_id
                        and prepared.calibrated_items == 1
                    )
                async with maker() as owner:
                    source = await owner.get(DatasetItem, camera_id)
                    assert source.width == 17 and source.height == 19
                    assert source.thumbnail_path == "thumbnails/keep.jpg"
                    assert source.metadata_["opaque"] == {"keep": True}
                    assert source.metadata_["video"] == {"keep": "value"}
                    assert "ignored_vendor_flag" not in source.metadata_["calibration"]
                    task = Task(
                        id=uuid4(),
                        display_id=f"T-SP-{identity[:10]}",
                        project_id=preparation.project_id,
                        dataset_item_id=preparation.items[0].id,
                        file_name="000000.pcd",
                        file_path=preparation.items[0].file_path,
                        file_type="point_cloud",
                    )
                    owner.add(task)
                    await owner.flush()
                    owner.add(
                        TaskDatasetItemLink(
                            task_id=task.id,
                            dataset_item_id=camera_id,
                            role="camera_front",
                            sensor_name="front",
                        )
                    )
                    task_id = task.id
                    await owner.execute(
                        text("UPDATE users SET is_active=true WHERE id=:actor"),
                        {"actor": actor_id},
                    )
                    await owner.commit()
                calibration_path = f"/api/v1/tasks/{task_id}/point-cloud/cameras/camera_front/calibration"
                history = await api.get(calibration_path, headers=headers)
                assert history.status_code == 200, history.text
                current = history.json()
                changed_calibration = dict(calibration_document)
                changed_calibration.pop("ignored_vendor_flag")
                changed_calibration["extrinsic"] = [*changed_calibration["extrinsic"]]
                changed_calibration["extrinsic"][3] = 2.0
                command = {
                    "expected_revision": current["current_revision"],
                    "expected_digest": current["current_digest"],
                    "calibration": changed_calibration,
                }
                changed = await api.patch(
                    calibration_path,
                    headers={**headers, "Idempotency-Key": str(calibration_patch_id)},
                    json=command,
                )
                assert changed.status_code == 200, changed.text
                assert changed.json()["revision"] == 2
                repeated = await api.patch(
                    calibration_path,
                    headers={**headers, "Idempotency-Key": str(calibration_patch_id)},
                    json=command,
                )
                assert repeated.status_code == 200 and repeated.json() == changed.json()
                async with test_engine.connect() as owner:
                    assert (
                        await owner.scalar(
                            text(
                                "SELECT count(*) FROM audit_logs WHERE actor_id=:actor AND action='sensor_calibration.update'"
                            ),
                            {"actor": actor_id},
                        )
                        == 1
                    )
                    assert (
                        await owner.scalar(
                            text(
                                "SELECT count(*) FROM dataset_spatial_operations WHERE operation_id=:operation"
                            ),
                            {"operation": patch_id},
                        )
                        == 1
                    )

                await asyncio.to_thread(_docker, "stop", "--time", "15", container)
                unavailable = await api.get(
                    f"/api/v1/scenes/{scene_id}", headers=headers
                )
                assert unavailable.status_code in {502, 503}
                monkeypatch.setattr(settings, "assets_catalog_url", "")
                local = await api.get(f"/api/v1/scenes/{scene_id}", headers=headers)
                assert local.status_code == 200 and local.json() == replay.json()
                await asyncio.to_thread(_docker, "start", container)
                await _wait_healthy(container)
                origin = _catalog_origin(container, image, identity)
                await _wait_public_ready(origin)
                monkeypatch.setattr(settings, "assets_catalog_url", origin)
                restarted = await api.patch(
                    f"/api/v1/scenes/{scene_id}",
                    json=body,
                    headers={**headers, "Idempotency-Key": str(patch_id)},
                )
                assert (
                    restarted.status_code == 200 and restarted.json() == replay.json()
                )

            peak = int(_docker("exec", container, "cat", "/sys/fs/cgroup/memory.peak"))
            report = {
                "pose_count": 1050,
                "pages": pages,
                "public_trajectory_bytes": trajectory_bytes,
                "public_trajectory_seconds": trajectory_seconds,
                "scene_pages": scene_page_evidence,
                "catalog_cpus": 0.5,
                "catalog_memory_limit_bytes": 256 * 1024 * 1024,
                "catalog_memory_peak_bytes": peak,
                "accepted_actor_disabled": True,
                "actual_s3_changed_source_rejected": True,
                "calibration_replay_audits": 1,
                "scene_lost_response_replay": True,
                "restart_and_local_cutover": True,
            }
            report_path = ROOT / ".worktree/assets-spatial-runtime-evidence.json"
            report_path.parent.mkdir(exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2) + "\n")
    finally:
        if previous_db is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_db
        if container is not None:
            await asyncio.to_thread(_stop_remove_container, container, identity)
        await asyncio.to_thread(_remove_worker_image, image, identity)
        for bucket, key in objects:
            owner_s3.delete_object(Bucket=bucket, Key=key)
        if actor_id is not None:
            await _cleanup_spatial_fixture(test_engine, actor_id, operation_id)
