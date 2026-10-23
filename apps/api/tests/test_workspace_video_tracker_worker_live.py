"""Cold video-tracker Celery acceptance under the Workspace consumer ACL."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import secrets
from urllib.parse import urlsplit, urlunsplit
import uuid

from celery import Celery
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.dataset import DatasetItem
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.task import Task
from app.db.models.video_tracker_job import VideoTrackerJob
from app.services.accepted_tracker_inputs import capture_video_tracker_input
from tests.conftest import create_registry_with_pool
from tests.factory import build_tool_bindings, create_membership, create_user
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    create_guide_storage_scope,
    endpoint_url,
)
from tests.test_module_data_roles import _role_url as _module_role_url
from tests.test_video_input_authorities_native import _seed_video_task
from tests.test_video_input_live import _video_bytes, _video_runtime
from tests.test_tracker_input_live import _prepared
from tests.test_video_window_recovery_live import (
    _bbox_artifact,
    _checkpoint_cpu_backend,
    _expire_exact_claim,
    _first_checkpoint,
    _first_window_parts,
    _reserve_port,
    _scope_rows,
)
from tests.test_workspace_cross_frame_worker_live import (
    _assert_workspace_consumer_closure,
    _api_role_url,
    _identity_gateway_for_owned_auth,
)
from tests.test_workspace_worker_assembly_live import (
    _broker_state,
    _assert_worker_database_identity,
    _delete_owned_queue,
    _wait_broker_ack,
    _wait_worker_ready,
)
from tests.workspace_worker_assembly_helpers import WorkspaceCeleryWorker

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_WORKSPACE_VIDEO_TRACKER_WORKER_LIVE") != "1",
        reason="requires the explicitly enabled owned Workspace video-tracker worker verifier",
    ),
]

_TASK_NAME = "app.workers.video_tracker.run_video_tracker_job"
_JOBS_SCHEMA = "aap_jobs_v1"
_ROUTING_SCHEMA = "aap_inference_routing_v1"


def _host_forward_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise AssertionError("owned Media and backend fixtures must bind loopback")
    netloc = (
        f"host.docker.internal:{parsed.port}" if parsed.port else "host.docker.internal"
    )
    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


class _VideoTrackerCeleryWorker(WorkspaceCeleryWorker):
    """Reuse the verified consumer worker, adding only the tracker owner bindings."""

    def __init__(self, *args, media_url: str, media_token: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # The production dispatcher targets the isolated verifier's gpu queue.
        self.queue = "gpu"
        routing_dsn = self._role_dsn(
            "AAP_MODULE_DATA_INFERENCE_ROUTING_URL", "inference_routing_client"
        )
        writer_dsn = self._role_dsn(
            "AAP_MODULE_DATA_LABELING_VIDEO_WRITER_URL",
            "labeling_video_writer_client",
        )
        self._tracker_environment = {
            "ASSETS_MEDIA_URL": _host_forward_url(media_url),
            "ASSETS_MEDIA_TOKEN": media_token,
            "ASSETS_MEDIA_TOKEN_FILE": "",
            "INFERENCE_ROUTING_URL": "",
            "INFERENCE_ROUTING_TOKEN": "",
            "INFERENCE_ROUTING_TOKEN_FILE": "",
            "INFERENCE_ROUTING_DATABASE_URL": routing_dsn.render_as_string(
                hide_password=False
            ),
            "INFERENCE_ROUTING_DATABASE_URL_FILE": "",
            "INFERENCE_ROUTING_TIMEOUT_SECONDS": "20",
            "ML_BACKEND_ROUTER_MODE": "enforce",
            "ML_BACKEND_ROUTER_LEASE_TTL_SECONDS": "30",
            "ML_BACKEND_ROUTER_HEARTBEAT_INTERVAL_SECONDS": "1",
            "VIDEO_TRACKER_WINDOW_SIZE_FRAMES": "2",
            "VIDEO_TRACKER_SAM3_WINDOW_SIZE_FRAMES": "2",
            "LABELING_VIDEO_WRITER_URL": "",
            "LABELING_VIDEO_WRITER_CONTROL_TOKEN": "",
            "LABELING_VIDEO_WRITER_CONTROL_TOKEN_FILE": "",
            "LABELING_VIDEO_WRITER_DATABASE_URL": writer_dsn.render_as_string(
                hide_password=False
            ),
            "LABELING_VIDEO_WRITER_DATABASE_URL_FILE": "",
        }

    def bootstrap_tracker_routes(self, worlds: dict[str, dict]) -> list[dict]:
        self.assert_runtime()
        payload = json.dumps(
            [
                {
                    "project_id": str(world["project_id"]),
                    "registry_id": str(world["registry_id"]),
                }
                for world in worlds.values()
            ],
            separators=(",", ":"),
        )
        code = """import asyncio, json, sys, uuid
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from aap_inference.routing_runtime import RoutingRuntime
from aap_inference_contract.routing import make_routing_command
from app.config import settings
from app.services.ml_backend import MLBackendService

