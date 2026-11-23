"""Real public admission, installed worker and scoped export expiry.

No deadline is shortened by mutation; uncertain workers and ownership evidence
remain available for recovery if any physical boundary fails.
The ordinary neighbor keeps its 168-hour birth deadline and delivery through
private fixture retirement; the test does not accelerate ordinary expiration.
"""

import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import uuid
from contextlib import closing
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import httpx
import pytest
from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.export_output import ExportOutput
from app.workers.celery_app import celery_app
from botocore.exceptions import ClientError
from celery import Celery
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.identity_e2e_support import e2e_consumer as e2e_consumer
from tests.identity_e2e_support import fenced_http as fenced_http
from tests.identity_e2e_support import fixture_routing_owner
from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.seed_owned_scenarios import _build_owned, _cleanup_owned
from tests.seed_owner_support import (
    OwnedSuite,
    _fixture_versions,
    _read_versions,
    fixture_original_reader,
)
from tests.test_asset_storage_cleanup_runtime import _verify_bucket_owner
from tests.test_fixture_inference_retirement_live import (
    _PreservedSeedOwner,
    _login,
    _start_image_worker,
    _start_media_runtime,
    _stop_media_runtime,
)
from tests.test_workspace_export_worker_live import (
    _SOURCE_PNG,
    _TASK_NAME,
    _TASK_OPTS,
    _job_snapshot,
    _seed_export_world,
    _verify_export_source,
)
from tests.test_workspace_worker_assembly_live import (
    _delete_owned_queue,
    _wait_broker_ack,
    _wait_worker_ready,
)
from tests.workspace_worker_assembly_helpers import WorkspaceCeleryWorker

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_module_data_boundary import (  # noqa: E402
    _cleanup_mc,
    _cleanup_storage_scope,
    _destroy_cleanup_storage_scope,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_FIXTURE_EXPORT_WORKER_LIVE") != "1",
        reason="requires the explicitly enabled installed-worker and real-deadline verifier",
    ),
]
_RETAINED_UNCERTAIN = []


def _export_writer(resources, store, ordinary):
    """Export writes and one ordinary source read; no private-source mutation."""
    scope = _cleanup_storage_scope(
        SimpleNamespace(settings=settings, resources=resources, s3=store)
    )
    scope["policy"] = "fixture-export-writer-" + secrets.token_hex(8)
    bucket = resources["buckets"]["MINIO_EXPORT_BUCKET"]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketVersioning"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:DeleteObject",
                    "s3:AbortMultipartUpload",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Resource": [
                    f"arn:aws:s3:::{resources['buckets']['MINIO_DATASETS_BUCKET']}"
                    f"/{ordinary['source_key']}"
                ],
            },
        ],
    }
    try:
        _cleanup_mc(
            scope,
            [
                "admin",
                "user",
                "add",
                "scoped",
                scope["access_key"],
                scope["secret_key"],
            ],
        )
        scope["user_created"] = True
        _cleanup_mc(
            scope,
            ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
            payload=json.dumps(policy),
        )
        scope["policy_created"] = True
        _cleanup_mc(
            scope,
            [
                "admin",
                "policy",
                "attach",
                "scoped",
                scope["policy"],
                "--user",
                scope["access_key"],
            ],
        )
        return scope
    except BaseException:
        _destroy_cleanup_storage_scope(scope)
        raise


async def _wait_export_delivery(engine, sender, worker, job_id, deliveries):
    async with asyncio.timeout(180):
        while True:
            status = await _job_snapshot(engine, job_id)
            if status["status"] in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.25)
    assert status["status"] == "completed", worker.redact(json.dumps(status))
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        job = await db.get(AsyncJob, job_id)
        delivery, payload = job.celery_task_id, dict(job.payload)
    assert delivery
    deliveries.append(delivery)
    return payload, await _wait_broker_ack(sender, worker, delivery)


async def _export_snapshot(engine, job_id):
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        job = await db.get(AsyncJob, job_id)
        output = (
            await db.scalars(select(ExportOutput).where(ExportOutput.job_id == job_id))
        ).one()
        bundle = (
            await db.execute(
                text(
                    "SELECT created_at,expires_at,fixture_run_id FROM public.export_media_bundles "
                    "WHERE id=:bundle AND job_id=:job"
                ),
                {"bundle": output.media_bundle_id, "job": job_id},
            )
        ).one()
        assert job.status == "completed"
        assert output.state == "ready"
        assert output.expires_at == bundle.expires_at
        return {
            "id": output.id,
            "format": output.format,
            "fixture_run_id": bundle.fixture_run_id,
            "version_id": (
                output.upload_proof["version_id"]
                if output.upload_proof is not None
                else None
            ),
            "object_key": output.storage_ref["object_key"],
            "sha256": output.sha256,
            "size_bytes": output.size_bytes,
            "created_at": output.created_at,
            "expires_at": output.expires_at,
            "bundle_created_at": bundle.created_at,
            "retention_seconds": (
                bundle.expires_at - bundle.created_at
            ).total_seconds(),
        }


