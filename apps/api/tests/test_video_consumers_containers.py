"""Bounded end-to-end proof for retained-video API and worker containers.

The verifier supplies a disposable database, scoped media credentials, versioned
test buckets, and the isolated worktree Redis. Images are built by the caller;
this test only starts and removes containers carrying its exact ownership labels.
"""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_assets_video_inputs.client import VideoInputClient
from aap_assets_video_inputs.execution import VideoFrameRequest
from aap_assets_video_inputs.evidence import (
    VideoInputEvidence,
    parse_prediction_evidence,
)
from app.config import settings
from app.core.security import create_access_token
from app.db.models.dataset import DatasetItem
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.task import Task
from tests.conftest import create_registry_with_pool
from tests.factory import create_project
from tests.fixtures.video_consumer_ml_peer import PROGRAM as VIDEO_ML_PEER_PROGRAM
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset
from tests.test_assets_media_containers import _memory_peak
from tests.test_assets_media_live import _guard_role_url
from tests.test_asset_storage_cleanup_runtime import (
    _container_database_url,
    _container_s3_endpoint,
    _docker as _shared_docker,
    _inspect_docker as _shared_inspect,
    _local_s3_client,
    _required_env,
    _stop_remove_container,
    _verified_resource_manifest,
    _verify_bucket_owner,
    _wait_healthy,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_VIDEO_CONSUMER_CONTAINER_ACCEPTANCE") != "1"
        or os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKTREE_MODE") != "test",
        reason="requires explicitly enabled verifier-owned DB/S3/Redis and images",
    ),
]

_ACCEPTANCE_LABEL = "org.aap.acceptance"
_IDENTITY_LABEL = "org.aap.acceptance-id"
_WORKTREE_OWNER_LABEL = "org.aap.worktree-owner"
_FRAME_COUNT = 12
_TASK_COUNT = 2
_CHUNK_SIZE = 12
_TERMINAL = {"completed", "failed", "cancelled"}


def _docker(*arguments: str, timeout: int = 90) -> str:
    try:
        return _shared_docker(*arguments, timeout=timeout)
    except Exception:
        pytest.fail("owned video-consumer Docker command failed", pytrace=False)


def _inspect(container_id: str) -> dict:
    try:
        observed = _shared_inspect(container_id)
    except Exception:
        pytest.fail("Docker returned no unique video-consumer identity", pytrace=False)
    return observed


def _failure_logs(
    containers: list[str], environment_files: list[Path], diagnostic: str | None = None
) -> None:
    sensitive = set()
    for path in environment_files:
        for line in path.read_text().splitlines():
            name, _, value = line.partition("=")
            if re.search(r"KEY|TOKEN|SECRET|PASSWORD", name) and len(value) > 4:
                sensitive.add(value)

    def safe(log):
        for value in sorted(sensitive, key=len, reverse=True):
            log = log.replace(value, "[secret]")
        return re.sub(
            r"(?:postgresql(?:\+\w+)?|redis|https?)://[^\s\"'<>]+", "[endpoint]", log
        )

    if diagnostic:
        print(f"Job diagnostic: {safe(diagnostic)}")
    for container in containers:
        try:
            result = subprocess.run(
                ["docker", "logs", "--tail", "25", container],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=10,
                check=False,
            )
            log = result.stdout
        except Exception:
            continue
        print(f"container {container[:12]}: {safe(log)[-2500:]}")


def _required_image(name: str) -> str:
    value = os.environ.get(name)
    if not value or "\n" in value or "\r" in value:
        pytest.fail(f"verifier did not supply {name}", pytrace=False)
    return value


def _image_identity(image: str) -> str:
    try:
        values = json.loads(_docker("image", "inspect", image, timeout=15))
    except json.JSONDecodeError:
        pytest.fail(
            "Docker returned an invalid video-consumer image identity", pytrace=False
        )
    if not isinstance(values, list) or len(values) != 1:
        pytest.fail("video-consumer image identity was not unique", pytrace=False)
    labels = values[0].get("Config", {}).get("Labels") or {}
    identity = labels.get(_IDENTITY_LABEL)
    if not (
        labels.get(_ACCEPTANCE_LABEL) == "module-data-boundary"
        and isinstance(identity, str)
        and re.fullmatch(r"[0-9a-f]{24}", identity)
    ):
        pytest.fail(
            "image was not built for this owned container acceptance", pytrace=False
        )
    return identity


def _loopback_container_url(value: str, *, username: str | None = None) -> URL:
    try:
        parsed = make_url(value)
    except Exception:
        pytest.fail("verifier supplied an invalid service URL", pytrace=False)
    if (
        parsed.host not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or (username is not None and parsed.username != username)
        or not parsed.database
        or not parsed.database.endswith("_test")
    ):
        pytest.fail(
            "service URL escaped the loopback disposable test environment",
            pytrace=False,
        )
    return parsed


def _verify_owned_redis(resources: dict) -> tuple[str, str]:
    broker = urlsplit(settings.effective_celery_broker)
    progress = urlsplit(settings.redis_url)
    if not (
        broker.scheme == progress.scheme == "redis"
        and broker.hostname == progress.hostname == "127.0.0.1"
        and broker.username is None
        and broker.password is None
        and progress.username is None
        and progress.password is None
        and broker.port is not None
        and broker.port == progress.port
        and broker.path == progress.path == "/0"
        and broker.query == progress.query
    ):
        pytest.fail(
            "Celery and progress Redis must share the test-mode broker", pytrace=False
        )

    container_name = resources.get("redis_name")
    owner = resources.get("owner")
    if not isinstance(container_name, str) or not isinstance(owner, str):
        pytest.fail(
            "test resource manifest omitted its Redis owner identity", pytrace=False
        )
    found = _docker(
        "container",
        "ls",
        "--all",
        "--filter",
        f"name=^/{container_name}$",
        "--format",
        "{{.ID}}",
    ).splitlines()
    if len(found) != 1:
        pytest.fail("test-mode Redis container identity was not unique", pytrace=False)
    observed = _inspect(found[0])
    labels = observed.get("Config", {}).get("Labels") or {}
    bindings = (observed.get("NetworkSettings", {}).get("Ports") or {}).get(
        "6379/tcp"
    ) or []
    if not (
        observed.get("Name") == f"/{container_name}"
        and observed.get("State", {}).get("Running") is True
        and labels.get(_WORKTREE_OWNER_LABEL) == owner
        and len(bindings) == 1
        and bindings[0].get("HostIp") == "127.0.0.1"
        and int(bindings[0].get("HostPort", 0)) == broker.port
    ):
        pytest.fail(
            "Redis container was not owned by this test-mode worktree", pytrace=False
        )

    try:
        import redis

        client = redis.Redis.from_url(
            settings.effective_celery_broker, socket_timeout=2
        )
        try:
            if client.ping() is not True:
                raise RuntimeError("ping")
        finally:
            client.close()
    except Exception:
        pytest.fail("owned test-mode Redis did not answer PING", pytrace=False)

    def gateway_url(value: str) -> str:
        parsed = urlsplit(value)
        netloc = f"host.docker.internal:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))

    return gateway_url(settings.redis_url), gateway_url(
        settings.effective_celery_broker
    )


