"""Local-only write fence for one verifier-owned, unbound video tracker fixture.

The evidence proves only that this exact worker and its observed write credentials
were fenced inside one disposable PostgreSQL container. The shared Celery broker
remains outside that claim; the test exercises a same-job redelivery with the stale
credentials and requires it to fail before it can write.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit
from uuid import uuid4, uuid5

from celery import Celery
import httpx
import pytest
from sqlalchemy import text

from aap_inference_contract.routing import RoutingScopeView
from aap_inference_contract.scope_retirement import make_scope_retirement_request
from app.config import settings
from app.services.ml_backend import MLBackendService
from app.services.ml_routing.authority import RoutingAuthority
from tests.legacy_video_fence_helpers import (
    LocalFenceRefused,
    build_local_fence_evidence,
    fence_disposable_database_logins,
    inspect_live_worker_credentials,
    owned_postgres_identity,
    owned_worker_identity,
    prove_s3_write_access,
    prove_s3_write_denied,
    prove_stale_worker_dsns_denied,
    run_fence_after_preflight,
    stop_fixture_workers,
    stop_owned_worker,
    validate_local_fence_preflight,
    worker_credential_evidence,
)
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    create_guide_storage_scope,
    endpoint_url,
)
from tests.test_video_input_live import _video_bytes, _video_runtime
from tests.test_inference_routing_control_recovery import _add_open_scope, _connect
from tests.test_video_window_recovery_live import (
    _bbox_artifact,
    _checkpoint_cpu_backend,
    _reserve_port,
)
from tests.test_workspace_video_tracker_worker_live import (
    _TASK_NAME,
    _VideoTrackerCeleryWorker,
    _prepared,
    _seed_tracker_world,
)
from tests.test_workspace_worker_assembly_live import (
    _broker_state,
    _delete_owned_queue,
    _wait_worker_ready,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_LEGACY_VIDEO_FENCE_LIVE") != "1",
        reason="requires the explicitly enabled owned local video-fence verifier",
    ),
]


class _FixtureQueueVideoTrackerWorker(_VideoTrackerCeleryWorker):
    """Use the accepted worker image on one random fixture-only queue."""

    def __init__(self, *args, queue_name: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.queue = queue_name or f"legacy_video_fence_{self.identity}"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value or "\n" in value or "\r" in value:
        raise LocalFenceRefused("verifier_artifact_identity_missing")
    return value


def _json_value(value):
    return json.loads(value) if isinstance(value, str) else value


def _worker(
    test_engine,
    tmp_path: Path,
    world: dict,
    media,
    guide_scope: dict,
    *,
    queue=None,
):
    media_scheme = urlsplit(endpoint_url()).scheme
    if media_scheme not in {"http", "https"}:
        raise LocalFenceRefused("media_endpoint_invalid")
    return _FixtureQueueVideoTrackerWorker(
        test_engine,
        tmp_path,
        backend_token=world["backend_token"],
        object_store_endpoint=endpoint_url(),
        object_store_bucket=guide_scope["bucket"],
        object_store_use_ssl=media_scheme == "https",
        object_store_access_key=guide_scope["access_key"],
        object_store_secret_key=guide_scope["secret_key"],
        media_url=media.origin,
        media_token=media.token,
        queue_name=queue,
    )


async def _seed_scope_only_route(test_engine, world):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    try:
        async with owner.transaction():
            pool_id = await owner.fetchval(
                "SELECT ml_backend_pool_id FROM public.projects WHERE id=$1",
                world["project_id"],
            )
            if pool_id is None:
                raise LocalFenceRefused("tracker_pool_missing")
            scope_id = uuid4()
            await _add_open_scope(
                owner,
                {"registry_id": world["registry_id"], "pool_id": pool_id},
                scope_id,
            )
            # Seed the pre-ledger shape using the existing owner fixture pattern.
            await owner.execute(
                "UPDATE aap_inference_routing_v1.scopes SET owner=$1,"
                "operation='tracker',project_id=$2 WHERE id=$3",
                f"tracker:{world['job_id']}",
                world["project_id"],
                scope_id,
            )
            raw = await owner.fetchval(
                "SELECT aap_inference_routing_v1._routing_scope_view($1,true)", scope_id
            )
            scope = RoutingScopeView.model_validate_json(raw)
    finally:
        await owner.close(timeout=5)
    if (
        scope.owner != f"tracker:{world['job_id']}"
        or scope.operation != "tracker"
        or scope.registry_id != world["registry_id"]
        or scope.capability is None
    ):
        raise LocalFenceRefused("legacy_route_scope_identity_mismatch")
    return scope


async def _retire_exact_scope(test_engine, scope):
    """Use the existing Inference scope-retirement protocol and real backend peer."""
    request = make_scope_retirement_request(
        uuid5(scope.id, "aap-video-execution-retirement/v1"),
        scope.id,
        scope.capability,
    )
    authority = RoutingAuthority(test_engine)
    deadline = asyncio.get_running_loop().time() + 360
    while asyncio.get_running_loop().time() < deadline:
        result = await authority.retire_scope(request)
        if (
            result.state == "committed"
            and result.result is not None
            and result.result.phase == "completed"
        ):
            async with test_engine.connect() as db:
                row = (
                    (
                        await db.execute(
                            text(
                                "SELECT retirement.retirement_id,retirement.phase,"
                                "retirement.registry_id,retirement.authority_epoch,"
                                "retirement.backend_incarnation,scope.state,scope.closed,"
                                "aap_inference_routing_v1.video_execution_scope_retired(scope.id) "
                                "AS scope_retired "
                                "FROM aap_inference_routing_v1.scope_retirements AS retirement "
                                "JOIN aap_inference_routing_v1.scopes AS scope "
                                "ON scope.id=retirement.scope_id WHERE scope.id=:scope"
                            ),
                            {"scope": scope.id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            if (
                row is None
                or row["phase"] != "completed"
                or row["state"] != "reconciled"
                or row["closed"] is not True
                or row["scope_retired"] is not True
            ):
                raise LocalFenceRefused("inference_scope_retirement_unverified")
            return {
                "scope_id": str(scope.id),
                "retirement_id": str(row["retirement_id"]),
                "registry_id": str(row["registry_id"]),
                "authority_epoch": str(row["authority_epoch"]),
                "backend_incarnation": str(row["backend_incarnation"]),
                "phase": row["phase"],
                "state": row["state"],
                "closed": row["closed"],
                "scope_retired": row["scope_retired"],
            }
        if result.state != "committed" or result.result is None:
            raise LocalFenceRefused("inference_scope_retirement_incomplete")
        await asyncio.sleep(1)
    raise LocalFenceRefused("inference_scope_retirement_timeout")


async def _wait_task_state(
    sender: Celery, task_id: str, expected: set[str], *, timeout: float
):
    deadline = asyncio.get_running_loop().time() + timeout
    result = sender.AsyncResult(task_id)
    while asyncio.get_running_loop().time() < deadline:
        state = await asyncio.to_thread(lambda: result.state)
        if state in expected:
            return state
        await asyncio.sleep(0.2)
    raise AssertionError("owned tracker task did not reach the expected delivery state")


async def _wait_worker_failure(
    sender: Celery,
    worker,
    task_id: str,
    *,
    timeout: float,
) -> dict:
    """Tie the failed result to this exact worker without global broker claims."""
    result = sender.AsyncResult(task_id)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = await asyncio.to_thread(lambda: result.state)
        inspect = sender.control.inspect(timeout=1, destination=[worker.node_name])
        stats = await asyncio.to_thread(inspect.stats)
        node_stats = (stats or {}).get(worker.node_name) or {}
        task_count = ((node_stats.get("total") or {}).get(_TASK_NAME)) or 0
        if state == "FAILURE" and task_count > 0:
            diagnostics_path = await asyncio.to_thread(worker.capture_diagnostics)
            logs = json.loads(Path(diagnostics_path).read_text())["logs"]
            if f"Task {_TASK_NAME}[{task_id}]" not in logs:
                await asyncio.sleep(0.25)
                continue
            broker = await asyncio.to_thread(_broker_state, sender, worker.queue)
            active = await asyncio.to_thread(inspect.active)
            reserved = await asyncio.to_thread(inspect.reserved)
            outstanding = [
                task
                for snapshot in (active or {}, reserved or {})
                for tasks in snapshot.values()
                for task in tasks or []
                if task.get("id") == task_id
            ]
            if (
                worker.node_name in (active or {})
                and worker.node_name in (reserved or {})
                and broker["queue_size"] == 0
                and not outstanding
            ):
                return {
                    "task_id": task_id,
                    "task_state": state,
                    "worker_node": worker.node_name,
                    "worker_task_count": task_count,
                    "queue": worker.queue,
                    "fixture_queue_size": broker["queue_size"],
                    "worker_executed": True,
                    "worker_diagnostics": diagnostics_path,
                }
        await asyncio.sleep(0.25)
    raise AssertionError("the exact fixture worker did not fail the stale retry")


async def _assert_unbound_job_still_blocked(test_engine, world, input_id):
    async with test_engine.begin() as db:
        raw_guard = await db.scalar(
            text("SELECT aap_labeling_v1.guard_project_video_deletion(:project)"),
            {"project": world["project_id"]},
        )
        guard = _json_value(raw_guard)
        raw_retention = await db.scalar(
            text("SELECT aap_labeling_v1.video_tracker_input_referenced(:input)"),
            {"input": input_id},
        )
        retention = _json_value(raw_retention)
        job = (
            (
                await db.execute(
                    text(
                        "SELECT status,started_at FROM public.video_tracker_jobs "
                        "WHERE id=:job"
                    ),
                    {"job": world["job_id"]},
                )
            )
            .mappings()
            .one_or_none()
        )
        execution_count = await db.scalar(
            text("SELECT count(*) FROM aap_jobs_v1.video_executions WHERE job_id=:job"),
            {"job": world["job_id"]},
        )
        journal_count = await db.scalar(
            text(
                "SELECT count(*) FROM aap_jobs_v1.video_execution_journals WHERE job_id=:job"
            ),
            {"job": world["job_id"]},
        )
        clearance_count = await db.scalar(
            text(
                "SELECT count(*) FROM aap_jobs_v1.video_deletion_clearances WHERE job_id=:job"
            ),
            {"job": world["job_id"]},
        )
    if (
        guard.get("ok") is not False
        or guard.get("error", {}).get("code") != "video_execution_not_retired"
        or retention.get("data", {}).get("referenced") is not True
        or job is None
        or job["status"] != "running"
        or job["started_at"] is None
        or execution_count != 0
        or journal_count != 0
        or clearance_count != 0
    ):
        raise AssertionError("local fence changed the unbound Job or bypassed 0300")
    return {
        "job_id": str(world["job_id"]),
        "status": job["status"],
        "started_at": job["started_at"].isoformat(),
        "business_status_unchanged": True,
        "execution_absent": execution_count == 0,
        "journal_absent": journal_count == 0,
        "clearance_absent": clearance_count == 0,
        "input_referenced": True,
        "deletion_guard_still_409": True,
    }


def _scoped_s3_client(settings, scope: dict):
    from botocore.config import Config

    import boto3

    endpoint = urlsplit(
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    if endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise LocalFenceRefused("s3_endpoint_not_local")
    return boto3.client(
        "s3",
        endpoint_url=f"{endpoint.scheme}://127.0.0.1:{endpoint.port or 9000}",
        aws_access_key_id=scope["access_key"],
        aws_secret_access_key=scope["secret_key"],
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=2,
            read_timeout=3,
            retries={"total_max_attempts": 1},
            proxies={},
            s3={"addressing_style": "path"},
        ),
    )


async def _stop_media_peer(media) -> dict:
    from tests.test_assets_media_live import _stop_process

    processes = list(getattr(media, "processes", []))
    controls = [
        process
        for process in processes
        if isinstance(process.args, (list, tuple))
        and "aap_assets_media_runtime" in process.args
    ]
    materializers = [
        process
        for process in processes
        if isinstance(process.args, (list, tuple))
        and "aap_assets_media_runtime.worker" in process.args
    ]
    if (
        len(controls) != 1
        or not materializers
        or any(
            process is not controls[0] and process.poll() is None
            for process in processes
        )
        or any(process.returncode != 0 for process in materializers)
        or len(controls) + len(materializers) != len(processes)
    ):
        raise LocalFenceRefused("media_process_inventory_ambiguous")
    process = controls[0]
    if process.poll() is not None or not isinstance(process.args, (list, tuple)):
        raise LocalFenceRefused("media_process_not_running")
    if "aap_assets_media_runtime" not in process.args:
        raise LocalFenceRefused("media_process_identity_mismatch")
    process_id = process.pid
    await asyncio.to_thread(_stop_process, process)
    if process.poll() is None:
        raise LocalFenceRefused("media_process_exit_unverified")
    try:
        await media.http.get("/health/ready")
    except httpx.HTTPError:
        unavailable = True
    else:
        unavailable = False
    if not unavailable:
        raise LocalFenceRefused("media_peer_still_serving")
    await media.http.aclose()
    return {
        "process_id": process_id,
        "process_exited": True,
        "process_return_code": process.returncode,
        "endpoint_unavailable": True,
    }


async def test_owned_local_fence_blocks_stale_video_tracker_redelivery(
    test_engine, tmp_path, record_property, monkeypatch
):
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    assert os.environ.get("AAP_LEGACY_VIDEO_FENCE_LIVE") == "1"
    await assert_owned_test_database(test_engine)
    expected_revision = _required("AAP_LEGACY_VIDEO_FENCE_API_SOURCE_REVISION")
    video = await asyncio.to_thread(_video_bytes, tmp_path)
    _artifact_bytes, artifact, artifact_digest = _bbox_artifact()

    # Keep the test-only routing authority on this exact owned PostgreSQL target.
    monkeypatch.setattr(settings, "inference_routing_url", "")
    monkeypatch.setattr(settings, "inference_routing_token", "")
    monkeypatch.setattr(settings, "inference_routing_token_file", "")
    monkeypatch.setattr(settings, "inference_routing_database_url", "")
    monkeypatch.setattr(settings, "inference_routing_database_url_file", "")

    peer = owner_s3 = sender = worker = probe_worker = None
    destroy_guide_scope = None
    guide_scope = None
    s3_probe_client = s3_write_probe = None
    s3_probe_key = None
    peer_stack = AsyncExitStack()
    task_delivery_id = str(uuid4())
    fixture_queue = f"legacy_video_fence_{secrets.token_hex(10)}"
    try:
        async with _video_runtime(test_engine, tmp_path) as media:
            owner_s3 = media.owner
            listener, backend_origin = _reserve_port()
            backend_token = secrets.token_urlsafe(24)
            world = await _seed_tracker_world(
                media,
                video,
                backend_url=backend_origin,
                backend_token=backend_token,
                label="legacy-fence",
            )
            peer = await peer_stack.enter_async_context(
                _checkpoint_cpu_backend(
                    listener,
                    registry_id=world["registry_id"],
                    control_token=backend_token,
                    storage_origin=media.endpoint,
                    expected_source_sha256=sha256(video).hexdigest(),
                    context_mode="none",
                    artifact=artifact,
                    weights_sha256=artifact_digest,
                )
            )
            await _prepared(
                media,
                tmp_path,
                world["actor_id"],
                world["task_id"],
                world["job_id"],
            )
            resources, guide_bucket, owner_s3 = await assert_workspace_identity(
                test_engine
            )
            guide_scope, destroy_guide_scope = create_guide_storage_scope(
                resources,
                owner_s3,
                guide_bucket,
                f"outside-legacy-video-fence/{uuid4()}.txt",
            )
            s3_probe_client = _scoped_s3_client(settings, guide_scope)
            s3_probe_key = f"projects/{world['project_id']}/guide/legacy-video-fence-{uuid4().hex}.txt"
            s3_write_probe = prove_s3_write_access(
                s3_probe_client, guide_scope["bucket"], s3_probe_key
            )
            worker = _worker(
                test_engine,
                tmp_path / "first-worker",
                world,
                media,
                guide_scope,
                queue=fixture_queue,
            )
            image_revision = worker._image_record["source_revision"]
            if image_revision != expected_revision:
                raise LocalFenceRefused("accepted_worker_image_revision_mismatch")
            sender_url = os.environ.get("CELERY_BROKER_URL") or _required("REDIS_URL")
            sender = Celery(
                "local-legacy-video-fence-sender",
                broker=sender_url,
                backend=sender_url,
            )
            sender.conf.update(
                task_serializer="json",
                result_serializer="json",
                accept_content=["json"],
            )
            worker.start()
            await _wait_worker_ready(sender, worker)
            signature = worker.task_signature(_TASK_NAME)
            delivery_policy = worker.task_delivery_policy(_TASK_NAME)
            if signature.get("task") != _TASK_NAME or delivery_policy != {
                "acks_late": True,
                "reject_on_worker_lost": True,
            }:
                raise LocalFenceRefused("accepted_worker_task_contract_mismatch")
            # This scope-only peer is owned by the host fixture, not the worker.
            async with media.sessions.begin() as health_db:
                service = MLBackendService(health_db)
                if not await service.check_health(world["registry_id"]):
                    raise LocalFenceRefused("owned_backend_health_not_ready")
            bootstrap = await RoutingAuthority(test_engine).execute(
                "reconcile",
                {
                    "registry_id": str(world["registry_id"]),
                    "control_id": str(uuid4()),
                },
            )
            if (
                bootstrap.registry_id != world["registry_id"]
                or bootstrap.phase != "completed"
            ):
                raise LocalFenceRefused("owned_backend_control_not_ready")

            # Seed the historical started/unbound state; this accepted binary
            # retries it instead of running an old pre-ledger implementation.
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        "UPDATE public.video_tracker_jobs SET status='running',"
                        "started_at=pg_catalog.clock_timestamp() WHERE id=:job"
                    ),
                    {"job": world["job_id"]},
                )
            scope = await _seed_scope_only_route(test_engine, world)
            async with test_engine.connect() as db:
                input_id = await db.scalar(
                    text(
                        "SELECT input_id FROM public.video_tracker_input_scopes "
                        "WHERE tracker_job_id=:job"
                    ),
                    {"job": world["job_id"]},
                )
            if input_id is None:
                raise LocalFenceRefused("accepted_tracker_input_missing")
            initial_guard = await _assert_unbound_job_still_blocked(
                test_engine, world, input_id
            )

            await asyncio.to_thread(
                sender.send_task,
                _TASK_NAME,
                args=[str(world["job_id"])],
                queue=worker.queue,
                task_id=task_delivery_id,
            )
            first_state = await _wait_task_state(
                sender, task_delivery_id, {"RETRY"}, timeout=45
            )
            if first_state != "RETRY":
                raise LocalFenceRefused("unbound_worker_attempt_not_observed")
            preflight_job_state = await _assert_unbound_job_still_blocked(
                test_engine, world, input_id
            )
            if (
                preflight_job_state["status"] != initial_guard["status"]
                or preflight_job_state["started_at"] != initial_guard["started_at"]
            ):
                raise LocalFenceRefused("initial_worker_attempt_changed_job_state")

            postgres = await owned_postgres_identity(test_engine)
            worker_credentials = inspect_live_worker_credentials(
                worker,
                expected_database=postgres.database,
                expected_s3_access_key=guide_scope["access_key"],
                expected_media_token=media.token,
            )
            if worker_credentials.identity["source_revision"] != expected_revision:
                raise LocalFenceRefused("accepted_worker_image_revision_mismatch")
            if not (
                guide_scope.get("user_created")
                and guide_scope.get("policy_created")
                and guide_scope.get("access_key") == worker.object_store_access_key
                and resources.get("owner")
            ):
                raise LocalFenceRefused("worker_s3_identity_not_owned")
            async with test_engine.connect() as db:
                scope_facts_raw = await db.scalar(
                    text(
                        "SELECT aap_inference_routing_v1.video_execution_scope_facts(:scope)"
                    ),
                    {"scope": scope.id},
                )
                scope_facts = _json_value(scope_facts_raw)
                scope_observation = (
                    (
                        await db.execute(
                            text(
                                "SELECT (SELECT count(*) FROM aap_inference_routing_v1.attempts "
                                "WHERE scope_id=:scope) AS attempt_count,"
                                "(SELECT count(*) FROM aap_inference_routing_v1.video_execution_scope_permits "
                                "WHERE scope_id=:scope) AS permit_count,"
                                "(SELECT phase FROM aap_inference_routing_v1.scope_retirements "
                                "WHERE scope_id=:scope) AS retirement_phase,"
                                "admission_expires_at<=pg_catalog.clock_timestamp() AS no_live_admission "
                                "FROM aap_inference_routing_v1.scopes WHERE id=:scope"
                            ),
                            {"scope": scope.id},
                        )
                    )
                    .mappings()
                    .one()
                )
            if (
                scope_facts is None
                or scope_facts.get("owner") != f"tracker:{world['job_id']}"
                or scope_facts.get("operation") != "tracker"
                or str(scope_facts.get("registry_id")) != str(world["registry_id"])
                or scope_facts.get("closed") is True
            ):
                raise LocalFenceRefused("legacy_route_scope_ownership_mismatch")
            media_processes = [
                process
                for process in media.processes
                if isinstance(process.args, (list, tuple))
                and "aap_assets_media_runtime" in process.args
            ]
            materializers = [
                process
                for process in media.processes
                if isinstance(process.args, (list, tuple))
                and "aap_assets_media_runtime.worker" in process.args
            ]
            if (
                len(media_processes) != 1
                or media_processes[0].poll() is not None
                or not materializers
                or any(process.returncode != 0 for process in materializers)
                or len(media_processes) + len(materializers) != len(media.processes)
            ):
                raise LocalFenceRefused("media_process_inventory_ambiguous")
            controller = peer.controller
            if (
                str(controller.registry_id) != str(world["registry_id"])
                or controller.management_available is not True
                or controller.active_requests != 0
            ):
                raise LocalFenceRefused("backend_peer_identity_mismatch")
            broker = worker_credentials.identity["broker"]
            worker_identity = worker_credentials.identity
            preflight = {
                "schema": "aap-local-video-fence-preflight/v1",
                "scope": {
                    "job_id": str(world["job_id"]),
                    "owner": scope_facts["owner"],
                    "operation": scope_facts["operation"],
                    "registry_id": str(scope_facts["registry_id"]),
                    "scope_id": str(scope.id),
                    "state": scope_facts["state"],
                    "closed": scope_facts["closed"],
                    "job_status": preflight_job_state["status"],
                    "job_started_at": preflight_job_state["started_at"],
                    "input_referenced": preflight_job_state["input_referenced"],
                    "deletion_guard_still_409": preflight_job_state[
                        "deletion_guard_still_409"
                    ],
                    **dict(scope_observation),
                    "jobs_execution_present": not preflight_job_state[
                        "execution_absent"
                    ],
                    "jobs_journal_present": not preflight_job_state["journal_absent"],
                    "deletion_clearance_present": not preflight_job_state[
                        "clearance_absent"
                    ],
                    "historical_state_source": "owner_seeded_job_and_scope_only_fixture",
                },
                "postgres": postgres.evidence(),
                "worker": worker_identity,
                "worker_database_credentials": worker_credential_evidence(
                    worker_credentials.credentials
                ),
                "worker_write_selectors": worker_credentials.write_selectors,
                "storage": {
                    "owner_verified": True,
                    "worker_identity_matches": True,
                    "user_created": guide_scope["user_created"],
                    "policy_created": guide_scope["policy_created"],
                    "access_key_id": guide_scope["access_key"],
                    "container_id": guide_scope["container"],
                    "resource_owner": resources["owner"],
                    "bucket": guide_scope["bucket"],
                    "policy_id": guide_scope["policy"],
                    "write_probe": s3_write_probe,
                },
                "media_peer": {
                    "owned_process_count": 1,
                    "process_id": media_processes[0].pid,
                    "completed_materializer_process_ids": [
                        process.pid for process in materializers
                    ],
                    "running": True,
                    "endpoint": media.origin,
                },
                "backend_peer": {
                    "registry_id": str(controller.registry_id),
                    "backend_incarnation": str(controller.backend_incarnation),
                    "management_available": True,
                    "active_requests": controller.active_requests,
                    "endpoint": peer.origin,
                },
                "broker": {
                    "queue": worker.queue,
                    **broker,
                },
            }
            validate_local_fence_preflight(preflight)

            async def perform_fence():
                worker_stop = stop_owned_worker(worker, worker_identity)
                scope_retirement = await _retire_exact_scope(test_engine, scope)
                media_stop = await _stop_media_peer(media)
                await peer.stop()
                backend_stop = {
                    "registry_id": str(controller.registry_id),
                    "backend_incarnation": str(controller.backend_incarnation),
                    "peer_stopped": peer.is_stopped() is True
                    and peer.server_task.done(),
                    "active_requests_at_stop": controller.active_requests,
                    "endpoint_unavailable": await _backend_endpoint_unavailable(
                        peer.origin
                    ),
                }
                if (
                    not backend_stop["peer_stopped"]
                    or not backend_stop["endpoint_unavailable"]
                    or backend_stop["active_requests_at_stop"] != 0
                ):
                    raise LocalFenceRefused("backend_peer_stop_unverified")

                destroy_guide_scope(guide_scope)
                old_s3_error = prove_s3_write_denied(
                    s3_probe_client, guide_scope["bucket"], s3_probe_key
                )
                s3_revocation = {
                    "access_key_id": guide_scope["access_key"],
                    "policy_id": guide_scope["policy"],
                    "container_id": guide_scope["container"],
                    "bucket": guide_scope["bucket"],
                    "user_removed": guide_scope["user_created"] is False,
                    "policy_removed": guide_scope["policy_created"] is False,
                    "old_credential_denied": True,
                    "denial_code": old_s3_error,
                    "write_probe": s3_write_probe,
                }
                database_fence = await fence_disposable_database_logins(
                    test_engine, postgres, worker_credentials.credentials
                )
                stale_denials = await prove_stale_worker_dsns_denied(
                    worker_credentials.credentials,
                    host=postgres.host,
                    port=postgres.port,
                    database=postgres.database,
                )
                return {
                    "worker_stop": worker_stop,
                    "scope_retirement": scope_retirement,
                    "media_peer_stop": media_stop,
                    "backend_peer_stop": backend_stop,
                    "database_write_fence": database_fence,
                    "stale_login_denials": stale_denials,
                    "s3_revocation": s3_revocation,
                }

            fence_observations = await run_fence_after_preflight(
                preflight, perform_fence
            )

            # The original worker's real Celery retry is the stale delivery.
            # The broker remains shared; keep the consumer on this exact queue.
            probe_worker = _worker(
                test_engine,
                tmp_path / "stale-redelivery-worker",
                world,
                media,
                guide_scope,
                queue=fixture_queue,
            )
            probe_worker.start()
            probe_identity = owned_worker_identity(probe_worker)
            if probe_identity["source_revision"] != expected_revision:
                raise LocalFenceRefused("stale_probe_image_revision_mismatch")
            probe_inventory = inspect_live_worker_credentials(
                probe_worker,
                expected_database=postgres.database,
                expected_s3_access_key=guide_scope["access_key"],
                expected_media_token=media.token,
            )
            if [
                (item.source_variable, item.role, item._url)
                for item in probe_inventory.credentials
            ] != [
                (item.source_variable, item.role, item._url)
                for item in worker_credentials.credentials
            ]:
                raise LocalFenceRefused("stale_probe_credentials_not_identical")
            await _wait_worker_ready(sender, probe_worker)
            worker_result = await _wait_worker_failure(
                sender,
                probe_worker,
                task_delivery_id,
                timeout=90,
            )
            post_job = await _assert_unbound_job_still_blocked(
                test_engine, world, input_id
            )
            if (
                post_job["status"] != preflight_job_state["status"]
                or post_job["started_at"] != preflight_job_state["started_at"]
            ):
                raise LocalFenceRefused("business_job_state_changed_after_fence")
            stale_worker_stop = stop_owned_worker(probe_worker, probe_identity)
            fence_observations["stale_redelivery"] = {
                "task_id": task_delivery_id,
                "worker_container_id": probe_identity["container_id"],
                "worker_container_name": probe_identity["container_name"],
                "worker_acceptance_id": probe_identity["acceptance_id"],
                "worker_image_id": probe_identity["image_id"],
                "worker_image_acceptance_id": probe_identity["image_acceptance_id"],
                "worker_source_revision": probe_identity["source_revision"],
                "worker_network_id": probe_identity["network_id"],
                "worker_network_name": probe_identity["network_name"],
                "worker_started_at": probe_identity["worker_started_at"],
                "task_state": worker_result["task_state"],
                "worker_executed": worker_result["worker_executed"],
                "stale_worker_db_access": False,
                "queue": probe_worker.queue,
                "worker_observation": worker_result,
            }
            fence_observations["stale_probe_worker_stop"] = stale_worker_stop
            fence_observations["job_state_after"] = post_job
            evidence = build_local_fence_evidence(preflight, fence_observations)
            serialized = json.dumps(
                evidence,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            evidence_path = tmp_path / "legacy-video-fence-evidence.json"
            evidence_path.write_text(serialized + "\n", encoding="utf-8")
            evidence_path.chmod(0o600)
            record_property("legacy_video_local_fence", serialized)
            record_property(
                "legacy_video_local_fence_evidence_path", str(evidence_path)
            )
            record_property("legacy_video_worker_image_revision", expected_revision)
            record_property("legacy_video_worker_image_id", worker_identity["image_id"])
            record_property("legacy_video_pg_identity", postgres.container_id)
            record_property("legacy_video_fence_scope", "fixture-only; no Jobs receipt")
            await asyncio.to_thread(sender.AsyncResult(task_delivery_id).forget)
    finally:
        cleanup_errors = stop_fixture_workers(probe_worker, worker)
        if sender is not None:
            try:
                if (
                    not cleanup_errors
                    and worker is not None
                    and worker.queue == fixture_queue
                ):
                    _delete_owned_queue(sender, fixture_queue)
            except Exception:
                cleanup_errors.append(LocalFenceRefused("fixture_queue_cleanup_failed"))
            finally:
                sender.close()
        try:
            await peer_stack.aclose()
        except Exception:
            cleanup_errors.append(LocalFenceRefused("backend_peer_cleanup_failed"))
        finally:
            if s3_probe_client is not None:
                s3_probe_client.close()
            try:
                if guide_scope and destroy_guide_scope is not None:
                    destroy_guide_scope(guide_scope)
            except Exception:
                cleanup_errors.append(LocalFenceRefused("s3_principal_cleanup_failed"))
            if owner_s3 is not None:
                try:
                    if s3_probe_key is not None:
                        version = (s3_write_probe or {}).get("version_id")
                        owner_s3.delete_object(
                            Bucket=guide_scope["bucket"],
                            Key=s3_probe_key,
                            **({"VersionId": version} if version is not None else {}),
                        )
                except Exception:
                    cleanup_errors.append(
                        LocalFenceRefused("s3_probe_object_cleanup_failed")
                    )
                finally:
                    owner_s3.close()
        if cleanup_errors:
            raise ExceptionGroup("local fence cleanup incomplete", cleanup_errors)


async def _backend_endpoint_unavailable(origin: str) -> bool:
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            await client.get(origin + "/health")
    except httpx.HTTPError:
        return True
    return False
