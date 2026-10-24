"""Owned-process/S3 acceptance for retained originals and exact decoded frames."""

from contextlib import asynccontextmanager
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncio
import httpx
from PIL import Image
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs.client import VideoInputClient
from tests.test_asset_storage_cleanup_runtime import (
    _expect_s3_denied,
    _local_s3_client,
    _required_env,
    _verified_resource_manifest,
    _verify_bucket_owner,
    _verify_postgres,
)
from tests.test_assets_media_live import (
    _free_loopback_port,
    _guard_role_url,
    _process_log,
    _runtime_database_url,
    _runtime_environment,
    _start_process,
    _stop_process,
    _wait_control_ready,
    _wait_process_exit,
)
from tests.test_video_input_authorities_native import _seed_video_job, _seed_video_task
from tests.test_video_input_ledger_control import _call
from tests.test_video_input_selection_native import _selection

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned role/S3/process qualification",
    ),
]


@asynccontextmanager
async def _video_runtime(test_engine, tmp_path, *, source_test_engine=None):
    from app.config import settings

    source_url = source_test_engine.url if source_test_engine is not None else None
    if source_url is not None and not (
        source_url.drivername == test_engine.url.drivername
        and source_url.username == test_engine.url.username == "owner"
        and source_url.host == test_engine.url.host == "127.0.0.1"
        and source_url.port == test_engine.url.port
        and source_url.database != test_engine.url.database
    ):
        pytest.fail(
            "source and restored video DBs do not share the owned PostgreSQL target"
        )
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
    if source_test_engine is None:
        resources = _verified_resource_manifest(test_engine, control_url)
    else:
        source_control_url = control_url.set(database=source_url.database)
        _verify_postgres(source_control_url)
        resources = _verified_resource_manifest(source_test_engine, source_control_url)
        if (
            re.fullmatch(
                r"aap_identity_module_restore_[0-9a-f]{12}_test",
                test_engine.url.database or "",
            )
            is None
        ):
            pytest.fail("restored video DB is not an identity-module restore clone")
        async with test_engine.connect() as identity_db:
            clone_identity = (
                await identity_db.execute(
                    text(
                        "SELECT current_database(), current_user, "
                        "pg_catalog.pg_get_userbyid(d.datdba), "
                        "pg_catalog.shobj_description(d.oid, 'pg_database') "
                        "FROM pg_catalog.pg_database d "
                        "WHERE d.datname = current_database()"
                    )
                )
            ).one()
        database_name, current_user, database_owner, marker = clone_identity
        marker_prefix = "identity-module-restore:"
        if not (
            database_name == test_engine.url.database
            and current_user == "owner"
            and database_owner == "owner"
            and isinstance(marker, str)
            and marker.startswith(marker_prefix)
        ):
            pytest.fail("restored video DB owner or restore marker did not match")
        try:
            marker_id = UUID(marker.removeprefix(marker_prefix))
        except ValueError:
            pytest.fail("restored video DB marker did not contain a UUID")
        if marker_id.version != 4 or str(marker_id) != marker.removeprefix(
            marker_prefix
        ):
            pytest.fail("restored video DB marker UUID was not canonical")
    buckets = resources["buckets"]
    endpoint = (
        ("https" if settings.minio_use_ssl else "http")
        + "://"
        + settings.minio_endpoint
    )
    owner = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    for bucket in (
        buckets["MINIO_DATASETS_BUCKET"],
        buckets["MINIO_MEDIA_CACHE_BUCKET"],
        buckets["MINIO_INPUTS_BUCKET"],
    ):
        _verify_bucket_owner(owner, bucket, resources["owner"])
    access = _required_env("AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_ACCESS_KEY")
    secret = _required_env("AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_SECRET_KEY")
    worker_access = _required_env("AAP_MODULE_DATA_ASSET_MEDIA_WORKER_S3_ACCESS_KEY")
    worker_secret = _required_env("AAP_MODULE_DATA_ASSET_MEDIA_WORKER_S3_SECRET_KEY")
    assert access != worker_access and secret != worker_secret
    token, port = secrets.token_urlsafe(32), _free_loopback_port()
    environment = _runtime_environment(
        tmp_path=tmp_path,
        role="control",
        database_url=_runtime_database_url(control_url),
        endpoint=endpoint,
        public_endpoint=endpoint,
        datasets_bucket=buckets["MINIO_DATASETS_BUCKET"],
        cache_bucket=buckets["MINIO_MEDIA_CACHE_BUCKET"],
        legacy_bucket=buckets["MINIO_BUCKET"],
        access_key=access,
        secret_key=secret,
        token=token,
        port=port,
    )
    log = tmp_path / "video-input-control.log"
    process, stream = _start_process(
        [sys.executable, "-m", "aap_assets_media_runtime"], environment, log
    )
    origin = f"http://127.0.0.1:{port}"
    http = httpx.AsyncClient(base_url=origin, timeout=15, trust_env=False)
    client = VideoInputClient(origin, token, client=http)
    runtime = SimpleNamespace(
        owner=owner,
        sessions=async_sessionmaker(test_engine, expire_on_commit=False),
        buckets=buckets,
        endpoint=endpoint,
        token=token,
        origin=origin,
        http=http,
        client=client,
        worker_url=worker_url,
        worker_access=worker_access,
        worker_secret=worker_secret,
        worker_s3=_local_s3_client(settings, worker_access, worker_secret),
        control_s3=_local_s3_client(settings, access, secret),
        processes=[process],
        streams=[stream],
    )
    try:
        await _wait_control_ready(http, process, log)
        yield runtime
    finally:
        for item in reversed(runtime.processes):
            await asyncio.to_thread(_stop_process, item)
        await http.aclose()
        for item in runtime.streams:
            item.close()
        for item in (owner, runtime.worker_s3, runtime.control_s3):
            item.close()