def _write_environment(path: Path, values: dict[str, str]) -> Path:
    if any(
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
        or not isinstance(value, str)
        or "\n" in value
        or "\r" in value
        for name, value in values.items()
    ):
        pytest.fail("video-consumer environment was not safe to write", pytrace=False)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("".join(f"{name}={value}\n" for name, value in values.items()))
    return path


def _start_container(
    *,
    role: str,
    name: str,
    image: str,
    identity: str,
    environment_file: Path,
    entrypoint: str,
    command: list[str],
    user: str,
    cpus: str,
    memory: str,
    port: int | None = None,
) -> str:
    arguments = [
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        f"{_ACCEPTANCE_LABEL}=module-data-boundary",
        "--label",
        f"{_IDENTITY_LABEL}={identity}",
        "--add-host",
        "host.docker.internal:host-gateway",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        user,
        "--pids-limit",
        "64",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--cpus",
        cpus,
        "--memory",
        memory,
        "--stop-timeout",
        "30",
        "--env-file",
        str(environment_file),
    ]
    if port is not None:
        arguments.extend(["--publish", f"127.0.0.1::{port}"])
    arguments.extend(["--entrypoint", entrypoint, image, *command])
    container_id = _docker(*arguments, timeout=120)
    if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        pytest.fail(
            f"Docker did not return an owned {role} container id", pytrace=False
        )
    return container_id


def _verify_container(
    *,
    container_id: str,
    role: str,
    name: str,
    image: str,
    identity: str,
    environment_file: Path,
    entrypoint: str,
    command: list[str],
    user: str,
    cpus: str,
    memory: str,
    port: int | None = None,
) -> str | None:
    observed = _inspect(container_id)
    config = observed.get("Config", {})
    host = observed.get("HostConfig", {})
    labels = config.get("Labels") or {}
    expected_cpus = int(float(cpus) * 1_000_000_000)
    memory_bytes = int(memory[:-1]) * (1024**3 if memory.endswith("g") else 1024**2)
    if not (
        observed.get("Id") == container_id
        and observed.get("Name") == f"/{name}"
        and config.get("Image") == image
        and config.get("Entrypoint") == [entrypoint]
        and config.get("Cmd") == command
        and config.get("User") == user
        and environment_file.stat().st_mode & 0o777 == 0o600
        and labels.get(_ACCEPTANCE_LABEL) == "module-data-boundary"
        and labels.get(_IDENTITY_LABEL) == identity
        and observed.get("Mounts") == []
        and host.get("ReadonlyRootfs") is True
        and host.get("CapDrop") == ["ALL"]
        and host.get("Privileged") is False
        and "no-new-privileges:true" in (host.get("SecurityOpt") or [])
        and host.get("PidsLimit") == 64
        and host.get("NanoCpus") == expected_cpus
        and host.get("Memory") == memory_bytes
        and "size=256m" in (host.get("Tmpfs") or {}).get("/tmp", "")
    ):
        pytest.fail(
            f"{role} container escaped its restricted owned profile", pytrace=False
        )
    if port is None:
        if any(
            bindings
            for bindings in (
                observed.get("NetworkSettings", {}).get("Ports") or {}
            ).values()
        ):
            pytest.fail(f"{role} unexpectedly publishes an HTTP port", pytrace=False)
        return None
    bindings = (observed.get("NetworkSettings", {}).get("Ports") or {}).get(
        f"{port}/tcp"
    ) or []
    if len(bindings) != 1 or bindings[0].get("HostIp") != "127.0.0.1":
        pytest.fail(
            f"{role} did not publish only its loopback HTTP port", pytrace=False
        )
    return f"http://127.0.0.1:{int(bindings[0]['HostPort'])}"


def _launch_container(*, owned: list[str], **settings) -> tuple[str, str | None]:
    container_id = _start_container(**settings)
    owned.append(container_id)
    origin = _verify_container(container_id=container_id, **settings)
    return container_id, origin


def _container_environment(container_id: str) -> dict[str, str]:
    raw = _inspect(container_id).get("Config", {}).get("Env") or []
    return dict(value.partition("=")[::2] for value in raw if "=" in value)


def _verify_runtime_database_role(
    container_id: str, variable: str, expected_user: str
) -> None:
    value = _container_environment(container_id).get(variable)
    try:
        url = make_url(value or "")
    except Exception:
        pytest.fail("container database environment was invalid", pytrace=False)
    if not (
        url.username == expected_user
        and url.host == "host.docker.internal"
        and url.port is not None
        and url.database
        and url.database.endswith("_test")
        and url.password
    ):
        pytest.fail(
            "container did not receive its verifier-scoped database role", pytrace=False
        )


