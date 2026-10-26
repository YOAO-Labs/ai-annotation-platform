"""Real restricted HTTP/S3/process acceptance for immutable image inputs."""

import asyncio
import base64
from hashlib import sha256
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import sys
import subprocess
import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from sqlalchemy import text

from aap_assets_inputs import contract as c
from aap_assets_inputs.client import InputClient
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import ProjectDataset
from tests.factory import create_project, create_task
from tests.test_assets_catalog_native import _seed_actor
from tests.test_assets_images_live import (
    _guard_role_url,
    _image_live_runtime,
    _seed_item,
    _solid_png,
)
from tests.test_assets_media_live import (
    _free_loopback_port,
    _process_log,
    _runtime_database_url,
    _start_process,
    _stop_process,
    _wait_process_exit,
)
from tests.test_asset_storage_cleanup_runtime import _required_env

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data PostgreSQL and scoped S3 verifier",
    ),
]


class _DropAcceptedResponse(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport(trust_env=False)
        self.dropped = False
        self.status = None

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        self.status = response.status_code
        if (
            request.url.path == "/v1/inputs/accept"
            and not self.dropped
            and response.status_code == 200
        ):
            await response.aread()
            await response.aclose()
            self.dropped = True
            raise httpx.ReadError(
                "injected accepted input response loss", request=request
            )
        return response

    async def aclose(self):
        await self.inner.aclose()


async def _prove_node_consumer(runtime, record, reference):
    root = Path(__file__).resolve().parents[3]
    result = await asyncio.to_thread(
        subprocess.run,
        ["node", str(root / "apps/assets-media/tests/fixtures/inputs-peer.mjs")],
        input=json.dumps(
            {
                "origin": str(runtime.control_client.base_url).rstrip("/"),
                "token": runtime.control_token,
                "input_id": str(record.input_id),
                "job_id": str(record.job_id),
                "task_id": str(record.task_id),
                "actor_id": str(record.actor_id),
                "sha256": record.sha256,
                "byte_size": record.byte_size,
            }
        ),
        text=True,
        capture_output=True,
        timeout=20,
        env={"PATH": os.environ.get("PATH", "")},
        check=False,
    )
    assert result.returncode == 0, (
        "independent Node input consumer rejected the contract"
    )
    assert json.loads(result.stdout) == {
        "schema": c.INPUT_SCHEMA,
        "input_id": str(record.input_id),
        "version_id": reference.version_id,
        "sha256": record.sha256,
        "byte_size": record.byte_size,
    }


async def _prove_later_candidate_adoption(
    runtime, record, project_id, task_id, monkeypatch
):
    from app.config import settings
    from app.db.models.prediction import Prediction, PredictionMeta
    from app.db.models.task import Task
    from app.services.annotation import AnnotationService
    from tests.test_annotation_attribute_provenance import _accept_scope, _bbox_shape

    monkeypatch.setattr(
        settings, "assets_media_url", str(runtime.control_client.base_url).rstrip("/")
    )
    monkeypatch.setattr(settings, "assets_media_token", runtime.control_token)
    async with runtime.owner_sessions.begin() as owner:
        later_actor = await _seed_actor(owner, role="super_admin")
        runtime.track_actor(later_actor)
        prediction = Prediction(
            id=uuid4(),
            task_id=task_id,
            project_id=project_id,
            result=[_bbox_shape("car", {"color": "original-red"})],
            score=0.9,
            tool_unit_id="bbox",
        )
        owner.add(prediction)
        await owner.flush()
        owner.add(
            PredictionMeta(
                prediction_id=prediction.id,
                prediction_created_at=prediction.created_at,
                extra={
                    "accepted_input": {
                        "input_id": str(record.input_id),
                        "job_id": str(record.job_id),
                        "task_id": str(record.task_id),
                        "sha256": record.sha256,
                        "byte_size": record.byte_size,
                    }
                },
            )
        )
        await owner.execute(
            text("UPDATE public.tasks SET version=version+1 WHERE id=:task"),
            {"task": task_id},
        )
        await owner.execute(
            text("UPDATE public.users SET is_active=false WHERE id=:actor"),
            {"actor": record.actor_id},
        )
        await owner.execute(
            text("DELETE FROM public.async_jobs WHERE id=:job"), {"job": record.job_id}
        )

    async with runtime.owner_sessions() as owner:
        current = await owner.get(Task, task_id)
        assert current.version > record.task_version
        annotations = await AnnotationService(owner)._accept_prediction(
            prediction.id, write_scope=_accept_scope(current, later_actor)
        )
        assert annotations and len(annotations) == 1
        annotation_id = annotations[0].id
        assert annotations[0].user_id == later_actor
        await owner.commit()
    async with runtime.owner_sessions.begin() as owner:
        pins = (
            await owner.execute(
                text(
                    "SELECT input_id,sha256 FROM public.annotation_input_sources WHERE annotation_id=:annotation"
                ),
                {"annotation": annotation_id},
            )
        ).all()
        assert pins == [(record.input_id, record.sha256)]
        await owner.execute(
            text("DELETE FROM public.prediction_metas WHERE prediction_id=:prediction"),
            {"prediction": prediction.id},
        )
        await owner.execute(
            text("DELETE FROM public.predictions WHERE id=:prediction"),
            {"prediction": prediction.id},
        )


async def _prove_cleanup_rescans_late_versions(runtime, input_id, tmp_path):
    cleanup_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_CLEANUP_URL", "asset_cleanup_client", runtime.test_engine
    )
    async with runtime.test_engine.connect() as owner:
        row = (
            await owner.execute(
                text(
                    "SELECT garbage.garbage_id, attempt.prefix FROM public.asset_accepted_image_input_attempts attempt "
                    "JOIN public.asset_accepted_input_garbage garbage USING (prefix) WHERE attempt.input_id=:input"
                ),
                {"input": input_id},
            )
        ).one()
    garbage_id, prefix = row
    assert prefix.startswith(f"accepted-inputs/{input_id}/")
    bucket = runtime.resources["buckets"]["MINIO_INPUTS_BUCKET"]
    key = prefix + "source"
    sibling = c.attempt_prefix(uuid4(), 1, uuid4()) + "source"
    for value in (b"abandoned-one", b"abandoned-two"):
        await asyncio.to_thread(
            runtime.owner_s3.put_object, Bucket=bucket, Key=key, Body=value
        )
    await asyncio.to_thread(
        runtime.owner_s3.create_multipart_upload, Bucket=bucket, Key=key
    )
    retained = await asyncio.to_thread(
        runtime.owner_s3.put_object, Bucket=bucket, Key=sibling, Body=b"retain sibling"
    )
    root = Path(__file__).resolve().parents[3]
    port = _free_loopback_port()
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join(
            (str(root / "apps/assets-cleanup/src"), str(root / "apps/assets-media/src"))
        ),
        "ASSET_CLEANUP_DATABASE_URL": _runtime_database_url(cleanup_url),
        "ASSET_CLEANUP_S3_ENDPOINT": runtime.endpoint,
        "ASSET_CLEANUP_BUCKET": runtime.cache_bucket,
        "ASSET_CLEANUP_INPUTS_BUCKET": bucket,
        "ASSET_CLEANUP_S3_ACCESS_KEY": _required_env(
            "AAP_MODULE_DATA_ASSET_CLEANUP_S3_ACCESS_KEY"
        ),
        "ASSET_CLEANUP_S3_SECRET_KEY": _required_env(
            "AAP_MODULE_DATA_ASSET_CLEANUP_S3_SECRET_KEY"
        ),
        "ASSET_CLEANUP_POLL_SECONDS": "0.1",
        "ASSET_CLEANUP_HEALTH_PORT": str(port),
    }
    log = tmp_path / "input-cleanup.log"
    process, stream = _start_process(
        [sys.executable, "-m", "aap_assets_cleanup"], environment, log
    )
    runtime.processes.append(process)
    runtime.streams.append(stream)

    async def wait_ack(previous=None):
        deadline = time.monotonic() + 30
        async with httpx.AsyncClient(trust_env=False, timeout=2) as health:
            while time.monotonic() < deadline:
                assert process.poll() is None, _process_log(log)
                async with runtime.test_engine.connect() as owner:
                    token = await owner.scalar(
                        text(
                            "SELECT last_ack_token FROM public.asset_accepted_input_garbage WHERE garbage_id=:garbage"
                        ),
                        {"garbage": garbage_id},
                    )
                if token is not None and token != previous:
                    status = await health.get(f"http://127.0.0.1:{port}/health/ready")
                    assert status.status_code == 200 and status.json()["ready"]
                    return token
                await asyncio.sleep(0.1)
        pytest.fail(
            "input cleanup did not acknowledge its exact prefix: " + _process_log(log)
        )

    try:
        first = await wait_ack()
        values = await asyncio.to_thread(
            runtime.owner_s3.list_object_versions, Bucket=bucket, Prefix=prefix
        )
        assert not values.get("Versions") and not values.get("DeleteMarkers")
        uploads = await asyncio.to_thread(
            runtime.owner_s3.list_multipart_uploads, Bucket=bucket, Prefix=prefix
        )
        assert not uploads.get("Uploads")
        await asyncio.to_thread(
            runtime.owner_s3.put_object,
            Bucket=bucket,
            Key=key,
            Body=b"late stale writer",
        )
        async with runtime.test_engine.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE public.asset_accepted_input_garbage SET not_before=clock_timestamp()-interval '1 second' WHERE garbage_id=:garbage"
                ),
                {"garbage": garbage_id},
            )
        assert await wait_ack(first) != first
        values = await asyncio.to_thread(
            runtime.owner_s3.list_object_versions, Bucket=bucket, Prefix=prefix
        )
        assert not values.get("Versions") and not values.get("DeleteMarkers")
        sibling_value = await asyncio.to_thread(
            runtime.owner_s3.head_object,
            Bucket=bucket,
            Key=sibling,
            VersionId=retained["VersionId"],
        )
        assert sibling_value["VersionId"] == retained["VersionId"]
    finally:
        await asyncio.to_thread(_stop_process, process)
    assert process.returncode == 0, _process_log(log)


