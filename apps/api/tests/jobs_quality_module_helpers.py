"""Cold acceptance helpers for quality dispatch through the private Jobs queue."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import secrets
import sys
from types import SimpleNamespace
import uuid


_QUALITY_TASKS = (
    {
        "task": "app.workers.mask_qc.run_mask_qc",
        "operation": "run",
        "kind": "mask_qc",
        "argument": "run_id",
    },
    {
        "task": "app.workers.point_cloud_quality.run",
        "operation": "run",
        "kind": "point_cloud_quality",
        "argument": "run_id",
    },
    {
        "task": "app.workers.video_track_quality.run",
        "operation": "run",
        "kind": "video_track_quality",
        "argument": "run_id",
    },
    {
        "task": "app.workers.mask_repair.run_mask_repair",
        "operation": "repair",
        "kind": None,
        "argument": "batch_id",
        "rollback": False,
    },
    {
        "task": "app.workers.mask_repair.rollback_mask_repair",
        "operation": "repair",
        "kind": None,
        "argument": "batch_id",
        "rollback": True,
    },
    {
        "task": "app.workers.mask_format_import.run_mask_format_import",
        "operation": "import",
        "kind": None,
        "argument": "batch_id",
    },
)

_API_DISPATCH_PROBE = r"""
import json
import sys
import uuid

sys.path.insert(0, "/app")

from app.config import settings
spec = json.loads(sys.argv[1])
if not settings.jobs_queue_url:
    raise RuntimeError("remote Jobs queue is not selected")

def loaded_worker_modules():
    return sorted(
        name
        for name in sys.modules
        if name == "app.workers" or name.startswith("app.workers.")
    )

before_deps = loaded_worker_modules()
from app.deps import get_quality_job_dispatcher
if loaded_worker_modules() != before_deps:
    raise RuntimeError("importing API dependencies loaded worker modules")

dispatcher = get_quality_job_dispatcher()
if type(dispatcher).__name__ != "JobsQueueQualityDispatcher":
    raise RuntimeError("the remote quality dispatcher was not selected")
if loaded_worker_modules() != before_deps:
    raise RuntimeError("quality dispatcher construction loaded worker modules")

records = []
for item in spec:
    resource_id = uuid.UUID(item["resource_id"])
    if item["operation"] == "run":
        execution_id = item["execution_id"]
        if str(uuid.UUID(execution_id)) != execution_id:
            raise RuntimeError("preselected run execution ID is not a UUID")
        dispatcher.dispatch_run(item["kind"], resource_id, execution_id=execution_id)
    elif item["operation"] == "repair":
        execution_id = str(uuid.uuid4())
        dispatcher.dispatch_mask_repair(
            resource_id, rollback=item["rollback"], execution_id=execution_id
        )
    elif item["operation"] == "import":
        execution_id = str(uuid.uuid4())
        dispatcher.dispatch_mask_import(resource_id, execution_id=execution_id)
    else:
        raise RuntimeError("unknown quality dispatch variant")
    if str(uuid.UUID(execution_id)) != execution_id:
        raise RuntimeError("generated execution ID is not a canonical UUID")
    if loaded_worker_modules() != before_deps:
        raise RuntimeError("quality dispatch loaded worker modules")
    records.append(
        {
            "task": item["task"],
            "resource_id": str(resource_id),
            "execution_id": execution_id,
        }
    )

