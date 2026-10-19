"""Verifier-owned container proof for the independent Assets media runtime."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from tests.factory import create_project
from tests.test_asset_storage_cleanup_runtime import (
    _container_database_url,
    _docker,
    _inspect_docker,
    _local_s3_client,
    _required_env,
    _stop_remove_container,
    _verified_resource_manifest,
    _verify_bucket_owner,
    _wait_healthy,
)
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset
from tests.test_assets_catalog_runtime import _write_environment
from tests.test_assets_media_live import (
    _delete_objects,
    _guard_role_url,
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
_ACCEPTANCE = "org.aap.acceptance"
_IDENTITY = "org.aap.acceptance-id"


def _container_s3_origins(settings) -> tuple[str, str]:
    scheme = "https" if settings.minio_use_ssl else "http"
    public = f"{scheme}://{settings.minio_endpoint}"
    parsed = urlsplit(public)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("acceptance MinIO endpoint must be loopback-only")
    internal = urlunsplit(
        (
            parsed.scheme,
            f"host.docker.internal:{parsed.port or 9000}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )
    return internal, public


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
            "ASSETS_MEDIA_WORKER_DATABASE_URL": database_url,
            "ASSETS_MEDIA_POLL_SECONDS": "0.2",
            "ASSETS_MEDIA_LEASE_SECONDS": "30",
            "ASSETS_MEDIA_FRAME_TTL_DAYS": "14",
            "ASSETS_MEDIA_CHUNK_TTL_DAYS": "30",
        }
    return common | role_values


def _start_container(
    *,
    role: str,
    name: str,
    image: str,
    identity: str,
    environment_file: Path,
) -> str:
    arguments = [
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        f"{_ACCEPTANCE}=module-data-boundary",
        "--label",
        f"{_IDENTITY}={identity}",
        "--add-host",
        "host.docker.internal:host-gateway",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "10005:10005",
        "--pids-limit",
        "64",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--env-file",
        str(environment_file),
    ]
    if role == "control":
        arguments.extend(
            [
                "--publish",
                "127.0.0.1::8768",
                "--cpus",
                "0.5",
                "--memory",
                "512m",
                "--stop-timeout",
                "30",
            ]
        )
    else:
        arguments.extend(["--cpus", "2", "--memory", "2g", "--stop-timeout", "60"])
    arguments.append(image)
    return _docker(*arguments, timeout=120)


def _verify_container(
    *,
    container_id: str,
    role: str,
    name: str,
    image: str,
    identity: str,
) -> str | None:
    observed = _inspect_docker(container_id)
    config = observed.get("Config", {})
    host = observed.get("HostConfig", {})
    labels = config.get("Labels") or {}
    names = {value.partition("=")[0] for value in config.get("Env", [])}
    control = role == "control"
    module = (
        "aap_assets_media_runtime" if control else "aap_assets_media_runtime.worker"
    )
    if not (
        observed.get("Id") == container_id
        and observed.get("Name") == f"/{name}"
        and config.get("Image") == image
        and config.get("User") == "10005:10005"
        and config.get("Cmd") == ["python", "-m", module]
        and labels.get(_ACCEPTANCE) == "module-data-boundary"
        and labels.get(_IDENTITY) == identity
        and observed.get("Mounts") == []
        and host.get("ReadonlyRootfs") is True
        and host.get("CapDrop") == ["ALL"]
        and host.get("Privileged") is False
        and "no-new-privileges:true" in (host.get("SecurityOpt") or [])
        and host.get("PidsLimit") == 64
        and host.get("NanoCpus") == (500_000_000 if control else 2_000_000_000)
        and host.get("Memory") == (512 * 1024**2 if control else 2 * 1024**3)
        and not any(
            key == "DATABASE_URL"
            or key.startswith(("MINIO_", "AWS_", "CELERY_", "REDIS_", "APP_"))
            for key in names
        )
    ):
        pytest.fail(f"{role} container did not match the restricted runtime profile")
    required = {
        "ASSETS_MEDIA_CONTROL_DATABASE_URL"
        if control
        else "ASSETS_MEDIA_WORKER_DATABASE_URL",
        "ASSETS_MEDIA_S3_ACCESS_KEY",
        "ASSETS_MEDIA_S3_SECRET_KEY",
    }
    if control:
        required.add("ASSETS_MEDIA_TOKEN")
        required.add("ASSETS_IMAGES_LEGACY_BUCKET")
    forbidden_credentials = (
        {"ASSETS_MEDIA_WORKER_DATABASE_URL"}
        if control
        else {"ASSETS_MEDIA_CONTROL_DATABASE_URL", "ASSETS_MEDIA_TOKEN"}
    )
    if not required <= names or forbidden_credentials & names:
        pytest.fail(f"{role} container received the wrong runtime credentials")

    probe = _docker(
        "exec",
        container_id,
        "python",
        "-c",
        "import importlib, os; s=open('/proc/self/status').read().splitlines(); "
        f"importlib.import_module('{module}'); "
        "print(os.getuid(), os.getgid(), "
        "next(x.split()[1] for x in s if x.startswith('CapEff:')), "
        "next(x.split()[1] for x in s if x.startswith('NoNewPrivs:')))",
    )
    uid, gid, capabilities, no_new_privileges = probe.split()
    if (
        uid != "10005"
        or gid != "10005"
        or int(capabilities, 16) != 0
        or no_new_privileges != "1"
    ):
        pytest.fail(f"{role} cold import escaped the restricted process profile")

    if role == "worker":
        ports = observed.get("NetworkSettings", {}).get("Ports") or {}
        if any(bindings for bindings in ports.values() if bindings):
            pytest.fail("worker container unexpectedly publishes an HTTP port")
        return None
    bindings = (
        observed.get("NetworkSettings", {}).get("Ports", {}).get("8768/tcp") or []
    )
    if len(bindings) != 1 or bindings[0].get("HostIp") != "127.0.0.1":
        pytest.fail("control container did not publish only its loopback HTTP port")
    return f"http://127.0.0.1:{int(bindings[0]['HostPort'])}"


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
    test_engine, tmp_path, record_property
):
    control_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL",
        "asset_media_control_client",
        test_engine,
    )
    worker_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_MEDIA_WORKER_URL",
        "asset_media_worker_client",
        test_engine,
    )
    async with test_engine.connect() as owner:
        if (
            await owner.scalar(text("SELECT current_database()"))
            != control_url.database
        ):
            pytest.fail("owner connection did not target the verifier-owned test DB")

    from app.config import settings

    control_image = _required_env("AAP_ASSETS_MEDIA_CONTROL_IMAGE")
    worker_image = _required_env("AAP_ASSETS_MEDIA_WORKER_IMAGE")
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

    internal_s3, public_s3 = _container_s3_origins(settings)
    control_db = _container_database_url(control_url)
    worker_db = _container_database_url(worker_url)
    control_token = secrets.token_urlsafe(32)
    identity = uuid4().hex
    control_name = f"aap-media-control-{identity[:12]}"
    worker_name = f"aap-media-worker-{identity[:12]}"
    control_env_file = tmp_path / "media-control.env"
    worker_env_file = tmp_path / "media-worker.env"
    _write_environment(
        control_env_file,
        _container_environment(
            role="control",
            database_url=control_db,
            s3_endpoint=internal_s3,
            s3_public_endpoint=public_s3,
            datasets_bucket=datasets_bucket,
            legacy_bucket=legacy_bucket,
            cache_bucket=cache_bucket,
            access_key=control_access,
            secret_key=control_secret,
            token=control_token,
        ),
    )
    _write_environment(
        worker_env_file,
        _container_environment(
            role="worker",
            database_url=worker_db,
            s3_endpoint=internal_s3,
            s3_public_endpoint=public_s3,
            datasets_bucket=datasets_bucket,
            cache_bucket=cache_bucket,
            access_key=worker_access,
            secret_key=worker_secret,
        ),
    )
    assert control_env_file.stat().st_mode & 0o777 == 0o600
    assert worker_env_file.stat().st_mode & 0o777 == 0o600

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
    control_container = worker_container = None

    try:
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

        control_container = _start_container(
            role="control",
            name=control_name,
            image=control_image,
            identity=identity,
            environment_file=control_env_file,
        )
        worker_container = _start_container(
            role="worker",
            name=worker_name,
            image=worker_image,
            identity=identity,
            environment_file=worker_env_file,
        )
        await _wait_healthy(control_container, timeout=90)
        await _wait_healthy(worker_container, timeout=90)
        control_origin = _verify_container(
            container_id=control_container,
            role="control",
            name=control_name,
            image=control_image,
            identity=identity,
        )
        _verify_container(
            container_id=worker_container,
            role="worker",
            name=worker_name,
            image=worker_image,
            identity=identity,
        )
        assert control_origin is not None
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
            source = await _wait_source_ready(
                client, control_token, request, worker_watch, worker_log
            )
            source_data = source["source"]
            assert source_data["file_size"] == len(source_bytes)
            assert source["poster_url"]
            assert source["video_url"] != source["original_url"]
            for url in (
                source["original_url"],
                source["poster_url"],
                source["video_url"],
            ):
                response = await client.get(url)
                assert response.status_code == 200 and response.content

            timetable = await _post(
                client,
                control_token,
                "/v1/commands/read_video_timetable",
                {**request, "page_size": 100},
            )
            assert timetable.status_code == 200
            timetable_data = timetable.json()["data"]
            assert timetable_data["complete"] and timetable_data["total"] >= 6

            frame = await _wait_asset_ready(
                client,
                control_token,
                {
                    **request,
                    "selection": {
                        "kind": "frames",
                        "intent": "access",
                        "expected_source_snapshot": source_data["source_snapshot"],
                        "frame_indices": [1],
                        "width": 96,
                        "format": "webp",
                        "force": False,
                        "failed_only": False,
                    },
                },
                "frame",
                1,
                worker_watch,
                worker_log,
            )
            frame_response = await client.get(frame["url"])
            assert frame_response.status_code == 200 and frame_response.content
        for role, container_id in (
            ("control", control_container),
            ("worker", worker_container),
        ):
            peak = _memory_peak(container_id)
            if peak is not None:
                record_property(f"{role}_cgroup_memory_peak_bytes", peak)
    finally:
        cleanup_errors = []
        for container_id in (worker_container, control_container):
            if container_id is not None:
                try:
                    _stop_remove_container(container_id, identity)
                except BaseException as error:
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        raise
                    cleanup_errors.append(type(error).__name__)
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
                cleanup_errors.append(type(error).__name__)
        if source_uploaded:
            try:
                owner_s3.delete_object(Bucket=datasets_bucket, Key=source_key)
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                cleanup_errors.append(type(error).__name__)
        if cleanup_errors:
            pytest.fail("container acceptance cleanup did not complete safely")
