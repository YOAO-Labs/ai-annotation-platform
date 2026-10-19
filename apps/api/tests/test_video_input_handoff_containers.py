"""Prove retained-video lease takeover with two independent Media containers."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
from uuid import UUID, uuid4

from botocore.exceptions import ClientError
from PIL import Image
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from aap_assets_video_inputs import execution as video_execution
from app.config import settings
from tests.test_asset_storage_cleanup_runtime import (
    _docker as _shared_docker,
    _inspect_docker as _shared_inspect,
    _required_env,
    _stop_remove_container,
)
from tests.test_assets_catalog_runtime import _write_environment
from tests.test_assets_media_containers import (
    _memory_peak,
    _container_environment,
    _container_database_url,
    _container_s3_origins,
)
from tests.test_assets_media_live import _guard_role_url
from tests.test_video_input_authorities_native import _seed_video_job, _seed_video_task
from tests.test_video_input_ledger_control import _call
from tests.test_video_input_live import _video_runtime
from tests.test_video_input_selection_native import _selection

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verifier-owned PostgreSQL/S3 resources and worker image",
    ),
]

_ACCEPTANCE_LABEL = "org.aap.acceptance"
_IDENTITY_LABEL = "org.aap.acceptance-id"
_TASK_RETRY_LIMIT = 3
_FRAME_COUNT = 600
_SOURCE_MAX_BYTES = 128 * 1024 * 1024
_SCENARIO_SECONDS = 180


def _remaining(deadline: float, maximum: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        pytest.fail(
            "video handoff exceeded its bounded 180-second scenario", pytrace=False
        )
    return min(maximum, remaining)


def _docker(*arguments: str, timeout: int = 90) -> str:
    try:
        return _shared_docker(*arguments, timeout=timeout)
    except Exception:
        raise RuntimeError("owned Media worker Docker command failed") from None


def _inspect(container_id: str) -> dict:
    try:
        return _shared_inspect(container_id)
    except Exception:
        pytest.fail("Docker returned no unique owned Media worker", pytrace=False)


def _start_worker_container(
    *,
    name: str,
    image: str,
    identity: str,
    environment_file: Path,
    command: list[str],
    cpu: str,
    timeout: float = 30,
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
        "10005:10005",
        "--pids-limit",
        "64",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--cpus",
        cpu,
        "--memory",
        "1g",
        "--stop-timeout",
        "60",
        "--env-file",
        str(environment_file),
        image,
        *command,
    ]
    container_id = _docker(*arguments, timeout=max(1, int(timeout)))
    if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        pytest.fail("Docker did not return a full Media worker id", pytrace=False)
    return container_id


def _verify_worker_container(
    *,
    container_id: str,
    name: str,
    image: str,
    identity: str,
    command: list[str],
    cpu: str,
    worker_database_url: str,
    access_key: str,
    secret_key: str,
) -> None:
    observed = _inspect(container_id)
    config = observed.get("Config", {})
    host = observed.get("HostConfig", {})
    labels = config.get("Labels") or {}
    if not (
        observed.get("Id") == container_id
        and observed.get("Name") == f"/{name}"
        and config.get("Image") == image
        and config.get("Cmd") == command
        and config.get("User") == "10005:10005"
        and labels.get(_ACCEPTANCE_LABEL) == "module-data-boundary"
        and labels.get(_IDENTITY_LABEL) == identity
        and observed.get("Mounts") == []
        and host.get("ReadonlyRootfs") is True
        and host.get("CapDrop") == ["ALL"]
        and host.get("Privileged") is False
        and "no-new-privileges:true" in (host.get("SecurityOpt") or [])
        and host.get("PidsLimit") == 64
        and host.get("NanoCpus") == int(float(cpu) * 1_000_000_000)
        and host.get("Memory") == 1024**3
    ):
        pytest.fail("Media worker escaped its restricted owned profile", pytrace=False)
    ports = observed.get("NetworkSettings", {}).get("Ports") or {}
    if any(bindings for bindings in ports.values() if bindings):
        pytest.fail("Media worker unexpectedly published an HTTP port", pytrace=False)
    environment = dict(
        value.partition("=")[::2] for value in config.get("Env", []) if "=" in value
    )
    if not (
        environment.get("ASSETS_MEDIA_WORKER_DATABASE_URL") == worker_database_url
        and environment.get("ASSETS_MEDIA_S3_ACCESS_KEY") == access_key
        and environment.get("ASSETS_MEDIA_S3_SECRET_KEY") == secret_key
        and "ASSETS_MEDIA_CONTROL_DATABASE_URL" not in environment
        and "ASSETS_MEDIA_TOKEN" not in environment
        and not any(
            key == "DATABASE_URL"
            or key.startswith(("MINIO_", "AWS_", "CELERY_", "REDIS_"))
            for key in environment
        )
    ):
        pytest.fail(
            "Media worker received a foreign DB or storage identity", pytrace=False
        )
    parsed = make_url(environment["ASSETS_MEDIA_WORKER_DATABASE_URL"])
    if not (
        parsed.username == "asset_media_worker_client"
        and parsed.host == "host.docker.internal"
        and parsed.database
        and parsed.database.endswith("_test")
        and parsed.password
    ):
        pytest.fail("worker DB URL escaped the disposable scoped role", pytrace=False)


def _write_worker_env(
    path: Path,
    *,
    database_url: str,
    internal_s3: str,
    public_s3: str,
    datasets_bucket: str,
    cache_bucket: str,
    inputs_bucket: str,
    access_key: str,
    secret_key: str,
) -> Path:
    environment = _container_environment(
        role="worker",
        database_url=database_url,
        s3_endpoint=internal_s3,
        s3_public_endpoint=public_s3,
        datasets_bucket=datasets_bucket,
        cache_bucket=cache_bucket,
        access_key=access_key,
        secret_key=secret_key,
    )
    environment["ASSETS_MEDIA_INPUTS_BUCKET"] = inputs_bucket
    environment["ASSETS_MEDIA_POLL_SECONDS"] = "0.1"
    environment["ASSETS_MEDIA_LEASE_SECONDS"] = "60"
    _write_environment(path, environment)
    if path.stat().st_mode & 0o777 != 0o600:
        pytest.fail("worker environment file must be mode 0600", pytrace=False)
    return path


def _bounded_video_bytes(directory: Path, *, timeout: float = 60) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.fail(
            "the verifier host needs ffmpeg for its bounded video fixture",
            pytrace=False,
        )
    destination = directory / f"handoff-{uuid4().hex}.mp4"
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30",
            "-frames:v",
            str(_FRAME_COUNT),
            "-an",
            "-c:v",
            "mpeg4",
            "-q:v",
            "2",
            "-g",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "1",
            str(destination),
        ],
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0 or not destination.is_file():
        pytest.fail("could not create the bounded moving-video fixture", pytrace=False)
    body = destination.read_bytes()
    if not 1024 * 1024 < len(body) <= _SOURCE_MAX_BYTES:
        pytest.fail(
            "generated video escaped the 1-128 MiB acceptance bound", pytrace=False
        )
    return body


async def _read_input_state(runtime, input_id: UUID):
    async with runtime.sessions() as owner:
        row = (
            (
                await owner.execute(
                    text(
                        "SELECT status,generation,attempt_count,lease_owner,lease_token,"
                        "lease_expires_at,last_lease_token,last_outcome,plan "
                        "FROM public.asset_accepted_video_inputs WHERE input_id=:input"
                    ),
                    {"input": input_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return dict(row)


async def _read_input_attempts(runtime, input_id: UUID) -> list[dict]:
    async with runtime.sessions() as owner:
        rows = await owner.execute(
            text(
                "SELECT generation,lease_token,prefix,status,lease_expires_at,completed_at "
                "FROM public.asset_accepted_video_input_attempts "
                "WHERE input_id=:input ORDER BY generation"
            ),
            {"input": input_id},
        )
        return [dict(row) for row in rows.mappings()]


async def _read_input_object(runtime, *, input_id: UUID, generation: int, token: UUID):
    key = video_execution.attempt_prefix(input_id, generation, token) + "source"
    try:
        head = await asyncio.to_thread(
            runtime.owner.head_object,
            Bucket=runtime.buckets["MINIO_INPUTS_BUCKET"],
            Key=key,
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        pytest.fail("could not inspect the owned attempt object", pytrace=False)
    return key, head.get("VersionId")


async def _wait_for_first_claim(
    runtime, input_id: UUID, container_id: str, timeout: float
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await _read_input_state(runtime, input_id)
        if state and (state["status"] == "ready" and state["generation"] == 1):
            return "completed", state
        if state and state["status"] == "failed":
            pytest.fail(
                "worker A failed while materializing generation one", pytrace=False
            )
        if state and (state["status"] == "materializing" and state["generation"] == 1):
            obj = await _read_input_object(
                runtime,
                input_id=input_id,
                generation=1,
                token=state["lease_token"],
            )
            if obj is not None:
                return "leased", state
        observed = _inspect(container_id)
        if observed.get("State", {}).get("Running") is not True:
            if state and state["status"] == "failed":
                pytest.fail(
                    "generation-one worker failed before the handoff", pytrace=False
                )
            if state and state["status"] == "ready":
                return "completed", state
        await asyncio.sleep(0.05)
    pytest.fail("worker A did not claim and stage the bounded original", pytrace=False)


async def _wait_for_generation_two_claim(
    runtime,
    input_id: UUID,
    owner_a: UUID,
    container_id: str,
    identity: str,
    timeout: float,
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await _read_input_state(runtime, input_id)
        if state and state["generation"] == 2:
            if state["status"] == "ready":
                pytest.fail(
                    "B finished before its distinct worker lease was observed",
                    pytrace=False,
                )
            if state["status"] == "materializing":
                if state["lease_owner"] == owner_a:
                    pytest.fail(
                        "lease generation advanced without ownership handoff",
                        pytrace=False,
                    )
                return state
        observed = _assert_owned_container(container_id, identity)
        if observed.get("State", {}).get("Running") is not True:
            pytest.fail("worker B exited before claiming generation two", pytrace=False)
        await asyncio.sleep(0.02)
    pytest.fail("worker B did not claim generation two", pytrace=False)


async def _wait_for_ready_input(
    runtime, input_id: UUID, generation: int, timeout: float
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await _read_input_state(runtime, input_id)
        if state and state["status"] == "ready" and state["generation"] == generation:
            return state
        if state and state["status"] == "failed":
            pytest.fail("accepted-video worker marked the input failed", pytrace=False)
        await asyncio.sleep(0.1)
    pytest.fail("accepted-video input did not reach its ready state", pytrace=False)


def _assert_owned_container(container_id: str, identity: str) -> dict:
    observed = _inspect(container_id)
    labels = observed.get("Config", {}).get("Labels") or {}
    if not (
        observed.get("Id") == container_id
        and labels.get(_ACCEPTANCE_LABEL) == "module-data-boundary"
        and labels.get(_IDENTITY_LABEL) == identity
    ):
        pytest.fail(
            "refusing to control a container outside this handoff", pytrace=False
        )
    return observed


async def _pause_owned_container(
    container_id: str,
    identity: str,
    paused_containers: set[str],
    timeout: float,
) -> bool:
    observed = _assert_owned_container(container_id, identity)
    if observed.get("State", {}).get("Running") is not True:
        return False
    try:
        await asyncio.to_thread(
            _docker,
            "pause",
            container_id,
            timeout=max(1, int(timeout)),
        )
    except Exception:
        return False
    paused_containers.add(container_id)
    observed = _assert_owned_container(container_id, identity)
    if observed.get("State", {}).get("Paused") is not True:
        paused_containers.discard(container_id)
        return False
    return True


async def _unpause_owned_container(container_id: str, identity: str) -> None:
    observed = _assert_owned_container(container_id, identity)
    if observed.get("State", {}).get("Paused") is True:
        await asyncio.to_thread(_docker, "unpause", container_id, timeout=30)


async def _wait_exit_zero(
    container_id: str, identity: str, timeout: float = 90
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = _assert_owned_container(container_id, identity)
        state = observed.get("State", {})
        if state.get("Running") is not True:
            if state.get("ExitCode") != 0:
                pytest.fail(
                    "Media worker container did not exit successfully", pytrace=False
                )
            return
        await asyncio.sleep(0.05)
    pytest.fail(
        "Media worker container did not exit within its bounded drain", pytrace=False
    )


async def _stop_owned_container(
    container_id: str, identity: str, grace_seconds: int = 30
) -> None:
    observed = _assert_owned_container(container_id, identity)
    if observed.get("State", {}).get("Running") is True:
        await asyncio.to_thread(
            _docker,
            "stop",
            "--time",
            str(grace_seconds),
            container_id,
            timeout=grace_seconds + 10,
        )
    await _wait_exit_zero(container_id, identity, timeout=5)


async def _expire_exact_lease(runtime, *, input_id: UUID, generation: int, token: UUID):
    async with runtime.sessions.begin() as owner:
        current = (
            await owner.execute(
                text(
                    "SELECT status,generation,lease_token,lease_owner FROM "
                    "public.asset_accepted_video_inputs WHERE input_id=:input FOR UPDATE"
                ),
                {"input": input_id},
            )
        ).first()
        if (
            current is None
            or current[0] != "materializing"
            or current[1] != generation
            or current[2] != token
        ):
            pytest.fail(
                "input lease changed before deterministic expiry", pytrace=False
            )
        attempt = (
            await owner.execute(
                text(
                    "SELECT status,lease_token FROM public.asset_accepted_video_input_attempts "
                    "WHERE input_id=:input AND generation=:generation FOR UPDATE"
                ),
                {"input": input_id, "generation": generation},
            )
        ).first()
        if attempt is None or attempt[0] != "leased" or attempt[1] != token:
            pytest.fail(
                "attempt lease changed before deterministic expiry", pytrace=False
            )
        input_update = await owner.execute(
            text(
                "UPDATE public.asset_accepted_video_inputs SET lease_expires_at="
                "pg_catalog.clock_timestamp()-interval '1 second' "
                "WHERE input_id=:input AND status='materializing' AND generation=:generation "
                "AND lease_token=:token AND lease_expires_at>pg_catalog.clock_timestamp() "
                "RETURNING input_id"
            ),
            {"input": input_id, "generation": generation, "token": token},
        )
        attempt_update = await owner.execute(
            text(
                "UPDATE public.asset_accepted_video_input_attempts SET lease_expires_at="
                "pg_catalog.clock_timestamp()-interval '1 second' "
                "WHERE input_id=:input AND status='leased' AND generation=:generation "
                "AND lease_token=:token AND lease_expires_at>pg_catalog.clock_timestamp() "
                "RETURNING input_id"
            ),
            {"input": input_id, "generation": generation, "token": token},
        )
        if len(input_update.all()) != 1 or len(attempt_update.all()) != 1:
            pytest.fail(
                "exact lease expiry did not update both guarded lease rows",
                pytrace=False,
            )
        expired = (
            await owner.execute(
                text(
                    "SELECT input.status,input.generation,input.lease_token,"
                    "input.lease_expires_at<pg_catalog.clock_timestamp(),"
                    "attempt.status,attempt.lease_token,"
                    "attempt.lease_expires_at<pg_catalog.clock_timestamp() "
                    "FROM public.asset_accepted_video_inputs AS input "
                    "JOIN public.asset_accepted_video_input_attempts AS attempt "
                    "ON attempt.input_id=input.input_id AND attempt.generation=input.generation "
                    "WHERE input.input_id=:input"
                ),
                {"input": input_id},
            )
        ).first()
        if not (
            expired
            and expired[0] == "materializing"
            and expired[1] == generation
            and expired[2] == token
            and expired[3] is True
            and expired[4] == "leased"
            and expired[5] == token
            and expired[6] is True
        ):
            pytest.fail("lease expiry update changed another lease fact", pytrace=False)


async def _heartbeat_as_worker(
    runtime, *, input_id: UUID, generation: int, token: UUID
):
    engine = create_async_engine(runtime.worker_url, poolclass=NullPool)
    try:
        async with engine.connect() as worker:
            value = await worker.scalar(
                text(
                    "SELECT aap_assets_v1.heartbeat_accepted_video_input("
                    "CAST(:input AS uuid),:generation,CAST(:token AS uuid),60)"
                ),
                {"input": input_id, "generation": generation, "token": token},
            )
    finally:
        await engine.dispose()
    reply = json.loads(value) if isinstance(value, str) else value
    if reply != {
        "schema": "aap-assets-video-input-execution/v1",
        "ok": False,
        "error": {"code": "lease_lost"},
    }:
        pytest.fail(
            "old worker heartbeat was not fenced with lease_lost", pytrace=False
        )


async def _wait_frame_ready(runtime, request, timeout: float = 90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = await runtime.client.frame(request)
        if frame.status == "ready":
            return frame
        if frame.status == "failed":
            pytest.fail("video frame worker failed the handoff plan", pytrace=False)
        await asyncio.sleep(0.1)
    pytest.fail(
        "B did not derive the bounded frame from its sealed plan", pytrace=False
    )


async def _new_video_input(runtime, *, actor, project, task, selection):
    async with runtime.sessions.begin() as owner:
        job = await _seed_video_job(
            owner,
            actor_id=actor,
            project_id=project,
            selection=selection,
        )
        header = await _call(
            owner,
            "capture_accepted_video_manifest",
            video_execution.CaptureVideoManifestRequest(
                job_id=job,
                actor_id=actor,
                project_id=project,
                selection=selection,
            ),
        )
    prepared = await runtime.client.prepare(
        video_execution.PrepareVideoMemberRequest(
            job_id=job,
            actor_id=actor,
            project_id=project,
            selection_digest=header.selection_digest,
            ordinal=1,
            task_id=task,
        )
    )
    if prepared.member.state != "preparing" or prepared.member.input is None:
        pytest.fail(
            "captured video member did not enter original materialization",
            pytrace=False,
        )
    return job, header, prepared.member.input.input_id


async def _wait_for_frame_object(owner, bucket: str, key: str, timeout: float = 90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return await asyncio.to_thread(owner.head_object, Bucket=bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {
                "404",
                "NoSuchKey",
                "NotFound",
            }:
                pytest.fail("could not observe the exact attempt object", pytrace=False)
        await asyncio.sleep(0.05)
    return None


async def test_media_containers_reclaim_and_fence_a_paused_original_lease(
    test_engine, tmp_path, record_property
):
    scenario_started = time.monotonic()
    deadline = scenario_started + _SCENARIO_SECONDS
    pytest.importorskip("asyncpg")
    worker_image = _required_env("AAP_VIDEO_CONSUMER_MEDIA_WORKER_IMAGE")
    worker_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_MEDIA_WORKER_URL",
        "asset_media_worker_client",
        test_engine,
    )
    control_db_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL",
        "asset_media_control_client",
        test_engine,
    )
    internal_s3, public_s3 = _container_s3_origins(settings)
    worker_database_url = _container_database_url(worker_url)
    identity = secrets.token_hex(12)
    containers: list[str] = []
    paused_containers: set[str] = set()
    env_file: Path | None = None
    cleanup_error = None

    async with _video_runtime(test_engine, tmp_path) as runtime:
        try:
            if (
                control_db_url.database != worker_url.database
                or control_db_url.database != test_engine.url.database
                or control_db_url.port != worker_url.port
            ):
                pytest.fail(
                    "Media roles did not target the verifier-owned test database",
                    pytrace=False,
                )
            if (
                _required_env("MINIO_INPUTS_BUCKET")
                != runtime.buckets["MINIO_INPUTS_BUCKET"]
            ):
                pytest.fail(
                    "worker inputs bucket differs from the verified resource manifest",
                    pytrace=False,
                )
            owner = runtime.owner
            worker_access, worker_secret = runtime.worker_access, runtime.worker_secret
            env_file = _write_worker_env(
                tmp_path / "video-handoff-worker.env",
                database_url=worker_database_url,
                internal_s3=internal_s3,
                public_s3=public_s3,
                datasets_bucket=runtime.buckets["MINIO_DATASETS_BUCKET"],
                cache_bucket=runtime.buckets["MINIO_MEDIA_CACHE_BUCKET"],
                inputs_bucket=runtime.buckets["MINIO_INPUTS_BUCKET"],
                access_key=worker_access,
                secret_key=worker_secret,
            )
            body = await asyncio.to_thread(
                _bounded_video_bytes,
                tmp_path,
                timeout=_remaining(deadline, 60),
            )
            record_property("video_handoff_source_bytes", len(body))
            record_property("video_handoff_source_frames", _FRAME_COUNT)
            record_property("video_handoff_source_width", 1280)
            record_property("video_handoff_source_height", 720)
            source_key = f"video-media-handoff/{identity}/{uuid4()}.mp4"
            upload = await asyncio.to_thread(
                owner.put_object,
                Bucket=runtime.buckets["MINIO_DATASETS_BUCKET"],
                Key=source_key,
                Body=body,
                ContentType="video/mp4",
            )
            source_version = upload.get("VersionId")
            if not source_version or source_version == "null":
                pytest.fail(
                    "handoff source upload did not create a version", pytrace=False
                )

            async with runtime.sessions.begin() as owner_db:
                actor, project, _, item, task = await _seed_video_task(owner_db)
                await owner_db.execute(
                    text(
                        "UPDATE public.dataset_items SET file_path=:path,file_size=:size,"
                        "content_hash=:sha WHERE id=:item"
                    ),
                    {
                        "path": source_key,
                        "size": len(body),
                        "sha": sha256(body).hexdigest(),
                        "item": item,
                    },
                )
            selection = _selection(task_ids=[task])
            selection["sampling"] = {
                "max_frames": 6,
                "step": 100,
                "width": 160,
                "format": "jpeg",
            }

            successful = None
            a_claim_seconds = None
            b_ready_seconds = None
            a_return_seconds = None
            frame_seconds = None
            worker_a_peak = None
            worker_b_peak = None
            reclaim_claim_seconds = None
            reclaim_ready_seconds = None
            for attempt_number in range(1, _TASK_RETRY_LIMIT + 1):
                _remaining(deadline, _SCENARIO_SECONDS)
                job, header, input_id = await _new_video_input(
                    runtime,
                    actor=actor,
                    project=project.id,
                    task=task,
                    selection=selection,
                )
                name = f"aap-video-handoff-{identity[:10]}-a{attempt_number}"
                command = [
                    "python",
                    "-m",
                    "aap_assets_media_runtime.worker",
                    "--once",
                    "--input-id",
                    str(input_id),
                ]
                worker_a = _start_worker_container(
                    name=name,
                    image=worker_image,
                    identity=identity,
                    environment_file=env_file,
                    command=command,
                    cpu="0.25",
                    timeout=_remaining(deadline, 30),
                )
                containers.append(worker_a)
                _verify_worker_container(
                    container_id=worker_a,
                    name=name,
                    image=worker_image,
                    identity=identity,
                    command=command,
                    cpu="0.25",
                    worker_database_url=worker_database_url,
                    access_key=worker_access,
                    secret_key=worker_secret,
                )
                worker_a_started = time.monotonic()
                outcome, first = await _wait_for_first_claim(
                    runtime,
                    input_id,
                    worker_a,
                    timeout=_remaining(deadline, 60),
                )
                if outcome == "completed":
                    await _wait_exit_zero(
                        worker_a, identity, timeout=_remaining(deadline, 45)
                    )
                    continue
                a_claim_seconds = time.monotonic() - worker_a_started

                old_token = first["lease_token"]
                old_worker_id = first["lease_owner"]
                original_key = (
                    video_execution.attempt_prefix(input_id, 1, old_token) + "source"
                )
                original_head = await _wait_for_frame_object(
                    owner,
                    runtime.buckets["MINIO_INPUTS_BUCKET"],
                    original_key,
                    timeout=_remaining(deadline, 45),
                )
                if original_head is None:
                    pytest.fail(
                        "worker A did not write its original attempt object",
                        pytrace=False,
                    )
                original_version = original_head.get("VersionId")
                if not original_version or original_version == "null":
                    pytest.fail(
                        "worker A original did not have a retained S3 version",
                        pytrace=False,
                    )
                first_worker_peak = await asyncio.to_thread(_memory_peak, worker_a)
                if await _read_input_state(runtime, input_id) is None:
                    pytest.fail("worker A input receipt disappeared", pytrace=False)
                paused = await _pause_owned_container(
                    worker_a,
                    identity,
                    paused_containers,
                    timeout=_remaining(deadline, 20),
                )
                if not paused:
                    current = await _read_input_state(runtime, input_id)
                    if (
                        current
                        and current["status"] == "ready"
                        and current["generation"] == 1
                    ):
                        await _wait_exit_zero(
                            worker_a, identity, timeout=_remaining(deadline, 45)
                        )
                        continue
                    pytest.fail(
                        "worker A could not be paused in its generation-one lease",
                        pytrace=False,
                    )
                paused_state = _assert_owned_container(worker_a, identity)
                if paused_state.get("State", {}).get("Paused") is not True:
                    pytest.fail("A was not paused before lease expiry", pytrace=False)
                current = await _read_input_state(runtime, input_id)
                if not (
                    current
                    and current["status"] == "materializing"
                    and current["generation"] == 1
                    and current["lease_token"] == old_token
                    and current["lease_owner"] == old_worker_id
                ):
                    await _unpause_owned_container(worker_a, identity)
                    paused_containers.discard(worker_a)
                    if (
                        current
                        and current["status"] == "ready"
                        and current["generation"] == 1
                    ):
                        await _wait_exit_zero(
                            worker_a, identity, timeout=_remaining(deadline, 45)
                        )
                        continue
                    pytest.fail(
                        "paused worker A no longer owned generation one", pytrace=False
                    )

                await _expire_exact_lease(
                    runtime,
                    input_id=input_id,
                    generation=1,
                    token=old_token,
                )
                reclaim_started = time.monotonic()
                worker_b_name = f"aap-video-handoff-{identity[:10]}-b{attempt_number}"
                worker_b_command = [
                    "python",
                    "-m",
                    "aap_assets_media_runtime.worker",
                ]
                worker_b = _start_worker_container(
                    name=worker_b_name,
                    image=worker_image,
                    identity=identity,
                    environment_file=env_file,
                    command=worker_b_command,
                    cpu="0.5",
                    timeout=_remaining(deadline, 30),
                )
                containers.append(worker_b)
                _verify_worker_container(
                    container_id=worker_b,
                    name=worker_b_name,
                    image=worker_image,
                    identity=identity,
                    command=worker_b_command,
                    cpu="0.5",
                    worker_database_url=worker_database_url,
                    access_key=worker_access,
                    secret_key=worker_secret,
                )
                second_lease = await _wait_for_generation_two_claim(
                    runtime,
                    input_id,
                    old_worker_id,
                    worker_b,
                    identity,
                    timeout=_remaining(deadline, 60),
                )
                reclaim_claim_seconds = time.monotonic() - reclaim_started
                worker_b_id = second_lease["lease_owner"]
                if worker_b_id == old_worker_id:
                    pytest.fail(
                        "worker B reused A's worker ownership UUID", pytrace=False
                    )
                ready_started = time.monotonic()
                ready = await _wait_for_ready_input(
                    runtime,
                    input_id,
                    2,
                    timeout=_remaining(deadline, 60),
                )
                b_ready_seconds = time.monotonic() - ready_started
                reclaim_ready_seconds = time.monotonic() - reclaim_started
                attempts = await _read_input_attempts(runtime, input_id)
                if not (
                    len(attempts) == 2
                    and attempts[0]["generation"] == 1
                    and attempts[0]["lease_token"] == old_token
                    and attempts[0]["status"] == "abandoned"
                    and attempts[1]["generation"] == 2
                    and attempts[1]["lease_token"] != old_token
                    and attempts[1]["status"] == "ready"
                    and ready["status"] == "ready"
                    and ready["generation"] == 2
                    and ready["attempt_count"] == 2
                ):
                    pytest.fail(
                        "B did not reclaim the exact expired original attempt",
                        pytrace=False,
                    )
                await _heartbeat_as_worker(
                    runtime,
                    input_id=input_id,
                    generation=1,
                    token=old_token,
                )
                successful = (
                    job,
                    header,
                    input_id,
                    ready,
                    worker_a,
                    worker_b,
                    old_token,
                    first_worker_peak,
                )
                break

            if successful is None:
                pytest.fail(
                    f"worker A completed before pause in all {_TASK_RETRY_LIMIT} bounded attempts",
                    pytrace=False,
                )
            (
                job,
                header,
                input_id,
                ready_before_old_return,
                worker_a,
                worker_b,
                old_token,
                worker_a_peak,
            ) = successful

            await _unpause_owned_container(worker_a, identity)
            paused_containers.discard(worker_a)
            old_return_started = time.monotonic()
            await _wait_exit_zero(worker_a, identity, timeout=_remaining(deadline, 45))
            a_return_seconds = time.monotonic() - old_return_started
            final_state = await _read_input_state(runtime, input_id)
            attempts_after_old_return = await _read_input_attempts(runtime, input_id)
            if not (
                final_state
                and final_state["status"] == "ready"
                and final_state["generation"] == 2
                and final_state["plan"]["plan_digest"]
                == ready_before_old_return["plan"]["plan_digest"]
                and [attempt["status"] for attempt in attempts_after_old_return]
                == ["abandoned", "ready"]
            ):
                pytest.fail(
                    "stale worker A changed B's ready plan after resuming",
                    pytrace=False,
                )

            await runtime.client.seal(
                video_execution.SealVideoManifestRequest(
                    job_id=job,
                    actor_id=actor,
                    project_id=project.id,
                    selection_digest=header.selection_digest,
                )
            )
            plan = video_execution.FrozenFramePlan.model_validate(final_state["plan"])
            if not (
                plan.input.source.object_identity.version_id == source_version
                and plan.input.blob.version_id not in {None, "null"}
                and plan.input.blob.sha256 == sha256(body).hexdigest()
                and plan.selected_count == 6
                and plan.input.video.frame_count == _FRAME_COUNT
                and plan.input.video.width == 1280
                and plan.input.video.height == 720
            ):
                pytest.fail(
                    "sealed B plan lost its captured original/version/timebase",
                    pytrace=False,
                )

            original = await asyncio.to_thread(
                owner.get_object,
                Bucket=runtime.buckets["MINIO_INPUTS_BUCKET"],
                Key=plan.input.blob.key,
                VersionId=plan.input.blob.version_id,
            )
            try:
                original_bytes = await asyncio.to_thread(original["Body"].read)
            finally:
                original["Body"].close()
            if sha256(original_bytes).hexdigest() != sha256(body).hexdigest():
                pytest.fail(
                    "retained original was not byte-identical to source version",
                    pytrace=False,
                )

            frame_request = video_execution.VideoFrameRequest(
                actor_id=actor,
                job_id=job,
                task_id=task,
                input_id=input_id,
                plan_digest=plan.plan_digest,
                ordinal=1,
            )
            frame_started = time.monotonic()
            pending = await runtime.client.require_frame(frame_request)
            if pending.status not in {"pending", "materializing", "ready"}:
                pytest.fail(
                    "B frame request returned an invalid readiness state", pytrace=False
                )
            frame = await _wait_frame_ready(
                runtime, frame_request, timeout=_remaining(deadline, 45)
            )
            frame_seconds = time.monotonic() - frame_started
            assert frame.blob is not None
            frame_object = await asyncio.to_thread(
                owner.get_object,
                Bucket=runtime.buckets["MINIO_INPUTS_BUCKET"],
                Key=frame.blob.key,
                VersionId=frame.blob.version_id,
            )
            try:
                frame_bytes = await asyncio.to_thread(frame_object["Body"].read)
            finally:
                frame_object["Body"].close()
            if sha256(frame_bytes).hexdigest() != frame.blob.sha256:
                pytest.fail(
                    "B frame bytes did not match the immutable versioned hash",
                    pytrace=False,
                )
            with Image.open(BytesIO(frame_bytes)) as image:
                image.load()
                if image.format != "JPEG" or image.size != (160, 90):
                    pytest.fail(
                        "B frame did not match the selected JPEG dimensions",
                        pytrace=False,
                    )
                frame_width, frame_height = image.size
            reference = await runtime.client.reference(frame_request)
            response = await runtime.http.get(reference.url)
            if (
                response.status_code != 200
                or sha256(response.content).hexdigest() != frame.blob.sha256
            ):
                pytest.fail(
                    "frame reference did not serve the exact versioned bytes",
                    pytrace=False,
                )

            worker_b_peak = await asyncio.to_thread(_memory_peak, worker_b)
            await _stop_owned_container(
                worker_b,
                identity,
                grace_seconds=max(1, int(_remaining(deadline, 30))),
            )
            final_state = await _read_input_state(runtime, input_id)
            if not (
                final_state
                and final_state["status"] == "ready"
                and final_state["generation"] == 2
                and final_state["plan"]["plan_digest"] == plan.plan_digest
            ):
                pytest.fail(
                    "B stop changed the ready retained-video plan", pytrace=False
                )

            record_property("video_handoff_selected_frames", plan.selected_count)
            record_property("video_handoff_attempt_count", ready["attempt_count"])
            record_property("video_handoff_input_generation", final_state["generation"])
            record_property("video_handoff_generation_one", attempts[0]["generation"])
            record_property("video_handoff_generation_two", attempts[1]["generation"])
            record_property(
                "video_handoff_generation_one_status", attempts[0]["status"]
            )
            record_property(
                "video_handoff_generation_two_status", attempts[1]["status"]
            )
            record_property(
                "video_handoff_generation_two_worker_differs",
                worker_b_id != old_worker_id,
            )
            record_property(
                "video_handoff_a_claim_seconds", round(a_claim_seconds or 0, 3)
            )
            record_property(
                "video_handoff_expiry_to_b_claim_seconds",
                round(reclaim_claim_seconds or 0, 3),
            )
            record_property(
                "video_handoff_expiry_to_b_ready_seconds",
                round(reclaim_ready_seconds or 0, 3),
            )
            record_property(
                "video_handoff_b_ready_seconds", round(b_ready_seconds or 0, 3)
            )
            record_property(
                "video_handoff_old_a_return_seconds", round(a_return_seconds or 0, 3)
            )
            record_property("video_handoff_frame_seconds", round(frame_seconds or 0, 3))
            record_property(
                "video_handoff_source_duration_ms", plan.input.video.duration_ms
            )
            record_property("video_handoff_source_width", plan.input.video.width)
            record_property("video_handoff_source_height", plan.input.video.height)
            record_property("video_handoff_frame_width", frame_width)
            record_property("video_handoff_frame_height", frame_height)
            record_property("video_handoff_frame_bytes", len(frame_bytes))
            record_property(
                "video_handoff_worker_a_cgroup_memory_peak_bytes",
                worker_a_peak if worker_a_peak is not None else "unavailable",
            )
            record_property(
                "video_handoff_worker_b_cgroup_memory_peak_bytes",
                worker_b_peak if worker_b_peak is not None else "unavailable",
            )
            record_property(
                "video_handoff_total_seconds",
                round(time.monotonic() - scenario_started, 3),
            )

        finally:
            for container_id in list(paused_containers):
                try:
                    await _unpause_owned_container(container_id, identity)
                except Exception:
                    cleanup_error = "paused owned worker could not be unpaused"
            for container_id in reversed(containers):
                try:
                    await asyncio.to_thread(
                        _stop_remove_container, container_id, identity
                    )
                except Exception:
                    cleanup_error = (
                        "one or more owned Media containers could not be removed"
                    )
            if env_file is not None:
                env_file.unlink(missing_ok=True)
            if cleanup_error is not None:
                pytest.fail(cleanup_error, pytrace=False)