print(
    json.dumps(
        {
            "dispatcher_type": type(dispatcher).__name__,
            "worker_modules_before": before_deps,
            "worker_modules_after": loaded_worker_modules(),
            "records": records,
        },
        separators=(",", ":"),
    )
)
"""


def _point_cloud_pcd() -> bytes:
    points = [
        (100.0 - 3.0 + 6.0 * index / 11, y, 0.0)
        for index in range(12)
        for y in (-1.0, 0.0, 1.0)
    ]
    header = (
        "VERSION .7\n"
        "FIELDS x y z\n"
        "SIZE 4 4 4\n"
        "TYPE F F F\n"
        "COUNT 1 1 1\n"
        "WIDTH 36\n"
        "HEIGHT 1\n"
        "VIEWPOINT 0 0 0 1 0 0 0\n"
        "POINTS 36\n"
        "DATA ascii\n"
    )
    body = "".join(f"{x:.6f} {y:.1f} {z:.1f}\n" for x, y, z in points)
    return (header + body).encode("ascii")


def _create_quality_storage_scope(resources, owner_s3, keys):
    root = Path(__file__).resolve().parents[3]
    scripts = str(root / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from verify_module_data_boundary import (
        _cleanup_mc,
        _cleanup_storage_scope,
        _destroy_cleanup_storage_scope,
    )

    from app.config import settings

    scope = _cleanup_storage_scope(
        SimpleNamespace(settings=settings, resources=resources, s3=owner_s3)
    )
    bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    scope["policy"] = f"aap-quality-read-{secrets.token_hex(8)}"
    resources_arns = [f"arn:aws:s3:::{bucket}/{key}" for key in keys]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": resources_arns,
            }
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
        scope["quality_policy_resources"] = resources_arns
        return scope, _destroy_cleanup_storage_scope
    except BaseException:
        _destroy_cleanup_storage_scope(scope)
        raise


def _media_envelopes(sender, queue: str) -> list[tuple[bytes, bytes, dict]]:
    envelopes = []
    with sender.connection_for_read() as connection:
        channel = connection.default_channel
        for priority in channel.priority_steps:
            key = channel._q_for_pri(queue, priority)
            for raw in channel.client.lrange(key, 0, -1):
                envelopes.append((key, raw, json.loads(raw)))
    return envelopes


def _unpack_task_envelope(envelope: dict) -> tuple[str, str, list, dict, str, str]:
    headers = envelope.get("headers")
    if not isinstance(headers, dict):
        raise AssertionError("Celery queue message has no task headers")
    properties = envelope.get("properties")
    if not isinstance(properties, dict):
        raise AssertionError("Celery queue message has no properties")
    delivery_info = properties.get("delivery_info")
    if not isinstance(delivery_info, dict):
        raise AssertionError("Celery queue message has no delivery route")
    payload = json.loads(base64.b64decode(envelope["body"], validate=True))
    if not isinstance(payload, list) or len(payload) != 3:
        raise AssertionError("Celery queue message body is not an args tuple")
    args, kwargs, _embedded = payload
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise AssertionError("Celery queue task arguments have an invalid shape")
    return (
        headers.get("task"),
        headers.get("id"),
        args,
        kwargs,
        delivery_info.get("exchange"),
        delivery_info.get("routing_key"),
    )


def _remove_probe_messages(sender, queue: str, specs: list[dict]) -> int:
    by_task_and_args = {(item["task"], (item["resource_id"],)): item for item in specs}
    removed = 0
    with sender.connection_for_write() as connection:
        channel = connection.default_channel
        for priority in channel.priority_steps:
            key = channel._q_for_pri(queue, priority)
            for raw in channel.client.lrange(key, 0, -1):
                try:
                    envelope = json.loads(raw)
                    task, execution_id, args, _kwargs, _exchange, _routing_key = (
                        _unpack_task_envelope(envelope)
                    )
                except (AssertionError, KeyError, ValueError, TypeError):
                    continue
                item = by_task_and_args.get((task, tuple(args)))
                expected_id = (
                    item.get("observed_execution_id", item.get("execution_id"))
                    if item is not None
                    else None
                )
                if item is not None and (
                    expected_id is None or execution_id == expected_id
                ):
                    count = channel.client.lrem(key, 1, raw)
                    if count != 1:
                        raise AssertionError(
                            "owned quality probe message was not removed"
                        )
                    removed += 1
    return removed


def _inspect_quality_probe_messages(sender, specs: list[dict]) -> list[dict]:
    from tests.test_workspace_worker_assembly_live import _broker_state

    queue = "media"
    assert _broker_state(sender, queue) == {"queue_size": 6, "unacknowledged": 0}
    found = {}
    envelopes = _media_envelopes(sender, queue)
    with sender.connection_for_read() as connection:
        channel = connection.default_channel
        for _key, _raw, envelope in envelopes:
            (
                task,
                execution_id,
                args,
                kwargs,
                exchange,
                routing_key,
            ) = _unpack_task_envelope(envelope)
            matched = [
                item
                for item in specs
                if task == item["task"] and args == [item["resource_id"]]
            ]
            if not matched:
                raise AssertionError("quality probe found an unrelated media message")
            item = matched[0]
            if task in found:
                raise AssertionError("quality probe published a duplicate task")
            if kwargs or exchange != "" or routing_key != queue:
                raise AssertionError("quality probe changed task kwargs or media route")
            if channel._lookup(exchange, routing_key) != [queue]:
                raise AssertionError("quality probe did not use the static media route")
            expected_id = item.get("execution_id")
            if expected_id is not None and execution_id != expected_id:
                raise AssertionError(
                    "persisted quality execution ID changed in transport"
                )
            if not execution_id or str(uuid.UUID(execution_id)) != execution_id:
                raise AssertionError(
                    "quality probe execution ID is not a canonical UUID"
                )
            item["observed_execution_id"] = execution_id
            found[task] = item
    assert set(found) == {item["task"] for item in specs}
    return [
        {
            "task": item["task"],
            "resource_id": item["resource_id"],
            "execution_id": item["observed_execution_id"],
            "queue": queue,
        }
        for item in specs
    ]


def _worker_quality_task_signatures(worker) -> dict[str, list[str]]:
    task_names = [item["task"] for item in _QUALITY_TASKS]
    code = r"""