async def _run_video_worker(runtime, tmp_path, *, input_id=None, frame_id=None):
    environment = _runtime_environment(
        tmp_path=tmp_path,
        role="worker",
        database_url=_runtime_database_url(runtime.worker_url),
        endpoint=runtime.endpoint,
        public_endpoint=runtime.endpoint,
        datasets_bucket=runtime.buckets["MINIO_DATASETS_BUCKET"],
        cache_bucket=runtime.buckets["MINIO_MEDIA_CACHE_BUCKET"],
        access_key=runtime.worker_access,
        secret_key=runtime.worker_secret,
    )
    assert (input_id is None) != (frame_id is None)
    selector, value = ("--input-id", input_id) if input_id else ("--frame-id", frame_id)
    log = tmp_path / ("original-worker.log" if input_id else "frame-worker.log")
    process, stream = _start_process(
        [
            sys.executable,
            "-m",
            "aap_assets_media_runtime.worker",
            "--once",
            selector,
            str(value),
        ],
        environment,
        log,
    )
    runtime.processes.append(process)
    runtime.streams.append(stream)
    await _wait_process_exit(
        process,
        timeout=45,
        message="video input worker did not finish its selected unit",
    )
    assert process.returncode == 0, _process_log(log)


def _video_bytes(tmp_path):
    path = tmp_path / "original.mp4"
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=80x48:r=6",
            "-frames:v",
            "6",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "1",
            str(path),
        ],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, "could not create owned video fixture"
    return path.read_bytes()


