"""Verifier-owned container proof for the independent Assets media runtime."""

from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
import os
import secrets
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from PIL import Image
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from yoao_assets_media import contract as media
from app.services.assets_media import media_gateway, run_media_job

from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from tests.assets_media_compose_support import CONTROL, VIDEO, MediaCompose
from tests.factory import create_project
from tests.test_asset_storage_cleanup_runtime import (
    _container_s3_endpoint,
    _inspect_docker,
    _local_s3_client,
    _required_env,
    _verified_resource_manifest,
    _verify_bucket_owner,
    _wait_healthy,
)
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset
from tests.test_assets_catalog_runtime import _ordinary_assets_engine
from tests.test_assets_media_live import (
    _assert_sql_role_is_narrow,
    _delete_objects,
    _guard_role_url,
    _job_for_frame,
    _post,
    _wait_asset_ready,
    _wait_source_ready,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier's owned PostgreSQL and S3 resources",
    ),
]


def _container_s3_origins(settings) -> tuple[str, str]:
    scheme = "https" if settings.minio_use_ssl else "http"
    public = f"{scheme}://{settings.minio_endpoint}"
    return _container_s3_endpoint(settings), public


def _container_environment(
    *,
    role: str,
    database_url: str,
    s3_endpoint: str,
    s3_public_endpoint: str,
    datasets_bucket: str,
    cache_bucket: str,
    access_key: str,
    secret_key: str,
    legacy_bucket: str | None = None,
    token: str | None = None,
) -> dict[str, str]:
    common = {
        "ASSETS_MEDIA_S3_ENDPOINT": s3_endpoint,
        "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT": s3_public_endpoint,
        "ASSETS_MEDIA_S3_REGION": "us-east-1",
        "ASSETS_MEDIA_S3_ACCESS_KEY": access_key,
        "ASSETS_MEDIA_S3_SECRET_KEY": secret_key,
        "ASSETS_MEDIA_DATASETS_BUCKET": datasets_bucket,
        "ASSETS_MEDIA_CACHE_BUCKET": cache_bucket,
        "ASSETS_MEDIA_INPUTS_BUCKET": _required_env("MINIO_INPUTS_BUCKET"),
        "ASSETS_MEDIA_S3_CONNECT_TIMEOUT_SECONDS": "2",
        "ASSETS_MEDIA_S3_READ_TIMEOUT_SECONDS": "10",
        "ASSETS_MEDIA_S3_MAX_ATTEMPTS": "1",
    }
    if role == "control":
        assert token is not None
        if legacy_bucket is None:
            pytest.fail("the owned legacy bucket is required for image control")
        role_values = {
            "ASSETS_MEDIA_CONTROL_DATABASE_URL": database_url,
            "ASSETS_MEDIA_TOKEN": token,
            "ASSETS_MEDIA_HOST": "0.0.0.0",
            "ASSETS_MEDIA_PORT": "8768",
            "ASSETS_MEDIA_POOL_MIN_SIZE": "1",
            "ASSETS_MEDIA_POOL_MAX_SIZE": "2",
            "ASSETS_IMAGES_LEGACY_BUCKET": legacy_bucket,
        }
    else:
        role_values = {
            "ASSETS_VIDEO_EXECUTOR": "split",
            "ASSETS_MEDIA_WORKER_DATABASE_URL": database_url,
            "ASSETS_MEDIA_POLL_SECONDS": "0.2",
            "ASSETS_MEDIA_LEASE_SECONDS": "30",
            "ASSETS_MEDIA_FRAME_TTL_DAYS": "14",
            "ASSETS_MEDIA_CHUNK_TTL_DAYS": "30",
        }
    return common | role_values


async def _wait_control(origin: str) -> None:
    deadline = time.monotonic() + 30
    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(f"{origin}/health/ready")
                if response.status_code == 200 and response.json() == {"status": "ok"}:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    pytest.fail("containerized media control did not become ready")


class _ContainerWatch:
    def __init__(self, container_id: str):
        self.container_id = container_id

    def poll(self):
        state = _inspect_docker(self.container_id).get("State", {})
        return None if state.get("Running") is True else state.get("ExitCode", 1)


