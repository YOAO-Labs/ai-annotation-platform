"""Run retained-video garbage recovery in the installed Cleanup container."""

import asyncio
from contextlib import asynccontextmanager
import os
from uuid import uuid4

import pytest

from app.config import settings
from tests.test_asset_storage_cleanup_runtime import (
    _container_database_url,
    _container_s3_endpoint,
    _inspect_docker,
    _required_env,
    _stop_remove_container,
    _verified_resource_manifest,
)
from tests.test_assets_images_containers import _owned_image_ref
from tests.test_assets_media_containers import _ContainerWatch, _memory_peak
from tests.test_video_consumers_containers import _launch_container, _write_environment
from tests.test_video_input_live import exercise_video_input_lifecycle

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier's owned database, S3 and Cleanup image",
    ),
]


async def test_cleanup_container_reaps_video_attempts_and_late_versions(
    test_engine, tmp_path, record_property, native_signer, committed_batch_job_id
):
    image_ref = _required_env("AAP_VIDEO_CONSUMER_CLEANUP_IMAGE")
    image_id, identity = _owned_image_ref(image_ref)
    cleanup_images = []

    @asynccontextmanager
    async def installed_cleanup(cleanup_url, environment, directory):
        resources = _verified_resource_manifest(test_engine, cleanup_url)
        assert (
            environment["ASSET_CLEANUP_BUCKET"]
            == resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"]
        )
        assert (
            environment["ASSET_CLEANUP_INPUTS_BUCKET"]
            == resources["buckets"]["MINIO_INPUTS_BUCKET"]
        )
        env_file = _write_environment(
            directory / "video-cleanup.env",
            {
                **environment,
                "ASSET_CLEANUP_DATABASE_URL": _container_database_url(cleanup_url),
                "ASSET_CLEANUP_S3_ENDPOINT": _container_s3_endpoint(settings),
                "ASSET_CLEANUP_PAGE_SIZE": "1",
            },
        )
        owned = []
        try:
            container, _ = _launch_container(
                owned=owned,
                use_image_defaults=True,
                role="video cleanup",
                name=f"aap-video-cleanup-{uuid4().hex[:12]}",
                image=image_id,
                identity=identity,
                environment_file=env_file,
                entrypoint=None,
                command=["python", "-m", "yoao_assets_cleanup"],
                user="10001:10001",
                cpus="0.5",
                memory="256m",
            )
            observed = _inspect_docker(container)
            assert not observed["NetworkSettings"]["Ports"]
            if any(
                value.startswith(
                    (
                        "DATABASE_URL=",
                        "REDIS_URL=",
                        "CELERY_",
                        "MINIO_",
                        "AWS_ACCESS_KEY_ID=",
                        "AWS_SECRET_ACCESS_KEY=",
                    )
                )
                for value in observed["Config"]["Env"]
            ):
                pytest.fail("Cleanup inherited a platform credential", pytrace=False)
            watch = _ContainerWatch(container)

            def assert_running():
                assert watch.poll() is None, (
                    "installed Cleanup exited before acknowledgement"
                )

            yield assert_running
            cleanup_images.append(observed["Image"])
            peak = await asyncio.to_thread(_memory_peak, container)
            if peak is not None:
                record_property("video_cleanup_cgroup_memory_peak_bytes", peak)
        finally:
            try:
                for container in owned:
                    await asyncio.to_thread(_stop_remove_container, container, identity)
            finally:
                env_file.unlink()

    await exercise_video_input_lifecycle(
        test_engine, tmp_path, native_signer, committed_batch_job_id, installed_cleanup
    )
    assert cleanup_images == [image_id]
    record_property("video_cleanup_image_id", image_id)
