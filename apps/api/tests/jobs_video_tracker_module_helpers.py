"""Cold API-to-Jobs queue acceptance for the existing video tracker worker."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import secrets
import uuid
from urllib.parse import urlsplit

from celery import Celery
from cryptography.fernet import Fernet
import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.identity_module_assembly_helpers import IdentityModuleStack

_VIDEO_TRACKER_TASK_NAME = "app.workers.video_tracker.run_video_tracker_job"


class _MediaBoundIdentityModuleStack(IdentityModuleStack):
    """Add the existing Media URL/token to the source-bound API test stack."""

    def __init__(self, *stack_args, media, **stack_kwargs):
        self._tracker_media_url = ""
        self._tracker_media_token = ""
        self._tracker_media_tokens: set[str] = set()
        self.bind_media(media)
        super().__init__(*stack_args, **stack_kwargs)

    def bind_media(self, media) -> None:
        from tests.test_workspace_video_tracker_worker_live import _host_forward_url

        self._tracker_media_url = _host_forward_url(media.origin)
        self._tracker_media_token = media.token
        self._tracker_media_tokens.add(media.token)

    def _api_environment(self) -> dict[str, str]:
        values = super()._api_environment()
        values.update(
            {
                "ASSETS_MEDIA_URL": self._tracker_media_url,
                "ASSETS_MEDIA_TOKEN": self._tracker_media_token,
            }
        )
        return values

    def redact(self, value: str) -> str:
        redacted = super().redact(value)
        for token in self._tracker_media_tokens:
            redacted = redacted.replace(token, "[REDACTED]")
        return redacted


def _unpack_tracker_envelope(envelope: dict) -> dict:
    """Normalize the existing Celery envelope decoder for the tracker lane."""
    from tests.jobs_quality_module_helpers import _unpack_task_envelope

    task, execution_id, args, kwargs, exchange, routing_key = _unpack_task_envelope(
        envelope
    )
    return {
        "task": task,
        "execution_id": execution_id,
        "args": args,
        "kwargs": kwargs,
        "exchange": exchange,
        "routing_key": routing_key,
    }


def _tracker_envelopes(sender, queue: str) -> list[dict]:
    from tests.jobs_quality_module_helpers import _media_envelopes

    return [
        _unpack_tracker_envelope(envelope)
        for _key, _raw, envelope in _media_envelopes(sender, queue)
    ]


def _uuid_text(value, *, name: str) -> str:
    assert isinstance(value, str) and value
    parsed = uuid.UUID(value)
    assert str(parsed) == value, f"{name} is not a canonical UUID"
    return value


def _assert_tracker_envelope(message: dict, job_id: str) -> str:
    assert message["task"] == _VIDEO_TRACKER_TASK_NAME
    assert message["args"] == [job_id]
    assert message["kwargs"] == {}
    assert message["routing_key"] == "gpu"
    return _uuid_text(message["execution_id"], name="tracker execution ID")


def _tracker_request() -> dict:
    return {
        "from_frame": 0,
        "to_frame": 5,
        "model_key": "sam2_video",
        "direction": "forward",
        "target_class_name": "car",
        "target_tool_unit_id": "bbox",
        "text": "car",
        "output_geometry": "bbox",
    }


async def _seed_tracker_api_world(
    media,
    video: bytes,
    *,
    backend_url: str,
    backend_token: str,
    label: str,
) -> dict:
    from app.db.models.dataset import DatasetItem
    from app.db.models.ml_backend_registry import ProjectMLBackendPool
    from app.db.models.task import Task
    from tests.conftest import create_registry_with_pool
    from tests.factory import build_tool_bindings, create_membership
    from tests.test_video_input_authorities_native import _seed_video_task
    from tests.test_workspace_video_tracker_worker_live import _make_actor

    key = f"jobs-video-tracker/{label}-{uuid.uuid4()}.mp4"
    media.owner.put_object(
        Bucket=media.buckets["MINIO_DATASETS_BUCKET"], Key=key, Body=video
    )
    async with media.sessions.begin() as db:
        owner_id, project, _dataset_id, item_id, task_id = await _seed_video_task(
            db, status="in_progress"
        )
        suffix = f"{label}-{uuid.uuid4().hex[:12]}"
        actor = await _make_actor(db, suffix)
        membership = await create_membership(
            db,
            project_id=project.id,
            user_id=actor.id,
            role="annotator",
            assigned_by=owner_id,
        )
        project.tool_bindings = build_tool_bindings(["car"], unit="bbox")
        item = await db.get(DatasetItem, item_id)
        assert item is not None
        item.file_path = key
        item.file_size = len(video)
        item.content_hash = sha256(video).hexdigest()
        item.width, item.height = 80, 48
        item.metadata_ = {
            "video": {
                "fps": 6,
                "frame_count": 6,
                "duration_ms": 1000,
                "width": 80,
                "height": 48,
            }
        }
        task = await db.get(Task, task_id)
        assert task is not None
        task.file_path = ""
        task.assignee_id = actor.id
        task.assignee_is_override = True
        task.assigned_at = datetime.now(timezone.utc)
        registry, pool = await create_registry_with_pool(
            db,
            name=f"jobs-video-tracker-{suffix}",
            url=backend_url,
            state="connected",
            is_interactive=True,
            enabled_pool=True,
            auth_method="token",
            auth_token=backend_token,
            extra_params={"device": "cpu", "max_concurrency": 2},
            health_meta={"capabilities": {"supported_trackers": ["sam2_video"]}},
        )
        project.ml_backend_pool_id = pool.id
        db.add(
            ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True)
        )
        return {
            "owner_id": owner_id,
            "actor_id": actor.id,
            "membership_id": membership.id,
            "project_id": project.id,
            "task_id": task_id,
            "dataset_item_id": item_id,
            "registry_id": registry.id,
            "backend_token": backend_token,
        }


async def _prepare_tracker_metadata(media, directory: Path, world: dict) -> dict:
    """Materialize the owner generation required by the tracker HTTP entry point."""
    from aap_assets_media import contract as media_contract
    from aap_assets_media.client import MediaClient
    from tests.test_assets_media_live import (
        _process_log,
        _runtime_database_url,
        _runtime_environment,
        _start_process,
        _wait_process_exit,
    )
    import sys

    directory.mkdir(parents=True, exist_ok=True)
    client = MediaClient(media.origin, media.token)
    request = media_contract.ReadSourceRequest(
        actor_id=world["actor_id"],
        task_id=world["task_id"],
        dataset_item_id=world["dataset_item_id"],
    )
    before = (await client.source_reference(request)).source
    accepted = await client.call(
        "schedule_video_media",
        media_contract.ScheduleWorkRequest(
            operation_id=None,
            dataset_item_id=world["dataset_item_id"],
            spec=media_contract.MetadataSpec(
                expected_source_snapshot=before.source_snapshot
            ),
        ),
    )
    assert accepted.work.kind == "video_metadata"
    environment = _runtime_environment(
        tmp_path=directory,
        role="worker",
        database_url=_runtime_database_url(media.worker_url),
        endpoint=media.endpoint,
        public_endpoint=media.endpoint,
        datasets_bucket=media.buckets["MINIO_DATASETS_BUCKET"],
        cache_bucket=media.buckets["MINIO_MEDIA_CACHE_BUCKET"],
        access_key=media.worker_access,
        secret_key=media.worker_secret,
    )
    log = directory / "metadata-worker.log"
    process, stream = _start_process(
        [
            sys.executable,
            "-m",
            "aap_assets_media_runtime.worker",
            "--once",
            "--job-id",
            str(accepted.work.job_id),
        ],
        environment,
        log,
    )
    media.processes.append(process)
    media.streams.append(stream)
    await _wait_process_exit(
        process, timeout=60, message="tracker fixture metadata worker did not finish"
    )
    assert process.returncode == 0, _process_log(log)
    ready = (await client.source_reference(request)).source
    assert not ready.source_drifted
    assert ready.materialized_generation == ready.source_generation
    assert ready.video_metadata["frame_count"] == 6
    return {
        "metadata_job_id": str(accepted.work.job_id),
        "source_generation": ready.source_generation,
        "materialized_generation": ready.materialized_generation,
        "frame_count": ready.video_metadata["frame_count"],
    }


async def _video_tracker_job(engine, job_id: str) -> dict:
    from app.db.models.video_tracker_job import VideoTrackerJob

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        row = await db.get(VideoTrackerJob, uuid.UUID(job_id))
        assert row is not None, f"tracker job {job_id} was not persisted"
        return {
            "id": str(row.id),
            "status": row.status,
            "celery_task_id": row.celery_task_id,
            "error_message": row.error_message,
            "staged_result": row.staged_result,
            "task_id": str(row.task_id),
        }


async def _async_job(engine, job_id: str) -> dict:
    from app.db.models.async_job import AsyncJob

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        row = await db.get(AsyncJob, uuid.UUID(job_id))
        assert row is not None, f"tracker summary {job_id} was not persisted"
        return {
            "id": str(row.id),
            "status": row.status,
            "celery_task_id": row.celery_task_id,
        }


async def _cancel_audit_count(engine, job_id: str) -> int:
    from app.db.models.audit_log import AuditLog
    from app.services.audit import AuditAction

    async with engine.connect() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == AuditAction.VIDEO_TRACKER_JOB_CANCEL,
                AuditLog.target_id == job_id,
            )
        )
    return int(count or 0)


async def _wait_cancel_audit_count(engine, job_id: str, expected: int) -> int:
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        observed = await _cancel_audit_count(engine, job_id)
        if observed >= expected:
            assert observed == expected, "tracker cancellation audit was duplicated"
            return observed
        await asyncio.sleep(0.1)
    raise AssertionError(f"tracker cancellation audit count did not reach {expected}")


async def _active_queues(sender: Celery, worker) -> list[dict]:
    inspect = sender.control.inspect(timeout=2, destination=[worker.node_name])
    observed = await asyncio.to_thread(inspect.active_queues)
    assert observed and worker.node_name in observed
    return observed[worker.node_name]


async def _pause_gpu_consumer(sender: Celery, worker) -> None:
    reply = await asyncio.to_thread(
        sender.control.cancel_consumer,
        "gpu",
        destination=[worker.node_name],
        reply=True,
    )
    assert reply, "GPU worker did not acknowledge consumer pause"
    assert await _active_queues(sender, worker) == []


async def _resume_gpu_consumer(sender: Celery, worker) -> None:
    reply = await asyncio.to_thread(
        sender.control.add_consumer,
        "gpu",
        destination=[worker.node_name],
        reply=True,
    )
    assert reply, "GPU worker did not acknowledge consumer resume"
    assert {row["name"] for row in await _active_queues(sender, worker)} == {"gpu"}


async def _wait_task_success(sender: Celery, worker, execution_id: str) -> dict:
    delivery = sender.AsyncResult(execution_id)
    result = await asyncio.to_thread(delivery.get, timeout=180, propagate=False)
    assert delivery.state == "SUCCESS", repr(result)
    from tests.test_workspace_worker_assembly_live import _wait_broker_ack

    ack = await _wait_broker_ack(sender, worker, execution_id)
    return {"task_id": execution_id, "ack": ack, "result_type": type(result).__name__}


async def _release_cpu_gates(peer) -> None:
    for entered, release in zip(peer.entered, peer.release, strict=True):
        await asyncio.wait_for(entered.wait(), timeout=90)
        release.set()


async def _submit_tracker(
    client: httpx.AsyncClient, headers: dict, world: dict, *, redact=lambda value: value
):
    response = await client.post(
        f"/api/v1/tasks/{world['task_id']}/video:track",
        json={**_tracker_request(), "segment_id": world["segment_id"]},
        headers=headers,
    )
    assert response.status_code == 202, redact(response.text)
    body = response.json()
    assert body.get("task_id") == str(world["task_id"])
    return body


async def _claim_tracker_segment(client, headers, world, *, redact) -> dict:
    path = f"/api/v1/tasks/{world['task_id']}/video/segments"
    response = await client.get(path, headers=headers)
    assert response.status_code == 200, redact(response.text)
    segments = [
        row
        for row in response.json()["segments"]
        if row["work_start_frame"] <= 0 and row["work_end_frame"] >= 5
    ]
    assert len(segments) == 1
    segment_id = segments[0]["id"]
    claimed = await client.post(path + f"/{segment_id}:claim", headers=headers)
    assert claimed.status_code == 200, redact(claimed.text)
    body = claimed.json()
    assert body["locked_by"] == str(world["actor_id"])
    assert body["status"] == "locked" and body["lock_expires_at"]
    world["segment_id"] = segment_id
    return {
        "segment_id": segment_id,
        "locked_by": body["locked_by"],
        "lock_expires_at": body["lock_expires_at"],
    }


async def _exercise_api_tracker_phase(
    *,
    phase: str,
    test_engine,
    media,
    world: dict,
    peer,
    stack,
    client: httpx.AsyncClient,
    headers: dict[str, str],
    sender: Celery,
    tmp_path: Path,
    guide_bucket: str,
    guide_scope: dict,
    minio_origin: str,
    minio_scheme: str,
    queue_outage: bool,
    workers: list,
    record_property,
) -> dict:
    from app.db.models.async_job import AsyncJobStatus
    from app.db.models.video_tracker_job import VideoTrackerJobStatus
    from aap_jobs.queue_contract import load_queue_configuration
    from tests.test_jobs_module_assembly_live import (
        _assert_owned_broker_target,
    )
    from tests.test_workspace_video_tracker_worker_live import (
        _TASK_NAME,
        _VideoTrackerCeleryWorker,
        _assert_terminal_execution,
    )
    from tests.test_workspace_worker_assembly_live import (
        _broker_state,
        _wait_worker_ready,
    )

    _assert_owned_broker_target(sender, "gpu")
    assert load_queue_configuration()["task_routes"][_TASK_NAME] == {"queue": "gpu"}
    worker = _VideoTrackerCeleryWorker(
        test_engine,
        tmp_path / f"jobs-video-tracker-worker-{phase}",
        backend_token=world["backend_token"],
        object_store_endpoint=minio_origin,
        object_store_bucket=guide_bucket,
        object_store_use_ssl=minio_scheme == "https",
        object_store_access_key=guide_scope["access_key"],
        object_store_secret_key=guide_scope["secret_key"],
        media_url=media.origin,
        media_token=media.token,
    )
    workers.append(worker)
    worker_started = False
    try:
        await asyncio.to_thread(worker.start)
        worker_started = True
        await _wait_worker_ready(sender, worker)
        worker.assert_private_network()
        signature = worker.task_signature(_TASK_NAME)
        assert signature == {"task": _TASK_NAME, "parameters": ["job_id"]}
        delivery_policy = worker.task_delivery_policy(_TASK_NAME)
        assert delivery_policy == {"acks_late": True, "reject_on_worker_lost": True}
        assert {row["name"] for row in await _active_queues(sender, worker)} == {"gpu"}
        bootstrap = await asyncio.to_thread(
            worker.bootstrap_tracker_routes, {phase: world}
        )
        assert bootstrap == [
            {"registry_id": str(world["registry_id"]), "phase": "completed"}
        ]
        assert peer.controller.phase == "bound"
        record_property(
            f"video_tracker_segment_{phase}",
            await _claim_tracker_segment(client, headers, world, redact=stack.redact),
        )

        await _pause_gpu_consumer(sender, worker)
        if queue_outage:
            await stack.stop_service("jobs-queue")
            failed = await _submit_tracker(client, headers, world, redact=stack.redact)
            assert failed["status"] == VideoTrackerJobStatus.FAILED.value
            assert failed.get("celery_task_id") is None
            assert failed.get("error_message") == "tracker_enqueue_failed"
            failed_row = await _video_tracker_job(test_engine, str(failed["id"]))
            assert failed_row["status"] == VideoTrackerJobStatus.FAILED.value
            assert failed_row["celery_task_id"] is None
            assert _broker_state(sender, "gpu") == {
                "queue_size": 0,
                "unacknowledged": 0,
            }
            assert _broker_state(sender, "default") == {
                "queue_size": 0,
                "unacknowledged": 0,
            }
            assert _tracker_envelopes(sender, "gpu") == []
            await stack.start_service("jobs-queue")
            queue_failure = {
                "http_status": 202,
                "job_id": failed["id"],
                "status": failed["status"],
                "execution_id": None,
                "gpu_queue_empty": True,
                "default_queue_empty": True,
            }
        else:
            queue_failure = None
        success = await _submit_tracker(client, headers, world, redact=stack.redact)
        success_job_id = _uuid_text(success["id"], name="tracker job ID")
        success_execution_id = _uuid_text(
            success["celery_task_id"], name="tracker execution ID"
        )
        assert success["status"] == VideoTrackerJobStatus.QUEUED.value
        persisted_success = await _video_tracker_job(test_engine, success_job_id)
        assert persisted_success["status"] == VideoTrackerJobStatus.QUEUED.value
        assert persisted_success["celery_task_id"] == success_execution_id
        from tests.test_tracker_input_live import _prepared

        input_tmp = tmp_path / f"jobs-video-tracker-input-{phase}"
        input_tmp.mkdir(parents=True, exist_ok=True)
        _input_client, _input_request, ready_input = await _prepared(
            media,
            input_tmp,
            world["actor_id"],
            world["task_id"],
            success_job_id,
        )
        assert ready_input.status == "ready" and ready_input.video.frame_count == 6
        success_messages = _tracker_envelopes(sender, "gpu")
        assert len(success_messages) == 1
        assert _assert_tracker_envelope(success_messages[0], success_job_id) == (
            success_execution_id
        )
        assert _broker_state(sender, "gpu") == {
            "queue_size": 1,
            "unacknowledged": 0,
        }
        await _resume_gpu_consumer(sender, worker)
        await _release_cpu_gates(peer)
        success_delivery = await _wait_task_success(
            sender, worker, success_execution_id
        )
        completed_success = await _video_tracker_job(test_engine, success_job_id)
        assert completed_success["status"] == VideoTrackerJobStatus.PENDING_REVIEW.value
        assert completed_success["celery_task_id"] == success_execution_id
        frames = [
            row["frame_index"]
            for row in (completed_success["staged_result"] or {}).get("results", [])
        ]
        assert frames == list(range(6))
        success_summary = await _async_job(test_engine, success_job_id)
        assert success_summary["status"] == AsyncJobStatus.COMPLETED.value
        assert success_summary["celery_task_id"] == success_execution_id
        success_retirement = await _assert_terminal_execution(
            test_engine, success_job_id, "completed", require_scope=True
        )
        assert [call["from_frame"] for call in peer.model_windows] == [0, 2, 4]
        success_input_id = str(ready_input.input_id)

        await _pause_gpu_consumer(sender, worker)
        cancel_created = await _submit_tracker(
            client, headers, world, redact=stack.redact
        )
        cancel_job_id = _uuid_text(cancel_created["id"], name="cancel job ID")
        original_execution_id = _uuid_text(
            cancel_created["celery_task_id"], name="cancel execution ID"
        )
        assert cancel_created["status"] == VideoTrackerJobStatus.QUEUED.value
        original_messages = _tracker_envelopes(sender, "gpu")
        assert len(original_messages) == 1
        assert _assert_tracker_envelope(original_messages[0], cancel_job_id) == (
            original_execution_id
        )
        assert _broker_state(sender, "gpu") == {
            "queue_size": 1,
            "unacknowledged": 0,
        }
        await stack.stop_service("jobs-queue")
        unavailable = await client.delete(
            f"/api/v1/video-tracker-jobs/{cancel_job_id}", headers=headers
        )
        assert unavailable.status_code == 503, stack.redact(unavailable.text)
        assert unavailable.json()["detail"] == {
            "reason": "tracker_retirement_redrive_unavailable",
            "job_id": cancel_job_id,
            "retryable": True,
        }
        cancelled_during_outage = await _video_tracker_job(test_engine, cancel_job_id)
        assert (
            cancelled_during_outage["status"] == VideoTrackerJobStatus.CANCELLED.value
        )
        audit_after_outage = await _wait_cancel_audit_count(
            test_engine, cancel_job_id, 1
        )
        assert _tracker_envelopes(sender, "gpu") == original_messages
        assert _broker_state(sender, "gpu") == {
            "queue_size": 1,
            "unacknowledged": 0,
        }
        assert _broker_state(sender, "default") == {
            "queue_size": 0,
            "unacknowledged": 0,
        }

        await stack.start_service("jobs-queue")
        redrive = await client.delete(
            f"/api/v1/video-tracker-jobs/{cancel_job_id}", headers=headers
        )
        assert redrive.status_code == 200, stack.redact(redrive.text)
        assert redrive.json()["status"] == VideoTrackerJobStatus.CANCELLED.value
        assert await _wait_cancel_audit_count(test_engine, cancel_job_id, 1) == 1
        cancelled_after_retry = await _video_tracker_job(test_engine, cancel_job_id)
        assert cancelled_after_retry["status"] == VideoTrackerJobStatus.CANCELLED.value
        assert cancelled_after_retry["celery_task_id"] == original_execution_id
        redrive_messages = _tracker_envelopes(sender, "gpu")
        assert len(redrive_messages) == 2
        observed_execution_ids = [
            _assert_tracker_envelope(message, cancel_job_id)
            for message in redrive_messages
        ]
        assert original_execution_id in observed_execution_ids
        redrive_execution_ids = [
            task_id
            for task_id in observed_execution_ids
            if task_id != original_execution_id
        ]
        assert len(redrive_execution_ids) == 1
        redrive_execution_id = redrive_execution_ids[0]
        assert _broker_state(sender, "gpu") == {
            "queue_size": 2,
            "unacknowledged": 0,
        }
        await _resume_gpu_consumer(sender, worker)
        cancel_deliveries = [
            await _wait_task_success(sender, worker, execution_id)
            for execution_id in sorted(observed_execution_ids)
        ]
        cancelled_after_delivery = await _video_tracker_job(test_engine, cancel_job_id)
        assert (
            cancelled_after_delivery["status"] == VideoTrackerJobStatus.CANCELLED.value
        )
        assert cancelled_after_delivery["staged_result"] is None
        cancel_summary = await _async_job(test_engine, cancel_job_id)
        assert cancel_summary["status"] == AsyncJobStatus.CANCELLED.value
        assert cancel_summary["celery_task_id"] == original_execution_id
        assert len(peer.model_windows) == 3, "cancelled redrives reached the backend"
        assert await _wait_cancel_audit_count(test_engine, cancel_job_id, 1) == 1
        assert _broker_state(sender, "gpu") == {
            "queue_size": 0,
            "unacknowledged": 0,
        }
        record = {
            "queue_failure": queue_failure,
            "success": {
                "job_id": success_job_id,
                "execution_id": success_execution_id,
                "status": completed_success["status"],
                "input_id": success_input_id,
                "frames": frames,
                "delivery": success_delivery,
                "retirement": success_retirement,
            },
            "cancellation": {
                "job_id": cancel_job_id,
                "original_execution_id": original_execution_id,
                "redrive_execution_id": redrive_execution_id,
                "persisted_execution_id_unchanged": True,
                "http_status_during_outage": unavailable.status_code,
                "http_status_after_retry": redrive.status_code,
                "audit_count": audit_after_outage,
                "delivery_acks": cancel_deliveries,
                "status": cancelled_after_delivery["status"],
                "backend_calls_unchanged": True,
            },
            "worker": {
                "signature": signature,
                "delivery_policy": delivery_policy,
                "active_queues": ["gpu"],
                "runtime": worker.assert_runtime(),
                "private_network": True,
            },
        }
        record_property(f"video_tracker_jobs_queue_{phase}", record)
        return {
            "actor_id": str(world["actor_id"]),
            "task_id": str(world["task_id"]),
            "success_job_id": success_job_id,
            "success_execution_id": success_execution_id,
            "success_input_id": success_input_id,
            "cancel_job_id": cancel_job_id,
            "cancel_execution_ids": sorted(observed_execution_ids),
            "queue_failure_job_id": queue_failure["job_id"] if queue_failure else None,
            "queue_failure": queue_failure,
            "success_status": completed_success["status"],
            "cancel_status": cancelled_after_delivery["status"],
        }
    finally:
        if (
            worker_started
            or worker.container_id is not None
            or worker.network_id is not None
        ):
            await asyncio.to_thread(worker.stop)
            workers.remove(worker)


def _media_runtime_stack(
    *,
    test_engine,
    tmp_path: Path,
    media,
    login_url: str,
    publisher_url: str,
    smtp_port: int,
):
    secret, api_secret, control, queue_token = [
        secrets.token_urlsafe(32) for _ in range(4)
    ]
    assert len({secret, api_secret, control, queue_token}) == 4
    queue_image, queue_identity, queue_revision = (
        os.environ[name]
        for name in (
            "AAP_JOBS_QUEUE_RUNTIME_IMAGE",
            "AAP_JOBS_QUEUE_RUNTIME_IDENTITY",
            "AAP_JOBS_QUEUE_RUNTIME_SOURCE_REVISION",
        )
    )
    return _MediaBoundIdentityModuleStack(
        test_engine,
        tmp_path,
        media=media,
        secret=secret,
        api_secret=api_secret,
        control=control,
        mail_key=Fernet.generate_key().decode(),
        login_redis_url=login_url,
        publisher_redis_url=publisher_url,
        smtp_port=smtp_port,
        jobs_queue_image=queue_image,
        jobs_queue_identity=queue_identity,
        jobs_queue_source_revision=queue_revision,
        jobs_queue_token=queue_token,
    )


async def _assert_restored_tracker_readback(
    *,
    stack,
    media,
    source_phase: dict,
    source_headers: dict[str, str],
) -> dict:
    from aap_assets_video_inputs import tracker as tracker_contract
    from aap_assets_video_inputs.tracker_client import TrackerVideoInputClient
    from app.db.models.async_job import AsyncJobStatus
    from app.db.models.video_tracker_job import VideoTrackerJobStatus

    async with httpx.AsyncClient(
        base_url=stack.api_origin, trust_env=False, timeout=40
    ) as client:
        old_tracker = await client.get(
            f"/api/v1/video-tracker-jobs/{source_phase['success_job_id']}",
            headers=source_headers,
        )
        assert old_tracker.status_code == 200, stack.redact(old_tracker.text)
        old_body = old_tracker.json()
        assert old_body["status"] == VideoTrackerJobStatus.PENDING_REVIEW.value
        assert old_body["celery_task_id"] == source_phase["success_execution_id"]
        old_async_job = await client.get(
            f"/api/v1/async-jobs/{source_phase['success_job_id']}",
            headers=source_headers,
        )
        assert old_async_job.status_code == 200, stack.redact(old_async_job.text)
        assert old_async_job.json()["status"] == AsyncJobStatus.COMPLETED.value
        assert (
            old_async_job.json()["celery_task_id"]
            == source_phase["success_execution_id"]
        )

    media_client = TrackerVideoInputClient(media.origin, media.token, client=media.http)
    scope = tracker_contract.TrackerInputReadRequest(
        actor_id=uuid.UUID(source_phase["actor_id"]),
        tracker_job_id=uuid.UUID(source_phase["success_job_id"]),
        task_id=uuid.UUID(source_phase["task_id"]),
    )
    old_input = await media_client.input(scope)
    assert old_input.status == "ready" and old_input.video.frame_count == 6
    assert str(old_input.input_id) == source_phase["success_input_id"]
    return {
        "tracker_job_id": source_phase["success_job_id"],
        "execution_id": source_phase["success_execution_id"],
        "tracker_status": old_body["status"],
        "async_job_status": old_async_job.json()["status"],
        "input_id": str(old_input.input_id),
        "input_status": old_input.status,
    }


async def exercise_video_tracker_queue_and_restore(
    test_engine,
    tmp_path,
    record_property,
) -> dict:
    """Exercise API dispatch, worker delivery, cancel redrive and PG restore."""
    from tests.project_guide_asset_owner_helpers import (
        assert_workspace_identity,
        create_guide_storage_scope,
        endpoint_url,
    )
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres
    from tests.test_identity_module_assembly_live import _restored_database
    from tests.test_identity_recovery import _postgres_tool
    from tests.identity_recovery_live_helpers import SMTPInbox
    from tests.test_identity_sessions_live import _login_redis
    from tests.test_jobs_module_assembly_live import (
        _assert_owned_broker_target,
        _login,
        _publisher_redis,
        _role_urls_for_database,
    )
    from tests.test_video_input_live import _video_bytes, _video_runtime
    from tests.test_workspace_video_tracker_worker_live import (
        _checkpoint_cpu_backend,
        _host_forward_url as host_forward_url,
        _reserve_port,
    )
    from tests.test_video_window_recovery_live import _bbox_artifact

    assert test_engine.url.database and str(test_engine.url.database).endswith("_test")
    _verify_postgres(test_engine.url)
    resources, guide_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    guide_scope = None
    destroy_guide_scope = None
    stack = None
    sender = None
    inbox = None
    clone_engine = None
    workers = []
    source_headers = None
    source_phase = None
    restored_phase = None
    readback = None

    @asynccontextmanager
    async def restored_database_with_cleanup():
        nonlocal clone_engine
        async with _restored_database(test_engine, snapshot) as restored_database:
            try:
                yield restored_database
            finally:
                try:
                    for worker in reversed(workers):
                        await asyncio.to_thread(worker.stop)
                    workers.clear()
                finally:
                    try:
                        if stack is not None:
                            await stack.stop()
                    finally:
                        if clone_engine is not None:
                            await clone_engine.dispose()
                            clone_engine = None

    try:
        snapshot = Path(tmp_path) / "jobs-video-tracker-module.dump"
        minio_origin = endpoint_url()
        minio_parts = urlsplit(minio_origin)
        assert minio_parts.scheme in {"http", "https"} and minio_parts.hostname
        minio_scheme = minio_parts.scheme
        video_dir = Path(tmp_path) / "video-source"
        video_dir.mkdir(parents=True, exist_ok=True)
        video = await asyncio.to_thread(_video_bytes, video_dir)
        artifact_bytes, artifact, weights_sha256 = _bbox_artifact()
        del artifact_bytes
        guide_scope, destroy_guide_scope = create_guide_storage_scope(
            resources,
            owner_s3,
            guide_bucket,
            f"outside-video-tracker/jobs-{uuid.uuid4()}.txt",
        )
        inbox = SMTPInbox()

        @asynccontextmanager
        async def redis_users_with_stack_cleanup():
            async with _login_redis() as (login_url, _login_owner):
                async with _publisher_redis() as (publisher_url, _publisher_owner):
                    try:
                        yield login_url, publisher_url
                    finally:
                        if stack is not None:
                            await stack.stop()

        async with redis_users_with_stack_cleanup() as (
            login_url,
            publisher_url,
        ):
            sender_url = os.environ.get("CELERY_BROKER_URL") or os.environ["REDIS_URL"]
            sender = Celery(
                "jobs-video-tracker-queue-acceptance",
                broker=sender_url,
                backend=sender_url,
            )
            sender.conf.update(
                task_serializer="json",
                result_serializer="json",
                accept_content=["json"],
            )
            _assert_owned_broker_target(sender, "gpu")
            _assert_owned_broker_target(sender, "default")

            source_media_dir = Path(tmp_path) / "video-media-source"
            source_media_dir.mkdir(parents=True, exist_ok=True)
            async with _video_runtime(test_engine, source_media_dir) as media:
                listener, backend_origin = _reserve_port()
                backend_token = secrets.token_urlsafe(24)
                world = await _seed_tracker_api_world(
                    media,
                    video,
                    backend_url=host_forward_url(backend_origin),
                    backend_token=backend_token,
                    label="source",
                )
                record_property(
                    "video_tracker_metadata_source",
                    await _prepare_tracker_metadata(
                        media, Path(tmp_path) / "metadata-source", world
                    ),
                )
                async with _checkpoint_cpu_backend(
                    listener,
                    registry_id=world["registry_id"],
                    control_token=backend_token,
                    storage_origin=media.endpoint,
                    expected_source_sha256=sha256(video).hexdigest(),
                    context_mode="none",
                    artifact=artifact,
                    weights_sha256=weights_sha256,
                ) as peer:
                    stack = _media_runtime_stack(
                        test_engine=test_engine,
                        tmp_path=Path(tmp_path) / "jobs-video-tracker-stack",
                        media=media,
                        login_url=login_url,
                        publisher_url=publisher_url,
                        smtp_port=inbox.port,
                    )
                    await stack.start()
                    queue_evidence = stack.inspection_evidence["jobs-queue"]
                    assert queue_evidence["user"] == "10005:10005"
                    assert queue_evidence["read_only"] is True
                    assert queue_evidence["secret_mount_targets"] == [
                        "/run/secrets/jobs_queue_broker_url",
                        "/run/secrets/jobs_queue_token",
                    ]
                    record_property("video_tracker_jobs_queue_runtime", queue_evidence)
                    async with httpx.AsyncClient(
                        base_url=stack.api_origin,
                        trust_env=False,
                        timeout=40,
                    ) as client:
                        maker = async_sessionmaker(test_engine, expire_on_commit=False)
                        source_headers = await _login(client, maker, world["actor_id"])
                        source_phase = await _exercise_api_tracker_phase(
                            phase="source",
                            test_engine=test_engine,
                            media=media,
                            world=world,
                            peer=peer,
                            stack=stack,
                            client=client,
                            headers=source_headers,
                            sender=sender,
                            tmp_path=Path(tmp_path),
                            guide_bucket=guide_bucket,
                            guide_scope=guide_scope,
                            minio_origin=minio_origin,
                            minio_scheme=minio_scheme,
                            queue_outage=True,
                            workers=workers,
                            record_property=record_property,
                        )

            _assert_owned_broker_target(sender, "gpu")
            snapshot.write_bytes(
                _postgres_tool(
                    test_engine,
                    [
                        "pg_dump",
                        "-U",
                        "owner",
                        "--format=custom",
                        "--dbname",
                        str(test_engine.url.database),
                    ],
                )
            )

            async with restored_database_with_cleanup() as restored_database:
                clone_engine = create_async_engine(
                    test_engine.url.set(database=restored_database),
                    hide_parameters=True,
                )
                with _role_urls_for_database(restored_database):
                    restored_media_dir = Path(tmp_path) / "video-media-restored"
                    restored_media_dir.mkdir(parents=True, exist_ok=True)
                    async with _video_runtime(
                        clone_engine, restored_media_dir
                    ) as clone_media:
                        stack.bind_media(clone_media)
                        await stack.switch_database(restored_database)
                        readback = await _assert_restored_tracker_readback(
                            stack=stack,
                            media=clone_media,
                            source_phase=source_phase,
                            source_headers=source_headers,
                        )
                        record_property(
                            "video_tracker_jobs_queue_restore_readback", readback
                        )

                        listener, backend_origin = _reserve_port()
                        backend_token = secrets.token_urlsafe(24)
                        restored_world = await _seed_tracker_api_world(
                            clone_media,
                            video,
                            backend_url=host_forward_url(backend_origin),
                            backend_token=backend_token,
                            label="restored",
                        )
                        record_property(
                            "video_tracker_metadata_restored",
                            await _prepare_tracker_metadata(
                                clone_media,
                                Path(tmp_path) / "metadata-restored",
                                restored_world,
                            ),
                        )
                        async with _checkpoint_cpu_backend(
                            listener,
                            registry_id=restored_world["registry_id"],
                            control_token=backend_token,
                            storage_origin=clone_media.endpoint,
                            expected_source_sha256=sha256(video).hexdigest(),
                            context_mode="none",
                            artifact=artifact,
                            weights_sha256=weights_sha256,
                        ) as restored_peer:
                            _assert_owned_broker_target(sender, "gpu")
                            async with httpx.AsyncClient(
                                base_url=stack.api_origin,
                                trust_env=False,
                                timeout=40,
                            ) as client:
                                clone_maker = async_sessionmaker(
                                    clone_engine, expire_on_commit=False
                                )
                                restored_headers = await _login(
                                    client,
                                    clone_maker,
                                    restored_world["actor_id"],
                                )
                                restored_phase = await _exercise_api_tracker_phase(
                                    phase="restored",
                                    test_engine=clone_engine,
                                    media=clone_media,
                                    world=restored_world,
                                    peer=restored_peer,
                                    stack=stack,
                                    client=client,
                                    headers=restored_headers,
                                    sender=sender,
                                    tmp_path=Path(tmp_path),
                                    guide_bucket=guide_bucket,
                                    guide_scope=guide_scope,
                                    minio_origin=minio_origin,
                                    minio_scheme=minio_scheme,
                                    queue_outage=False,
                                    workers=workers,
                                    record_property=record_property,
                                )
            clone_database = str(restored_database)
            record_property(
                "video_tracker_jobs_queue_full_restore",
                {
                    "database": clone_database,
                    "source_success_job_id": source_phase["success_job_id"],
                    "restored_success_job_id": restored_phase["success_job_id"],
                    "old_job_and_ready_input_read_back": True,
                    "source_cancel_job_id": source_phase["cancel_job_id"],
                    "restored_cancel_job_id": restored_phase["cancel_job_id"],
                },
            )
        return {
            "actor_id": source_phase["actor_id"],
            "source": source_phase,
            "restored": restored_phase,
            "readback": readback,
        }
    finally:
        try:
            for worker in reversed(workers):
                await asyncio.to_thread(worker.stop)
            workers.clear()
        finally:
            try:
                if stack is not None:
                    await stack.stop()
            finally:
                try:
                    if guide_scope is not None and destroy_guide_scope is not None:
                        destroy_guide_scope(guide_scope)
                finally:
                    try:
                        if clone_engine is not None:
                            await clone_engine.dispose()
                    finally:
                        try:
                            if sender is not None:
                                sender.close()
                        finally:
                            try:
                                if inbox is not None:
                                    inbox.close()
                            finally:
                                owner_s3.close()