def _memory_peak(container_id: str) -> int | None:
    result = subprocess.run(
        [
            "docker",
            "exec",
            container_id,
            "python",
            "-c",
            "print(open('/sys/fs/cgroup/memory.peak').read().strip())",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    value = result.stdout.strip()
    return int(value) if result.returncode == 0 and value.isdigit() else None


async def test_restricted_media_images_serve_real_video_assets(
    test_engine, tmp_path, record_property, monkeypatch
):
    control_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL",
        "yoao_asset_media_control_client",
        test_engine,
    )
    worker_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_MEDIA_WORKER_URL",
        "yoao_asset_media_worker_client",
        test_engine,
    )
    async with test_engine.connect() as owner:
        if (
            await owner.scalar(text("SELECT current_database()"))
            != control_url.database
        ):
            pytest.fail("owner connection did not target the verifier-owned test DB")

    from app.config import settings

    resources = _verified_resource_manifest(test_engine, control_url)
    datasets_bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    legacy_bucket = resources["buckets"]["MINIO_BUCKET"]
    cache_bucket = resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"]
    owner_s3 = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    for bucket in (datasets_bucket, legacy_bucket, cache_bucket):
        _verify_bucket_owner(owner_s3, bucket, resources["owner"])

    control_access = _required_env("AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_ACCESS_KEY")
    control_secret = _required_env("AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_SECRET_KEY")
    worker_access = _required_env("AAP_MODULE_DATA_ASSET_MEDIA_WORKER_S3_ACCESS_KEY")
    worker_secret = _required_env("AAP_MODULE_DATA_ASSET_MEDIA_WORKER_S3_SECRET_KEY")
    if (
        (control_access, control_secret)
        == (settings.minio_access_key, settings.minio_secret_key)
        or (worker_access, worker_secret)
        == (settings.minio_access_key, settings.minio_secret_key)
        or control_access == worker_access
        or control_secret == worker_secret
    ):
        pytest.fail("media containers need distinct scoped S3 identities")
    for url, table in (
        (control_url, "public.dataset_items"),
        (worker_url, "public.asset_media_jobs"),
    ):
        await _assert_sql_role_is_narrow(
            url.render_as_string(hide_password=False), url.username, table
        )

    control_token = secrets.token_urlsafe(32)
    identity = uuid4().hex
    compose = MediaCompose(tmp_path, settings, resources["buckets"], record_property)
    public_s3 = compose.public_s3

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "container acceptance needs host ffmpeg for the source fixture"
    source_path = tmp_path / "source.mp4"
    generated = await asyncio.to_thread(
        subprocess.run,
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=128x96:rate=6",
            "-t",
            "2",
            "-an",
            "-c:v",
            "mpeg4",
            "-g",
            "3",
            "-pix_fmt",
            "yuv420p",
            str(source_path),
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert generated.returncode == 0, "could not generate the mpeg4 acceptance source"
    source_bytes = source_path.read_bytes()
    source_key = f"source/{identity}/original.mp4"
    item_id = uuid4()
    actor_id = dataset_id = project_id = None
    source_uploaded = False
    embedded_engine = _ordinary_assets_engine(test_engine)
    control_sessions = async_sessionmaker(embedded_engine, expire_on_commit=False)

    try:
        compose.add(
            CONTROL, control_url, control_access, control_secret, token=control_token
        )
        compose.add(VIDEO, worker_url, worker_access, worker_secret)
        compose.create()
        owner_s3.put_object(
            Bucket=datasets_bucket,
            Key=source_key,
            Body=source_bytes,
            ContentType="video/mp4",
        )
        source_uploaded = True
        async with async_sessionmaker(
            test_engine, expire_on_commit=False
        ).begin() as owner:
            actor_id = await _seed_actor(owner, role="super_admin")
            dataset_id = await _seed_dataset(
                owner,
                actor_id=actor_id,
                name=f"media-container-{identity}",
                data_type="video",
                file_count=1,
            )
            owner.add(
                DatasetItem(
                    id=item_id,
                    dataset_id=dataset_id,
                    file_name="source.mp4",
                    file_path=source_key,
                    file_type="video",
                    file_size=len(source_bytes),
                    content_hash=hashlib.sha256(source_bytes).hexdigest(),
                    metadata_={"video": {}},
                )
            )
            await owner.flush()
            project = await create_project(
                owner,
                owner_id=actor_id,
                name=f"media-container-{identity}",
                type_key="video",
            )
            project_id = project.id
            task_id = uuid4()
            owner.add(
                Task(
                    id=task_id,
                    project_id=project_id,
                    dataset_item_id=item_id,
                    display_id=f"T-MEDIA-C-{task_id.hex[:12]}",
                    file_name="source.mp4",
                    file_path=source_key,
                    file_type="video",
                )
            )

        monkeypatch.setattr(settings, "assets_media_url", "")
        monkeypatch.setattr(settings, "assets_video_executor", "split")
        monkeypatch.setattr(settings, "minio_public_url", public_s3)
        with monkeypatch.context() as scoped:
            scoped.setattr(settings, "minio_access_key", control_access)
            scoped.setattr(settings, "minio_secret_key", control_secret)
            async with media_gateway(session_factory=control_sessions) as embedded:
                accepted = await embedded.call(
                    "request_video_media",
                    media.RequestWorkRequest(
                        operation_id=uuid4(),
                        actor_id=actor_id,
                        dataset_item_id=item_id,
                        spec=media.MetadataSpec(),
                    ),
                )
                assert embedded.provider.wakeup is None
                assert accepted.work.status == "pending"
            assert not await run_media_job(accepted.work.job_id)

        control_container = compose.start(CONTROL)
        worker_container = compose.start(VIDEO)
        await _wait_healthy(control_container, timeout=90)
        await _wait_healthy(worker_container, timeout=90)
        control_origin = compose.origin()
        await _wait_control(control_origin)
        worker_watch = _ContainerWatch(worker_container)
        worker_log = tmp_path / "container-media-worker.log"
        worker_log.write_text("", encoding="utf-8")

        async with httpx.AsyncClient(
            base_url=control_origin,
            trust_env=False,
            timeout=5,
        ) as client:
            request = {
                "actor_id": str(actor_id),
                "dataset_item_id": str(item_id),
                "task_id": str(task_id),
            }
            unauthorized = await client.post("/v1/source-reference", json=request)
            assert unauthorized.status_code == 401
            source = await _wait_source_ready(
                client, control_token, request, worker_watch, worker_log
            )
            source_data = source["source"]
            assert source_data["file_size"] == len(source_bytes)
            assert source["poster_url"]
            assert source["video_url"] != source["original_url"]
            original = await client.get(source["original_url"])
            poster = await client.get(source["poster_url"])
            playback = await client.get(source["video_url"])
            assert original.status_code == 200 and original.content == source_bytes
            assert poster.status_code == 200
            with Image.open(BytesIO(poster.content)) as decoded:
                decoded.load()
                assert decoded.format == "WEBP"
            assert playback.status_code == 200 and playback.content[4:8] == b"ftyp"
            async with test_engine.connect() as owner:
                metadata = (
                    await owner.execute(
                        text(
                            "SELECT status,last_outcome FROM public.asset_media_jobs "
                            "WHERE job_id=:id"
                        ),
                        {"id": accepted.work.job_id},
                    )
                ).one()
                persisted = await owner.scalar(
                    text(
                        "SELECT metadata->'video' FROM public.dataset_items WHERE id=:id"
                    ),
                    {"id": item_id},
                )
            assert (metadata.status, metadata.last_outcome) == ("ready", "published")
            for key, url in (
                ("poster_frame_path", source["poster_url"]),
                ("playback_path", source["video_url"]),
            ):
                assert httpx.URL(url).path == f"/{cache_bucket}/{persisted[key]}"
            timetable = await _post(
                client,
                control_token,
                "/v1/commands/read_video_timetable",
                {**request, "page_size": 100},
            )
            assert timetable.status_code == 200
            timetable_data = timetable.json()["data"]
            assert timetable_data["complete"] and timetable_data["total"] >= 6

            async def verify_frame(index):
                frame = await _wait_asset_ready(
                    client,
                    control_token,
                    {
                        **request,
                        "selection": {
                            "kind": "frames",
                            "intent": "access",
                            "expected_source_snapshot": source_data["source_snapshot"],
                            "frame_indices": [index],
                            "width": 96,
                            "format": "webp",
                            "force": False,
                            "failed_only": False,
                        },
                    },
                    "frame",
                    index,
                    worker_watch,
                    worker_log,
                )
                response = await client.get(frame["url"])
                assert response.status_code == 200
                with Image.open(BytesIO(response.content)) as decoded:
                    decoded.load()
                    assert decoded.format == "WEBP" and decoded.size == (96, 72)
                job = await _job_for_frame(test_engine, item_id, index, 96)
                assert job["status"] == "ready" and job["lease_token"] is None
                async with test_engine.connect() as owner:
                    cache = (
                        await owner.execute(
                            text(
                                "SELECT storage_key, byte_size, status "
                                "FROM public.video_frame_cache WHERE dataset_item_id=:id "
                                "AND frame_index=:frame AND width=96 AND format='webp'"
                            ),
                            {"id": item_id, "frame": index},
                        )
                    ).one()
                assert cache.status == "ready" and cache.byte_size == len(
                    response.content
                )
                assert (
                    httpx.URL(frame["url"]).path
                    == f"/{cache_bucket}/{cache.storage_key}"
                )

            await verify_frame(1)
            compose.restart(VIDEO)
            await _wait_healthy(worker_container, timeout=90)
            await verify_frame(2)
            with monkeypatch.context() as scoped:
                scoped.setattr(settings, "minio_access_key", control_access)
                scoped.setattr(settings, "minio_secret_key", control_secret)
                async with media_gateway(session_factory=control_sessions) as embedded:
                    current = await embedded.ready_source_reference(
                        media.ReadSourceRequest(
                            actor_id=actor_id, dataset_item_id=item_id, task_id=task_id
                        )
                    )
                    assert (
                        current.source.source_snapshot == source_data["source_snapshot"]
                    )
                    assert current.poster_url is not None
        for role, container_id in (
            ("control", control_container),
            ("worker", worker_container),
        ):
            peak = _memory_peak(container_id)
            if peak is not None:
                record_property(f"{role}_cgroup_memory_peak_bytes", peak)

        compose.stop(VIDEO)
        async with media_gateway(session_factory=control_sessions) as embedded:
            work = await embedded.call(
                "request_video_media",
                media.RequestWorkRequest(
                    operation_id=uuid4(),
                    actor_id=actor_id,
                    dataset_item_id=item_id,
                    spec=media.FrameSpec(
                        frame_index=3,
                        width=96,
                        expected_source_snapshot=source_data["source_snapshot"],
                    ),
                ),
            )
        with monkeypatch.context() as scoped:
            scoped.setattr(
                settings,
                "database_url",
                embedded_engine.url.render_as_string(hide_password=False),
            )
            scoped.setattr(settings, "assets_video_executor", "celery")
            scoped.setattr(
                settings,
                "assets_celery_video_database_url",
                worker_url.render_as_string(hide_password=False),
            )
            scoped.setattr(settings, "assets_celery_video_database_url_file", "")
            scoped.setattr(settings, "minio_access_key", worker_access)
            scoped.setattr(settings, "minio_secret_key", worker_secret)
            assert await run_media_job(work.work.job_id)
        async with media_gateway(session_factory=control_sessions) as embedded:
            status = await embedded.call(
                "read_video_work",
                media.WorkStatusRequest(job_id=work.work.job_id, actor_id=actor_id),
            )
        assert status.work.status == "ready"
    finally:
        primary = sys.exception()
        cleanup_errors = []
        await embedded_engine.dispose()
        try:
            await asyncio.to_thread(compose.close)
        except Exception as error:
            cleanup_errors.append(error)
        if actor_id is not None:
            try:
                async with test_engine.connect() as owner:
                    job_ids = (
                        (
                            await owner.execute(
                                text(
                                    "SELECT job_id FROM public.asset_media_jobs "
                                    "WHERE dataset_item_id=:item"
                                ),
                                {"item": item_id},
                            )
                        )
                        .scalars()
                        .all()
                    )
                await _delete_objects(
                    owner_s3,
                    cache_bucket,
                    [f"media-assets/{job_id}/" for job_id in job_ids],
                )
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                cleanup_errors.append(error)
        if source_uploaded:
            try:
                owner_s3.delete_object(Bucket=datasets_bucket, Key=source_key)
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                cleanup_errors.append(error)
        if cleanup_errors:
            raise BaseExceptionGroup(
                "Media container acceptance cleanup failed",
                ([primary] if primary is not None else []) + cleanup_errors,
            ) from None