async def test_real_video_input_control_worker_versions_and_independent_node_peer(
    test_engine, tmp_path
):
    async with _video_runtime(test_engine, tmp_path) as runtime:
        body = await asyncio.to_thread(_video_bytes, tmp_path)
        key = f"video-input-qualification/{uuid4()}.mp4"
        runtime.owner.put_object(
            Bucket=runtime.buckets["MINIO_DATASETS_BUCKET"], Key=key, Body=body
        )
        async with runtime.sessions.begin() as owner:
            actor, project, _, item, task = await _seed_video_task(owner)
            await owner.execute(
                text(
                    "UPDATE public.dataset_items SET file_path=:path,file_size=:size,content_hash=:sha WHERE id=:item"
                ),
                {
                    "path": key,
                    "size": len(body),
                    "sha": sha256(body).hexdigest(),
                    "item": item,
                },
            )
            selection = _selection(task_ids=[task])
            job = await _seed_video_job(
                owner, actor_id=actor, project_id=project.id, selection=selection
            )
            header = await _call(
                owner,
                "capture_accepted_video_manifest",
                c.CaptureVideoManifestRequest(
                    job_id=job,
                    actor_id=actor,
                    project_id=project.id,
                    selection=selection,
                ),
            )
            await owner.execute(
                text(
                    "UPDATE public.async_jobs SET payload=payload || pg_catalog.jsonb_build_object("
                    "'accepted_video_selection_digest',CAST(:digest AS text)) WHERE id=:job"
                ),
                {"digest": header.selection_digest, "job": job},
            )
        prepared = await runtime.client.prepare(
            c.PrepareVideoMemberRequest(
                job_id=job,
                actor_id=actor,
                project_id=project.id,
                selection_digest=header.selection_digest,
                ordinal=1,
                task_id=task,
            )
        )
        assert prepared.member.state == "preparing"
        input_id = prepared.member.input.input_id
        await _run_video_worker(runtime, tmp_path, input_id=input_id)
        request = c.ReadVideoInputRequest(
            input_id=input_id, job_id=job, task_id=task, actor_id=actor
        )
        record = await runtime.client.input(request)
        assert (
            record.status == "ready"
            and record.plan.input.blob.sha256 == sha256(body).hexdigest()
        )
        assert (
            record.plan.input.video.frame_count == 6 and record.plan.selected_count == 6
        )
        await runtime.client.seal(
            c.SealVideoManifestRequest(
                job_id=job,
                actor_id=actor,
                project_id=project.id,
                selection_digest=header.selection_digest,
            )
        )
        frame_request = c.VideoFrameRequest(
            actor_id=actor,
            job_id=job,
            task_id=task,
            input_id=input_id,
            plan_digest=record.plan.plan_digest,
            ordinal=1,
        )
        pending = await runtime.client.require_frame(frame_request)
        assert pending.status == "pending"
        await _run_video_worker(runtime, tmp_path, frame_id=pending.frame_id)
        frame = await runtime.client.frame(frame_request)
        assert frame.status == "ready" and frame.blob is not None
        reference = await runtime.client.reference(frame_request)
        response = await runtime.http.get(reference.url)
        assert (
            response.status_code == 200
            and sha256(response.content).hexdigest() == frame.blob.sha256
        )
        with Image.open(BytesIO(response.content)) as image:
            assert image.size == (80, 48)
            red, green, blue = image.convert("RGB").getpixel((40, 24))
            assert red > 240 and green < 20 and blue < 20
        async with runtime.sessions.begin() as owner:
            await owner.execute(
                text("UPDATE public.async_jobs SET status='completed' WHERE id=:job"),
                {"job": job},
            )
        peer = await asyncio.to_thread(
            subprocess.run,
            [
                "node",
                str(
                    ROOT / "apps/assets-media/tests/fixtures/video-input-http-peer.mjs"
                ),
            ],
            input=json.dumps(
                {
                    "origin": runtime.origin,
                    "token": runtime.token,
                    "input_id": str(input_id),
                    "job_id": str(job),
                    "task_id": str(task),
                    "actor_id": str(actor),
                    "project_id": str(project.id),
                    "plan_digest": record.plan.plan_digest,
                }
            ),
            text=True,
            capture_output=True,
            timeout=40,
            check=False,
            env={"PATH": os.environ.get("PATH", "")},
        )
        assert peer.returncode == 0, (
            "independent video HTTP consumer rejected the contract"
        )
        result = json.loads(peer.stdout)
        assert (
            result["operations"] == 10
            and result["frame_sha256"] == frame.blob.sha256
            and result["version_id"] == frame.blob.version_id
        )
        accepted_bucket = runtime.buckets["MINIO_INPUTS_BUCKET"]
        _expect_s3_denied(
            lambda: runtime.worker_s3.put_object(
                Bucket=accepted_bucket,
                Key=f"accepted-inputs/{uuid4()}/g1/a{uuid4()}/source",
                Body=b"forbidden",
            ),
        )
        _expect_s3_denied(
            lambda: runtime.worker_s3.delete_object(
                Bucket=accepted_bucket,
                Key=frame.blob.key,
                VersionId=frame.blob.version_id,
            ),
        )
        _expect_s3_denied(
            lambda: runtime.control_s3.get_object(
                Bucket=accepted_bucket, Key=frame.blob.key
            ),
        )
        runtime.owner.put_object(
            Bucket=accepted_bucket, Key=frame.blob.key, Body=b"new latest version"
        )
        unchanged = await runtime.client.reference(frame_request)
        assert unchanged.blob == frame.blob
        assert (await runtime.http.get(unchanged.url)).content == response.content
        runtime.owner.delete_object(
            Bucket=accepted_bucket, Key=frame.blob.key, VersionId=frame.blob.version_id
        )
        with pytest.raises(c.VideoInputFailure, match="input_corrupt"):
            await runtime.client.reference(frame_request)
        runtime.owner.put_object(
            Bucket=runtime.buckets["MINIO_DATASETS_BUCKET"],
            Key=key,
            Body=b"replacement source",
        )
        with pytest.raises(c.VideoInputFailure, match="source_changed"):
            await runtime.client.verify(
                c.LockVideoInputRequest(
                    **request.model_dump(), plan_digest=record.plan.plan_digest
                )
            )
        await _prove_video_cleanup_process(
            runtime, test_engine, tmp_path, request, record.plan, frame
        )