async def main():
    records = json.loads(sys.argv[1])
    engine = create_async_engine(settings.database_url, hide_parameters=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    runtime = RoutingRuntime(settings.inference_routing_database_url)
    results = []
    try:
        if not await runtime.readiness():
            raise RuntimeError('routing owner is not ready')
        for record in records:
            registry_id = uuid.UUID(record['registry_id'])
            project_id = uuid.UUID(record['project_id'])
            async with sessions.begin() as db:
                service = MLBackendService(db)
                if not await service.check_health(registry_id):
                    raise RuntimeError('backend health check failed')
                selected = await service.get_tracker_backend_for_capabilities(
                    project_id, ['sam2_video']
                )
                if selected is None or selected.id != registry_id:
                    raise RuntimeError('tracker backend selection mismatch')
            result = await runtime.execute(make_routing_command('reconcile', {
                'registry_id': str(registry_id),
                'control_id': str(uuid.uuid4()),
            }))
            if result.state != 'committed' or result.result.phase != 'completed':
                raise RuntimeError('backend route reconciliation did not complete')
            results.append({
                'registry_id': str(registry_id),
                'phase': result.result.phase,
            })
    finally:
        await runtime.close()
        await engine.dispose()
    print(json.dumps(results, separators=(',', ':')))

asyncio.run(main())
"""
        result = self._docker(
            "exec",
            self.container_id,
            "/opt/venv/bin/python",
            "-c",
            code,
            payload,
            timeout=120,
        )
        return json.loads(result)

    def _write_env_file(self, path, values: dict[str, str]) -> None:
        super()._write_env_file(path, {**values, **self._tracker_environment})


async def _seed_tracker_world(
    media,
    video: bytes,
    *,
    backend_url: str,
    backend_token: str,
    label: str,
) -> dict:
    key = f"workspace-video-tracker/{uuid.uuid4()}.mp4"
    media.owner.put_object(
        Bucket=media.buckets["MINIO_DATASETS_BUCKET"], Key=key, Body=video
    )
    async with media.sessions.begin() as db:
        owner_id, project, _dataset_id, item_id, task_id = await _seed_video_task(
            db, status="in_progress"
        )
        suffix = uuid.uuid4().hex[:12]
        actor = await _make_actor(db, f"{label}-{suffix}")
        membership = await create_membership(
            db,
            project_id=project.id,
            user_id=actor.id,
            role="annotator",
            assigned_by=owner_id,
        )
        replacement_id = None
        if label == "removed":
            replacement = await _make_actor(db, f"replacement-{suffix}")
            replacement_id = replacement.id
            await create_membership(
                db,
                project_id=project.id,
                user_id=replacement_id,
                role="annotator",
                assigned_by=owner_id,
            )
        project.tool_bindings = build_tool_bindings(["car"], unit="bbox")
        item = await db.get(DatasetItem, item_id)
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
        task.file_path = ""
        task.assignee_id = actor.id
        task.assignee_is_override = True
        task.assigned_at = datetime.now(timezone.utc)
        registry, pool = await create_registry_with_pool(
            db,
            name=f"workspace-video-tracker-{label}-{suffix}",
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
        job_id = uuid.uuid4()
        job = VideoTrackerJob(
            id=job_id,
            task_id=task_id,
            dataset_item_id=item_id,
            created_by=actor.id,
            status="queued",
            model_key="sam2_video",
            direction="forward",
            from_frame=0,
            to_frame=5,
            target_class_name="car",
            target_tool_unit_id="bbox",
            prompt={"text": "car", "output_geometry": "bbox"},
            event_channel=f"video-tracker-job:{job_id}",
        )
        db.add(job)
        await db.flush()
        await capture_video_tracker_input(
            db, actor_id=actor.id, tracker_job_id=job.id, grid_step=1
        )
        return {
            "owner_id": owner_id,
            "owner_token": _access_token(owner_id, "super_admin"),
            "actor_id": actor.id,
            "membership_id": membership.id,
            "replacement_id": replacement_id,
            "actor_token": _access_token(actor.id, "employee"),
            "project_id": project.id,
            "task_id": task_id,
            "job_id": job.id,
            "registry_id": registry.id,
            "backend_token": backend_token,
        }


async def _make_actor(db, suffix: str):
    return await create_user(
        db,
        "employee",
        f"workspace-video-tracker-{suffix}@example.test",
        f"Workspace video tracker {suffix}",
    )


def _access_token(actor_id: uuid.UUID, role: str) -> str:
    from app.core.security import create_access_token

    return create_access_token(subject=str(actor_id), role=role)


@asynccontextmanager
async def _committed_api_client(app_module, test_engine, *, dispatcher=None):
    from app.deps import (
        get_db,
        get_identity_gateway,
        get_video_tracker_job_dispatcher,
    )

    api_engine = create_async_engine(_api_role_url(), hide_parameters=True)
    api_sessions = async_sessionmaker(api_engine, expire_on_commit=False)
    gateway = _identity_gateway_for_owned_auth(test_engine)
    owner_bindings = {}
    for group, role, setting in (
        (
            "SUSPENSION",
            "identity_suspension_client",
            "identity_suspension_database_url",
        ),
        ("BRIEFS", "identity_briefs_client", "identity_briefs_database_url"),
    ):
        dsn = make_url(_module_role_url(f"AAP_MODULE_DATA_IDENTITY_{group}_URL", role))
        assert (dsn.host, dsn.port, dsn.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
        owner_bindings[setting] = dsn.render_as_string(hide_password=False)
    previous_db = app_module.dependency_overrides.get(get_db)
    previous_gateway = app_module.dependency_overrides.get(get_identity_gateway)
    previous_dispatcher = app_module.dependency_overrides.get(
        get_video_tracker_job_dispatcher
    )

    async def api_session_dependency():
        async with api_sessions() as session:
            yield session

    app_module.dependency_overrides[get_db] = api_session_dependency
    app_module.dependency_overrides[get_identity_gateway] = lambda: gateway
    if dispatcher is not None:
        app_module.dependency_overrides[get_video_tracker_job_dispatcher] = lambda: (
            dispatcher
        )
    scoped = pytest.MonkeyPatch()
    for setting, dsn in owner_bindings.items():
        scoped.setattr(settings, setting, dsn)
        scoped.setattr(settings, setting + "_file", "")
    scoped.setattr(settings, "identity_url", "")
    scoped.setattr(settings, "identity_token", "")
    scoped.setattr(settings, "identity_auth_database_url", gateway.auth_database_url)
    scoped.setattr(settings, "identity_auth_database_url_file", "")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            yield client
    finally:
        if previous_db is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_db
        if previous_gateway is None:
            app_module.dependency_overrides.pop(get_identity_gateway, None)
        else:
            app_module.dependency_overrides[get_identity_gateway] = previous_gateway
        if previous_dispatcher is None:
            app_module.dependency_overrides.pop(get_video_tracker_job_dispatcher, None)
        else:
            app_module.dependency_overrides[get_video_tracker_job_dispatcher] = (
                previous_dispatcher
            )
        scoped.undo()
        await api_engine.dispose()


async def _exercise_workspace_consumer_access(test_engine, world) -> None:
    engine = create_async_engine(_api_role_url(), hide_parameters=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        from app.services.project_access import get_workspace_project
        from app.services.project_write_guard import lock_actor_scope

        async with sessions.begin() as db:
            project = await get_workspace_project(
                db,
                actor_id=world["actor_id"],
                project_id=world["project_id"],
            )
            assert project is not None and project.id == world["project_id"]
            await lock_actor_scope(db, world["actor_id"], world["project_id"])
    finally:
        await engine.dispose()


async def _wait_window_checkpoint(
    test_engine, sessions_factory, job_id, ordinal: int
) -> dict:
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        checkpoint = (
            await _first_checkpoint(sessions_factory, job_id) if ordinal == 0 else None
        )
        if ordinal == 0 and checkpoint is not None:
            return dict(checkpoint)
        if ordinal > 0:
            async with test_engine.connect() as db:
                row = (
                    (
                        await db.execute(
                            text(
                                "SELECT checkpoint_id,manifest_digest,manifest "
                                f"FROM {_JOBS_SCHEMA}.video_execution_windows "
                                "WHERE job_id=:job AND window_ordinal=:ordinal"
                            ),
                            {"job": job_id, "ordinal": ordinal},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            if row is not None:
                return dict(row)
        await asyncio.sleep(0.05)
    raise AssertionError(f"video tracker window {ordinal} did not checkpoint")


async def _assert_terminal_execution(
    test_engine, job_id, expected_outcome: str, *, require_scope: bool = False
) -> dict:
    async with test_engine.connect() as db:
        journal = (
            (
                await db.execute(
                    text(
                        f"SELECT writer_terminal,writer_terminal_outcome,execution_retired "
                        f"FROM {_JOBS_SCHEMA}.video_execution_journals WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .one()
        )
        scopes = (
            (
                await db.execute(
                    text(
                        f"SELECT s.id,s.state,s.closed,r.phase FROM {_ROUTING_SCHEMA}.scopes s "
                        f"LEFT JOIN {_ROUTING_SCHEMA}.scope_retirements r ON r.scope_id=s.id "
                        "WHERE s.owner=:owner ORDER BY s.created_at,s.id"
                    ),
                    {"owner": f"tracker:{job_id}"},
                )
            )
            .mappings()
            .all()
        )
    assert journal["writer_terminal"] is True
    assert journal["writer_terminal_outcome"] == expected_outcome
    assert journal["execution_retired"] is True
    if require_scope:
        assert scopes, f"tracker {job_id} did not create a real routing scope"
    if scopes:
        assert all(
            row["closed"] is True
            and row["state"] == "reconciled"
            and row["phase"] == "completed"
            for row in scopes
        )
    return {"journal": dict(journal), "scopes": [dict(row) for row in scopes]}


async def _send_delivery(sender, worker, job_id):
    return await asyncio.to_thread(
        sender.send_task,
        _TASK_NAME,
        args=[str(job_id)],
        queue=worker.queue,
        task_id=str(job_id),
    )


async def _wait_delivery(sender, worker, delivery, job_id):
    result = await asyncio.to_thread(delivery.get, timeout=150, propagate=False)
    assert delivery.state == "SUCCESS", repr(result)
    ack = await _wait_broker_ack(sender, worker, job_id)
    return delivery, result, ack


async def _deliver(sender, worker, job_id):
    delivery = await _send_delivery(sender, worker, job_id)
    return await _wait_delivery(sender, worker, delivery, job_id)


def _record_worker_diagnostics(worker, record_property) -> None:
    try:
        record_property(
            "workspace_video_tracker_diagnostics", worker.capture_diagnostics()
        )
    except Exception as exc:
        record_property("workspace_video_tracker_diagnostics_error", type(exc).__name__)


def _stop_worker(worker, record_property) -> None:
    try:
        worker.stop()
    except Exception as exc:
        record_property("workspace_video_tracker_cleanup_error", type(exc).__name__)
        raise


def _delete_worker_queue(sender, worker, record_property) -> None:
    try:
        _delete_owned_queue(sender, worker.queue)
    except Exception as exc:
        record_property(
            "workspace_video_tracker_queue_cleanup_error", type(exc).__name__
        )
        raise


async def _revoke_queued_worlds(api, worlds):
    removed = worlds["removed"]
    member_path = (
        f"/api/v1/projects/{removed['project_id']}/members/{removed['membership_id']}"
    )
    manager_headers = {"Authorization": f"Bearer {removed['owner_token']}"}
    preview_response = await api.post(
        member_path + "/role/preview",
        headers=manager_headers,
        json={
            "project_role": "viewer",
            "replacement_annotator_id": str(removed["replacement_id"]),
        },
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["blockers"] == [], preview
    assert set(preview["resource_snapshot"]["annotation_task_ids"]) == {
        str(removed["task_id"])
    }
    handover_response = await api.patch(
        member_path + "/role",
        headers=manager_headers,
        json={
            "project_role": "viewer",
            "replacement_annotator_id": str(removed["replacement_id"]),
            "expected_version": preview["current_version"],
            "preview_token": preview["preview_token"],
            "reason": "Queued tracker authority revocation after explicit handover",
        },
    )
    assert handover_response.status_code == 200, handover_response.text
    remove_response = await api.delete(
        member_path,
        headers=manager_headers,
    )
    assert remove_response.status_code == 204, remove_response.text
    deactivate_response = await api.post(
        f"/api/v1/users/{worlds['disabled']['actor_id']}/deactivate",
        headers={"Authorization": f"Bearer {worlds['disabled']['owner_token']}"},
    )
    assert deactivate_response.status_code == 200, deactivate_response.text


async def test_tracker_revocation_http_prerequisites(test_engine, app_module):
    await assert_owned_test_database(test_engine)
    await _assert_workspace_consumer_closure(test_engine)
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    worlds = {}
    async with sessions.begin() as db:
        for label in ("removed", "disabled"):
            owner_id, project, _dataset_id, _item_id, task_id = await _seed_video_task(
                db, status="in_progress"
            )
            actor = await _make_actor(db, f"http-{label}-{uuid.uuid4().hex}")
            member = await create_membership(
                db,
                project_id=project.id,
                user_id=actor.id,
                role="annotator",
                assigned_by=owner_id,
            )
            task = await db.get(Task, task_id)
            task.assignee_id = actor.id
            replacement_id = None
            if label == "removed":
                receiver = await _make_actor(db, f"http-receiver-{uuid.uuid4().hex}")
                replacement_id = receiver.id
                await create_membership(
                    db,
                    project_id=project.id,
                    user_id=replacement_id,
                    role="annotator",
                    assigned_by=owner_id,
                )
            worlds[label] = {
                "owner_token": _access_token(owner_id, "super_admin"),
                "actor_id": actor.id,
                "project_id": project.id,
                "membership_id": member.id,
                "replacement_id": replacement_id,
                "task_id": task_id,
            }
    async with _committed_api_client(app_module, test_engine) as api:
        await _revoke_queued_worlds(api, worlds)
    async with sessions() as db:
        assert (
            await db.scalar(
                text("SELECT count(*) FROM public.project_members WHERE id=:member"),
                {"member": worlds["removed"]["membership_id"]},
            )
            == 0
        )
        assert (
            await db.scalar(
                text("SELECT assignee_id FROM public.tasks WHERE id=:task"),
                {"task": worlds["removed"]["task_id"]},
            )
            == worlds["removed"]["replacement_id"]
        )
        assert (
            await db.scalar(
                text("SELECT is_active FROM public.users WHERE id=:actor"),
                {"actor": worlds["disabled"]["actor_id"]},
            )
            is False
        )


async def test_real_video_tracker_cold_worker_uses_workspace_acl_and_genuine_retirement(
    test_engine, tmp_path, app_module, record_property, monkeypatch
):
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    video = await asyncio.to_thread(_video_bytes, tmp_path)
    artifact_bytes, artifact, artifact_digest = _bbox_artifact()
    del artifact_bytes

    worker = None
    sender = None
    worlds: dict[str, dict] = {}
    deliveries = []
    async with _video_runtime(test_engine, tmp_path) as media:
        async with AsyncExitStack() as peers:
            peer_specs = {}
            for label in ("success", "cancel", "removed", "disabled", "replay"):
                listener, backend_origin = _reserve_port()
                backend_token = secrets.token_urlsafe(24)
                world = await _seed_tracker_world(
                    media,
                    video,
                    backend_url=_host_forward_url(backend_origin),
                    backend_token=backend_token,
                    label=label,
                )
                worlds[label] = world
                peer = await peers.enter_async_context(
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
                peer_specs[label] = peer
                actor_id = world["actor_id"]
                await _prepared(
                    media,
                    tmp_path,
                    actor_id,
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
                f"outside-video-tracker/{uuid.uuid4()}.txt",
            )
            peers.callback(owner_s3.close)
            peers.callback(destroy_guide_scope, guide_scope)
            minio_origin = endpoint_url()
            minio_scheme = urlsplit(minio_origin).scheme
            assert minio_scheme in {"http", "https"}
            worker = _VideoTrackerCeleryWorker(
                test_engine,
                tmp_path,
                backend_token=worlds["success"]["backend_token"],
                object_store_endpoint=minio_origin,
                object_store_bucket=guide_bucket,
                object_store_use_ssl=minio_scheme == "https",
                object_store_access_key=guide_scope["access_key"],
                object_store_secret_key=guide_scope["secret_key"],
                media_url=media.origin,
                media_token=media.token,
            )
            sender_url = os.environ.get("CELERY_BROKER_URL") or os.environ["REDIS_URL"]
            sender = Celery(
                "workspace-video-tracker-worker-sender",
                broker=sender_url,
                backend=sender_url,
            )
            sender.conf.update(
                task_serializer="json",
                result_serializer="json",
                accept_content=["json"],
            )
            peers.callback(sender.close)
            try:
                worker.start()
            except BaseException:
                _stop_worker(worker, record_property)
                raise
            peers.callback(_delete_worker_queue, sender, worker, record_property)
            peers.callback(_stop_worker, worker, record_property)
            peers.callback(_record_worker_diagnostics, worker, record_property)
            await _wait_worker_ready(sender, worker)
            signature = worker.task_signature(_TASK_NAME)
            assert signature["task"] == _TASK_NAME
            delivery_policy = worker.task_delivery_policy(_TASK_NAME)
            assert delivery_policy == {"acks_late": True, "reject_on_worker_lost": True}
            await _assert_workspace_consumer_closure(test_engine)
            await _exercise_workspace_consumer_access(test_engine, worlds["success"])
            record_property("workspace_video_tracker_worker", worker.assert_runtime())
            record_property("video_tracker_task_signature", signature)
            record_property("video_tracker_delivery_policy", delivery_policy)
            record_property(
                "peer_route_limitation",
                "PostgreSQL and Redis use verified private container aliases; Assets Media and deterministic backend use the existing host.docker.internal forwarding route.",
            )

            bootstrap = await asyncio.to_thread(
                worker.bootstrap_tracker_routes, {"success": worlds["success"]}
            )
            assert bootstrap == [
                {
                    "registry_id": str(worlds["success"]["registry_id"]),
                    "phase": "completed",
                }
            ]
            assert peer_specs["success"].controller.phase == "bound"
            success_delivery = await _send_delivery(
                sender, worker, worlds["success"]["job_id"]
            )
            deliveries.append(success_delivery)
            for index, gate in enumerate(peer_specs["success"].release):
                await asyncio.wait_for(
                    peer_specs["success"].entered[index].wait(),
                    timeout=60,
                )
                if index == 0:
                    await _assert_worker_database_identity(test_engine, worker)
                gate.set()
            _, success_result, success_ack = await _wait_delivery(
                sender, worker, success_delivery, worlds["success"]["job_id"]
            )
            async with media.sessions() as db:
                success_job = await db.get(VideoTrackerJob, worlds["success"]["job_id"])
                assert (
                    success_job is not None and success_job.status == "pending_review"
                )
                assert [
                    row["frame_index"]
                    for row in (success_job.staged_result or {}).get("results", [])
                ] == list(range(6))
            success_checkpoint = await _wait_window_checkpoint(
                test_engine, media.sessions, worlds["success"]["job_id"], 0
            )
            success_execution = await _assert_terminal_execution(
                test_engine,
                worlds["success"]["job_id"],
                "completed",
                require_scope=True,
            )
            assert [
                row["from_frame"] for row in peer_specs["success"].model_windows
            ] == [0, 2, 4]
            record_property("video_tracker_success_ack", success_ack)
            record_property("video_tracker_success_result", repr(success_result))
            record_property("video_tracker_success_checkpoint", success_checkpoint)
            record_property("video_tracker_success_retirement", success_execution)

            cancel_world = worlds["cancel"]
            cancel_peer = peer_specs["cancel"]
            bootstrap = await asyncio.to_thread(
                worker.bootstrap_tracker_routes, {"cancel": cancel_world}
            )
            assert bootstrap == [
                {
                    "registry_id": str(cancel_world["registry_id"]),
                    "phase": "completed",
                }
            ]
            assert cancel_peer.controller.phase == "bound"
            cancel_delivery = await asyncio.to_thread(
                sender.send_task,
                _TASK_NAME,
                args=[str(cancel_world["job_id"])],
                queue=worker.queue,
                task_id=str(cancel_world["job_id"]),
            )
            deliveries.append(cancel_delivery)
            await asyncio.wait_for(cancel_peer.entered[0].wait(), timeout=60)
            cancel_peer.release[0].set()
            cancel_checkpoint = await _wait_window_checkpoint(
                test_engine, media.sessions, cancel_world["job_id"], 0
            )
            await asyncio.wait_for(cancel_peer.entered[1].wait(), timeout=60)
            from app.workers.video_tracker_dispatch import (
                CeleryVideoTrackerJobDispatcher,
            )

            monkeypatch.setattr(
                "app.workers.video_tracker_dispatch.current_app", sender
            )
            cancel_dispatcher = CeleryVideoTrackerJobDispatcher()
            async with _committed_api_client(
                app_module, test_engine, dispatcher=cancel_dispatcher
            ) as api:
                cancel_response = await api.delete(
                    f"/api/v1/video-tracker-jobs/{cancel_world['job_id']}",
                    headers={"Authorization": f"Bearer {cancel_world['actor_token']}"},
                )
            assert cancel_response.status_code == 200, cancel_response.text
            assert cancel_response.json()["status"] == "cancelled"
            broker_after_cancel = await asyncio.to_thread(
                _broker_state, sender, worker.queue
            )
            assert (
                broker_after_cancel["queue_size"]
                + broker_after_cancel["unacknowledged"]
                >= 2
            ), "authorized cancellation did not redrive the same video job"
            inspection = await asyncio.to_thread(
                sender.control.inspect(timeout=2, destination=[worker.node_name]).active
            )
            active = [
                task
                for tasks in (inspection or {}).values()
                for task in tasks or []
                if task.get("id") == str(cancel_world["job_id"])
            ]
            assert len(active) == 1 and type(active[0].get("worker_pid")) is int
            cancel_child_loss = worker.kill_prefork_child(active[0]["worker_pid"])
            await _expire_exact_claim(
                media.sessions,
                cancel_world["job_id"],
                expected_generation=1,
            )
            cancel_peer.release[1].set()
            async with asyncio.timeout(180):
                while (
                    await asyncio.to_thread(lambda: cancel_delivery.state)
                ) != "SUCCESS":
                    await asyncio.sleep(0.25)
            cancel_result = await asyncio.to_thread(
                cancel_delivery.get, timeout=10, propagate=False
            )
            assert cancel_delivery.state == "SUCCESS", repr(cancel_result)
            cancel_ack = await _wait_broker_ack(sender, worker, cancel_world["job_id"])
            async with media.sessions() as db:
                cancelled = await db.get(VideoTrackerJob, cancel_world["job_id"])
                assert cancelled is not None and cancelled.status == "cancelled"
                assert [
                    row["frame_index"]
                    for row in (cancelled.staged_result or {}).get("results", [])
                ] == [0, 1]
            assert await _first_window_parts(media.sessions, cancel_world["job_id"])
            cancel_execution = await _assert_terminal_execution(
                test_engine,
                cancel_world["job_id"],
                "cancelled",
                require_scope=True,
            )
            record_property("video_tracker_cancel_ack", cancel_ack)
            record_property("video_tracker_cancel_checkpoint", cancel_checkpoint)
            record_property("video_tracker_cancel_retirement", cancel_execution)
            record_property("video_tracker_cancel_child_loss", cancel_child_loss)
            record_property("video_tracker_cancel_recovery_result", repr(cancel_result))

            async with _committed_api_client(app_module, test_engine) as api:
                await _revoke_queued_worlds(api, worlds)

            for label in ("removed", "disabled"):
                delivery, result, ack = await _deliver(
                    sender, worker, worlds[label]["job_id"]
                )
                deliveries.append(delivery)
                async with media.sessions() as db:
                    rejected = await db.get(VideoTrackerJob, worlds[label]["job_id"])
                    assert rejected is not None and rejected.status == "failed"
                    assert rejected.error_message in {
                        "permission_revoked",
                        "permission_denied",
                    }
                assert peer_specs[label].model_windows == []
                await _assert_terminal_execution(
                    test_engine, worlds[label]["job_id"], "failed"
                )
                record_property(f"video_tracker_{label}_ack", ack)
                record_property(f"video_tracker_{label}_result", repr(result))

            replay_world = worlds["replay"]
            replay_peer = peer_specs["replay"]
            bootstrap = await asyncio.to_thread(
                worker.bootstrap_tracker_routes, {"replay": replay_world}
            )
            assert bootstrap == [
                {
                    "registry_id": str(replay_world["registry_id"]),
                    "phase": "completed",
                }
            ]
            assert replay_peer.controller.phase == "bound"
            replay_delivery = await asyncio.to_thread(
                sender.send_task,
                _TASK_NAME,
                args=[str(replay_world["job_id"])],
                queue=worker.queue,
                task_id=str(replay_world["job_id"]),
            )
            deliveries.append(replay_delivery)
            await asyncio.wait_for(replay_peer.entered[0].wait(), timeout=60)
            replay_peer.release[0].set()
            replay_checkpoint = await _wait_window_checkpoint(
                test_engine, media.sessions, replay_world["job_id"], 0
            )
            replay_parts = await _first_window_parts(
                media.sessions, replay_world["job_id"]
            )
            await asyncio.wait_for(replay_peer.entered[1].wait(), timeout=60)
            inspection = await asyncio.to_thread(
                sender.control.inspect(timeout=2, destination=[worker.node_name]).active
            )
            active = [
                task
                for tasks in (inspection or {}).values()
                for task in tasks or []
                if task.get("id") == str(replay_world["job_id"])
            ]
            assert len(active) == 1 and type(active[0].get("worker_pid")) is int
            child_loss = worker.kill_prefork_child(active[0]["worker_pid"])
            await _expire_exact_claim(
                media.sessions,
                replay_world["job_id"],
                expected_generation=1,
            )
            replay_peer.release[1].set()
            async with asyncio.timeout(180):
                while (
                    await asyncio.to_thread(lambda: replay_delivery.state)
                ) != "SUCCESS":
                    await asyncio.sleep(0.25)
            replay_ack = await _wait_broker_ack(sender, worker, replay_world["job_id"])
            replay_result = await asyncio.to_thread(
                replay_delivery.get, timeout=10, propagate=False
            )
            async with media.sessions() as db:
                replayed = await db.get(VideoTrackerJob, replay_world["job_id"])
                assert replayed is not None and replayed.status == "pending_review"
                assert [
                    row["frame_index"]
                    for row in (replayed.staged_result or {}).get("results", [])
                ] == list(range(6))
                generation = await db.scalar(
                    text(
                        f"SELECT generation FROM {_JOBS_SCHEMA}.video_executions "
                        "WHERE job_id=:job"
                    ),
                    {"job": replay_world["job_id"]},
                )
            assert generation >= 2
            assert (
                await _first_window_parts(media.sessions, replay_world["job_id"])
                == replay_parts
            )
            replay_execution = await _assert_terminal_execution(
                test_engine,
                replay_world["job_id"],
                "completed",
                require_scope=True,
            )
            replay_scopes = await _scope_rows(media.sessions, replay_world["job_id"])
            assert replay_scopes and all(
                row["closed"] is True and row["state"] == "reconciled"
                for row in replay_scopes
            )
            assert len(replay_peer.model_windows) == 4
            assert [row["from_frame"] for row in replay_peer.model_windows] == [
                0,
                2,
                2,
                4,
            ]
            record_property("video_tracker_replay_ack", replay_ack)
            record_property("video_tracker_replay_result", repr(replay_result))
            record_property("video_tracker_replay_checkpoint", replay_checkpoint)
            record_property("video_tracker_replay_child_loss", child_loss)
            record_property(
                "video_tracker_recovery_latency_qualification",
                "SIGKILL replay uses exact fixture lease expiry; default production recovery latency remains unqualified.",
            )
            record_property("video_tracker_replay_retirement", replay_execution)

            audit_task = "app.workers.audit.persist_audit_entry"
            assert worker.task_signature(audit_task) == {
                "task": audit_task,
                "parameters": ["payload"],
            }
            audit_request_id = str(uuid.uuid4())
            anonymous_request_id = str(uuid.uuid4())
            audit_payload = {
                "actor_id": str(worlds["success"]["actor_id"]),
                "actor_role": "employee",
                "action": "request.POST",
                "method": "POST",
                "path": "/api/v1/fixture-audit",
                "status_code": 200,
                "ip": "127.0.0.1",
                "request_id": audit_request_id,
            }
            audit_acks = []
            for payload in (
                audit_payload,
                audit_payload,
                {
                    **audit_payload,
                    "actor_id": str(uuid.uuid4()),
                    "request_id": anonymous_request_id,
                },
            ):
                task_id = uuid.uuid4()
                delivery = await asyncio.to_thread(
                    sender.send_task,
                    audit_task,
                    args=[payload],
                    queue=worker.queue,
                    task_id=str(task_id),
                )
                deliveries.append(delivery)
                audit_acks.append(await _wait_broker_ack(sender, worker, task_id))
                assert await asyncio.to_thread(delivery.get, timeout=10) is None
            async with media.sessions() as db:
                rows = (
                    (
                        await db.execute(
                            text(
                                "SELECT actor_id,request_id,jsonb_typeof(detail_json) AS detail_kind "
                                "FROM public.audit_logs WHERE request_id IN (:known,:anonymous)"
                            ),
                            {
                                "known": audit_request_id,
                                "anonymous": anonymous_request_id,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
            known_rows = [row for row in rows if row["request_id"] == audit_request_id]
            anonymous_rows = [
                row for row in rows if row["request_id"] == anonymous_request_id
            ]
            assert len(known_rows) == 2 and all(
                row["actor_id"] == worlds["success"]["actor_id"] for row in known_rows
            )
            assert len(anonymous_rows) == 1 and anonymous_rows[0]["actor_id"] is None
            assert all(row["detail_kind"] == "null" for row in rows)
            record_property("audit_metadata_worker_acks", audit_acks)
            record_property("audit_metadata_duplicate_payload_rows", len(known_rows))
            record_property("audit_metadata_missing_actor_rows", len(anonymous_rows))

            for delivery in deliveries:
                await asyncio.to_thread(delivery.forget)
            record_property("video_tracker_cold_acceptance", "completed")
