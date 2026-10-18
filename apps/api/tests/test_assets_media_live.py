"""Real control/worker acceptance using the module-data verifier's owned resources."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from tests.factory import create_project
from tests.test_asset_storage_cleanup_runtime import (
    _expect_s3_denied,
    _local_s3_client,
    _object_exists,
    _required_env,
    _verified_resource_manifest,
    _verify_bucket_owner,
    _verify_postgres,
)
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the module-data verifier's owned PostgreSQL and S3 resources",
    ),
]
ROOT = Path(__file__).resolve().parents[3]


def _guard_role_url(env_name: str, username: str, test_engine):
    raw = _required_env(env_name)
    try:
        parsed = make_url(raw)
    except Exception:
        pytest.fail(f"the verifier supplied an invalid {username} URL", pytrace=False)
    if not (
        parsed.username == username
        and parsed.password
        and parsed.host == "127.0.0.1"
        and parsed.port is not None
        and parsed.database
        and parsed.database.endswith("_test")
        and parsed.database == test_engine.url.database
    ):
        pytest.fail(f"{username} URL did not match the owned loopback test DB")
    _verify_postgres(parsed)
    return parsed


def _runtime_environment(
    *,
    tmp_path: Path,
    role: str,
    database_url: str,
    endpoint: str,
    public_endpoint: str,
    datasets_bucket: str,
    cache_bucket: str,
    access_key: str,
    secret_key: str,
    legacy_bucket: str | None = None,
    token: str | None = None,
    port: int | None = None,
    ffmpeg_gate: tuple[Path, Path] | None = None,
    image_worker: bool = False,
    srgb_profile: Path | None = None,
    image_control_settings: dict[str, str] | None = None,
    vips_gate: tuple[Path, Path] | None = None,
) -> dict[str, str]:
    environment = {
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join(
            (
                str(ROOT / "apps/assets-media/src"),
                str(ROOT / "apps/media-probe/src"),
            )
        ),
        "TMPDIR": str(tmp_path),
        "ASSETS_MEDIA_S3_ENDPOINT": endpoint,
        "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT": public_endpoint,
        "ASSETS_MEDIA_S3_REGION": "us-east-1",
        "ASSETS_MEDIA_S3_ACCESS_KEY": access_key,
        "ASSETS_MEDIA_S3_SECRET_KEY": secret_key,
        "ASSETS_MEDIA_DATASETS_BUCKET": datasets_bucket,
        "ASSETS_MEDIA_CACHE_BUCKET": cache_bucket,
        "ASSETS_MEDIA_S3_CONNECT_TIMEOUT_SECONDS": "2",
        "ASSETS_MEDIA_S3_READ_TIMEOUT_SECONDS": "10",
        "ASSETS_MEDIA_S3_MAX_ATTEMPTS": "1",
    }
    for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    if role == "control":
        assert token is not None and port is not None
        if legacy_bucket is None:
            pytest.fail("the owned legacy bucket is required for image control")
        environment.update(
            {
                "ASSETS_MEDIA_CONTROL_DATABASE_URL": database_url,
                "ASSETS_MEDIA_TOKEN": token,
                "ASSETS_MEDIA_HOST": "127.0.0.1",
                "ASSETS_MEDIA_PORT": str(port),
                "ASSETS_MEDIA_POOL_MIN_SIZE": "1",
                "ASSETS_MEDIA_POOL_MAX_SIZE": "2",
                "ASSETS_IMAGES_LEGACY_BUCKET": legacy_bucket,
            }
        )
        if image_control_settings is not None:
            environment.update(image_control_settings)
    else:
        if image_worker:
            if legacy_bucket is None or srgb_profile is None:
                pytest.fail(
                    "image worker requires owned legacy bucket and sRGB profile"
                )
            environment.update(
                {
                    "ASSETS_IMAGES_DATABASE_URL": database_url,
                    "ASSETS_IMAGES_LEGACY_BUCKET": legacy_bucket,
                    "ASSETS_IMAGES_POLL_SECONDS": "0.1",
                    "ASSETS_IMAGES_SRGB_PROFILE": str(srgb_profile),
                }
            )
        else:
            environment.update(
                {
                    "ASSETS_MEDIA_WORKER_DATABASE_URL": database_url,
                    "ASSETS_MEDIA_POLL_SECONDS": "0.1",
                    "ASSETS_MEDIA_LEASE_SECONDS": "30",
                    "ASSETS_MEDIA_FRAME_TTL_DAYS": "14",
                    "ASSETS_MEDIA_CHUNK_TTL_DAYS": "30",
                }
            )
    if ffmpeg_gate is not None:
        gate_directory, marker_directory = ffmpeg_gate
        environment["PATH"] = str(gate_directory) + os.pathsep + environment["PATH"]
        environment["AAP_MEDIA_FFMPEG_REAL"] = shutil.which("ffmpeg") or ""
        environment["AAP_MEDIA_FFMPEG_GATE_ENTERED"] = str(marker_directory / "entered")
        environment["AAP_MEDIA_FFMPEG_GATE_RELEASE"] = str(marker_directory / "release")
    if vips_gate is not None:
        gate_directory, marker_directory = vips_gate
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(gate_directory), environment["PYTHONPATH"])
        )
        environment["AAP_IMAGES_VIPS_GATE_ENTERED"] = str(marker_directory / "entered")
        environment["AAP_IMAGES_VIPS_GATE_RELEASE"] = str(marker_directory / "release")
    return environment


def _runtime_database_url(url) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _start_process(command: list[str], environment: dict[str, str], log_path: Path):
    log_stream = log_path.open("wb")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log_stream,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
    )
    return process, log_stream


def _process_log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[-4000:]


def _stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGCONT)
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _wait_control_ready(
    client: httpx.AsyncClient, process: subprocess.Popen, log_path: Path
) -> str:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(
                "media control exited before readiness: " + _process_log(log_path)
            )
        try:
            response = await client.get("/health/ready")
            if response.status_code == 200 and response.json() == {"status": "ok"}:
                return str(client.base_url).rstrip("/")
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    pytest.fail("media control did not become ready")


async def _wait_for_file(
    path: Path, process: subprocess.Popen, log_path: Path, *, timeout: float = 60
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        if process.poll() is not None:
            pytest.fail(
                "media worker exited before reaching the FFmpeg gate: "
                + _process_log(log_path)
            )
        await asyncio.sleep(0.1)
    pytest.fail("media worker did not reach the FFmpeg gate")


async def _wait_process_exit(
    process: subprocess.Popen, *, timeout: float, message: str
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = process.poll()
        if result is not None:
            return result
        await asyncio.sleep(0.1)
    pytest.fail(message)


async def _post(
    client: httpx.AsyncClient, token: str, path: str, body: dict
) -> httpx.Response:
    return await client.post(
        path,
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )


async def _wait_source_ready(
    client: httpx.AsyncClient,
    token: str,
    request: dict,
    worker: subprocess.Popen,
    worker_log: Path,
) -> dict:
    deadline = time.monotonic() + 90
    last = None
    while time.monotonic() < deadline:
        if worker.poll() is not None:
            pytest.fail(
                "media worker exited during metadata work: " + _process_log(worker_log)
            )
        response = await _post(client, token, "/v1/source-reference", request)
        if response.status_code == 409 and response.json().get("error", {}).get(
            "code"
        ) in {"operation_busy", "source_busy", "task_busy", "account_busy"}:
            await asyncio.sleep(0.2)
            continue
        if response.status_code != 200:
            pytest.fail(
                "source reference failed with "
                f"HTTP {response.status_code}: {response.text[:300]}"
            )
        last = response.json()["data"]
        source = last["source"]
        metadata = source["video_metadata"]
        if (
            not source["source_drifted"]
            and source["materialized_generation"] == source["source_generation"]
            and metadata.get("frame_count", 0) >= 10
            and metadata.get("poster_frame_path")
            and metadata.get("playback_path")
        ):
            return last
        await asyncio.sleep(0.2)
    pytest.fail(
        f"video metadata/poster/playback did not become ready: {last['source']['video_metadata'] if last else None}"
    )


async def _wait_asset_ready(
    client: httpx.AsyncClient,
    token: str,
    request: dict,
    kind: str,
    locator: int,
    worker: subprocess.Popen,
    worker_log: Path,
) -> dict:
    deadline = time.monotonic() + 90
    last = None
    while time.monotonic() < deadline:
        if worker.poll() is not None:
            pytest.fail(
                "media worker exited during cache work: " + _process_log(worker_log)
            )
        response = await _post(client, token, "/v1/assets-reference", request)
        if response.status_code in {409, 503} and response.json().get("error", {}).get(
            "code"
        ) in {
            "metadata_not_ready",
            "operation_busy",
            "source_busy",
            "task_busy",
            "account_busy",
        }:
            await asyncio.sleep(0.2)
            continue
        if response.status_code != 200:
            pytest.fail(
                "asset reference failed with "
                f"HTTP {response.status_code}: {response.text[:300]}"
            )
        last = response.json()["data"]["state"]["assets"]
        asset = next(
            row
            for row in last
            if row["kind"] == kind
            and row["frame_index" if kind == "frame" else "chunk_id"] == locator
        )
        if asset["status"] == "ready" and asset.get("url"):
            return asset
        await asyncio.sleep(0.2)
    pytest.fail(
        f"{kind} asset did not become ready: {[(row['status'], row.get('error')) for row in last] if last else None}"
    )


async def _job_for_frame(test_engine, item_id, frame_index: int, width: int):
    async with test_engine.connect() as owner:
        row = (
            (
                await owner.execute(
                    text(
                        "SELECT job_id,generation,lease_token,status "
                        "FROM public.asset_media_jobs "
                        "WHERE dataset_item_id=:item AND kind='frame' "
                        "AND spec->>'frame_index'=:frame "
                        "AND spec->>'width'=:width AND spec->>'format'='webp' "
                        "ORDER BY updated_at DESC LIMIT 1"
                    ),
                    {"item": item_id, "frame": str(frame_index), "width": str(width)},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    return dict(row)


async def _wait_job_ready(test_engine, job_id, *, timeout: float = 60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with test_engine.connect() as owner:
            row = (
                await owner.execute(
                    text(
                        "SELECT status FROM public.asset_media_jobs WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
            ).first()
        if row is not None and row.status == "ready":
            return
        await asyncio.sleep(0.1)
    pytest.fail("expired media attempt was not reclaimed and completed")


def _write_ffmpeg_gate(directory: Path) -> tuple[Path, Path]:
    gate_directory = directory / "gate-bin"
    marker_directory = directory / "gate-state"
    gate_directory.mkdir(parents=True)
    marker_directory.mkdir(parents=True)
    wrapper = gate_directory / "ffmpeg"
    wrapper.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        ': > "$AAP_MEDIA_FFMPEG_GATE_ENTERED"\n'
        'while [ ! -f "$AAP_MEDIA_FFMPEG_GATE_RELEASE" ]; do sleep 0.05; done\n'
        'exec "$AAP_MEDIA_FFMPEG_REAL" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    return gate_directory, marker_directory


async def _assert_sql_role_is_narrow(raw_url: str, username: str, table: str):
    engine = create_async_engine(raw_url, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT current_user")) == username
            with pytest.raises(DBAPIError) as denied_table:
                await connection.execute(text(f"SELECT count(*) FROM {table}"))
            assert getattr(denied_table.value.orig, "sqlstate", None) == "42501"
            await connection.rollback()
            with pytest.raises(DBAPIError) as denied_foreign:
                await connection.execute(
                    text("SELECT aap_identity_v1.read_actor_facts(NULL::uuid)")
                )
            assert getattr(denied_foreign.value.orig, "sqlstate", None) == "42501"
            await connection.rollback()
    finally:
        await engine.dispose()


def _assert_storage_scopes(
    *,
    control_s3,
    worker_s3,
    owner_s3,
    datasets_bucket: str,
    cache_bucket: str,
    source_key: str,
    frame_key: str,
    item_id,
    owned_objects: list[tuple[str, str]],
) -> None:
    for client in (control_s3, worker_s3):
        body = client.get_object(Bucket=datasets_bucket, Key=source_key)["Body"]
        assert body.read()
        body.close()
        _expect_s3_denied(
            lambda client=client: client.list_objects_v2(
                Bucket=datasets_bucket, Prefix="source/", MaxKeys=1
            )
        )
        probe_key = f"source/denied-{uuid4()}.bin"
        owned_objects.append((datasets_bucket, probe_key))
        _expect_s3_denied(
            lambda client=client, key=probe_key: client.put_object(
                Bucket=datasets_bucket, Key=key, Body=b"must be denied"
            )
        )
        delete_key = f"source/delete-probe-{uuid4()}.bin"
        owner_s3.put_object(Bucket=datasets_bucket, Key=delete_key, Body=b"retain")
        owned_objects.append((datasets_bucket, delete_key))
        _expect_s3_denied(
            lambda client=client, key=delete_key: client.delete_object(
                Bucket=datasets_bucket, Key=key
            )
        )

    cache_head = control_s3.head_object(Bucket=cache_bucket, Key=frame_key)
    assert cache_head["ContentLength"] > 0
    cache_body = control_s3.get_object(Bucket=cache_bucket, Key=frame_key)["Body"]
    assert cache_body.read()
    cache_body.close()
    _expect_s3_denied(
        lambda: control_s3.list_objects_v2(
            Bucket=cache_bucket, Prefix="media-assets/", MaxKeys=1
        )
    )
    _expect_s3_denied(
        lambda: control_s3.put_object(
            Bucket=cache_bucket, Key=frame_key, Body=b"must be denied"
        )
    )
    _expect_s3_denied(
        lambda: control_s3.delete_object(Bucket=cache_bucket, Key=frame_key)
    )

    worker_body = worker_s3.get_object(Bucket=cache_bucket, Key=frame_key)["Body"]
    assert worker_body.read()
    worker_body.close()
    prefix = frame_key.rsplit("/", 1)[0] + "/"
    assert (
        worker_s3.list_objects_v2(Bucket=cache_bucket, Prefix=prefix, MaxKeys=10)[
            "KeyCount"
        ]
        >= 1
    )
    _expect_s3_denied(
        lambda: worker_s3.list_objects_v2(
            Bucket=cache_bucket, Prefix="videos/", MaxKeys=1
        )
    )
    worker_probe = f"media-assets/{uuid4()}/g1/a{uuid4()}/permission-probe.webp"
    owned_objects.append((cache_bucket, worker_probe))
    worker_s3.put_object(Bucket=cache_bucket, Key=worker_probe, Body=b"scoped")
    worker_read = worker_s3.get_object(Bucket=cache_bucket, Key=worker_probe)["Body"]
    assert worker_read.read() == b"scoped"
    worker_read.close()
    assert (
        worker_s3.list_objects_v2(
            Bucket=cache_bucket, Prefix=worker_probe.rsplit("/", 1)[0] + "/", MaxKeys=1
        )["KeyCount"]
        == 1
    )
    worker_s3.delete_object(Bucket=cache_bucket, Key=worker_probe)

    legacy_key = f"videos/{item_id}/frames/0_512.webp"
    owner_s3.put_object(Bucket=cache_bucket, Key=legacy_key, Body=b"legacy")
    owned_objects.append((cache_bucket, legacy_key))
    worker_s3.delete_object(Bucket=cache_bucket, Key=legacy_key)
    assert not _object_exists(owner_s3, cache_bucket, legacy_key)
    _expect_s3_denied(
        lambda: worker_s3.delete_object(
            Bucket=cache_bucket, Key=f"videos/{item_id}/poster.webp"
        )
    )


async def _delete_objects(client, bucket: str, prefixes: list[str]) -> None:
    for prefix in prefixes:
        continuation = None
        while True:
            request = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
            if continuation is not None:
                request["ContinuationToken"] = continuation
            page = await asyncio.to_thread(client.list_objects_v2, **request)
            keys = [row["Key"] for row in page.get("Contents", [])]
            if keys:
                await asyncio.to_thread(
                    client.delete_objects,
                    Bucket=bucket,
                    Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
                )
            if not page.get("IsTruncated"):
                break
            continuation = page.get("NextContinuationToken")


async def test_real_control_and_worker_runtime_scopes_and_fences(test_engine, tmp_path):
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
    if not (
        test_engine.url.database
        and test_engine.url.database.endswith("_test")
        and control_url.database == worker_url.database
        and control_url.port == worker_url.port
    ):
        pytest.fail("media roles did not share the owned test database")
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
    scheme = "https" if settings.minio_use_ssl else "http"
    endpoint = f"{scheme}://{settings.minio_endpoint}"
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
        pytest.fail("media runtimes were given an unscoped or shared S3 identity")
    control_s3 = _local_s3_client(settings, control_access, control_secret)
    worker_s3 = _local_s3_client(settings, worker_access, worker_secret)

    await _assert_sql_role_is_narrow(
        control_url.render_as_string(hide_password=False),
        "asset_media_control_client",
        "public.dataset_items",
    )
    await _assert_sql_role_is_narrow(
        worker_url.render_as_string(hide_password=False),
        "asset_media_worker_client",
        "public.asset_media_jobs",
    )

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    assert ffmpeg and ffprobe, "live media acceptance requires ffmpeg and ffprobe"
    video_path = tmp_path / "source.mp4"
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
            "testsrc2=size=160x90:rate=10",
            "-t",
            "2",
            "-an",
            "-c:v",
            "mpeg4",
            "-q:v",
            "2",
            "-g",
            "5",
            "-keyint_min",
            "5",
            "-bf",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert generated.returncode == 0, "ffmpeg media fixture generation failed"
    source_bytes = video_path.read_bytes()
    source_key = f"source/{uuid4()}/original.mp4"
    denied_objects: list[tuple[str, str]] = []
    owned_objects: list[tuple[str, str]] = [(datasets_bucket, source_key)]
    actor_id = dataset_id = project_id = task_id = None
    item_id = uuid4()
    source_identity = None
    control_process = worker_process = worker_a = worker_c = worker_b = None
    log_streams = []
    owner_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    control_log = tmp_path / "media-control.log"
    worker_log = tmp_path / "media-worker.log"
    worker_a_log = tmp_path / "media-worker-expired.log"
    worker_b_log = tmp_path / "media-worker-reclaim.log"
    worker_c_log = tmp_path / "media-worker-source-change.log"
    gate_a = _write_ffmpeg_gate(tmp_path / "lease-gate")
    gate_c = _write_ffmpeg_gate(tmp_path / "source-gate")
    control_token = secrets.token_urlsafe(32)
    control_port = _free_loopback_port()
    control_environment = _runtime_environment(
        tmp_path=tmp_path,
        role="control",
        database_url=_runtime_database_url(control_url),
        endpoint=endpoint,
        public_endpoint=endpoint,
        datasets_bucket=datasets_bucket,
        legacy_bucket=legacy_bucket,
        cache_bucket=cache_bucket,
        access_key=control_access,
        secret_key=control_secret,
        token=control_token,
        port=control_port,
    )
    worker_environment = _runtime_environment(
        tmp_path=tmp_path,
        role="worker",
        database_url=_runtime_database_url(worker_url),
        endpoint=endpoint,
        public_endpoint=endpoint,
        datasets_bucket=datasets_bucket,
        cache_bucket=cache_bucket,
        access_key=worker_access,
        secret_key=worker_secret,
    )
    assert "DATABASE_URL" not in control_environment
    assert "CELERY_BROKER_URL" not in control_environment
    assert "ASSETS_MEDIA_TOKEN" not in worker_environment

    try:
        owner_s3.put_object(
            Bucket=datasets_bucket,
            Key=source_key,
            Body=source_bytes,
            ContentType="video/mp4",
        )
        async with owner_sessions.begin() as owner:
            actor_id = await _seed_actor(owner, role="super_admin")
            dataset_id = await _seed_dataset(
                owner,
                actor_id=actor_id,
                name=f"media-live-{uuid4().hex}",
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
                name=f"media-live-{uuid4().hex}",
                type_key="video",
            )
            project_id = project.id
            task_id = uuid4()
            owner.add(
                Task(
                    id=task_id,
                    project_id=project_id,
                    dataset_item_id=item_id,
                    display_id=f"T-MEDIA-LIVE-{task_id.hex[:12]}",
                    file_name="source.mp4",
                    file_path=source_key,
                    file_type="video",
                )
            )

        control_process, stream = _start_process(
            [sys.executable, "-m", "aap_assets_media_runtime"],
            control_environment,
            control_log,
        )
        log_streams.append(stream)
        control_origin = f"http://127.0.0.1:{control_port}"
        async with httpx.AsyncClient(
            base_url=control_origin, timeout=5, trust_env=False
        ) as client:
            await _wait_control_ready(client, control_process, control_log)

            probe = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    "-m",
                    "aap_assets_media_runtime.worker",
                    "--healthcheck",
                ],
                cwd=ROOT,
                env=worker_environment,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert probe.returncode == 0, (
                "restricted media worker login failed healthcheck"
            )
            worker_process, stream = _start_process(
                [sys.executable, "-m", "aap_assets_media_runtime.worker"],
                worker_environment,
                worker_log,
            )
            log_streams.append(stream)

            source_request = {
                "actor_id": str(actor_id),
                "dataset_item_id": str(item_id),
                "task_id": str(task_id),
            }
            source_reference = await _wait_source_ready(
                client, control_token, source_request, worker_process, worker_log
            )
            source = source_reference["source"]
            source_identity = source["source_snapshot"]
            assert source["file_size"] == len(source_bytes)
            assert source["video_metadata"]["frame_count"] >= 10
            assert source_reference["poster_url"]
            assert source_reference["video_url"] != source_reference["original_url"]
            for signed_url in (
                source_reference["original_url"],
                source_reference["poster_url"],
                source_reference["video_url"],
            ):
                fetched = await client.get(signed_url)
                assert fetched.status_code == 200 and fetched.content

            timetable_response = await _post(
                client,
                control_token,
                "/v1/commands/read_video_timetable",
                {**source_request, "page_size": 100},
            )
            assert timetable_response.status_code == 200, _process_log(control_log)
            timetable = timetable_response.json()["data"]
            assert timetable["complete"] and timetable["total"] >= 10
            assert len(timetable["frames"]) == timetable["total"]

            frame_request = {
                **source_request,
                "selection": {
                    "kind": "frames",
                    "intent": "access",
                    "expected_source_snapshot": source_identity,
                    "frame_indices": [1],
                    "width": 96,
                    "format": "webp",
                    "force": False,
                    "failed_only": False,
                },
            }
            frame = await _wait_asset_ready(
                client,
                control_token,
                frame_request,
                "frame",
                1,
                worker_process,
                worker_log,
            )
            frame_response = await client.get(frame["url"])
            assert frame_response.status_code == 200 and frame_response.content
            frame_key = frame["storage_key"]

            chunk_request = {
                **source_request,
                "selection": {
                    "kind": "chunks",
                    "expected_source_snapshot": source_identity,
                    "chunk_ids": [0],
                    "chunk_size_frames": 4,
                    "force": False,
                    "failed_only": False,
                },
            }
            chunk = await _wait_asset_ready(
                client,
                control_token,
                chunk_request,
                "chunk",
                0,
                worker_process,
                worker_log,
            )
            chunk_response = await client.get(chunk["url"])
            assert chunk_response.status_code == 200 and chunk_response.content
            samples_response = await _post(
                client,
                control_token,
                "/v1/commands/read_video_samples",
                {**source_request, "chunk_id": 0, "page_size": 100},
            )
            assert samples_response.status_code == 200
            samples = samples_response.json()["data"]
            assert samples["complete"] and samples["total"] > 0
            assert len(samples["samples"]) == samples["total"]

            _assert_storage_scopes(
                control_s3=control_s3,
                worker_s3=worker_s3,
                owner_s3=owner_s3,
                datasets_bucket=datasets_bucket,
                cache_bucket=cache_bucket,
                source_key=source_key,
                frame_key=frame_key,
                item_id=item_id,
                owned_objects=denied_objects,
            )

            original_frame_key = frame_key
            await asyncio.to_thread(
                owner_s3.delete_object, Bucket=cache_bucket, Key=original_frame_key
            )
            regenerated = await _wait_asset_ready(
                client,
                control_token,
                frame_request,
                "frame",
                1,
                worker_process,
                worker_log,
            )
            assert regenerated["storage_key"] != original_frame_key
            assert (await client.get(regenerated["url"])).status_code == 200

            rejected_worker_http = await _post(
                client,
                control_token,
                "/v1/commands/claim_video_media",
                {},
            )
            assert rejected_worker_http.status_code == 403
            assert rejected_worker_http.json()["error"]["code"] == "permission_denied"

            # Pause one claimed frame attempt, expire its lease, and let another
            # restricted worker reclaim and publish the new lease's object.
            _stop_process(worker_process)
            worker_process = None
            expired_frame_index = 12
            expired_request = {
                **source_request,
                "selection": {
                    "kind": "frames",
                    "intent": "access",
                    "expected_source_snapshot": source_identity,
                    "frame_indices": [expired_frame_index],
                    "width": 96,
                    "format": "webp",
                    "force": False,
                    "failed_only": False,
                },
            }
            pending = await _post(
                client, control_token, "/v1/assets-reference", expired_request
            )
            assert pending.status_code == 200
            expired_asset = next(
                row
                for row in pending.json()["data"]["state"]["assets"]
                if row["frame_index"] == expired_frame_index
            )
            assert expired_asset["status"] in {"pending", "running"}
            expired_job = await _job_for_frame(
                test_engine, item_id, expired_frame_index, 96
            )
            assert expired_job["lease_token"] is None
            gate_entered = gate_a[1] / "entered"
            worker_a_environment = _runtime_environment(
                tmp_path=tmp_path,
                role="worker",
                database_url=_runtime_database_url(worker_url),
                endpoint=endpoint,
                public_endpoint=endpoint,
                datasets_bucket=datasets_bucket,
                cache_bucket=cache_bucket,
                access_key=worker_access,
                secret_key=worker_secret,
                ffmpeg_gate=gate_a,
            )
            worker_a, stream = _start_process(
                [
                    sys.executable,
                    "-m",
                    "aap_assets_media_runtime.worker",
                    "--once",
                    "--job-id",
                    str(expired_job["job_id"]),
                ],
                worker_a_environment,
                worker_a_log,
            )
            log_streams.append(stream)
            await _wait_for_file(gate_entered, worker_a, worker_a_log)
            claimed = await _job_for_frame(
                test_engine, item_id, expired_frame_index, 96
            )
            assert claimed["status"] == "running" and claimed["lease_token"]
            stale_lease_token = claimed["lease_token"]
            os.killpg(worker_a.pid, signal.SIGSTOP)
            await asyncio.sleep(0.1)
            async with test_engine.begin() as owner:
                changed = await owner.execute(
                    text(
                        "UPDATE public.asset_media_jobs "
                        "SET lease_expires_at=now()-interval '1 second' "
                        "WHERE job_id=:job AND status='running' AND lease_token=:lease"
                    ),
                    {"job": expired_job["job_id"], "lease": stale_lease_token},
                )
            assert changed.rowcount == 1
            worker_b, stream = _start_process(
                [
                    sys.executable,
                    "-m",
                    "aap_assets_media_runtime.worker",
                    "--once",
                    "--job-id",
                    str(expired_job["job_id"]),
                ],
                worker_environment,
                worker_b_log,
            )
            log_streams.append(stream)
            await _wait_process_exit(
                worker_b,
                timeout=60,
                message="replacement media worker did not finish its reclaimed job",
            )
            assert worker_b.returncode == 0, _process_log(worker_b_log)
            await _wait_job_ready(test_engine, expired_job["job_id"])
            async with test_engine.connect() as owner:
                replacement_key = await owner.scalar(
                    text(
                        "SELECT storage_key FROM public.video_frame_cache "
                        "WHERE dataset_item_id=:item AND frame_index=:frame "
                        "AND width=96 AND format='webp' AND status='ready'"
                    ),
                    {"item": item_id, "frame": expired_frame_index},
                )
            assert replacement_key
            assert f"/a{stale_lease_token}/" not in replacement_key
            gate_release = gate_a[1] / "release"
            gate_release.touch()
            os.killpg(worker_a.pid, signal.SIGCONT)
            await _wait_process_exit(
                worker_a,
                timeout=45,
                message="stale media worker did not exit after its lost lease",
            )
            async with test_engine.connect() as owner:
                assert (
                    await owner.scalar(
                        text(
                            "SELECT storage_key FROM public.video_frame_cache "
                            "WHERE dataset_item_id=:item AND frame_index=:frame "
                            "AND width=96 AND format='webp' AND status='ready'"
                        ),
                        {"item": item_id, "frame": expired_frame_index},
                    )
                    == replacement_key
                )

            # A separately accepted old-source attempt is fenced after the
            # registered raw object changes; only the owner replaces the bytes.
            worker_c_frame_index = 16
            source_change_request = {
                **source_request,
                "selection": {
                    "kind": "frames",
                    "intent": "access",
                    "expected_source_snapshot": source_identity,
                    "frame_indices": [worker_c_frame_index],
                    "width": 96,
                    "format": "webp",
                    "force": False,
                    "failed_only": False,
                },
            }
            accepted_old_source = await _post(
                client,
                control_token,
                "/v1/assets-reference",
                source_change_request,
            )
            assert accepted_old_source.status_code == 200
            worker_c_environment = _runtime_environment(
                tmp_path=tmp_path,
                role="worker",
                database_url=_runtime_database_url(worker_url),
                endpoint=endpoint,
                public_endpoint=endpoint,
                datasets_bucket=datasets_bucket,
                cache_bucket=cache_bucket,
                access_key=worker_access,
                secret_key=worker_secret,
                ffmpeg_gate=gate_c,
            )
            worker_c, stream = _start_process(
                [
                    sys.executable,
                    "-m",
                    "aap_assets_media_runtime.worker",
                    "--once",
                    "--job-id",
                    str(
                        next(
                            row["job_id"]
                            for row in accepted_old_source.json()["data"]["state"][
                                "assets"
                            ]
                            if row["frame_index"] == worker_c_frame_index
                        )
                    ),
                ],
                worker_c_environment,
                worker_c_log,
            )
            log_streams.append(stream)
            await _wait_for_file(gate_c[1] / "entered", worker_c, worker_c_log)
            replacement_bytes = b"media source changed by the owned test fixture"
            owner_s3.put_object(
                Bucket=datasets_bucket,
                Key=source_key,
                Body=replacement_bytes,
                ContentType="application/octet-stream",
            )
            changed_source = await _post(
                client, control_token, "/v1/source-reference", source_request
            )
            assert changed_source.status_code == 409
            assert changed_source.json()["error"]["code"] == "source_changed"
            owner_body = owner_s3.get_object(Bucket=datasets_bucket, Key=source_key)[
                "Body"
            ]
            assert owner_body.read() == replacement_bytes
            owner_body.close()
            gate_c_release = gate_c[1] / "release"
            gate_c_release.touch()
            await _wait_process_exit(
                worker_c,
                timeout=45,
                message="old-source media worker did not exit after the source fence",
            )
            async with test_engine.connect() as owner:
                source_row = (
                    await owner.execute(
                        text(
                            "SELECT media_source_drifted,media_source_generation "
                            "FROM public.dataset_items WHERE id=:item"
                        ),
                        {"item": item_id},
                    )
                ).one()
                cache_row = (
                    await owner.execute(
                        text(
                            "SELECT status,storage_key FROM public.video_frame_cache "
                            "WHERE dataset_item_id=:item AND frame_index=:frame "
                            "AND width=96 AND format='webp'"
                        ),
                        {"item": item_id, "frame": worker_c_frame_index},
                    )
                ).first()
            assert source_row.media_source_drifted is True
            assert source_row.media_source_generation > 1
            assert cache_row is None or cache_row.status != "ready"

    finally:
        for process in (worker_c, worker_b, worker_a, worker_process, control_process):
            _stop_process(process)
        for stream in log_streams:
            stream.close()
        if actor_id is not None and dataset_id is not None:
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
            try:
                await _delete_objects(
                    owner_s3,
                    cache_bucket,
                    [f"media-assets/{job_id}/" for job_id in job_ids],
                )
            finally:
                async with test_engine.begin() as owner:
                    # Retained ledgers leave with the verifier-owned database.
                    await owner.execute(
                        text(
                            "DELETE FROM public.asset_media_timetable_staging "
                            "WHERE job_id IN (SELECT job_id "
                            "FROM public.asset_media_jobs "
                            "WHERE dataset_item_id=:item)"
                        ),
                        {"item": item_id},
                    )
                    await owner.execute(
                        text(
                            "DELETE FROM public.video_frame_cache "
                            "WHERE dataset_item_id=:item"
                        ),
                        {"item": item_id},
                    )
                    await owner.execute(
                        text(
                            "DELETE FROM public.video_chunks "
                            "WHERE dataset_item_id=:item"
                        ),
                        {"item": item_id},
                    )
                    await owner.execute(
                        text(
                            "DELETE FROM public.video_frame_indices "
                            "WHERE dataset_item_id=:item"
                        ),
                        {"item": item_id},
                    )
                    await owner.execute(
                        text(
                            "DELETE FROM public.asset_media_jobs "
                            "WHERE dataset_item_id=:item"
                        ),
                        {"item": item_id},
                    )
                    await owner.execute(
                        text("DELETE FROM public.tasks WHERE dataset_item_id=:item"),
                        {"item": item_id},
                    )
                    await owner.execute(
                        text("DELETE FROM public.projects WHERE id=:project"),
                        {"project": project_id},
                    )
                    await owner.execute(
                        text("DELETE FROM public.datasets WHERE id=:dataset"),
                        {"dataset": dataset_id},
                    )
                    await owner.execute(
                        text(
                            "DELETE FROM public.users WHERE id=:actor "
                            "AND NOT EXISTS (SELECT 1 FROM public.audit_logs "
                            "WHERE actor_id=:actor)"
                        ),
                        {"actor": actor_id},
                    )
        for bucket, key in owned_objects + denied_objects:
            await asyncio.to_thread(owner_s3.delete_object, Bucket=bucket, Key=key)