async def test_installed_worker_reads_private_original_and_retires_real_delivery(
    fenced_http, test_engine, monkeypatch, tmp_path, record_property
):
    assert make_url(celery_app.conf.broker_url) == make_url(
        settings.effective_celery_broker
    )
    monkeypatch.setattr(settings, "jobs_queue_url", "")
    routing = fixture_routing_owner(test_engine, monkeypatch)
    owner = suite = media = worker = sender = scope = owner_log = None
    neighbor_suite = None
    store = None
    run_a = run_b = run_c = job_id = None
    ordinary = ordinary_job_id = None
    deliveries = []
    completed = False
    evidence = {}
    try:
        await routing.__aenter__()
        media = await _start_media_runtime(test_engine, tmp_path)
        monkeypatch.setattr(
            settings, "assets_media_url", str(media.control_client.base_url).rstrip("/")
        )
        monkeypatch.setattr(settings, "assets_media_token", media.control_token)
        monkeypatch.setenv("ASSETS_MEDIA_URL", settings.assets_media_url)
        monkeypatch.setenv("ASSETS_MEDIA_TOKEN", media.control_token)
        monkeypatch.setenv("ASSETS_MEDIA_TOKEN_FILE", "")
        monkeypatch.setattr(settings, "assets_image_executor", "split")
        monkeypatch.setenv("ASSETS_IMAGE_EXECUTOR", "split")
        await _start_image_worker(media, tmp_path)
        owner_log = tempfile.TemporaryFile()
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "apps/web/e2e/fixtures/seed-request.py")],
            cwd=ROOT / "apps/api",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=owner_log,
            close_fds=True,
            start_new_session=True,
            bufsize=0,
        )
        owner = _PreservedSeedOwner(process, owner_log)
        suite = OwnedSuite(owner, uuid.uuid4())
        await asyncio.wait_for(owner.prepare_suite(suite.suite_id, "owned"), 60)
        neighbor_suite = OwnedSuite(owner, uuid.uuid4())
        await asyncio.wait_for(
            owner.prepare_suite(neighbor_suite.suite_id, "owned"), 60
        )
        run_c, run_a = sorted(
            [
                suite.new_run("zip" + uuid.uuid4().hex[:8]),
                suite.new_run("aux" + uuid.uuid4().hex[:8]),
            ],
            key=lambda run: run.run_id.int,
        )
        run_b = neighbor_suite.new_run("peer" + uuid.uuid4().hex[:8])
        world = await asyncio.wait_for(_build_owned(owner, run_a), 120)
        await asyncio.wait_for(_build_owned(owner, run_c), 120)
        await asyncio.wait_for(_build_owned(owner, run_b), 120)
        versions_a = await _fixture_versions(test_engine, run_a)
        versions_b = await _fixture_versions(test_engine, run_b)
        with closing(fixture_original_reader()) as reader:
            bytes_a = await asyncio.to_thread(_read_versions, reader, versions_a)
            bytes_b = await asyncio.to_thread(_read_versions, reader, versions_b)
        resources, _, store = await assert_workspace_identity(test_engine)
        bucket = resources["buckets"]["MINIO_EXPORT_BUCKET"]
        inventory = await asyncio.to_thread(
            store.list_object_versions, Bucket=bucket, MaxKeys=1
        )
        assert not inventory.get("Versions") and not inventory.get("DeleteMarkers")
        await asyncio.to_thread(
            store.put_bucket_versioning,
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
        ordinary = await _seed_export_world(test_engine)
        await asyncio.to_thread(
            _verify_bucket_owner,
            store,
            resources["buckets"]["MINIO_DATASETS_BUCKET"],
            resources["owner"],
        )
        await asyncio.to_thread(
            store.put_object,
            Bucket=resources["buckets"]["MINIO_DATASETS_BUCKET"],
            Key=ordinary["source_key"],
            Body=_SOURCE_PNG,
            ContentType="image/png",
        )
        await _verify_export_source(test_engine, ordinary)
        scope = await asyncio.to_thread(_export_writer, resources, store, ordinary)
        worker = WorkspaceCeleryWorker(
            test_engine,
            tmp_path,
            backend_token=secrets.token_urlsafe(32),
            object_store_endpoint=endpoint_url(),
            object_store_bucket=resources["buckets"]["MINIO_BUCKET"],
            object_store_use_ssl=endpoint_url().startswith("https://"),
            object_store_access_key=scope["access_key"],
            object_store_secret_key=scope["secret_key"],
            object_store_buckets=resources["buckets"],
            queue_name="export",
            export_execution=True,
        )
        await worker.configure_fixture_original_reader(
            media_url=settings.assets_media_url, media_token=media.control_token
        )
        sender = Celery(
            "fixture-export-verifier",
            broker=settings.effective_celery_broker,
            backend=settings.effective_celery_broker,
        )
        worker.start()
        await _wait_worker_ready(sender, worker)
        evidence["runtime"] = worker.assert_runtime()
        assert evidence["runtime"]["network_mode"] == "host"
        headers = await _login(owner, world)
        accepted = await asyncio.wait_for(
            fenced_http.post(
                f"/api/v1/projects/{world['project_id']}/export",
                params=[("targets", "native_json"), ("targets", "coco")],
                headers=headers,
            ),
            120,
        )
        assert accepted.status_code == 202, accepted.text
        job_id = uuid.UUID(accepted.json()["job_id"])
        sessions = async_sessionmaker(test_engine, expire_on_commit=False)
        payload, evidence["broker_ack"] = await _wait_export_delivery(
            test_engine, sender, worker, job_id, deliveries
        )
        output = await _export_snapshot(test_engine, job_id)
        assert output["format"] == "coco,native_json"
        assert output["retention_seconds"] == 900
        assert output["fixture_run_id"] == run_a.run_id
        assert output["version_id"] not in (None, "", "null")
        output_id, version_id = output["id"], output["version_id"]
        object_key, digest = output["object_key"], output["sha256"]
        deadline = output["expires_at"]
        evidence["output"] = output
        response = await asyncio.to_thread(
            store.get_object,
            Bucket=bucket,
            Key=object_key,
            VersionId=version_id,
        )
        with closing(response["Body"]) as body:
            archive_bytes = body.read()
        assert hashlib.sha256(archive_bytes).hexdigest() == digest
        with ZipFile(BytesIO(archive_bytes)) as archive:
            images = json.loads(archive.read("images_manifest.json"))["images"]
            native = json.loads(archive.read("native_json/annotations.json"))
            coco = json.loads(archive.read("coco/annotations.json"))
            paths = {image["archive_path"] for image in images}
            assert {task["file_path"] for task in native["tasks"]} == paths
            assert {image["file_name"] for image in coco["images"]} == paths
        assert {image["task_id"] for image in images} == set(world["task_ids"])
        expected_images = {
            hashlib.sha256(value).hexdigest(): value for value in bytes_a.values()
        }
        assert {image["sha256"] for image in images} == set(expected_images)
        async with httpx.AsyncClient(trust_env=False, timeout=30) as consumer:
            for image in images:
                delivered = await consumer.get(image["presigned_url"])
                assert delivered.status_code == 200
                assert delivered.content == expected_images[image["sha256"]]
                assert len(delivered.content) == image["size_bytes"]
        ordinary_headers = await _login(
            owner, {"admin_email": ordinary["reviewer_email"]}
        )
        ordinary_accepted = await asyncio.wait_for(
            fenced_http.post(
                f"/api/v1/projects/{ordinary['project_id']}/export",
                params={"targets": "native_json"},
                headers=ordinary_headers,
            ),
            120,
        )
        assert ordinary_accepted.status_code == 202, ordinary_accepted.text
        ordinary_job_id = uuid.UUID(ordinary_accepted.json()["job_id"])
        _, evidence["ordinary_broker_ack"] = await _wait_export_delivery(
            test_engine, sender, worker, ordinary_job_id, deliveries
        )
        ordinary_output = await _export_snapshot(test_engine, ordinary_job_id)
        assert ordinary_output["retention_seconds"] == 168 * 60 * 60
        assert ordinary_output["format"] == "native_json"
        assert ordinary_output["fixture_run_id"] is None
        ordinary_response = await asyncio.to_thread(
            store.get_object,
            Bucket=bucket,
            Key=ordinary_output["object_key"],
        )
        ordinary_version = ordinary_response["VersionId"]
        assert ordinary_version not in ("", "null")
        evidence["ordinary_observed_version"] = ordinary_version
        with closing(ordinary_response["Body"]) as body:
            ordinary_archive = body.read()
        assert hashlib.sha256(ordinary_archive).hexdigest() == ordinary_output["sha256"]
        with ZipFile(BytesIO(ordinary_archive)) as archive:
            ordinary_native = json.loads(archive.read("annotations.json"))
            ordinary_images = json.loads(archive.read("images_manifest.json"))["images"]
        assert {image["task_id"] for image in ordinary_images} == {
            str(ordinary["task_id"])
        }
        assert (
            ordinary_native["tasks"][0]["task_match"]["display_id"]
            == ordinary["task_display_id"]
        )
        assert ordinary_native["tasks"][0]["annotations"][0]["class_name"] == "car"
        async with httpx.AsyncClient(trust_env=False, timeout=30) as consumer:
            ordinary_source = await consumer.get(ordinary_images[0]["presigned_url"])
            assert ordinary_source.status_code == 200
            assert ordinary_source.content == _SOURCE_PNG
        evidence["ordinary_output"] = ordinary_output
        early = await asyncio.wait_for(_cleanup_owned(owner, run_a), 120)
        assert early.status_code == 409, early.text
        assert early.json()["detail"]["code"] == "fixture_export_retirement_unsettled"
        while True:
            async with test_engine.connect() as db:
                now = await db.scalar(text("SELECT clock_timestamp()"))
            if now >= deadline:
                break
            await asyncio.sleep(min(5, (deadline - now).total_seconds()))
        await asyncio.wait_for(suite.finish(), 120)
        with pytest.raises(ClientError) as missing:
            await asyncio.to_thread(
                store.head_object,
                Bucket=bucket,
                Key=object_key,
                VersionId=version_id,
            )
        assert missing.value.response["Error"]["Code"] in {
            "404",
            "NoSuchKey",
            "NoSuchVersion",
            "NotFound",
        }
        replay = str(uuid.uuid4())
        sender.send_task(
            _TASK_NAME,
            kwargs={
                "project_id": world["project_id"],
                "batch_id": None,
                "targets": payload["targets"],
                "opts": {key: payload[key] for key in _TASK_OPTS},
                "async_job_id": str(job_id),
            },
            queue=worker.queue,
            task_id=replay,
        )
        deliveries.append(replay)
        evidence["late_delivery_ack"] = await _wait_broker_ack(sender, worker, replay)
        async with sessions() as db:
            assert await db.get(AsyncJob, job_id) is None
            assert await db.get(ExportOutput, output_id) is None
            assert (
                await db.scalar(
                    text(
                        "SELECT state FROM yoao_export_v1.executions WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
                == "retired"
            )
        with closing(fixture_original_reader()) as reader:
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_b) == bytes_b
            )
        retired_b = await asyncio.wait_for(_cleanup_owned(owner, run_b), 120)
        assert retired_b.status_code == 200, retired_b.text
        await asyncio.wait_for(neighbor_suite.finish(), 120)
        assert await _export_snapshot(test_engine, ordinary_job_id) == ordinary_output
        ordinary_preserved = await asyncio.to_thread(
            store.get_object,
            Bucket=bucket,
            Key=ordinary_output["object_key"],
        )
        assert ordinary_preserved["VersionId"] == ordinary_version
        with closing(ordinary_preserved["Body"]) as body:
            assert body.read() == ordinary_archive
        async with httpx.AsyncClient(trust_env=False, timeout=30) as consumer:
            ordinary_source = await consumer.get(ordinary_images[0]["presigned_url"])
            assert ordinary_source.status_code == 200
            assert ordinary_source.content == _SOURCE_PNG
        evidence["ordinary_survives_fixture_retirement"] = True
        worker.stop()
        await asyncio.to_thread(_delete_owned_queue, sender, worker.queue)
        for delivery in deliveries:
            await asyncio.to_thread(sender.AsyncResult(delivery).forget)
        sender.close()
        await asyncio.to_thread(_destroy_cleanup_storage_scope, scope)
        await owner.shutdown_authority(neighbor_suite.suite_id)
        owner.process.stdin.close()
        await asyncio.to_thread(owner.process.wait, timeout=30)
        assert owner.process.returncode == 0
        owner_log.close()
        await _stop_media_runtime(media)
        await routing.__aexit__(None, None, None)
        store.close()
        record_property("installed_private_export", json.dumps(evidence, default=str))
        completed = True
    finally:
        if not completed:
            _RETAINED_UNCERTAIN.append(
                (
                    owner,
                    suite,
                    neighbor_suite,
                    media,
                    worker,
                    sender,
                    scope,
                    owner_log,
                    store,
                    routing,
                )
            )
            if worker is not None and worker.container_id is not None:
                evidence["diagnostics"] = worker.capture_diagnostics()
            record_property(
                "installed_private_export_failure", json.dumps(evidence, default=str)
            )
            print(
                "Retained uncertain private export: "
                f"suite={suite.suite_id if suite else None} "
                f"neighbor_suite={neighbor_suite.suite_id if neighbor_suite else None} "
                f"runs={run_a},{run_b},{run_c} job={job_id} "
                f"ordinary_project={ordinary['project_id'] if ordinary else None} ordinary_job={ordinary_job_id} "
                f"worker={worker.container_id if worker else None} owner_pid={owner.process.pid if owner else None} "
                f"media_pid={media.control_process.pid if media else None}; "
                "no forced cleanup attempted",
                flush=True,
            )