async def _prove_video_cleanup_process(
    runtime, test_engine, tmp_path, request, plan, frame
):
    cleanup_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_CLEANUP_URL", "asset_cleanup_client", test_engine
    )
    inputs_bucket = runtime.buckets["MINIO_INPUTS_BUCKET"]
    frame_prefix = frame.blob.key.split("frames/", 1)[0]
    multipart = runtime.owner.create_multipart_upload(
        Bucket=inputs_bucket, Key=frame_prefix + "abandoned"
    )
    runtime.owner.upload_part(
        Bucket=inputs_bucket,
        Key=frame_prefix + "abandoned",
        UploadId=multipart["UploadId"],
        PartNumber=1,
        Body=b"incomplete",
    )
    foreign_key = f"accepted-video-inputs/{uuid4()}/g1/a{uuid4()}/source"
    foreign = runtime.owner.create_multipart_upload(
        Bucket=inputs_bucket, Key=foreign_key
    )
    runtime.owner.upload_part(
        Bucket=inputs_bucket,
        Key=foreign_key,
        UploadId=foreign["UploadId"],
        PartNumber=1,
        Body=b"must survive",
    )
    async with runtime.sessions.begin() as owner:
        await owner.execute(
            text(
                "UPDATE public.asset_accepted_video_inputs SET retire_after=pg_catalog.clock_timestamp()-interval '1 second' "
                "WHERE input_id=:input"
            ),
            {"input": plan.input.input_id},
        )
        swept = await _call(
            owner, "sweep_accepted_video_inputs", c.SweepVideoInputsRequest()
        )
        assert swept.expired == 1
        rows = (
            await owner.execute(
                text(
                    "SELECT garbage_id,prefix,not_before > pg_catalog.clock_timestamp()+interval '23 hours' "
                    "FROM public.asset_accepted_input_garbage WHERE prefix LIKE :prefix"
                ),
                {"prefix": f"accepted-video-inputs/{plan.input.input_id}/%"},
            )
        ).all()
        assert len(rows) == 2 and all(row[2] for row in rows)
        await owner.execute(
            text(
                "UPDATE public.asset_accepted_input_garbage SET not_before=pg_catalog.clock_timestamp()-interval '1 second' "
                "WHERE garbage_id=ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": [row[0] for row in rows]},
        )
    environment = {
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": str(tmp_path),
        "PYTHONPATH": os.pathsep.join(
            (str(ROOT / "apps/assets-cleanup/src"), str(ROOT / "apps/assets-media/src"))
        ),
        "ASSET_CLEANUP_DATABASE_URL": _runtime_database_url(cleanup_url),
        "ASSET_CLEANUP_S3_ENDPOINT": runtime.endpoint,
        "ASSET_CLEANUP_S3_REGION": "us-east-1",
        "ASSET_CLEANUP_BUCKET": runtime.buckets["MINIO_MEDIA_CACHE_BUCKET"],
        "ASSET_CLEANUP_INPUTS_BUCKET": inputs_bucket,
        "ASSET_CLEANUP_S3_ACCESS_KEY": _required_env(
            "AAP_MODULE_DATA_ASSET_CLEANUP_S3_ACCESS_KEY"
        ),
        "ASSET_CLEANUP_S3_SECRET_KEY": _required_env(
            "AAP_MODULE_DATA_ASSET_CLEANUP_S3_SECRET_KEY"
        ),
        "ASSET_CLEANUP_POLL_SECONDS": "0.1",
        "ASSET_CLEANUP_HEALTH_HOST": "127.0.0.1",
        "ASSET_CLEANUP_HEALTH_PORT": str(_free_loopback_port()),
    }
    log = tmp_path / "video-input-cleanup.log"
    process, stream = _start_process(
        [sys.executable, "-m", "aap_assets_cleanup"], environment, log
    )
    runtime.processes.append(process)
    runtime.streams.append(stream)

    async def wait_ack(previous=None):
        from time import monotonic

        deadline = monotonic() + 25
        while monotonic() < deadline:
            async with runtime.sessions() as owner:
                current = (
                    await owner.execute(
                        text(
                            "SELECT prefix,last_ack_token,lease_token,completed_at FROM public.asset_accepted_input_garbage "
                            "WHERE garbage_id=ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": [row[0] for row in rows]},
                    )
                ).all()
            if len(current) == 2 and all(
                row[1] and row[2] is None and row[3] for row in current
            ):
                if previous is None or any(
                    row[0] == frame_prefix and row[1] != previous for row in current
                ):
                    return {row[0]: row[1] for row in current}
            assert process.poll() is None, _process_log(log)
            await asyncio.sleep(0.1)
        pytest.fail("video cleanup did not acknowledge its exact attempt prefixes")

    first_ack = await wait_ack()
    assert not runtime.owner.list_object_versions(
        Bucket=inputs_bucket, Prefix=frame_prefix
    ).get("Versions")
    uploads = runtime.owner.list_multipart_uploads(Bucket=inputs_bucket).get(
        "Uploads", []
    )
    assert any(row["UploadId"] == foreign["UploadId"] for row in uploads)
    assert not any(row["UploadId"] == multipart["UploadId"] for row in uploads)
    runtime.owner.put_object(
        Bucket=inputs_bucket, Key=frame.blob.key, Body=b"late writer version"
    )
    async with runtime.sessions.begin() as owner:
        await owner.execute(
            text(
                "UPDATE public.asset_accepted_input_garbage SET not_before=pg_catalog.clock_timestamp()-interval '1 second' "
                "WHERE prefix=:prefix"
            ),
            {"prefix": frame_prefix},
        )
    await wait_ack(first_ack[frame_prefix])
    assert not runtime.owner.list_object_versions(
        Bucket=inputs_bucket, Prefix=frame_prefix
    ).get("Versions")
    expired = await runtime.client.input(request)
    assert expired.status == "expired" and expired.plan == plan
    with pytest.raises(c.VideoInputFailure, match="input_expired"):
        await runtime.client.reference(
            c.VideoFrameRequest(
                actor_id=request.actor_id,
                job_id=request.job_id,
                task_id=request.task_id,
                input_id=request.input_id,
                plan_digest=plan.plan_digest,
                ordinal=1,
            )
        )
    await asyncio.to_thread(_stop_process, process)
    runtime.owner.abort_multipart_upload(
        Bucket=inputs_bucket, Key=foreign_key, UploadId=foreign["UploadId"]
    )
