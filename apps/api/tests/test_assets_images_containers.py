"""Verifier-owned container acceptance for the dedicated Assets image worker."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from uuid import UUID, uuid4

from io import BytesIO

from PIL import Image
import pytest

from tests.test_asset_storage_cleanup_runtime import (
    _container_database_url,
    _docker,
    _inspect_docker,
    _required_env,
    _stop_remove_container,
    _wait_healthy,
)
from tests.test_assets_catalog_runtime import _write_environment
from tests.test_assets_catalog_native import _seed_actor
from tests.test_assets_media_containers import (
    _ACCEPTANCE,
    _IDENTITY,
    _container_s3_origins,
    _memory_peak,
    _start_container,
)
from tests.test_assets_images_live import (
    _accept_image_work,
    _assert_image_storage_scopes,
    _image_live_runtime,
    _quadrant_png,
    _read_job,
    _seed_item,
    _solid_png,
)
from tests.test_assets_media_live import _post

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier's owned PostgreSQL, S3 and image-worker image",
    ),
]


def _owned_image_ref(image_ref: str) -> tuple[str, str]:
    try:
        values = json.loads(_docker("image", "inspect", image_ref, timeout=20))
    except json.JSONDecodeError:
        pytest.fail("Docker returned an invalid image-worker image record")
    if not isinstance(values, list) or len(values) != 1:
        pytest.fail("image-worker image reference did not resolve uniquely")
    image = values[0]
    labels = image.get("Config", {}).get("Labels") or {}
    image_identity = labels.get(_IDENTITY)
    published_refs = set(image.get("RepoTags") or []) | set(
        image.get("RepoDigests") or []
    )
    if not (
        image.get("Id")
        and (image_ref in published_refs or image_ref == image.get("Id"))
        and labels.get(_ACCEPTANCE) == "module-data-boundary"
        and isinstance(image_identity, str)
        and re.fullmatch(r"[0-9a-f]{24}", image_identity)
    ):
        pytest.fail("image-worker image ownership label did not match this acceptance")
    return image["Id"], image_identity


def _image_worker_environment(
    runtime, internal_s3: str, public_s3: str
) -> dict[str, str]:
    return {
        "ASSETS_IMAGES_DATABASE_URL": _container_database_url(runtime.worker_url),
        "ASSETS_IMAGES_LEGACY_BUCKET": runtime.legacy_bucket,
        "ASSETS_IMAGES_POLL_SECONDS": "0.1",
        "ASSETS_IMAGES_SRGB_PROFILE": "/usr/share/color/icc/sRGB.icc",
        "ASSETS_MEDIA_S3_ENDPOINT": internal_s3,
        "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT": public_s3,
        "ASSETS_MEDIA_S3_REGION": "us-east-1",
        "ASSETS_MEDIA_S3_ACCESS_KEY": runtime.worker_access,
        "ASSETS_MEDIA_S3_SECRET_KEY": runtime.worker_secret,
        "ASSETS_MEDIA_S3_CONNECT_TIMEOUT_SECONDS": "2",
        "ASSETS_MEDIA_S3_READ_TIMEOUT_SECONDS": "10",
        "ASSETS_MEDIA_S3_MAX_ATTEMPTS": "1",
        "ASSETS_MEDIA_DATASETS_BUCKET": runtime.datasets_bucket,
        "ASSETS_MEDIA_CACHE_BUCKET": runtime.cache_bucket,
    }


def _verify_image_worker_container(
    container_id: str,
    *,
    name: str,
    image_ref: str,
    image_id: str,
    container_identity: str,
) -> None:
    observed = _inspect_docker(container_id)
    config = observed.get("Config", {})
    host = observed.get("HostConfig", {})
    labels = config.get("Labels") or {}
    env_names = {value.partition("=")[0] for value in config.get("Env", [])}
    required = {
        "ASSETS_IMAGES_DATABASE_URL",
        "ASSETS_IMAGES_LEGACY_BUCKET",
        "ASSETS_IMAGES_POLL_SECONDS",
        "ASSETS_IMAGES_SRGB_PROFILE",
        "ASSETS_MEDIA_S3_ENDPOINT",
        "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT",
        "ASSETS_MEDIA_S3_ACCESS_KEY",
        "ASSETS_MEDIA_S3_SECRET_KEY",
        "ASSETS_MEDIA_DATASETS_BUCKET",
        "ASSETS_MEDIA_CACHE_BUCKET",
    }
    forbidden = {
        "ASSETS_MEDIA_CONTROL_DATABASE_URL",
        "ASSETS_MEDIA_WORKER_DATABASE_URL",
        "ASSETS_MEDIA_TOKEN",
        "DATABASE_URL",
    }
    forbidden_prefixes = ("CELERY_", "REDIS_", "MINIO_", "AWS_", "APP_")
    ports = observed.get("NetworkSettings", {}).get("Ports") or {}
    if not (
        observed.get("Id") == container_id
        and observed.get("Name") == f"/{name}"
        and observed.get("Image") == image_id
        and config.get("Image") == image_ref
        and config.get("User") == "10005:10005"
        and config.get("Cmd") == ["python", "-m", "aap_assets_images_runtime.worker"]
        and labels.get(_ACCEPTANCE) == "module-data-boundary"
        and labels.get(_IDENTITY) == container_identity
        and observed.get("Mounts") == []
        and host.get("Binds") in (None, [])
        and host.get("ReadonlyRootfs") is True
        and host.get("CapDrop") == ["ALL"]
        and host.get("Privileged") is False
        and "no-new-privileges:true" in (host.get("SecurityOpt") or [])
        and host.get("PidsLimit") == 64
        and host.get("NanoCpus") == 2_000_000_000
        and host.get("Memory") == 2 * 1024**3
        and not any(bindings for bindings in ports.values())
        and required <= env_names
        and not (forbidden & env_names)
        and not any(name.startswith(forbidden_prefixes) for name in env_names)
    ):
        pytest.fail("image-worker container escaped its restricted runtime profile")


async def _wait_for_image_jobs(
    runtime, container_id: str, job_ids: list[UUID]
) -> dict[UUID, dict]:
    deadline = time.monotonic() + 240
    last: dict[UUID, dict] = {}
    while time.monotonic() < deadline:
        last = {
            job_id: await _read_job(runtime.test_engine, job_id) for job_id in job_ids
        }
        failed = {
            str(job_id): (row["status"], row["error_code"], row["error_detail"])
            for job_id, row in last.items()
            if row["status"] in {"failed", "canceled"}
        }
        if failed:
            pytest.fail(f"container image worker failed accepted jobs: {failed}")
        if all(row["status"] == "ready" for row in last.values()):
            return last
        observed = _inspect_docker(container_id)
        state = observed.get("State", {})
        if state.get("Running") is not True:
            logs = _docker("logs", container_id, timeout=20)
            pytest.fail(
                f"image-worker container exited during image work: {logs[-5000:]}"
            )
        await asyncio.sleep(0.2)
    logs = _docker("logs", container_id, timeout=20)
    pytest.fail(f"container image jobs did not finish: {last}; logs={logs[-5000:]}")


async def test_restricted_image_worker_container_generates_thumbnail_and_vips_pyramid(
    test_engine, tmp_path, record_property
):
    image_ref = _required_env("AAP_ASSETS_IMAGES_WORKER_IMAGE")
    image_id, _image_identity = _owned_image_ref(image_ref)
    async with _image_live_runtime(test_engine, tmp_path) as runtime:
        identity = uuid4().hex[:24]
        name = f"aap-images-worker-{identity[:12]}"
        environment_file = tmp_path / "image-worker.env"
        container_id = None
        worker_started_at = None
        try:
            internal_s3, public_s3 = _container_s3_origins(runtime.settings)
            _write_environment(
                environment_file,
                _image_worker_environment(runtime, internal_s3, public_s3),
            )
            assert environment_file.stat().st_mode & 0o777 == 0o600

            async with runtime.owner_sessions.begin() as owner:
                actor_id = await _seed_actor(owner, role="super_admin")
            runtime.track_actor(actor_id)
            small_source_key = f"image-source/{uuid4()}/container-thumb.png"
            pyramid_source_key = f"image-source/{uuid4()}/container-pyramid.png"
            _dataset_id, small_item_id = await _seed_item(
                runtime,
                actor_id=actor_id,
                source_key=small_source_key,
                source_bytes=_solid_png((32, 24), (238, 18, 32)),
                width=32,
                height=24,
            )
            _dataset_id, pyramid_item_id = await _seed_item(
                runtime,
                actor_id=actor_id,
                source_key=pyramid_source_key,
                source_bytes=_quadrant_png((1024, 1024)),
                width=1024,
                height=1024,
            )
            thumbnail_acceptance, _ = await _accept_image_work(
                runtime,
                actor_id=actor_id,
                item_id=small_item_id,
                purpose="thumbnail",
            )
            pyramid_acceptance, _ = await _accept_image_work(
                runtime,
                actor_id=actor_id,
                item_id=pyramid_item_id,
                purpose="pyramid",
            )
            thumbnail_job_id = UUID(thumbnail_acceptance["work"]["job_id"])
            pyramid_job_id = UUID(pyramid_acceptance["work"]["job_id"])

            worker_started_at = time.monotonic()
            container_id = _start_container(
                role="worker",
                name=name,
                image=image_ref,
                identity=identity,
                environment_file=environment_file,
            )
            _verify_image_worker_container(
                container_id,
                name=name,
                image_ref=image_ref,
                image_id=image_id,
                container_identity=identity,
            )
            await _wait_healthy(container_id, timeout=120)
            _docker(
                "exec",
                container_id,
                "python",
                "-m",
                "aap_assets_images_runtime.worker",
                "--healthcheck",
                timeout=30,
            )

            jobs = await _wait_for_image_jobs(
                runtime,
                container_id,
                [thumbnail_job_id, pyramid_job_id],
            )
            assert jobs[thumbnail_job_id]["result"]["kind"] == "thumbnail"
            assert jobs[pyramid_job_id]["result"]["kind"] == "pyramid"
            assert jobs[thumbnail_job_id]["attempt_count"] == 1
            assert jobs[pyramid_job_id]["attempt_count"] == 1

            thumbnail_reference = await _post(
                runtime.control_client,
                runtime.control_token,
                "/v1/images/source-reference",
                {
                    "actor_id": str(actor_id),
                    "task_id": None,
                    "dataset_item_id": str(small_item_id),
                },
            )
            assert thumbnail_reference.status_code == 200
            thumb_data = thumbnail_reference.json()["data"]
            assert thumb_data["thumbnail_url"]
            thumb_response = await runtime.control_client.get(
                thumb_data["thumbnail_url"]
            )
            assert thumb_response.status_code == 200 and thumb_response.content
            with Image.open(BytesIO(thumb_response.content)) as thumbnail:
                thumbnail.load()
                assert thumbnail.format == "WEBP"
                assert thumbnail.size == (256, 192)
                red, green, blue = thumbnail.convert("RGB").getpixel((128, 96))
                assert abs(red - 238) <= 8 and green <= 30 and blue <= 45

            generation_response = await _post(
                runtime.control_client,
                runtime.control_token,
                "/v1/images/generation-reference",
                {
                    "actor_id": str(actor_id),
                    "task_id": None,
                    "dataset_item_id": str(pyramid_item_id),
                    "expected_source_snapshot": pyramid_acceptance["source"][
                        "source_snapshot"
                    ],
                },
            )
            assert generation_response.status_code == 200
            generation = generation_response.json()["data"]
            manifest = generation["manifest"]
            assert generation["pyramid"]["status"] == "ready"
            assert manifest["width"] == 1024 and manifest["height"] == 1024
            assert len(manifest["levels"]) == 11
            for index, level in enumerate(manifest["levels"]):
                scale = 1 << index
                width = max(1, (1024 + scale - 1) // scale)
                height = max(1, (1024 + scale - 1) // scale)
                assert level == {
                    "level": index,
                    "scaleFactor": scale,
                    "width": width,
                    "height": height,
                    "columns": (width + 511) // 512,
                    "rows": (height + 511) // 512,
                }
            overview = await runtime.control_client.get(generation["overview_url"])
            assert overview.status_code == 200 and overview.content
            with Image.open(BytesIO(overview.content)) as pyramid_overview:
                pyramid_overview.load()
                assert pyramid_overview.format == "WEBP"
                assert pyramid_overview.size == (
                    manifest["overview"]["width"],
                    manifest["overview"]["height"],
                )

            tile_references = await _post(
                runtime.control_client,
                runtime.control_token,
                "/v1/images/asset-references",
                {
                    "actor_id": str(actor_id),
                    "task_id": None,
                    "dataset_item_id": str(pyramid_item_id),
                    "expected_source_snapshot": pyramid_acceptance["source"][
                        "source_snapshot"
                    ],
                    "items": [
                        {
                            "kind": "tile",
                            "generation": generation["pyramid"]["generation"],
                            "level": 0,
                            "x": 0,
                            "y": 0,
                        }
                    ],
                },
            )
            assert tile_references.status_code == 200
            tile = await runtime.control_client.get(
                tile_references.json()["data"]["items"][0]["url"]
            )
            assert tile.status_code == 200 and tile.content
            with Image.open(BytesIO(tile.content)) as pyramid_tile:
                pyramid_tile.load()
                assert pyramid_tile.format == "WEBP"
                assert pyramid_tile.convert("RGB").getpixel((100, 100))[0] > 150

            await _assert_image_storage_scopes(
                runtime,
                source_key=small_source_key,
                image_job_id=thumbnail_job_id,
            )

            peak = _memory_peak(container_id)
            assert worker_started_at is not None
            wall_seconds = time.monotonic() - worker_started_at
            record_property("image_worker_image_id", image_id)
            record_property("image_worker_wall_seconds", round(wall_seconds, 3))
            record_property("image_worker_cgroup_memory_peak_bytes", peak)
            if peak is not None:
                assert peak <= 2 * 1024**3
        finally:
            if container_id is not None:
                await asyncio.to_thread(_stop_remove_container, container_id, identity)