import inspect
import json
import sys

sys.path.insert(0, "/app")
from app.workers.celery_app import celery_app
celery_app.loader.import_default_modules()

records = {}
for task_name in sys.argv[1:]:
    task = celery_app.tasks.get(task_name)
    if task is None:
        raise RuntimeError("task is not registered: " + task_name)
    records[task_name] = list(inspect.signature(task.run).parameters)
print(json.dumps(records, separators=(",", ":")))
"""
    worker.assert_runtime()
    worker.assert_private_network()
    output = worker._docker(
        "exec",
        worker.container_id,
        "/opt/venv/bin/python",
        "-I",
        "-c",
        code,
        *task_names,
        timeout=45,
    )
    signatures = json.loads(output)
    expected = {item["task"]: [item["argument"]] for item in _QUALITY_TASKS}
    assert signatures == expected
    return signatures


async def _wait_quality_job(test_engine, run_id, job_id, *, timeout: float = 120):
    from app.db.models.async_job import AsyncJob
    from app.db.models.point_cloud_quality import PointCloudQualityRun
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    deadline = asyncio.get_running_loop().time() + timeout
    latest = None
    while asyncio.get_running_loop().time() < deadline:
        async with maker() as db:
            run = await db.get(PointCloudQualityRun, run_id)
            job = await db.get(AsyncJob, job_id)
            latest = (run, job)
            if (
                run is not None
                and job is not None
                and run.status in {"completed", "failed"}
                and job.status in {"completed", "failed", "cancelled"}
            ):
                return run, job
        await asyncio.sleep(0.25)
    raise AssertionError(f"quality run did not become terminal: {latest!r}")


async def exercise_quality_dispatch(
    test_engine,
    stack,
    sender,
    resources,
    owner_s3,
    directory,
    known_task_ids,
    *,
    check_outage: bool,
) -> tuple[dict, dict[str, str]]:
    """Return public evidence and a separate reusable authenticated session."""
    import httpx
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config import settings
    from app.db.models.async_job import AsyncJob, AsyncJobStatus
    from app.db.models.dataset import DatasetItem
    from app.db.models.point_cloud_quality import (
        PointCloudQualityIssue,
        PointCloudQualityRun,
    )
    from tests.factory import create_user
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres
    from tests.test_jobs_module_assembly_live import _assert_owned_broker_target, _login
    from tests.test_point_cloud_quality_api import _seed_quality_scene
    from tests.test_workspace_worker_assembly_live import (
        _broker_state,
        _wait_broker_ack,
        _wait_worker_ready,
    )
    from tests.workspace_worker_assembly_helpers import WorkspaceCeleryWorker

    _verify_postgres(test_engine.url)
    assert str(test_engine.url.database).endswith("_test")
    _assert_owned_broker_target(sender, "media")

    from aap_jobs.queue_contract import load_queue_configuration

    routes = load_queue_configuration()["task_routes"]
    assert {routes[item["task"]]["queue"] for item in _QUALITY_TASKS} == {"media"}

    probe_specs = []
    for item in _QUALITY_TASKS:
        spec = {**item, "resource_id": str(uuid.uuid4())}
        if item["operation"] == "run":
            spec["execution_id"] = str(uuid.uuid4())
        probe_specs.append(spec)

    api_containers = stack._verify_project_ownership()
    api_container = api_containers.get("identity-api")
    api_container_id = stack.service_ids.get("identity-api")
    assert (
        api_container is not None
        and api_container_id
        and api_container.get("Id") == api_container_id
    ), "quality dispatch probe did not resolve the owned API container"

    source_keys = []
    storage_scope = None
    destroy_storage_scope = None
    worker = None
    queue_stopped = False
    owner_id = project_id = scene_id = run_id = job_id = None
    execution_id = None
    findings: list[str] = []
    probe_records: list[dict] = []
    worker_signatures: dict[str, list[str]] = {}
    worker_runtime: dict | None = None
    outage: dict | None = None
    storage_evidence: dict | None = None
    probe_completed = False
    ack = None

    try:
        try:
            output = stack._docker(
                [
                    "exec",
                    api_container_id,
                    "/opt/venv/bin/python",
                    "-I",
                    "-c",
                    _API_DISPATCH_PROBE,
                    json.dumps(probe_specs, separators=(",", ":")),
                ],
                operation="Quality API queue dispatch probe",
                timeout=60,
            )
            probe_evidence = json.loads(stack.redact(output))
            assert probe_evidence["dispatcher_type"] == "JobsQueueQualityDispatcher"
            assert (
                probe_evidence["worker_modules_before"]
                == probe_evidence["worker_modules_after"]
            )
            api_records = {row["task"]: row for row in probe_evidence["records"]}
            assert set(api_records) == {item["task"] for item in probe_specs}
            probe_records = _inspect_quality_probe_messages(sender, probe_specs)
            for item, record in zip(probe_specs, probe_records, strict=True):
                api_record = api_records[item["task"]]
                assert api_record["resource_id"] == item["resource_id"]
                assert api_record["execution_id"] == record["execution_id"]
                known_task_ids.add(record["execution_id"])
            probe_completed = True
        finally:
            removed = _remove_probe_messages(sender, "media", probe_specs)
            if probe_completed:
                assert removed == 6, (
                    "exactly the six owned quality probe messages must be removed"
                )
            assert _broker_state(sender, "media") == {
                "queue_size": 0,
                "unacknowledged": 0,
            }

        maker = async_sessionmaker(test_engine, expire_on_commit=False)
        source_prefix = f"quality/{uuid.uuid4().hex}"
        pcd = _point_cloud_pcd()
        source_keys = [f"{source_prefix}/{frame:06d}.pcd" for frame in range(3)]
        async with maker.begin() as db:
            owner = await create_user(
                db,
                "super_admin",
                f"jobs-quality-{uuid.uuid4().hex[:12]}@worker.test",
                "Jobs quality acceptance",
            )
            project, scene, tasks, _track, _annotations = await _seed_quality_scene(
                db, owner_id=owner.id
            )
            for frame, task in tasks.items():
                item = await db.get(DatasetItem, task.dataset_item_id)
                key = source_keys[frame]
                item.file_path = key
                item.file_size = len(pcd)
                item.content_hash = hashlib.sha256(pcd).hexdigest()
                task.file_path = key
            owner_id = owner.id
            project_id = project.id
            scene_id = scene.id

        bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
        storage_scope, destroy_storage_scope = _create_quality_storage_scope(
            resources, owner_s3, source_keys
        )
        for key in source_keys:
            owner_s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=pcd,
                ContentType="application/octet-stream",
            )
        storage_evidence = {
            "bucket": bucket,
            "keys": list(source_keys),
            "allowed_action": "s3:GetObject",
            "allowed_resource_count": len(storage_scope["quality_policy_resources"]),
            "cleanup_owner": "outer verifier-owned bucket cleanup",
        }

        outage = {"checked": False}
        async with httpx.AsyncClient(
            base_url=stack.api_origin,
            trust_env=False,
            timeout=40,
        ) as client:
            headers = await _login(client, maker, owner_id)
            request_body = {
                "scope": "scene_ids",
                "scene_ids": [str(scene_id)],
            }
            endpoint = f"/api/v1/projects/{project_id}/point-cloud-quality/runs"
            if check_outage:
                await stack.stop_service("jobs-queue")
                queue_stopped = True
                try:
                    failed = await client.post(
                        endpoint, headers=headers, json=request_body
                    )
                    assert failed.status_code == 503, stack.redact(failed.text)
                    detail = failed.json()["detail"]
                    assert detail["reason"] == "point_cloud_quality_dispatch_failed"
                    failed_run_id = uuid.UUID(detail["run_id"])
                    failed_job_id = uuid.UUID(detail["async_job_id"])
                    async with maker() as db:
                        failed_run = await db.get(PointCloudQualityRun, failed_run_id)
                        failed_job = await db.get(AsyncJob, failed_job_id)
                        assert failed_run is not None and failed_job is not None
                        assert failed_run.status == "failed"
                        assert failed_job.status == AsyncJobStatus.FAILED.value
                        failed_execution_id = failed_job.celery_task_id
                        assert failed_execution_id
                        known_task_ids.add(failed_execution_id)
                    assert _broker_state(sender, "media") == {
                        "queue_size": 0,
                        "unacknowledged": 0,
                    }, (
                        "quality API fell back to the embedded broker "
                        "during owner outage"
                    )
                    outage = {
                        "checked": True,
                        "http_status": failed.status_code,
                        "run_id": str(failed_run_id),
                        "job_id": str(failed_job_id),
                        "run_status": failed_run.status,
                        "job_status": failed_job.status,
                        "execution_id": failed_execution_id,
                        "broker_empty": True,
                    }
                finally:
                    await stack.start_service("jobs-queue")
                    queue_stopped = False

            accepted = await client.post(endpoint, headers=headers, json=request_body)
            assert accepted.status_code == 202, stack.redact(accepted.text)
            accepted_body = accepted.json()
            run_id = uuid.UUID(accepted_body["id"])
            job_id = uuid.UUID(accepted_body["async_job_id"])
            async with maker() as db:
                run = await db.get(PointCloudQualityRun, run_id)
                job = await db.get(AsyncJob, job_id)
                assert run is not None and job is not None
                assert run.status == "pending"
                assert job.status == AsyncJobStatus.PENDING.value
                assert run.async_job_id == job.id
                execution_id = job.celery_task_id
                assert execution_id and str(uuid.UUID(execution_id)) == execution_id
            known_task_ids.add(execution_id)
            assert _broker_state(sender, "media") == {
                "queue_size": 1,
                "unacknowledged": 0,
            }
            accepted_envelopes = _media_envelopes(sender, "media")
            assert len(accepted_envelopes) == 1
            (
                task,
                envelope_id,
                args,
                kwargs,
                exchange,
                routing_key,
            ) = _unpack_task_envelope(accepted_envelopes[0][2])
            assert task == "app.workers.point_cloud_quality.run"
            assert envelope_id == execution_id
            assert args == [str(run_id)] and kwargs == {}
            assert exchange == "" and routing_key == "media"

            reused = await client.post(endpoint, headers=headers, json=request_body)
            assert reused.status_code == 202, stack.redact(reused.text)
            assert reused.json()["id"] == str(run_id)
            assert reused.json()["async_job_id"] == str(job_id)
            assert _broker_state(sender, "media") == {
                "queue_size": 1,
                "unacknowledged": 0,
            }, "singleflight replay published another quality task"

            from urllib.parse import urlsplit

            minio_origin = (
                f"{'https' if settings.minio_use_ssl else 'http'}://"
                f"{settings.minio_endpoint}"
            )
            minio_parts = urlsplit(minio_origin)
            worker = WorkspaceCeleryWorker(
                test_engine,
                Path(directory) / "quality-media-worker",
                backend_token=uuid.uuid4().hex,
                object_store_endpoint=minio_origin,
                object_store_bucket=resources["buckets"]["MINIO_BUCKET"],
                object_store_use_ssl=minio_parts.scheme == "https",
                object_store_access_key=storage_scope["access_key"],
                object_store_secret_key=storage_scope["secret_key"],
                object_store_buckets=resources["buckets"],
                queue_name="media",
            )
            worker.start()
            await _wait_worker_ready(sender, worker)
            runtime = worker.assert_runtime()
            worker_runtime = {
                "container_id": runtime["container_id"],
                "image_id": runtime["image_id"],
                "user": runtime["user"],
                "read_only": runtime["read_only"],
                "limits": runtime["limits"],
                "database_role": "identity_api_consumer",
                "database": str(test_engine.url.database),
                "database_evidence": (
                    "validated configuration; task-session liveness not observed"
                ),
            }
            worker_signatures = _worker_quality_task_signatures(worker)
            active_queues = await asyncio.to_thread(
                sender.control.inspect(
                    timeout=2, destination=[worker.node_name]
                ).active_queues
            )
            assert active_queues and {
                row["name"] for row in active_queues[worker.node_name]
            } == {"media"}
            result = sender.AsyncResult(execution_id)
            assert (
                await asyncio.to_thread(result.get, timeout=120, propagate=False)
                is None
            )
            ack = await _wait_broker_ack(sender, worker, execution_id)
            completed_run, completed_job = await _wait_quality_job(
                test_engine, run_id, job_id
            )
            assert completed_run.status == "completed"
            assert completed_job.status == AsyncJobStatus.COMPLETED.value
            assert completed_job.celery_task_id == execution_id

            async with maker() as db:
                findings = sorted(
                    set(
                        (
                            await db.execute(
                                select(PointCloudQualityIssue.code).where(
                                    PointCloudQualityIssue.last_seen_run_id == run_id
                                )
                            )
                        ).scalars()
                    )
                )
            assert {"track_gap", "temporal_jump", "low_point_count"}.issubset(findings)
            issue_response = await client.get(
                f"/api/v1/projects/{project_id}/point-cloud-quality/issues",
                params={"scene_id": str(scene_id), "limit": 200},
                headers=headers,
            )
            assert issue_response.status_code == 200, stack.redact(issue_response.text)
            api_findings = {item["code"] for item in issue_response.json()["items"]}
            assert {"track_gap", "temporal_jump", "low_point_count"}.issubset(
                api_findings
            )

        return {
            "owner_id": str(owner_id),
            "project_id": str(project_id),
            "scene_id": str(scene_id),
            "run_id": str(run_id),
            "job_id": str(job_id),
            "execution_id": execution_id,
            "findings": findings,
            "api_findings": sorted(api_findings),
            "transport_probes": probe_records,
            "transport_probe_count": len(probe_records),
            "worker_signatures": worker_signatures,
            "worker_active_queues": ["media"],
            "worker_runtime": worker_runtime,
            "worker_ack": ack,
            "singleflight_reused": True,
            "outage": outage,
            "storage_read_scope": storage_evidence,
        }, headers
    finally:
        try:
            if queue_stopped:
                await stack.start_service("jobs-queue")
        finally:
            try:
                if worker is not None:
                    worker.stop()
            finally:
                if storage_scope is not None and destroy_storage_scope is not None:
                    destroy_storage_scope(storage_scope)