def _verify_consumer_environment(
    container_id: str,
    *,
    role: str,
    database_user: str,
    s3_access: str | None = None,
    s3_secret: str | None = None,
) -> None:
    environment = _container_environment(container_id)
    database_variable = {
        "api": "DATABASE_URL",
        "celery": "DATABASE_URL",
        "control": "ASSETS_MEDIA_CONTROL_DATABASE_URL",
        "worker": "ASSETS_MEDIA_WORKER_DATABASE_URL",
    }[role]
    _verify_runtime_database_role(container_id, database_variable, database_user)
    if role in {"api", "celery"}:
        expected_access = s3_access if role == "api" else "video-acceptance-unused"
        expected_secret = (
            s3_secret
            if role == "api"
            else "video-acceptance-unused-only-for-disabled-direct-storage"
        )
        if not (
            environment.get("MIGRATION_DATABASE_URL", "") == ""
            and environment.get("TEST_DATABASE_URL", "") == ""
            and environment.get("MINIO_ACCESS_KEY") == expected_access
            and environment.get("MINIO_SECRET_KEY") == expected_secret
        ):
            pytest.fail(
                "API/Celery received owner or migration credentials", pytrace=False
            )
    else:
        wrong_database = (
            "ASSETS_MEDIA_WORKER_DATABASE_URL"
            if role == "control"
            else "ASSETS_MEDIA_CONTROL_DATABASE_URL"
        )
        if wrong_database in environment:
            pytest.fail(
                "Media process received the other module's database role", pytrace=False
            )
        if (
            environment.get("ASSETS_MEDIA_S3_ACCESS_KEY") != s3_access
            or environment.get("ASSETS_MEDIA_S3_SECRET_KEY") != s3_secret
        ):
            pytest.fail(
                "Media process did not receive its scoped S3 identity", pytrace=False
            )
        if role == "control" and "ASSETS_MEDIA_TOKEN" not in environment:
            pytest.fail("Media control token was not configured", pytrace=False)
        if role == "worker" and "ASSETS_MEDIA_TOKEN" in environment:
            pytest.fail(
                "Media worker must not receive the control token", pytrace=False
            )


def _container_port(url: str) -> int:
    parsed = urlsplit(url)
    if parsed.port is None:
        pytest.fail("local object storage port is missing", pytrace=False)
    return parsed.port


def _video_bytes(directory: Path) -> bytes:
    destination = directory / f"video-consumer-{uuid4().hex}.mp4"
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x90:r=6:d=2",
            "-frames:v",
            str(_FRAME_COUNT),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(destination),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or not destination.is_file():
        pytest.fail(
            "could not create the bounded retained-video fixture", pytrace=False
        )
    return destination.read_bytes()


async def _wait_http_ready(origin: str, path: str, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(f"{origin}{path}")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    pytest.fail("owned video-consumer HTTP service did not become ready", pytrace=False)


async def _wait_job(engine, job_id: UUID, timeout: float = 240) -> tuple[str, dict]:
    deadline = time.monotonic() + timeout
    current = "missing"
    result = {}
    while time.monotonic() < deadline:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT status,result FROM public.async_jobs WHERE id=:job"),
                    {"job": job_id},
                )
            ).first()
        if row is not None:
            current, result = row[0], row[1] or {}
            if current in _TERMINAL:
                return current, result
        await asyncio.sleep(0.2)
    pytest.fail(
        f"containerized video Job did not become terminal (status={current})",
        pytrace=False,
    )


async def _wait_celery_workers(prefix: str, timeout: float = 45) -> None:
    from app.workers.celery_app import celery_app

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            responses = await asyncio.to_thread(celery_app.control.ping, timeout=2)
        except Exception:
            responses = None
        names = {
            name
            for response in responses or []
            for name, result in response.items()
            if name.startswith(prefix) and result == {"ok": "pong"}
        }
        if len(names) >= 2:
            return
        await asyncio.sleep(0.5)
    pytest.fail(
        "two owned Celery containers did not answer the broker ping", pytrace=False
    )


async def _peer_calls(origin: str) -> list[dict]:
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        response = await client.get(f"{origin}/__acceptance/calls")
    if response.status_code != 200:
        pytest.fail(
            "deterministic ML peer did not return its bounded observations",
            pytrace=False,
        )
    payload = response.json()
    calls = payload.get("calls") if isinstance(payload, dict) else None
    if not isinstance(calls, list):
        pytest.fail(
            "deterministic ML peer returned an invalid observation list", pytrace=False
        )
    return calls


async def _seed_video_project(
    engine,
    *,
    task_ids: list[UUID],
    source_keys: list[str],
    source_bytes: bytes,
    peer_url: str,
):

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as db:
        actor_id = await _seed_actor(db)
        project = await create_project(
            db,
            owner_id=actor_id,
            type_key="video-track",
            classes=["car"],
        )
        project.ai_enabled = True
        dataset_id = await _seed_dataset(
            db,
            actor_id=actor_id,
            data_type="video",
            name=f"video consumer {uuid4().hex[:8]}",
            file_count=len(task_ids),
        )
        for index, (task_id, source_key) in enumerate(
            zip(task_ids, source_keys), start=1
        ):
            item_id = uuid4()
            db.add(
                DatasetItem(
                    id=item_id,
                    dataset_id=dataset_id,
                    file_name=f"source-{index}.mp4",
                    file_path=source_key,
                    file_type="video",
                    file_size=len(source_bytes),
                    content_hash=sha256(source_bytes).hexdigest(),
                    metadata_={},
                )
            )
            db.add(
                Task(
                    id=task_id,
                    project_id=project.id,
                    dataset_item_id=item_id,
                    display_id=f"T-VC-{task_id.hex[:12]}",
                    file_name=f"source-{index}.mp4",
                    file_path=source_key,
                    file_type="video",
                    status="pending",
                )
            )
        backend, pool = await create_registry_with_pool(
            db,
            name=f"video-container-{uuid4().hex[:8]}",
            url=peer_url,
            state="connected",
            is_interactive=False,
            enabled_pool=True,
        )
        project.ml_backend_pool_id = pool.id
        db.add(
            ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True)
        )
        await db.flush()
        return actor_id, project.id, backend.id, pool.id


