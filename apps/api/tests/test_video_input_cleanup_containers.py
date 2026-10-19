"""Run retained-video garbage recovery in the installed Cleanup container."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url

from app.config import settings
from tests import test_video_input_live as video
from tests.test_asset_storage_cleanup_runtime import (
    _container_database_url,
    _container_s3_endpoint,
    _docker,
    _required_env,
    _start_worker,
    _stop_remove_container,
    _verified_resource_manifest,
    _verify_worker_container,
    _write_worker_environment,
)
from tests.test_assets_images_containers import _owned_image_ref
from tests.test_assets_media_containers import _ContainerWatch, _memory_peak
from tests.test_assets_media_live import _guard_role_url

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier's owned database, S3 and Cleanup image",
    ),
]


async def test_cleanup_container_reaps_video_attempts_and_late_versions(
    test_engine, tmp_path, monkeypatch, record_property
):
    image_ref = _required_env("AAP_VIDEO_CONSUMER_CLEANUP_IMAGE")
    image_id, _ = _owned_image_ref(image_ref)
    cleanup_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_CLEANUP_URL", "asset_cleanup_client", test_engine
    )
    resources = _verified_resource_manifest(test_engine, cleanup_url)
    original_start, original_stop = video._start_process, video._stop_process
    owned = []
    peaks = []

    def start(command, environment, log):
        if command[1:3] != ["-m", "aap_assets_cleanup"]:
            return original_start(command, environment, log)
        supplied = make_url(environment["ASSET_CLEANUP_DATABASE_URL"])
        assert (supplied.host, supplied.port, supplied.database, supplied.username) == (
            cleanup_url.host,
            cleanup_url.port,
            cleanup_url.database,
            cleanup_url.username,
        )
        assert (
            environment["ASSET_CLEANUP_BUCKET"]
            == resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"]
        )
        assert (
            environment["ASSET_CLEANUP_INPUTS_BUCKET"]
            == resources["buckets"]["MINIO_INPUTS_BUCKET"]
        )
        identity = uuid4().hex[:24]
        name = f"aap-video-cleanup-{identity[:12]}"
        env_file = tmp_path / f"{name}.env"
        _write_worker_environment(
            env_file,
            database_url=_container_database_url(cleanup_url),
            endpoint=_container_s3_endpoint(settings),
            bucket=resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"],
            access_key=environment["ASSET_CLEANUP_S3_ACCESS_KEY"],
            secret_key=environment["ASSET_CLEANUP_S3_SECRET_KEY"],
        )
        container = _start_worker(name, image_ref, identity, env_file)
        owned.append((container, identity))
        _verify_worker_container(container, name, image_ref, identity)
        return _ContainerWatch(container), log.open("w")

    def stop(process):
        if not isinstance(process, _ContainerWatch):
            return original_stop(process)
        if process.poll() is None:
            peak = _memory_peak(process.container_id)
            if peak is not None:
                peaks.append(peak)
            _docker("stop", "--time", "45", process.container_id, timeout=60)

    # Replace only the test harness launcher. The application, native functions,
    # S3 operations and complete version/multipart assertions remain real.
    monkeypatch.setattr(video, "_start_process", start)
    monkeypatch.setattr(video, "_stop_process", stop)
    try:
        await video.test_real_video_input_control_worker_versions_and_independent_node_peer(
            test_engine, tmp_path
        )
        assert len(owned) == 1
        record_property("video_cleanup_image_id", image_id)
        if peaks:
            record_property("video_cleanup_cgroup_memory_peak_bytes", max(peaks))
    finally:
        for container, identity in reversed(owned):
            await asyncio.to_thread(_stop_remove_container, container, identity)
