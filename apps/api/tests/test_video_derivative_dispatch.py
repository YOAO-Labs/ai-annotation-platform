"""Real HTTP delegation through the owned Redis broker and a current worker."""

import asyncio
import hashlib
import os
from pathlib import Path
import sys
import uuid

import httpx

from app.config import settings
from app.core.security import create_access_token
from app.deps import get_db
from app.main import app
from app.workers.celery_app import celery_app
from tests.test_video_derivative_pipeline import real_video as real_video


async def test_http_authority_dispatches_real_video_worker_and_verified_outputs(
    real_video, tmp_path
):
    context = real_video
    node = "video-derivative-test-" + uuid.uuid4().hex + "@local"
    environment = dict(os.environ)
    for key in (
        "MIGRATION_DATABASE_URL",
        "TEST_DATABASE_URL",
        "PLAYWRIGHT_E2E_DATABASE_URL",
    ):
        environment.pop(key, None)
    environment.update(
        DEBUG="false",
        LOG_LEVEL="WARNING",
        VIDEO_CHUNK_SIZE_FRAMES=str(settings.video_chunk_size_frames),
    )
    log = (tmp_path / "worker.log").open("wb")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "celery",
        "-A",
        "app.workers.celery_app",
        "worker",
        "--pool=solo",
        "--concurrency=1",
        "--queues=media",
        f"--hostname={node}",
        "--without-gossip",
        "--without-mingle",
        "--without-heartbeat",
        "--loglevel=WARNING",
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=log,
        stderr=log,
    )

    async def database():
        async with context.factory() as db:
            yield db

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = database
    headers = {
        "Authorization": "Bearer "
        + create_access_token(str(context.user_id), "super_admin")
    }
    try:
        for _ in range(30):
            assert process.returncode is None, (
                "Owned Celery worker exited before readiness"
            )
            reply = await asyncio.to_thread(
                celery_app.control.ping, destination=[node], timeout=1
            )
            if reply:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Owned Celery worker did not become ready")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test", headers=headers
        ) as client:
            manifest_path = f"/api/v1/tasks/{context.task_id}/video/manifest"
            first = await client.get(manifest_path)
            assert (
                first.status_code == 503
                and first.json()["detail"]["code"] == "video_metadata_preparing"
            )
            for _ in range(120):
                response = await client.get(manifest_path)
                if response.status_code == 200:
                    break
                assert response.status_code == 503
                await asyncio.sleep(0.25)
            assert response.status_code == 200
            generation = response.json()["metadata_generation_id"]
            assert generation and response.json()["video_url"] is None

            async def ready(path):
                for _ in range(120):
                    value = await client.get(path)
                    if value.status_code == 409 and value.json().get("detail", {}).get(
                        "code"
                    ) in {
                        "content_busy",
                        "storage_original_busy",
                        "storage_maintenance_busy",
                    }:
                        await asyncio.sleep(0.25)
                        continue
                    assert value.status_code in {200, 202}, value.json()
                    if value.status_code == 200 and value.json()["status"] == "ready":
                        return value.json()
                    await asyncio.sleep(0.25)
                raise AssertionError("Real worker did not publish its committed output")

            frame = await ready(
                f"/api/v1/tasks/{context.task_id}/video/frames/3?w=48&format=webp"
            )
            chunk_path = f"/api/v1/tasks/{context.task_id}/video/chunks/0"
            chunk = await ready(chunk_path)
            for value in (frame, chunk):
                assert value["metadata_generation_id"] == generation
                assert value["output_id"] and value["sha256"] and value["byte_size"] > 0
                raw_url = value["url"]
                url = (
                    ("https" if settings.minio_use_ssl else "http")
                    + "://"
                    + settings.minio_endpoint
                    + raw_url.removeprefix("/minio")
                )
                async with httpx.AsyncClient() as provider:
                    body = await provider.get(url)
                    assert body.status_code == 200
                    assert len(body.content) == value["byte_size"]
                    assert hashlib.sha256(body.content).hexdigest() == value["sha256"]
            samples = await client.get(
                f"/api/v1/videos/{context.item_id}/chunks/0/samples",
                params={"expected_output_id": chunk["output_id"]},
            )
            assert samples.status_code == 200
            assert samples.json()["sha256"] == chunk["sha256"]
            assert samples.json()["metadata_generation_id"] == generation
            assert len(samples.json()["samples"]) == 4
            stale = await client.get(
                f"/api/v1/videos/{context.item_id}/chunks/0/samples",
                params={"expected_output_id": str(uuid.uuid4())},
            )
            assert stale.status_code == 409
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
        try:
            if process.returncode is None:
                await asyncio.to_thread(
                    celery_app.control.broadcast, "shutdown", destination=[node]
                )
                await asyncio.wait_for(process.wait(), 60)
        finally:
            log.close()
            (tmp_path / "worker.log").unlink()