@pytest.mark.parametrize("replace_source", [False, True])
async def test_replayed_input_materializes_in_restricted_process_and_binds_one_version(
    test_engine, tmp_path, caplog, replace_source, monkeypatch
):
    caplog.set_level(logging.WARNING, logger="httpx")
    async with _image_live_runtime(test_engine, tmp_path) as runtime:
        body = _solid_png((128, 96), (240, 20, 30))
        source_key = f"image-source/{uuid4()}/input.png"
        async with runtime.owner_sessions.begin() as owner:
            actor_id = await _seed_actor(owner, role="super_admin")
        runtime.track_actor(actor_id)
        dataset_id, item_id = await _seed_item(
            runtime,
            actor_id=actor_id,
            source_key=source_key,
            source_bytes=body,
            width=128,
            height=96,
        )
        job_id = uuid4()
        async with runtime.owner_sessions.begin() as owner:
            project = await create_project(owner, owner_id=actor_id)
            runtime.project_ids.append(project.id)
            owner.add(ProjectDataset(project_id=project.id, dataset_id=dataset_id))
            task = await create_task(owner, project_id=project.id)
            task.dataset_item_id = item_id
            runtime.task_ids.append(task.id)
            task_ids = [str(task.id)]
            owner.add(
                AsyncJob(
                    id=job_id,
                    kind="batch_predict",
                    project_id=project.id,
                    user_id=actor_id,
                    status="pending",
                    celery_task_id=str(job_id),
                    payload={
                        "execution_scope": "workbench",
                        "task_ids": task_ids,
                        "scope": {"task_ids": task_ids},
                    },
                    result={},
                )
            )
        request = c.FindInputRequest(job_id=job_id, task_id=task.id, actor_id=actor_id)
        transport = _DropAcceptedResponse()
        try:
            async with httpx.AsyncClient(
                transport=transport, timeout=10, trust_env=False
            ) as http:
                client = InputClient(
                    str(runtime.control_client.base_url),
                    runtime.control_token,
                    client=http,
                )
                assert (await client.find(request)).input is None
                try:
                    await client.accept(request)
                except c.InputFailure as failure:
                    assert failure.code == "storage_unavailable", _process_log(
                        runtime.control_log
                    )
                else:
                    pytest.fail("expected the injected accepted response loss")
                assert transport.dropped, (
                    f"accept returned HTTP {transport.status} before injection"
                )
                if replace_source:
                    await asyncio.to_thread(
                        runtime.owner_s3.put_object,
                        Bucket=runtime.datasets_bucket,
                        Key=source_key,
                        Body=b"replacement source",
                    )
                accepted = await client.accept(request)
                assert accepted == await client.accept(request)
                assert accepted.status == "pending"
                identity = c.InputRequest(
                    input_id=accepted.input_id, job_id=job_id, task_id=task.id
                )
                with pytest.raises(c.InputFailure, match="input_not_ready"):
                    await client.reference(identity)
                process, log = runtime.start_worker(
                    "input-materializer", input_id=accepted.input_id
                )
                code = await _wait_process_exit(
                    process, timeout=45, message="input materialization did not finish"
                )
                assert code == 0, _process_log(log)
                record = await client.record(identity)
                if replace_source:
                    assert (
                        record.status == "failed" and record.error == "source_changed"
                    )
                    with pytest.raises(c.InputFailure, match="source_changed"):
                        await client.reference(identity)
                    await _prove_cleanup_rescans_late_versions(
                        runtime, accepted.input_id, tmp_path
                    )
                    return

                assert record.status == "ready"
                assert record.sha256 == sha256(body).hexdigest()
                assert record.byte_size == len(body)
                reference = await client.reference(identity)
                assert reference.input.input_id == accepted.input_id
                assert (await client.verify_source(identity)).sha256 == record.sha256
                await client.download(reference, tmp_path / "accepted-original.png")
                assert (tmp_path / "accepted-original.png").read_bytes() == body
                bucket = runtime.resources["buckets"]["MINIO_INPUTS_BUCKET"]
                path = unquote(urlsplit(reference.url).path)
                assert path.startswith(
                    f"/{bucket}/accepted-inputs/{accepted.input_id}/"
                )
                key = path.removeprefix(f"/{bucket}/")
                changed = await asyncio.to_thread(
                    runtime.owner_s3.put_object,
                    Bucket=bucket,
                    Key=key,
                    Body=b"different latest bytes",
                )
                assert changed["VersionId"] != reference.version_id
                replay_reference = await client.reference(identity)
                assert replay_reference.version_id == reference.version_id
                await client.download(
                    replay_reference, tmp_path / "accepted-after-overwrite.png"
                )
                assert (tmp_path / "accepted-after-overwrite.png").read_bytes() == body
                await _prove_node_consumer(runtime, record, replay_reference)
                await _prove_later_candidate_adoption(
                    runtime, record, project.id, task.id, monkeypatch
                )
                assert (
                    await client.reference(identity)
                ).version_id == reference.version_id
                from app.application.preannotation_execution import _run_task_pipeline
                from tests.inference_execution_test_ports import (
                    FakeModelExecutionPort,
                )
                from tests.test_staged_pipeline_worker import _bbox

                root_hashes = []
                crop_pixels = []

                async def detect(_operation, task_input):
                    async with httpx.AsyncClient(trust_env=False) as bare:
                        result = await bare.get(task_input["file_path"])
                    assert result.status_code == 200
                    root_hashes.append(sha256(result.content).hexdigest())
                    return [_bbox(0, 0, 100, 100)]

                async def classify(_operation, task_input):
                    raw = base64.b64decode(task_input["file_path"].split(",", 1)[1])
                    with Image.open(BytesIO(raw)) as crop:
                        assert crop.size == (128, 96)
                        crop_pixels.append(crop.getpixel((20, 20)))
                    return [{"attributes": {"color": "original-red"}}]

                def no_task_reload(_task):
                    pytest.fail("frozen ROI must not reload a mutable Task image")

                monkeypatch.setattr(
                    "app.services.prediction_context.load_task_image", no_task_reload
                )
                # Even a later overwrite of the original source cannot change
                # either model's input. The final source check will reject writes.
                await asyncio.to_thread(
                    runtime.owner_s3.put_object,
                    Bucket=runtime.datasets_bucket,
                    Key=source_key,
                    Body=_solid_png((128, 96), (10, 20, 240)),
                )
                result = await _run_task_pipeline(
                    task,
                    [
                        {"stage": 3, "parent_stage": None},
                        {
                            "stage": 11,
                            "parent_stage": 3,
                            "roi": {"pad": 0},
                            "write": {"target": "attributes", "keys": ["color"]},
                        },
                    ],
                    [
                        FakeModelExecutionPort(detect),
                        FakeModelExecutionPort(classify),
                    ],
                    [None, None],
                    resolve_url=lambda _task: replay_reference.url,
                    input_image_path=tmp_path / "accepted-after-overwrite.png",
                    project_id=project.id,
                    owner_prefix="accepted-input-live",
                    route_operation="batch_predict",
                )
                assert root_hashes == [record.sha256]
                assert crop_pixels and all(
                    abs(actual - expected) <= 2
                    for actual, expected in zip(
                        crop_pixels[0], (240, 20, 30), strict=True
                    )
                )
                assert (
                    result.results[0].result[0]["attributes"]["color"] == "original-red"
                )
                assert result.stage_stats[11]["ok"] == 1
                with pytest.raises(c.InputFailure, match="source_changed"):
                    await client.verify_source(identity)
                await asyncio.to_thread(
                    runtime.owner_s3.delete_object,
                    Bucket=bucket,
                    Key=key,
                    VersionId=reference.version_id,
                )
                with pytest.raises(c.InputFailure, match="input_corrupt"):
                    await client.reference(identity)
                with pytest.raises(c.InputFailure, match="input_corrupt"):
                    await client.download(
                        reference, tmp_path / "must-not-use-latest.png"
                    )
                assert not (tmp_path / "must-not-use-latest.png").exists()
        finally:
            # Receipts/attempts are immutable provenance; the verifier destroys
            # their owned database and versioned bucket after the qualification.
            async with runtime.owner_sessions.begin() as owner:
                await owner.execute(
                    text("DELETE FROM public.annotations WHERE task_id=:task"),
                    {"task": task.id},
                )
                await owner.execute(
                    text(
                        "DELETE FROM public.prediction_metas WHERE prediction_id IN (SELECT id FROM public.predictions WHERE task_id=:task)"
                    ),
                    {"task": task.id},
                )
                await owner.execute(
                    text("DELETE FROM public.predictions WHERE task_id=:task"),
                    {"task": task.id},
                )
                await owner.execute(
                    text("DELETE FROM public.async_jobs WHERE id=:job"), {"job": job_id}
                )
