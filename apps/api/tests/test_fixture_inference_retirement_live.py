"""Disposable, real-worker acceptance for a held fixture inference retirement.

Run explicitly under the module-data verifier; this is not part of TEST_FILES.
The deterministic provider qualifies its own physical lifecycle, not a model or GPU.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import select
import subprocess
import sys
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import httpx
import pytest
from app.config import settings
from botocore.exceptions import ClientError
from celery import Celery
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.identity_e2e_support import (
    e2e_consumer as e2e_consumer,
)
from tests.identity_e2e_support import (
    fenced_http as fenced_http,
)
from tests.identity_e2e_support import (
    fixture_routing_owner,
)
from tests.seed_owned_scenarios import _build_owned, _cleanup_owned
from tests.seed_owner_support import (
    OwnedSuite,
    SeedOwner,
    _fixture_versions,
    _read_versions,
    fixture_original_reader,
    private_seed_owner,
)
from tests.test_asset_storage_cleanup_runtime import (
    _local_s3_client,
    _verified_resource_manifest,
    _verify_bucket_owner,
)
from tests.test_assets_images_live import (
    _CONTROL_POLICY_ENV,
    _assert_sql_role_is_narrow,
    _srgb_profile,
)
from tests.test_assets_media_live import (
    _free_loopback_port,
    _guard_role_url,
    _runtime_database_url,
    _runtime_environment,
    _start_process,
    _wait_control_ready,
)
from tests.test_batch_result_worker_live import _delete_owned_queue
from tests.test_identity_sessions_live import _verify_redis

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned database, storage and runtime roles",
    ),
]
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from linux_pidfd import open_pidfd as _process_handle  # noqa: E402
from worktree_env import atomic_json  # noqa: E402

# Keep all exact process and pipe handles after indeterminate native work.
_RETAINED_UNCERTAIN = []


class _PreservedSeedOwner(SeedOwner):
    """The standard private protocol over retained, nonblocking process pipes."""

    def __init__(self, process, errors):
        super().__init__(process, errors)
        self._buffer = bytearray()
        os.set_blocking(process.stdout.fileno(), False)

    async def _exchange(self, envelope):
        self.process.stdin.write((json.dumps(envelope) + "\n").encode())
        self.process.stdin.flush()
        fd = self.process.stdout.fileno()
        while True:
            if b"\n" in self._buffer:
                line, _, rest = self._buffer.partition(b"\n")
                self._buffer = bytearray(rest)
                result = json.loads(line)
                if result.get("operation_id") != envelope["operation_id"]:
                    raise RuntimeError(
                        "Fixture owner returned a different operation identity"
                    )
                return result
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                loop = asyncio.get_running_loop()
                ready = loop.create_future()
                loop.add_reader(
                    fd, lambda: None if ready.done() else ready.set_result(None)
                )
                try:
                    await ready
                finally:
                    loop.remove_reader(fd)
                continue
            if not chunk:
                raise RuntimeError(
                    f"Fixture client exited before replying: {self.process.poll()}"
                )
            self._buffer.extend(chunk)


async def test_shutdown_ack_reconnects_and_preserves_exact_owner_identity(test_engine):
    fixture_db = make_url(os.environ["PLAYWRIGHT_E2E_DATABASE_URL"])
    assert (fixture_db.host, fixture_db.port, fixture_db.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    record_dir = (
        Path(os.environ["YOAO_WORKTREE_RESOURCE_ROOT"])
        / ".worktree"
        / os.environ["YOAO_WORKTREE_MODE"]
        / "fixture-authority"
    )
    first_suite, neighbor = uuid.uuid4(), uuid.uuid4()
    async with private_seed_owner() as first:
        await first.prepare_suite(first_suite, "owned")
        await first.prepare_suite(neighbor, "owned")
        await OwnedSuite(first, first_suite).finish()
        shared_id = str(uuid.uuid4())
        shared = await first._exchange(
            {
                "action": "execute",
                "operation_id": shared_id,
                "payload": {
                    "operation": "owner_shutdown",
                    "suite_id": str(first_suite),
                },
            }
        )
        assert json.loads(shared["body"])["state"] == "shared"
        async with private_seed_owner() as fresh:
            await fresh.acknowledge_operation(shared_id)
        assert await first.recover_operation(shared_id) == shared
        seal = json.loads(
            (await first._request({"operation": "owner_offline_seal"}))["body"]
        )
        async with private_seed_owner() as sealed:
            status = await sealed._request({"operation": "owner_offline_status"})
            assert json.loads(status["body"])["phase"] == "sealed"
            await sealed._request(
                {
                    "operation": "owner_offline_resume",
                    "seal_secret": seal["seal_secret"],
                }
            )
            await OwnedSuite(sealed, neighbor).finish()
            assert await sealed.shutdown_authority(neighbor) == "stopped"
    assert not (record_dir / "owner.json").exists()
    assert json.loads((record_dir / "generation.json").read_text())["state"] == "closed"

    final_suite = uuid.uuid4()
    async with private_seed_owner() as first:
        await first.prepare_suite(final_suite, "owned")
        await OwnedSuite(first, final_suite).finish()
        operation_id = str(uuid.uuid4())
        result = await first._exchange(
            {
                "action": "execute",
                "operation_id": operation_id,
                "payload": {
                    "operation": "owner_shutdown",
                    "suite_id": str(final_suite),
                },
            }
        )
        assert json.loads(result["body"])["state"] == "stopped"
        # Attach before replacement, then prove that the final ACK cannot
        # finalize an altered record; restore the exact record before recovery.
        probe = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "apps/web/e2e/fixtures/seed-request.py"),
            cwd=ROOT / "apps/api",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        probe_id = str(uuid.uuid4())
        probe.stdin.write(
            (
                json.dumps({"action": "recover", "operation_id": probe_id}) + "\n"
            ).encode()
        )
        await probe.stdin.drain()
        assert json.loads(await probe.stdout.readline())["state"] == "unknown"
        path = record_dir / "owner.json"
        record = json.loads(path.read_text())
        try:
            atomic_json(path, {**record, "nonce": secrets.token_hex(32)})
            probe.stdin.write(
                (
                    json.dumps({"action": "ack", "operation_id": operation_id}) + "\n"
                ).encode()
            )
            await probe.stdin.drain()
            assert await probe.stdout.readline() == b""
            assert await probe.wait() != 0
        finally:
            atomic_json(path, record)
            probe.stdin.close()
        async with private_seed_owner() as fresh:
            await fresh.acknowledge_operation(operation_id)
    assert not (record_dir / "owner.json").exists()
    assert json.loads((record_dir / "generation.json").read_text())["state"] == "closed"


async def _eventually(description, probe, *, timeout=120):
    try:
        async with asyncio.timeout(timeout):
            while True:
                result = await probe()
                if result:
                    return result
                await asyncio.sleep(0.2)
    except TimeoutError as exc:
        raise AssertionError(f"Timed out waiting for {description}") from exc


async def _owner_post(owner, path, *, payload, timeout=120):
    return await asyncio.wait_for(owner.post(path, json=payload), timeout)


async def _control(owner, run, fixture, action):
    response = await _owner_post(
        owner,
        "/api/v1/__test/seed/provider-control",
        payload={
            **run.payload(),
            "registry_id": fixture["ml_backend_id"],
            "action": action,
        },
        timeout=15,
    )
    assert response.status_code == 200, response.text
    return response.json()["requests"]


async def _provider_handle(run):
    async def find():
        root = Path(os.environ["YOAO_WORKTREE_RESOURCE_ROOT"])
        mode = os.environ["YOAO_WORKTREE_MODE"]
        record = json.loads(
            (root / ".worktree" / mode / "fixture-authority" / "owner.json").read_text()
        )
        authority_pid = record["pid"]
        process_stat = Path(f"/proc/{authority_pid}/stat").read_text()
        assert (
            int(process_stat[process_stat.rfind(")") + 2 :].split()[19])
            == record["start"]
        )
        child_file = Path(f"/proc/{authority_pid}/task/{authority_pid}/children")
        for child in child_file.read_text().split():
            try:
                command = Path(f"/proc/{child}/cmdline").read_bytes().split(b"\0")
            except FileNotFoundError:
                continue
            if (
                b"app.services.e2e_fixture_backend" in command
                and str(run.run_id).encode() in command
            ):
                return int(child), _process_handle(int(child))
        return None

    return await _eventually("fixture provider process", find, timeout=15)


def _exited(handle):
    poller = select.poll()
    poller.register(handle, select.POLLIN)
    return bool(poller.poll(0))


async def _wait_exit(handle):
    async def exited():
        return _exited(handle)

    await _eventually("fixture provider exit", exited, timeout=30)


async def _snapshot(engine, registry_id, project_id):
    async with engine.connect() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT p.id AS pool_id,p.enabled,p.routing_generation,"
                        "p.legacy_instance_id,m.id AS member_id,m.traffic_state,m.weight,"
                        "r.config_revision,r.eligibility_revision,r.authority_epoch,"
                        "r.backend_incarnation,r.continuity,project.id AS project_id "
                        "FROM public.ml_backend_pool_members m "
                        "JOIN public.ml_backend_service_pools p ON p.id=m.pool_id "
                        "JOIN yoao_inference_routing_v1.registry_state r "
                        "ON r.registry_id=m.registry_id "
                        "JOIN public.projects project ON project.ml_backend_pool_id=p.id "
                        "WHERE m.registry_id=:registry AND project.id=:project"
                    ),
                    {
                        "registry": uuid.UUID(registry_id),
                        "project": uuid.UUID(project_id),
                    },
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _ownership_evidence(engine, run):
    async with engine.connect() as db:
        rows = await db.execute(
            text(
                "SELECT kind,resource_id,logical_key,state,creation_id,admission_id,"
                "storage_identity,item_id,item_fingerprint "
                "FROM yoao_fixture_v1.fixture_run_resources "
                "WHERE run_id=:run ORDER BY kind,resource_id"
            ),
            {"run": run.run_id},
        )
        return tuple(rows.all())


async def _work(engine, job_id):
    async with engine.connect() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT j.status AS job_status,x.state AS execution_state,"
                        "x.execution_id,x.generation,x.worker_id,x.lease_token,"
                        "x.lease_expires_at,x.lease_expires_at > pg_catalog.clock_timestamp() AS lease_live,"
                        "b.generation AS route_generation,"
                        "s.id AS scope_id,s.registry_id,"
                        "s.state AS scope_state,s.closed,a.id AS attempt_id,"
                        "a.state AS attempt_state,a.dispatch_state,"
                        "i.input_id,i.status AS input_status,i.blob_version_id "
                        "FROM public.async_jobs j "
                        "JOIN public.batch_result_executions x ON x.job_id=j.id "
                        "JOIN yoao_inference_routing_v1.prediction_execution_scopes b "
                        "ON b.job_id=j.id "
                        "JOIN yoao_inference_routing_v1.scopes s ON s.id=b.scope_id "
                        "JOIN yoao_inference_routing_v1.attempts a ON a.scope_id=s.id "
                        "JOIN public.asset_accepted_image_inputs i ON i.job_id=j.id "
                        "WHERE j.id=:job ORDER BY a.created_at DESC LIMIT 1"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def _terminal(engine, job_id):
    async with engine.connect() as db:
        row = (
            await db.execute(
                text(
                    "SELECT j.status,x.state FROM public.async_jobs j "
                    "JOIN public.batch_result_executions x ON x.job_id=j.id "
                    "WHERE j.id=:job"
                ),
                {"job": job_id},
            )
        ).first()
    if (
        row
        and row[0] in {"completed", "cancelled"}
        and row[1] in {"completed", "cancelled"}
    ):
        return tuple(row)
    return None


async def _login(owner, fixture):
    response = await _owner_post(
        owner, "/api/v1/__test/seed/login", payload={"email": fixture["admin_email"]}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


async def _submit(client, fixture, headers):
    response = await asyncio.wait_for(
        client.post(
            f"/api/v1/projects/{fixture['project_id']}/preannotate",
            headers=headers,
            json={
                "ml_backend_id": fixture["ml_backend_id"],
                "model_id": "e2-detect",
                "output_mode": "box",
                "task_ids": [fixture["task_ids"][0]],
                "execution_scope": "workbench",
                "predict_mode": "overwrite",
            },
        ),
        timeout=30,
    )
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()["job_id"])


def _worker_environment(test_engine, media, execution_url, execution_token, broker):
    legacy = _guard_role_url(
        "AAP_MODULE_DATA_LEGACY_RUNTIME_URL", "legacy_runtime", test_engine
    )
    result = _guard_role_url(
        "AAP_MODULE_DATA_PREANNOTATION_RESULT_URL",
        "yoao_preannotation_result_client",
        test_engine,
    )
    publication = _guard_role_url(
        "AAP_MODULE_DATA_JOBS_PUBLICATION_URL",
        "yoao_jobs_publication_client",
        test_engine,
    )
    # app.config normally reads the checkout's absolute .env. Override its
    # credential/role fields rather than exposing owner or storage credentials
    # to the ordinary worker, then provide only its actual narrow authorities.
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONPATH": os.pathsep.join(
            [str(ROOT / "apps/api"), *(p for p in sys.path if p)]
        ),
    }
    for name, field in type(settings).model_fields.items():
        annotation = field.annotation
        is_string = annotation is str or str in get_args(annotation)
        if is_string and any(
            part in name
            for part in (
                "_url",
                "secret",
                "token",
                "password",
                "access_key",
                "api_key",
                "encryption_key",
            )
        ):
            environment[name.upper()] = ""
    environment.update(
        DATABASE_URL=legacy.set(drivername="postgresql+asyncpg").render_as_string(
            hide_password=False
        ),
        MIGRATION_DATABASE_URL="",
        PREANNOTATION_RESULTS_DATABASE_URL=_runtime_database_url(result),
        JOBS_PUBLICATION_DATABASE_URL=_runtime_database_url(publication),
        JOBS_PUBLICATION_DATABASE_URL_FILE="",
        REDIS_URL=broker,
        CELERY_BROKER_URL=broker,
        ASSETS_MEDIA_URL=str(media.control_client.base_url).rstrip("/"),
        ASSETS_MEDIA_TOKEN=media.control_token,
        INFERENCE_ROUTING_URL=settings.inference_routing_url,
        INFERENCE_ROUTING_TOKEN=settings.inference_routing_token,
        INFERENCE_MODEL_EXECUTION_URL=execution_url,
        INFERENCE_MODEL_EXECUTION_TOKEN=execution_token,
        ML_BACKEND_ROUTER_MODE="enforce",
        GPU_ARBITER_MODE="off",
        ML_PREDICT_TIMEOUT="180",
        ENVIRONMENT="development",
        DEBUG="false",
    )
    return environment


async def _start_execution_owner(tmp_path):
    # Same real owner and narrow role set as _start_model_execution_owner, but
    # Popen deliberately survives pytest if physical work remains uncertain.
    role_sources = {
        "INFERENCE_BACKEND_ENDPOINT_EXECUTION_DATABASE_URL": "AAP_MODULE_DATA_INFERENCE_ENDPOINT_EXECUTION_URL",
        "INFERENCE_GPU_DATABASE_URL": "AAP_MODULE_DATA_INFERENCE_GPU_URL",
        "INFERENCE_GPU_ENDPOINT_HEALTH_OBSERVATION_DATABASE_URL": "AAP_MODULE_DATA_INFERENCE_GPU_HEALTH_ENDPOINT_URL",
        "INFERENCE_GPU_ENDPOINT_GPU_CONTROL_DATABASE_URL": "AAP_MODULE_DATA_INFERENCE_GPU_CONTROL_ENDPOINT_URL",
        "INFERENCE_HEALTH_DATA_DATABASE_URL": "AAP_MODULE_DATA_INFERENCE_HEALTH_DATA_URL",
    }
    port = _free_loopback_port()
    token = "execution-owner-" + uuid.uuid4().hex
    environment = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "TMPDIR")
        if name in os.environ
    }
    environment.update(
        **{target: os.environ[source] for target, source in role_sources.items()},
        PYTHONPATH=os.pathsep.join(path for path in sys.path if path),
        INFERENCE_ROUTING_DATABASE_URL=os.environ[
            "AAP_MODULE_DATA_INFERENCE_ROUTING_URL"
        ],
        INFERENCE_ROUTING_TOKEN="execution-routing-" + uuid.uuid4().hex,
        INFERENCE_MODEL_EXECUTION_TOKEN=token,
        INFERENCE_GPU_REDIS_URL=settings.redis_url,
        INFERENCE_HOST="127.0.0.1",
        INFERENCE_PORT=str(port),
        GPU_ARBITER_MODE="off",
        GPU_ARBITER_RESOURCES_JSON="{}",
    )
    process, stream = _start_process(
        [sys.executable, "-m", "yoao_inference"],
        environment,
        tmp_path / "held-model-execution.log",
    )
    url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:

            async def ready():
                assert process.poll() is None, (
                    "model execution owner exited before readiness"
                )
                try:
                    return (await client.get(url + "/health")).status_code == 200
                except httpx.HTTPError:
                    return False

            await _eventually("model execution owner readiness", ready, timeout=30)
    except BaseException:
        # No execution requests exist before this owner is returned.
        await _stop_known_process(process)
        stream.close()
        raise
    return process, url, token, stream


async def _start_media_runtime(test_engine, tmp_path):
    control_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL",
        "yoao_asset_media_control_client",
        test_engine,
    )
    worker_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_IMAGE_WORKER_URL",
        "yoao_asset_image_worker_client",
        test_engine,
    )
    resources = _verified_resource_manifest(test_engine, control_url)
    buckets = resources["buckets"]
    inputs_bucket = buckets["MINIO_INPUTS_BUCKET"]
    assert os.environ["MINIO_INPUTS_BUCKET"] == inputs_bucket
    scheme = "https" if settings.minio_use_ssl else "http"
    endpoint = f"{scheme}://{settings.minio_endpoint}"
    owner_s3 = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    for bucket in (
        buckets["MINIO_DATASETS_BUCKET"],
        buckets["MINIO_BUCKET"],
        buckets["MINIO_MEDIA_CACHE_BUCKET"],
        inputs_bucket,
    ):
        _verify_bucket_owner(owner_s3, bucket, resources["owner"])
    assert (
        owner_s3.get_bucket_versioning(Bucket=inputs_bucket).get("Status") == "Enabled"
    )
    control_access = os.environ["AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_ACCESS_KEY"]
    control_secret = os.environ["AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_SECRET_KEY"]
    worker_access = os.environ["AAP_MODULE_DATA_ASSET_IMAGE_WORKER_S3_ACCESS_KEY"]
    worker_secret = os.environ["AAP_MODULE_DATA_ASSET_IMAGE_WORKER_S3_SECRET_KEY"]
    assert control_access != worker_access and control_secret != worker_secret
    assert (control_access, control_secret) != (
        settings.minio_access_key,
        settings.minio_secret_key,
    )
    assert (worker_access, worker_secret) != (
        settings.minio_access_key,
        settings.minio_secret_key,
    )
    await _assert_sql_role_is_narrow(
        control_url.render_as_string(hide_password=False),
        "yoao_asset_media_control_client",
        "public.dataset_items",
    )
    await _assert_sql_role_is_narrow(
        worker_url.render_as_string(hide_password=False),
        "yoao_asset_image_worker_client",
        "public.asset_image_jobs",
    )
    token = secrets.token_urlsafe(32)
    port = _free_loopback_port()
    environment = _runtime_environment(
        tmp_path=tmp_path,
        role="control",
        database_url=_runtime_database_url(control_url),
        endpoint=endpoint,
        public_endpoint=endpoint,
        datasets_bucket=buckets["MINIO_DATASETS_BUCKET"],
        legacy_bucket=buckets["MINIO_BUCKET"],
        cache_bucket=buckets["MINIO_MEDIA_CACHE_BUCKET"],
        access_key=control_access,
        secret_key=control_secret,
        token=token,
        port=port,
        image_control_settings=_CONTROL_POLICY_ENV,
    )
    for suffix in ("ENDPOINT", "BUCKET", "ACCESS_KEY", "SECRET_KEY"):
        name = "AAP_ASSETS_ADDITIONAL_ORIGINAL_" + suffix
        environment[name] = os.environ[name]
    log_path = tmp_path / "held-media-control.log"
    process, stream = _start_process(
        [sys.executable, "-m", "yoao_assets_media_runtime"], environment, log_path
    )
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False
    )
    media = SimpleNamespace(
        worker_url=worker_url,
        resources=resources,
        datasets_bucket=buckets["MINIO_DATASETS_BUCKET"],
        legacy_bucket=buckets["MINIO_BUCKET"],
        cache_bucket=buckets["MINIO_MEDIA_CACHE_BUCKET"],
        endpoint=endpoint,
        owner_s3=owner_s3,
        worker_access=worker_access,
        worker_secret=worker_secret,
        control_token=token,
        control_client=client,
        control_process=process,
        control_stream=stream,
        srgb_profile=_srgb_profile(),
        image_process=None,
        image_stream=None,
    )
    try:
        await _wait_control_ready(client, process, log_path)
    except BaseException:
        # No fixture or input work exists at this startup boundary.
        if process.poll() is None:
            process.terminate()
        await asyncio.to_thread(process.wait, timeout=30)
        await client.aclose()
        stream.close()
        raise
    return media


async def _start_image_worker(media, tmp_path):
    environment = _runtime_environment(
        tmp_path=tmp_path,
        role="worker",
        database_url=_runtime_database_url(media.worker_url),
        endpoint=media.endpoint,
        public_endpoint=media.endpoint,
        datasets_bucket=media.datasets_bucket,
        legacy_bucket=media.legacy_bucket,
        cache_bucket=media.cache_bucket,
        access_key=media.worker_access,
        secret_key=media.worker_secret,
        image_worker=True,
        srgb_profile=media.srgb_profile,
    )
    for suffix in ("ENDPOINT", "BUCKET", "ACCESS_KEY", "SECRET_KEY"):
        name = "AAP_ASSETS_ADDITIONAL_ORIGINAL_" + suffix
        environment[name] = os.environ[name]
    process, stream = _start_process(
        [sys.executable, "-m", "yoao_assets_images_runtime.worker"],
        environment,
        tmp_path / "held-input-worker.log",
    )
    media.image_process = process
    media.image_stream = stream
    return process


async def _stop_known_process(process):
    if process.poll() is None:
        process.terminate()
    await asyncio.to_thread(process.wait, timeout=30)


async def _stop_media_runtime(media):
    if media.image_process is not None:
        await _stop_known_process(media.image_process)
        media.image_stream.close()
    await _stop_known_process(media.control_process)
    await media.control_client.aclose()
    media.control_stream.close()
    media.owner_s3.close()


async def test_held_inference_blocks_exact_retirement_until_physical_settlement(
    fenced_http, test_engine, monkeypatch, tmp_path
):
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1", (
        "run this explicit qualification only inside the disposable module-data verifier"
    )
    redis = _verify_redis()
    broker = settings.effective_celery_broker
    parsed_broker = make_url(broker)
    assert (parsed_broker.host, parsed_broker.port) == (redis.host, redis.port)
    fixture_db = make_url(os.environ["PLAYWRIGHT_E2E_DATABASE_URL"])
    assert (fixture_db.host, fixture_db.port, fixture_db.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    publication = _guard_role_url(
        "AAP_MODULE_DATA_JOBS_PUBLICATION_URL",
        "yoao_jobs_publication_client",
        test_engine,
    )
    # The in-process product API reads these settings per request; the
    # verifier supplies the narrow role URL under its own prefix.
    monkeypatch.setattr(
        settings, "jobs_publication_database_url", _runtime_database_url(publication)
    )
    monkeypatch.setattr(settings, "jobs_publication_database_url_file", "")
    for name, role in (
        ("AAP_MODULE_DATA_INFERENCE_ROUTING_URL", "yoao_inference_routing_client"),
        (
            "AAP_MODULE_DATA_INFERENCE_ENDPOINT_EXECUTION_URL",
            "yoao_inference_endpoint_execution_client",
        ),
        ("AAP_MODULE_DATA_INFERENCE_GPU_URL", "yoao_inference_gpu_authority_client"),
        (
            "AAP_MODULE_DATA_INFERENCE_GPU_HEALTH_ENDPOINT_URL",
            "yoao_inference_gpu_health_endpoint_client",
        ),
        (
            "AAP_MODULE_DATA_INFERENCE_GPU_CONTROL_ENDPOINT_URL",
            "yoao_inference_gpu_control_endpoint_client",
        ),
        (
            "AAP_MODULE_DATA_INFERENCE_HEALTH_DATA_URL",
            "yoao_inference_health_data_client",
        ),
    ):
        _guard_role_url(name, role, test_engine)
    from app.workers.celery_app import celery_app

    assert make_url(celery_app.conf.broker_url) == parsed_broker
    queue = "fixture_held_inference_" + uuid.uuid4().hex
    monkeypatch.setattr(settings, "preannotate_cpu_queue", queue)
    monkeypatch.setattr(settings, "preannotate_gpu_queue", queue)
    # Exercise the normal Celery publication, not a fixture dispatcher.
    monkeypatch.setattr(settings, "jobs_queue_url", "")
    routing_context = fixture_routing_owner(test_engine, monkeypatch)
    media = None
    owner = suite = execution_process = celery_worker = sender = None
    owner_errors = worker_log = execution_log = None
    handles = []
    jobs_started = False
    completed = False
    run_a = run_b = None
    job_a = job_b = None
    try:
        await routing_context.__aenter__()
        media = await _start_media_runtime(test_engine, tmp_path)
        monkeypatch.setattr(
            settings, "assets_media_url", str(media.control_client.base_url).rstrip("/")
        )
        monkeypatch.setattr(settings, "assets_media_token", media.control_token)
        monkeypatch.setenv("ASSETS_MEDIA_URL", settings.assets_media_url)
        monkeypatch.setenv("ASSETS_MEDIA_TOKEN", media.control_token)
        monkeypatch.setenv("ASSETS_MEDIA_TOKEN_FILE", "")
        image_worker = await _start_image_worker(media, tmp_path)
        (
            execution_process,
            execution_url,
            execution_token,
            execution_log,
        ) = await _start_execution_owner(tmp_path)
        owner_errors = tempfile.TemporaryFile()
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "apps/web/e2e/fixtures/seed-request.py")],
            cwd=ROOT / "apps/api",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=owner_errors,
            close_fds=True,
            start_new_session=True,
            bufsize=0,
        )
        owner = _PreservedSeedOwner(process, owner_errors)
        suite = OwnedSuite(owner, uuid.uuid4())
        await asyncio.wait_for(owner.prepare_suite(suite.suite_id, "owned"), 60)
        run_a = suite.new_run("held" + uuid.uuid4().hex[:8])
        run_b = suite.new_run("peer" + uuid.uuid4().hex[:8])
        fixture_b = await asyncio.wait_for(_build_owned(owner, run_b), 120)
        fixture_a = await asyncio.wait_for(_build_owned(owner, run_a), 120)
        _, handle_a = await _provider_handle(run_a)
        _, handle_b = await _provider_handle(run_b)
        handles.extend((handle_a, handle_b))
        assert not _exited(handle_a) and not _exited(handle_b)
        headers_a, headers_b = (
            await _login(owner, fixture_a),
            await _login(owner, fixture_b),
        )
        versions_a = await _fixture_versions(test_engine, run_a)
        versions_b = await _fixture_versions(test_engine, run_b)
        evidence_a = await _ownership_evidence(test_engine, run_a)
        evidence_b = await _ownership_evidence(test_engine, run_b)
        assert evidence_a and evidence_b
        with closing(fixture_original_reader()) as reader:
            bytes_a = await asyncio.to_thread(_read_versions, reader, versions_a)
            bytes_b = await asyncio.to_thread(_read_versions, reader, versions_b)
            topology_b = await _snapshot(
                test_engine, fixture_b["ml_backend_id"], fixture_b["project_id"]
            )
            assert topology_b["enabled"] and topology_b["traffic_state"] == "active"
            await _control(owner, run_a, fixture_a, "hold")
            environment = _worker_environment(
                test_engine, media, execution_url, execution_token, broker
            )
            celery_worker, worker_log = _start_process(
                [
                    sys.executable,
                    "-m",
                    "celery",
                    "-A",
                    "app.workers.celery_app:celery_app",
                    "worker",
                    "--pool=solo",
                    "--concurrency=1",
                    f"--queues={queue}",
                    f"--hostname=held-{uuid.uuid4().hex}@%h",
                    "--without-gossip",
                    "--without-mingle",
                    "--without-heartbeat",
                    "--loglevel=INFO",
                ],
                environment,
                tmp_path / "held-celery-worker.log",
            )
            sender = Celery("fixture-held-inference", broker=broker, backend=broker)

            async def worker_ready():
                assert celery_worker.poll() is None, (
                    "Celery worker exited during startup"
                )
                assert image_worker.poll() is None, (
                    "image input worker exited during startup"
                )
                return b" ready." in (tmp_path / "held-celery-worker.log").read_bytes()

            await _eventually("Celery worker readiness", worker_ready, timeout=60)
            jobs_started = True
            job_a = await _submit(fenced_http, fixture_a, headers_a)

            async def entered_a():
                assert celery_worker.poll() is None, (
                    "Celery worker exited before provider entry"
                )
                assert image_worker.poll() is None, (
                    "image input worker exited before provider entry"
                )
                requests = await _control(owner, run_a, fixture_a, "requests")
                return next(
                    (
                        r
                        for r in requests
                        if any(t["id"] == fixture_a["task_ids"][0] for t in r["tasks"])
                    ),
                    None,
                )

            entered = await _eventually("held provider entry", entered_a)
            assert "responseStatus" not in entered
            # Lose the actual response to a committed owned-fixture mutation while
            # the original provider is physically held.  Reconnect to that same
            # authority, recover by identity, and refuse ambiguous re-execution.
            run_c = suite.new_run("loss" + uuid.uuid4().hex[:8])
            lost_id = str(uuid.uuid4())
            lost_payload = {
                "path": "/api/v1/__test/seed/owned",
                "data": run_c.payload(),
                "headers": None,
            }
            lost_log = tempfile.TemporaryFile()
            lost = subprocess.Popen(
                [sys.executable, str(ROOT / "apps/web/e2e/fixtures/seed-request.py")],
                cwd=ROOT / "apps/api",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=lost_log,
                close_fds=True,
                bufsize=0,
            )
            try:
                lost.stdin.write(
                    (
                        json.dumps(
                            {
                                "action": "execute",
                                "operation_id": lost_id,
                                "payload": lost_payload,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                lost.stdin.flush()
                lost.stdout.close()
                await asyncio.to_thread(lost.wait, timeout=120)
                recovered = await owner.recover_operation(lost_id)
                assert (
                    recovered["operation_id"] == lost_id and recovered["status"] == 200
                )
                refused = await owner._exchange(
                    {
                        "action": "execute",
                        "operation_id": str(uuid.uuid4()),
                        "payload": lost_payload,
                    }
                )
                assert refused["owner_error"] == "OperationOutcomeUnresolved"
                ack_log = tempfile.TemporaryFile()
                lost_ack = subprocess.Popen(
                    [
                        sys.executable,
                        str(ROOT / "apps/web/e2e/fixtures/seed-request.py"),
                    ],
                    cwd=ROOT / "apps/api",
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=ack_log,
                    close_fds=True,
                    bufsize=0,
                )
                try:
                    lost_ack.stdin.write(
                        (
                            json.dumps(
                                {
                                    "action": "recover",
                                    "operation_id": lost_id,
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    lost_ack.stdin.flush()
                    recovered_again = json.loads(
                        await asyncio.wait_for(
                            asyncio.to_thread(lost_ack.stdout.readline), 120
                        )
                    )
                    assert recovered_again == recovered
                    lost_ack.stdin.write(
                        (
                            json.dumps(
                                {
                                    "action": "ack",
                                    "operation_id": lost_id,
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    lost_ack.stdin.flush()
                    lost_ack.stdout.close()
                    await asyncio.to_thread(lost_ack.wait, timeout=120)
                    assert await owner.recover_operation(lost_id) == recovered
                    await owner.acknowledge_operation(lost_id)
                except BaseException:
                    _RETAINED_UNCERTAIN.append((lost_ack, ack_log))
                    raise
                else:
                    lost_ack.stdin.close()
                    ack_log.close()
                fixture_c = json.loads(recovered["body"])
                assert fixture_c["project_id"] and fixture_c["ml_backend_id"]
                assert not _exited(handle_a)
                still_held = await _control(owner, run_a, fixture_a, "requests")
                original_request = next(
                    r
                    for r in still_held
                    if r["receivedAt"] == entered["receivedAt"]
                    and r["tasks"] == entered["tasks"]
                )
                assert "responseStatus" not in original_request
                cleaned_c = await _cleanup_owned(owner, run_c)
                assert cleaned_c.status_code == 200, cleaned_c.text
            except BaseException:
                _RETAINED_UNCERTAIN.append((lost, lost_log))
                raise
            else:
                lost.stdin.close()
                lost_log.close()
            # A later busy provider fences admission but cannot prove quiescence;
            # the earlier provider's real acknowledgement must remain visible.
            with pytest.raises(RuntimeError, match="Fixture operation failed"):
                await owner._request({"operation": "owner_offline_seal"})
            partial = json.loads(
                (
                    await owner._request(
                        {
                            "operation": "owner_offline_status",
                        }
                    )
                )["body"]
            )
            assert partial["phase"] == "incomplete" and not partial["sealed"]
            assert not partial["engine_disposed"]
            by_run = {item["run_id"]: item for item in partial["providers"]}
            assert by_run[str(run_b.run_id)]["sealed"]
            assert by_run[str(run_a.run_id)]["attempted"]
            assert not by_run[str(run_a.run_id)]["sealed"]
            record = json.loads(
                (
                    Path(os.environ["YOAO_WORKTREE_RESOURCE_ROOT"])
                    / ".worktree"
                    / os.environ["YOAO_WORKTREE_MODE"]
                    / "fixture-authority"
                    / "owner.json"
                ).read_text()
            )
            await owner._request(
                {
                    "operation": "owner_offline_resume",
                    "seal_secret": record["seal_secret"],
                }
            )
            reopened = json.loads(
                (
                    await owner._request(
                        {
                            "operation": "owner_offline_status",
                        }
                    )
                )["body"]
            )
            assert reopened["phase"] == "open" and not reopened["sealed"]
            held = await _work(test_engine, job_a)
            assert held is not None
            assert (
                held["job_status"] == "running" and held["execution_state"] == "running"
            )
            assert held["execution_id"] == str(job_a) and held["generation"] > 0
            assert held["route_generation"] == held["generation"]
            assert (
                held["worker_id"] and held["lease_token"] and held["lease_expires_at"]
            )
            assert held["lease_live"]
            assert str(held["registry_id"]) == fixture_a["ml_backend_id"]
            assert held["scope_state"] == "active" and not held["closed"]
            assert (
                held["attempt_state"] == "active"
                and held["dispatch_state"] == "started"
            )
            assert held["attempt_id"] and held["input_id"]
            assert held["input_status"] == "ready" and held["blob_version_id"]
            denied = await asyncio.wait_for(_cleanup_owned(owner, run_a), 120)
            assert denied.status_code == 409, denied.text
            assert denied.json()["detail"]["code"] in {
                "fixture_prediction_retirement_blocked",
                "fixture_run_unsettled",
            }
            async with test_engine.connect() as db:
                state = await db.scalar(
                    text(
                        "SELECT state FROM yoao_fixture_v1.fixture_runs WHERE id=:run"
                    ),
                    {"run": run_a.run_id},
                )
            assert state == "sealing"
            assert {
                r["id"]: dict(r) for r in await _fixture_versions(test_engine, run_a)
            } == {r["id"]: dict(r) for r in versions_a}
            assert await _ownership_evidence(test_engine, run_a) == evidence_a
            assert await _ownership_evidence(test_engine, run_b) == evidence_b
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_a) == bytes_a
            )
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_b) == bytes_b
            )
            assert (
                await _snapshot(
                    test_engine, fixture_b["ml_backend_id"], fixture_b["project_id"]
                )
                == topology_b
            )
            still_held = await _work(test_engine, job_a)
            assert still_held and still_held["attempt_id"] == held["attempt_id"]
            assert still_held["attempt_state"] == "active" and not _exited(handle_a)
            assert not _exited(handle_b)
            late = await _owner_post(
                owner, "/api/v1/__test/seed/owned", payload=run_a.payload()
            )
            assert late.status_code == 409, late.text
            assert late.json()["detail"]["code"] == "fixture_scope_changed"
            from app.services import fixture_run_ownership

            async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
                with pytest.raises(HTTPException) as admission:
                    await fixture_run_ownership.admit(db, run_a.run_id, "owned_seed")
                assert admission.value.status_code == 409
                assert admission.value.detail["code"] == "fixture_run_closed"
            requests = await _control(owner, run_a, fixture_a, "requests")
            assert len(requests) == 1 and "responseStatus" not in requests[0]

            # Request cooperative cancellation while the real provider is still held;
            # no revoke with terminate, fabricated result, or lease mutation.
            cancel = await asyncio.wait_for(
                fenced_http.post(
                    f"/api/v1/async-jobs/{job_a}/cancel", headers=headers_a
                ),
                30,
            )
            assert cancel.status_code == 200, cancel.text
            await _control(owner, run_a, fixture_a, "release")
            await _eventually(
                "native cancellation or completion",
                lambda: _terminal(test_engine, job_a),
                timeout=180,
            )
            requests = await _control(owner, run_a, fixture_a, "requests")
            assert requests[0].get("responseStatus") == 200
            retired_a = await asyncio.wait_for(_cleanup_owned(owner, run_a), 120)
            assert retired_a.status_code == 200, retired_a.text
            await _wait_exit(handle_a)
            for bucket, key, version in bytes_a:
                with pytest.raises(ClientError) as missing:
                    await asyncio.to_thread(
                        reader.head_object, Bucket=bucket, Key=key, VersionId=version
                    )
                assert missing.value.response["Error"]["Code"] in {
                    "404",
                    "NoSuchKey",
                    "NoSuchVersion",
                    "NotFound",
                }
            assert not _exited(handle_b)
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_b) == bytes_b
            )
            assert (
                await _snapshot(
                    test_engine, fixture_b["ml_backend_id"], fixture_b["project_id"]
                )
                == topology_b
            )
            job_b = await _submit(fenced_http, fixture_b, headers_b)
            assert await _eventually(
                "neighbour inference completion",
                lambda: _terminal(test_engine, job_b),
                timeout=180,
            ) == ("completed", "completed")
            neighbour_requests = await _control(owner, run_b, fixture_b, "requests")
            assert any(
                r.get("responseStatus") == 200
                and any(t["id"] == fixture_b["task_ids"][0] for t in r["tasks"])
                for r in neighbour_requests
            )
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_b) == bytes_b
            )
            retired_b = await asyncio.wait_for(_cleanup_owned(owner, run_b), 120)
            assert retired_b.status_code == 200, retired_b.text
            await _wait_exit(handle_b)
        await asyncio.wait_for(suite.finish(), 120)
        completed = True
    finally:
        if completed:
            await owner.shutdown_authority(suite.suite_id)
            owner.process.stdin.close()
            await asyncio.to_thread(owner.process.wait, timeout=30)
            assert owner.process.returncode == 0
            await _stop_known_process(celery_worker)
            await _stop_known_process(execution_process)
            await asyncio.to_thread(_delete_owned_queue, sender, queue)
            sender.close()
            worker_log.close()
            execution_log.close()
            owner_errors.close()
            for handle in handles:
                os.close(handle)
            await _stop_media_runtime(media)
            await routing_context.__aexit__(None, None, None)
        else:
            # Never close the private owner, terminate workers, delete a queue,
            # or retire source versions while work might still be physical.
            # The verifier owns inspection/recovery of this exact failed run.
            _RETAINED_UNCERTAIN.append(
                (
                    owner,
                    suite,
                    execution_process,
                    celery_worker,
                    sender,
                    owner_errors,
                    worker_log,
                    execution_log,
                    handles,
                    media,
                    routing_context,
                )
            )
            print(
                "Retained uncertain fixture inference resources: "
                f"suite={suite.suite_id if suite else None} "
                f"runs={run_a.run_id if run_a else None},{run_b.run_id if run_b else None} "
                f"client_pid={owner.process.pid if owner else None} "
                f"authority_record=.worktree/{os.environ.get('YOAO_WORKTREE_MODE', 'unknown')}/fixture-authority/owner.json "
                f"jobs={job_a},{job_b} queue={queue} "
                f"celery_pid={celery_worker.pid if celery_worker else None} "
                f"execution_pid={execution_process.pid if execution_process else None} "
                f"media_pid={media.control_process.pid if media else None} "
                f"image_pid={media.image_process.pid if media and media.image_process else None} "
                f"jobs_started={jobs_started}; no forced cleanup attempted",
                flush=True,
            )