async def _read_result_state(engine, job_id: UUID, task_ids: list[UUID]):
    async with engine.connect() as connection:
        manifest = (
            await connection.execute(
                text(
                    "SELECT selection_digest,status,candidate_count,selected_count,"
                    "prepared_count,ready_count,failed_count,sealed_digest "
                    "FROM public.asset_accepted_video_manifests WHERE job_id=:job"
                ),
                {"job": job_id},
            )
        ).first()
        inputs = (
            await connection.execute(
                text(
                    "SELECT input_id,task_id,status,plan FROM public.asset_accepted_video_inputs "
                    "WHERE job_id=:job ORDER BY task_id"
                ),
                {"job": job_id},
            )
        ).all()
        input_ids = [row[0] for row in inputs]
        frames = []
        if input_ids:
            frames = (
                await connection.execute(
                    text(
                        "SELECT frame_id,input_id,ordinal,frame_index,status,blob "
                        "FROM public.asset_accepted_video_frames "
                        "WHERE input_id=ANY(CAST(:ids AS uuid[])) ORDER BY input_id,ordinal"
                    ),
                    {"ids": input_ids},
                )
            ).all()
        predictions = (
            await connection.execute(
                select(
                    Prediction.id,
                    Prediction.task_id,
                    Prediction.result,
                    PredictionMeta.extra,
                )
                .join(
                    PredictionMeta,
                    (PredictionMeta.prediction_id == Prediction.id)
                    & (PredictionMeta.prediction_created_at == Prediction.created_at),
                )
                .where(Prediction.task_id.in_(task_ids))
            )
        ).all()
        return manifest, inputs, frames, predictions


async def test_retained_video_consumers_run_in_owned_api_and_worker_containers(
    test_engine, test_db_url, tmp_path, record_property
):
    from app.workers.celery_app import celery_app
    from app.workers.frame_preannotate import predict_video_segment

    api_image = _required_image("AAP_VIDEO_CONSUMER_API_IMAGE")
    control_image = _required_image("AAP_VIDEO_CONSUMER_MEDIA_CONTROL_IMAGE")
    media_worker_image = _required_image("AAP_VIDEO_CONSUMER_MEDIA_WORKER_IMAGE")
    identities = {
        _image_identity(api_image),
        _image_identity(control_image),
        _image_identity(media_worker_image),
    }
    if len(identities) != 1:
        pytest.fail(
            "video-consumer images do not share one acceptance identity", pytrace=False
        )
    identity = identities.pop()

    test_url = _loopback_container_url(test_db_url)
    legacy_url = _loopback_container_url(
        _required_env("AAP_MODULE_DATA_LEGACY_RUNTIME_URL"), username="legacy_runtime"
    )
    if legacy_url.database != test_url.database or legacy_url.port != test_url.port:
        pytest.fail(
            "API runtime URL did not target the verifier-owned test database",
            pytrace=False,
        )
    control_db = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL",
        "asset_media_control_client",
        test_engine,
    )
    worker_db = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_MEDIA_WORKER_URL",
        "asset_media_worker_client",
        test_engine,
    )
    resources = _verified_resource_manifest(test_engine, control_db)
    redis_internal, broker_internal = _verify_owned_redis(resources)
    buckets = resources["buckets"]
    datasets_bucket = buckets["MINIO_DATASETS_BUCKET"]
    inputs_bucket = buckets["MINIO_INPUTS_BUCKET"]
    cache_bucket = buckets["MINIO_MEDIA_CACHE_BUCKET"]
    owner_s3 = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    for bucket in (datasets_bucket, inputs_bucket, cache_bucket):
        _verify_bucket_owner(owner_s3, bucket, resources["owner"])
    for bucket in (datasets_bucket, inputs_bucket):
        versioning = owner_s3.get_bucket_versioning(Bucket=bucket)
        if versioning.get("Status") != "Enabled":
            pytest.fail(
                "video acceptance requires versioned verifier-owned buckets",
                pytrace=False,
            )

    control_s3_access = _required_env(
        "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_ACCESS_KEY"
    )
    control_s3_secret = _required_env(
        "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_SECRET_KEY"
    )
    worker_s3_access = _required_env("AAP_MODULE_DATA_ASSET_MEDIA_WORKER_S3_ACCESS_KEY")
    worker_s3_secret = _required_env("AAP_MODULE_DATA_ASSET_MEDIA_WORKER_S3_SECRET_KEY")
    api_access = _required_env("AAP_MODULE_DATA_API_BOOTSTRAP_S3_ACCESS_KEY")
    api_secret = _required_env("AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY")
    if (
        api_access == settings.minio_access_key
        or api_secret == settings.minio_secret_key
    ):
        pytest.fail(
            "API startup must not receive the storage administrator identity",
            pytrace=False,
        )
    if control_s3_access == worker_s3_access or control_s3_secret == worker_s3_secret:
        pytest.fail(
            "Media control and worker require distinct scoped S3 identities",
            pytrace=False,
        )

    minio_internal = _container_s3_endpoint(settings)
    minio_port = _container_port(minio_internal)
    host_db = legacy_url.set(
        host="host.docker.internal", drivername="postgresql+asyncpg"
    ).render_as_string(hide_password=False)
    host_control_db = _container_database_url(control_db)
    host_worker_db = _container_database_url(worker_db)
    host_s3 = minio_internal
    token = secrets.token_urlsafe(32)
    run_suffix = secrets.token_hex(6)
    container_names = {
        "peer": f"aap-video-consumer-{run_suffix}-peer",
        "control": f"aap-video-consumer-{run_suffix}-control",
        "media_worker": f"aap-video-consumer-{run_suffix}-media",
        "api": f"aap-video-consumer-{run_suffix}-api",
        "celery_a": f"aap-video-consumer-{run_suffix}-celery-a",
        "celery_b": f"aap-video-consumer-{run_suffix}-celery-b",
    }
    containers: list[str] = []
    environment_files: list[Path] = []
    source_versions: list[tuple[str, str]] = []
    job_id: UUID | None = None
    api_origin: str | None = None
    peer_origin: str | None = None
    control_origin: str | None = None
    total_started = time.monotonic()
    container_roles: dict[str, str] = {}
    startup_seconds: dict[str, float] = {}

    try:
        media_s3_common = {
            "ASSETS_MEDIA_S3_ENDPOINT": host_s3,
            "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT": host_s3,
            "ASSETS_MEDIA_S3_REGION": "us-east-1",
            "ASSETS_MEDIA_S3_CONNECT_TIMEOUT_SECONDS": "2",
            "ASSETS_MEDIA_S3_READ_TIMEOUT_SECONDS": "10",
            "ASSETS_MEDIA_S3_MAX_ATTEMPTS": "1",
            "ASSETS_MEDIA_DATASETS_BUCKET": datasets_bucket,
            "ASSETS_MEDIA_CACHE_BUCKET": cache_bucket,
            "ASSETS_MEDIA_INPUTS_BUCKET": inputs_bucket,
        }
        peer_env = _write_environment(
            tmp_path / "video-peer.env",
            {
                "AAP_VIDEO_PEER_MINIO_HOST": "host.docker.internal",
                "AAP_VIDEO_PEER_MINIO_PORT": str(minio_port),
                "AAP_VIDEO_PEER_PORT": "9100",
            },
        )
        environment_files.append(peer_env)
        started = time.monotonic()
        peer_id, peer_origin = _launch_container(
            owned=containers,
            role="echo backend",
            name=container_names["peer"],
            image=api_image,
            identity=identity,
            environment_file=peer_env,
            entrypoint="python",
            command=["-c", VIDEO_ML_PEER_PROGRAM],
            user="10001:10001",
            cpus="0.25",
            memory="384m",
            port=9100,
        )
        assert peer_origin is not None
        peer_environment = _container_environment(peer_id)
        if any(
            name in peer_environment
            for name in (
                "DATABASE_URL",
                "ASSETS_MEDIA_CONTROL_DATABASE_URL",
                "ASSETS_MEDIA_WORKER_DATABASE_URL",
                "ASSETS_MEDIA_S3_ACCESS_KEY",
                "ASSETS_MEDIA_S3_SECRET_KEY",
                "ASSETS_MEDIA_TOKEN",
                "MINIO_ACCESS_KEY",
                "AWS_ACCESS_KEY_ID",
            )
        ):
            pytest.fail(
                "deterministic ML peer received database or storage credentials",
                pytrace=False,
            )
        await _wait_http_ready(peer_origin, "/health")
        startup_seconds["peer"] = time.monotonic() - started
        container_roles["peer"] = peer_id
        peer_port = _container_port(peer_origin)
        peer_internal_url = f"http://host.docker.internal:{peer_port}"

        control_env = _write_environment(
            tmp_path / "video-control.env",
            media_s3_common
            | {
                "ASSETS_MEDIA_CONTROL_DATABASE_URL": host_control_db,
                "ASSETS_IMAGES_LEGACY_BUCKET": buckets["MINIO_BUCKET"],
                "ASSETS_MEDIA_TOKEN": token,
                "ASSETS_MEDIA_HOST": "0.0.0.0",
                "ASSETS_MEDIA_PORT": "8768",
                "ASSETS_MEDIA_POOL_MIN_SIZE": "1",
                "ASSETS_MEDIA_POOL_MAX_SIZE": "2",
                "ASSETS_MEDIA_S3_ACCESS_KEY": control_s3_access,
                "ASSETS_MEDIA_S3_SECRET_KEY": control_s3_secret,
            },
        )
        environment_files.append(control_env)
        started = time.monotonic()
        control_id, control_origin = _launch_container(
            owned=containers,
            role="media control",
            name=container_names["control"],
            image=control_image,
            identity=identity,
            environment_file=control_env,
            entrypoint="python",
            command=["-m", "aap_assets_media_runtime"],
            user="10005:10005",
            cpus="0.25",
            memory="384m",
            port=8768,
        )
        assert control_origin is not None
        _verify_consumer_environment(
            control_id,
            role="control",
            database_user="asset_media_control_client",
            s3_access=control_s3_access,
            s3_secret=control_s3_secret,
        )
        await _wait_healthy(control_id, timeout=60)
        await _wait_http_ready(control_origin, "/health/ready")
        startup_seconds["media_control"] = time.monotonic() - started
        container_roles["media_control"] = control_id
        control_port = _container_port(control_origin)
        control_internal_url = f"http://host.docker.internal:{control_port}"

        worker_env = _write_environment(
            tmp_path / "video-media-worker.env",
            media_s3_common
            | {
                "ASSETS_MEDIA_WORKER_DATABASE_URL": host_worker_db,
                "ASSETS_MEDIA_S3_ACCESS_KEY": worker_s3_access,
                "ASSETS_MEDIA_S3_SECRET_KEY": worker_s3_secret,
                "ASSETS_MEDIA_POLL_SECONDS": "0.1",
                "ASSETS_MEDIA_LEASE_SECONDS": "60",
                "ASSETS_MEDIA_FRAME_TTL_DAYS": "14",
                "ASSETS_MEDIA_CHUNK_TTL_DAYS": "30",
            },
        )
        environment_files.append(worker_env)
        started = time.monotonic()
        media_worker_id, _ = _launch_container(
            owned=containers,
            role="media original/frame worker",
            name=container_names["media_worker"],
            image=media_worker_image,
            identity=identity,
            environment_file=worker_env,
            entrypoint="python",
            command=["-m", "aap_assets_media_runtime.worker"],
            user="10005:10005",
            cpus="0.75",
            memory="768m",
        )
        await _wait_healthy(media_worker_id, timeout=90)
        _verify_consumer_environment(
            media_worker_id,
            role="worker",
            database_user="asset_media_worker_client",
            s3_access=worker_s3_access,
            s3_secret=worker_s3_secret,
        )
        startup_seconds["media_worker"] = time.monotonic() - started
        container_roles["media_worker"] = media_worker_id

        api_env_values = {
            "DATABASE_URL": host_db,
            "MIGRATION_DATABASE_URL": "",
            "TEST_DATABASE_URL": "",
            "ALEMBIC_AUTO_UPGRADE": "false",
            "REDIS_URL": redis_internal,
            "CELERY_BROKER_URL": broker_internal,
            "CELERY_RESULT_BACKEND": broker_internal,
            "SECRET_KEY": settings.secret_key,
            "ENVIRONMENT": "development",
            "DEBUG": "false",
            "ASSETS_MEDIA_URL": control_internal_url,
            "ASSETS_MEDIA_TOKEN": token,
            "MINIO_ENDPOINT": f"host.docker.internal:{minio_port}",
            "MINIO_USE_SSL": "true" if settings.minio_use_ssl else "false",
            "MINIO_ACCESS_KEY": api_access,
            "MINIO_SECRET_KEY": api_secret,
            **buckets,
            "FRAME_PREANNOTATE_CHUNK_SIZE": str(_CHUNK_SIZE),
            "ML_PREDICT_TIMEOUT": "30",
        }
        connector_key = os.environ.get("CONNECTOR_ENCRYPTION_KEY")
        if connector_key:
            api_env_values["CONNECTOR_ENCRYPTION_KEY"] = connector_key
        api_env = _write_environment(tmp_path / "video-api.env", api_env_values)
        environment_files.append(api_env)
        started = time.monotonic()
        api_id, api_origin = _launch_container(
            owned=containers,
            role="API",
            name=container_names["api"],
            image=api_image,
            identity=identity,
            environment_file=api_env,
            entrypoint="python",
            command=[
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
            ],
            user="10001:10001",
            cpus="0.5",
            memory="1g",
            port=8000,
        )
        assert api_origin is not None
        _verify_consumer_environment(
            api_id,
            role="api",
            database_user="legacy_runtime",
            s3_access=api_access,
            s3_secret=api_secret,
        )
        await _wait_http_ready(api_origin, "/health/db")
        startup_seconds["api"] = time.monotonic() - started
        container_roles["api"] = api_id

        celery_env = _write_environment(
            tmp_path / "video-celery.env",
            {
                **api_env_values,
                "MINIO_ACCESS_KEY": "video-acceptance-unused",
                "MINIO_SECRET_KEY": "video-acceptance-unused-only-for-disabled-direct-storage",
            },
        )
        environment_files.append(celery_env)
        if celery_app.conf.task_always_eager:
            pytest.fail(
                "Celery container acceptance cannot run with eager tasks", pytrace=False
            )
        worker_prefix = f"video-consumer-{identity[:8]}-"
        started = time.monotonic()
        celery_worker_ids = []
        for label in ("celery_a", "celery_b"):
            index = 1 if label == "celery_a" else 2
            worker_id, _ = _launch_container(
                owned=containers,
                role=f"Celery worker {index}",
                name=container_names[label],
                image=api_image,
                identity=identity,
                environment_file=celery_env,
                entrypoint="celery",
                command=[
                    "-A",
                    "app.workers.celery_app",
                    "worker",
                    "--loglevel=warning",
                    "--queues=default,ml,ml.cpu,media,cleanup,audit",
                    "--concurrency=1",
                    "--prefetch-multiplier=1",
                    f"--hostname={worker_prefix}{index}@%h",
                ],
                user="10001:10001",
                cpus="0.5",
                memory="768m",
            )
            _verify_consumer_environment(
                worker_id, role="celery", database_user="legacy_runtime"
            )
            celery_worker_ids.append(worker_id)
        await _wait_celery_workers(worker_prefix)
        startup_seconds["celery_pair"] = time.monotonic() - started
        container_roles.update(
            {"celery_a": celery_worker_ids[0], "celery_b": celery_worker_ids[1]}
        )

        source_bytes = await asyncio.to_thread(_video_bytes, tmp_path)
        task_ids = [uuid4() for _ in range(_TASK_COUNT)]
        source_keys = [
            f"video-consumer-container/{identity}/{task_id}.mp4" for task_id in task_ids
        ]
        source_digests = []
        for source_key in source_keys:
            uploaded = await asyncio.to_thread(
                owner_s3.put_object,
                Bucket=datasets_bucket,
                Key=source_key,
                Body=source_bytes,
                ContentType="video/mp4",
            )
            version_id = uploaded.get("VersionId")
            if not version_id or version_id == "null":
                pytest.fail(
                    "source upload did not create an exact test version", pytrace=False
                )
            source_versions.append((source_key, version_id))
            source_digests.append(sha256(source_bytes).hexdigest())
        actor_id, project_id, backend_id, _pool_id = await _seed_video_project(
            test_engine,
            task_ids=task_ids,
            source_keys=source_keys,
            source_bytes=source_bytes,
            peer_url=peer_internal_url,
        )
        token_for_api = create_access_token(subject=str(actor_id), role="super_admin")
        request_body = {
            "task_ids": [str(task_id) for task_id in task_ids],
            "predict_mode": "overwrite",
            "pipeline_stages": [
                {
                    "stage": 7,
                    "ml_backend_id": str(backend_id),
                    "source": {
                        "kind": "dataset",
                        "data_type": "video",
                        "execution_unit": "frame",
                    },
                }
            ],
        }
        started = time.monotonic()
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            response = await client.post(
                f"{api_origin}/api/v1/projects/{project_id}/preannotate",
                headers={"Authorization": f"Bearer {token_for_api}"},
                json=request_body,
            )
        if response.status_code != 200:
            pytest.fail(
                f"containerized preannotation API returned HTTP {response.status_code}",
                pytrace=False,
            )
        try:
            job_id = UUID(response.json()["job_id"])
        except (KeyError, TypeError, ValueError):
            pytest.fail(
                "containerized preannotation API omitted its Job identity",
                pytrace=False,
            )

        status, job_result = await _wait_job(test_engine, job_id)
        api_to_complete_seconds = time.monotonic() - started
        if status != "completed":
            pytest.fail(
                "containerized retained-video Job did not complete successfully "
                f"(status={status}, frames={job_result.get('frames_done')}, "
                f"failed={job_result.get('frame_failed_count')})",
                pytrace=False,
            )
        if not (
            job_result.get("execution_unit") == "frame"
            and job_result.get("frames_done") == _TASK_COUNT * _FRAME_COUNT
            and job_result.get("frame_failed_count") == 0
            and job_result.get("skipped") == 0
        ):
            pytest.fail(
                "video Job result did not account for every selected frame",
                pytrace=False,
            )

        manifest, inputs, frames, predictions = await _read_result_state(
            test_engine, job_id, task_ids
        )
        if manifest is None:
            pytest.fail(
                "API did not persist the retained-video manifest", pytrace=False
            )
        if not (
            manifest[1] == "sealed"
            and manifest[2] == _TASK_COUNT
            and manifest[3] == _TASK_COUNT
            and manifest[4] == _TASK_COUNT
            and manifest[5] == _TASK_COUNT
            and manifest[6] == 0
            and manifest[7]
        ):
            pytest.fail(
                "video manifest did not seal all admitted source members", pytrace=False
            )
        if len(inputs) != _TASK_COUNT or len(frames) != _TASK_COUNT * _FRAME_COUNT:
            pytest.fail(
                "Media worker did not persist the bounded original/frame plans",
                pytrace=False,
            )
        if len(predictions) != _TASK_COUNT * _FRAME_COUNT:
            pytest.fail(
                "Celery workers did not persist one prediction per selected frame",
                pytrace=False,
            )

        input_by_task: dict[UUID, tuple] = {}
        frame_by_identity: dict[tuple[UUID, int], tuple] = {}
        expected_source_versions = dict(zip(task_ids, source_versions))
        for row in inputs:
            input_id, task_id, input_status, plan = row
            if input_status != "ready" or not isinstance(plan, dict):
                pytest.fail(
                    "an accepted original did not reach its immutable ready plan",
                    pytrace=False,
                )
            task_uuid = UUID(str(task_id))
            input_by_task[task_uuid] = row
            plan_input = plan.get("input", {})
            source_identity = plan_input.get("source", {}).get("object_identity", {})
            source_key, source_version = expected_source_versions[task_uuid]
            if not (
                source_identity.get("version_id") == source_version
                and plan_input.get("video", {}).get("frame_count") == _FRAME_COUNT
                and plan.get("selected_count") == _FRAME_COUNT
                and plan_input.get("blob", {}).get("version_id") not in {None, "null"}
                and plan_input.get("blob", {}).get("sha256")
                == source_digests[task_ids.index(task_uuid)]
            ):
                pytest.fail(
                    "retained original lost its exact source/version identity",
                    pytrace=False,
                )
            source_object = await asyncio.to_thread(
                owner_s3.get_object,
                Bucket=datasets_bucket,
                Key=source_key,
                VersionId=source_version,
            )
            try:
                captured_source_bytes = await asyncio.to_thread(
                    source_object["Body"].read
                )
            finally:
                source_object["Body"].close()
            if (
                sha256(captured_source_bytes).hexdigest()
                != source_digests[task_ids.index(task_uuid)]
            ):
                pytest.fail(
                    "captured source version returned unexpected bytes",
                    pytrace=False,
                )
            original_blob = plan_input["blob"]
            retained = await asyncio.to_thread(
                owner_s3.get_object,
                Bucket=inputs_bucket,
                Key=original_blob["key"],
                VersionId=original_blob["version_id"],
            )
            try:
                retained_bytes = await asyncio.to_thread(retained["Body"].read)
            finally:
                retained["Body"].close()
            if (
                sha256(retained_bytes).hexdigest()
                != source_digests[task_ids.index(task_uuid)]
            ):
                pytest.fail(
                    "retained source bytes differ from the captured source object",
                    pytrace=False,
                )

        for row in frames:
            frame_id, input_id, ordinal, frame_index, frame_status, blob = row
            if frame_status != "ready" or not isinstance(blob, dict):
                pytest.fail(
                    "selected frame did not reach its immutable ready receipt",
                    pytrace=False,
                )
            input_row = next(value for value in inputs if value[0] == input_id)
            plan_digest = input_row[3]["plan_digest"]
            owner_frame = await asyncio.to_thread(
                owner_s3.get_object,
                Bucket=inputs_bucket,
                Key=blob["key"],
                VersionId=blob["version_id"],
            )
            try:
                frame_bytes = await asyncio.to_thread(owner_frame["Body"].read)
            finally:
                owner_frame["Body"].close()
            if sha256(frame_bytes).hexdigest() != blob.get("sha256"):
                pytest.fail(
                    "frame bytes did not match their versioned receipt hash",
                    pytrace=False,
                )
            task_uuid = UUID(str(input_row[1]))
            frame_by_identity[(task_uuid, int(frame_index))] = row
            if not re.fullmatch(r"[0-9a-f]{64}", plan_digest):
                pytest.fail("retained frame plan digest was malformed", pytrace=False)

        calls = await _peer_calls(peer_origin)
        expected_call_ids = {
            f"{task_id}:{frame_index}"
            for task_id in task_ids
            for frame_index in range(_FRAME_COUNT)
        }
        observed_call_ids = [call.get("task") for call in calls]
        if (
            len(calls) != len(expected_call_ids)
            or set(observed_call_ids) != expected_call_ids
            or len(set(observed_call_ids)) != len(expected_call_ids)
            or any(
                call.get("width") != 160
                or call.get("height") != 90
                or call.get("pixel", [0, 255, 255])[0] < 200
                for call in calls
            )
        ):
            pytest.fail(
                "HTTP echo peer did not receive each real decoded frame exactly once",
                pytrace=False,
            )
        call_by_task = {call["task"]: call for call in calls}

        proof_frame_hashes = {}
        per_task_predictions = {task_id: 0 for task_id in task_ids}
        for prediction_id, prediction_task_id, result, extra in predictions:
            task_id = UUID(str(prediction_task_id))
            if task_id not in per_task_predictions or not isinstance(extra, dict):
                pytest.fail(
                    "prediction escaped the selected video Task scope", pytrace=False
                )
            proof = parse_prediction_evidence(extra.get("accepted_video_input"))
            if not isinstance(proof, VideoInputEvidence):
                pytest.fail(
                    "prediction omitted typed retained-video evidence", pytrace=False
                )
            expected_id = f"{task_id}:{proof.entry.frame_index}"
            peer_call = call_by_task.get(expected_id)
            stored_frame = frame_by_identity.get((task_id, proof.entry.frame_index))
            if not (
                proof.job_id == job_id
                and proof.task_id == task_id
                and proof.source_sha256 == source_digests[task_ids.index(task_id)]
                and peer_call is not None
                and stored_frame is not None
                and proof.frame_sha256 == peer_call["sha256"]
                and proof.frame_sha256 == stored_frame[5]["sha256"]
                and extra.get("accepted_video_execution_job_id") == str(job_id)
                and isinstance(result, list)
                and len(result) == 1
                and result[0].get("type") == "video_bbox"
                and result[0].get("geometry", {}).get("frame_index")
                == proof.entry.frame_index
            ):
                pytest.fail(
                    "prediction write did not preserve its source/frame evidence",
                    pytrace=False,
                )
            per_task_predictions[task_id] += 1
            proof_frame_hashes[(task_id, proof.entry.frame_index)] = proof.frame_sha256
        if set(per_task_predictions.values()) != {_FRAME_COUNT}:
            pytest.fail(
                "each selected video Task must have exactly one prediction per frame",
                pytrace=False,
            )

        for task_id in expected_source_versions:
            input_row = input_by_task[task_id]
            input_id = UUID(str(input_row[0]))
            plan = input_row[3]
            request = VideoFrameRequest(
                actor_id=actor_id,
                job_id=job_id,
                task_id=task_id,
                input_id=input_id,
                plan_digest=plan["plan_digest"],
                ordinal=1,
            )
            async with httpx.AsyncClient(
                base_url=control_origin,
                trust_env=False,
                timeout=10,
            ) as control_http:
                control_client = VideoInputClient(
                    control_origin,
                    token,
                    client=control_http,
                )
                repeated = await control_client.require_frame(request)
            stored = frame_by_identity[(task_id, 0)]
            if repeated.frame_id != stored[0] or repeated.status != "ready":
                pytest.fail(
                    "repeated require-frame did not return the stable frame receipt",
                    pytrace=False,
                )

        first_input = inputs[0]
        first_task = UUID(str(first_input[1]))
        first_plan = first_input[3]
        segment = {
            "task_id": str(first_task),
            "input_id": str(first_input[0]),
            "input_job_id": str(job_id),
            "plan_digest": first_plan["plan_digest"],
            "frame_ordinals": list(range(1, _FRAME_COUNT + 1)),
        }
        before_replay_calls = len(calls)
        before_replay_predictions = len(predictions)
        from sqlalchemy.ext.asyncio import async_sessionmaker

        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            stored_job = await db.execute(
                text("SELECT payload,result FROM public.async_jobs WHERE id=:job"),
                {"job": job_id},
            )
            job_payload, job_final_result = stored_job.one()
        selection_digest = manifest[0]
        sealed_digest = manifest[7]
        ml_backend_id = UUID(str(backend_id))
        stage0 = request_body["pipeline_stages"][0]
        replay = predict_video_segment.apply_async(
            args=[segment],
            kwargs={
                "project_id": str(project_id),
                "ml_backend_id": str(ml_backend_id),
                "stage0": stage0,
                "actor_id": str(actor_id),
                "job_id": str(job_id),
                "accepted_video_manifest_id": str(job_id),
                "accepted_video_selection_digest": selection_digest,
                "accepted_video_sealed_digest": sealed_digest,
                "celery_root_task_id": None,
            },
            queue="ml",
        )
        try:
            await asyncio.to_thread(replay.get, timeout=90, propagate=True)
        except Exception:
            pytest.fail(
                "duplicate Celery frame segment did not settle safely", pytrace=False
            )
        after_replay_calls = await _peer_calls(peer_origin)
        (
            _manifest_again,
            _inputs_again,
            _frames_again,
            predictions_after_replay,
        ) = await _read_result_state(test_engine, job_id, task_ids)
        if (
            len(after_replay_calls) != before_replay_calls
            or len(predictions_after_replay) != before_replay_predictions
        ):
            pytest.fail(
                "Celery redelivery duplicated model calls or prediction writes",
                pytrace=False,
            )

        for role, seconds in startup_seconds.items():
            record_property(
                f"video_container_startup_{role}_seconds", round(seconds, 3)
            )
        for role, container_id in container_roles.items():
            peak = await asyncio.to_thread(_memory_peak, container_id)
            record_property(
                f"video_container_{role}_cgroup_memory_peak_bytes",
                peak if peak is not None else "unavailable",
            )

        record_property("video_container_tasks", _TASK_COUNT)
        record_property("video_container_frames_per_task", _FRAME_COUNT)
        record_property("video_container_total_frame_predictions", len(predictions))
        record_property("video_container_ml_http_calls", len(calls))
        record_property(
            "video_container_duplicate_replay_calls",
            len(after_replay_calls) - before_replay_calls,
        )
        record_property(
            "video_container_api_to_complete_seconds",
            round(api_to_complete_seconds, 3),
        )
        record_property(
            "video_container_end_to_end_seconds",
            round(time.monotonic() - total_started, 3),
        )
        assert len(proof_frame_hashes) == _TASK_COUNT * _FRAME_COUNT
        assert set(job_final_result.get("failed_prediction_ids", [])) == set()
        assert (
            job_payload.get("accepted_video_inputs_protocol")
            == "aap-assets-video-inputs/v1"
        )
        stopping = time.monotonic()
        await asyncio.to_thread(
            _docker, "stop", "--time", "10", media_worker_id, timeout=15
        )
        stopped = _inspect(media_worker_id).get("State", {})
        assert stopped.get("ExitCode") == 0 and not stopped.get("OOMKilled")
        record_property(
            "video_media_worker_stop_seconds", round(time.monotonic() - stopping, 3)
        )
        record_property("video_media_worker_stop_exit_code", stopped.get("ExitCode"))
    except BaseException:
        diagnostic = None
        if job_id is not None:
            try:
                async with test_engine.connect() as db:
                    job_error = await db.scalar(
                        text(
                            "SELECT error_message FROM public.async_jobs WHERE id=:job"
                        ),
                        {"job": job_id},
                    )
                    states = {}
                    for table, status_column in (
                        ("asset_accepted_video_members", "state"),
                        ("asset_accepted_video_inputs", "status"),
                    ):
                        states[table] = [
                            list(row)
                            for row in (
                                await db.execute(
                                    text(
                                        f"SELECT {status_column},error_code FROM public.{table} WHERE job_id=:job LIMIT 20"
                                    ),
                                    {"job": job_id},
                                )
                            ).all()
                        ]
                    diagnostic = json.dumps({"job_error": job_error, **states})
            except Exception:
                pass
        _failure_logs(containers, environment_files, diagnostic)
        raise
    finally:
        cleanup_error = None
        for container_id in reversed(containers):
            try:
                _stop_remove_container(container_id, identity)
            except Exception:
                cleanup_error = (
                    "one or more video-consumer containers could not be removed"
                )
        for environment_file in environment_files:
            environment_file.unlink(missing_ok=True)
        owner_s3.close()
        if cleanup_error is not None:
            pytest.fail(cleanup_error, pytrace=False)
