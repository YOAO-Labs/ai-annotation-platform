"""Committed video execution through the durable Inference routing owner."""

from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from hashlib import sha256
import asyncio
import json
import os
import socket
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import asyncpg
from aap_assets_video_inputs.tracker import TrackerSourceReferenceRequest
from fastapi import FastAPI, HTTPException, Request
import httpx
import pytest
import uvicorn
from sqlalchemy import text

from aap_inference.routing_runtime import RoutingRuntime
from aap_inference.pipeline_server import PipelineHttpServer
from aap_inference_contract.routing import make_routing_command
from aap_inference_contract.routing import decode_routing_command, RoutingUnavailable
from aap_protocol_v2.routing import ROUTE_ADMISSION_HEADER
from aap_protocol_v2.routing_runtime import install_backend_routing_controller
from app.config import settings
from app.db.models.video_tracker_job import VideoTrackerJob
from app.services.ml_backend import MLBackendService
from app.services.video_tracking.jobs import cancel_tracker_job
from app.services.video_tracking.runner import run_tracker_job
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_tracker_input_live import _prepared, _seed_tracker, _silent_event
from tests.test_video_input_live import _video_bytes, _video_runtime


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified owned module-data environment",
    ),
]

_ROUTING_ROLE = "inference_routing_client"
_ROUTING_SCHEMA = "aap_inference_routing_v1"


def _reserve_loopback_listener() -> tuple[socket.socket, str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    return listener, f"http://127.0.0.1:{listener.getsockname()[1]}"


@asynccontextmanager
async def _routed_video_backend(
    listener: socket.socket,
    *,
    registry_id,
    control_token: str,
    storage_origin: str,
    expected_sha256: str,
):
    app = FastAPI()
    storage = httpx.AsyncClient(timeout=15, trust_env=False)
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls: list[dict] = []
    app.state.acceptance_calls = calls

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "loaded": False,
            "capabilities": {"supported_trackers": ["sam2_video"]},
        }

    @app.get("/setup")
    async def setup():
        return {
            "name": "routing-video-acceptance",
            "is_interactive": True,
            "supported_trackers": ["sam2_video"],
            "supported_prompts": ["text"],
            "supported_geometric_outputs": ["bbox"],
        }

    @app.post("/predict")
    async def predict(request: Request):
        payload = await request.json()
        task, context = payload.get("task"), payload.get("context")
        if not isinstance(task, dict) or not isinstance(context, dict):
            raise HTTPException(status_code=400, detail="invalid_tracker_request")
        source_url = task.get("file_path")
        if not isinstance(source_url, str):
            raise HTTPException(status_code=400, detail="missing_accepted_source")
        parsed = urlsplit(source_url)
        query = parse_qs(parsed.query)
        origin = urlsplit(storage_origin)
        if not (
            parsed.scheme == origin.scheme
            and parsed.netloc == origin.netloc
            and not parsed.username
            and not parsed.password
            and "/accepted-video-inputs/" in parsed.path
            and parsed.path.endswith("/source")
            and len(query.get("versionId", [])) == 1
            and query["versionId"][0] not in {"", "null"}
            and ("X-Amz-Signature" in query or "AWSAccessKeyId" in query)
        ):
            raise HTTPException(status_code=502, detail="invalid_accepted_source_scope")
        try:
            source_response = await storage.get(source_url)
            source_response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail="accepted_source_unavailable"
            ) from exc
        source_body = source_response.content
        source_sha256 = sha256(source_body).hexdigest()
        first, last = context.get("from_frame"), context.get("to_frame")
        if (
            context.get("type") != "video_tracker"
            or type(first) is not int
            or type(last) is not int
            or first < 0
            or last < first
            or source_sha256 != expected_sha256
        ):
            raise HTTPException(status_code=502, detail="invalid_tracker_execution")

        call_index = len(calls)
        calls.append(
            {
                "job_id": context.get("job_id"),
                "from_frame": first,
                "to_frame": last,
                "source_sha256": source_sha256,
                "source_size": len(source_body),
                "source_version_id": query["versionId"][0],
                "admission_header_present": bool(
                    request.headers.get(ROUTE_ADMISSION_HEADER)
                ),
                "authorization_bound": request.headers.get("authorization")
                == f"Bearer {control_token}",
            }
        )
        if call_index < len(entered):
            entered[call_index].set()
            try:
                await asyncio.wait_for(release[call_index].wait(), timeout=30)
            except TimeoutError as exc:
                raise HTTPException(
                    status_code=504, detail="acceptance_gate_timeout"
                ) from exc

        return {
            "result": [
                {
                    "frame_index": frame,
                    "geometry": {
                        "type": "bbox",
                        "x": 0.1,
                        "y": 0.2,
                        "w": 0.3,
                        "h": 0.4,
                    },
                    "confidence": 0.9,
                }
                for frame in range(first, last + 1)
            ],
            "model_version": "routing-video-acceptance",
        }

    controller = install_backend_routing_controller(
        app,
        registry_id=registry_id,
        environ={
            "AAP_ROUTE_CONTROL_TOKEN": control_token,
            "AAP_ROUTE_ALLOW_LEGACY_EXECUTION": "0",
        },
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=int(listener.getsockname()[1]),
            log_level="critical",
            access_log=False,
            lifespan="off",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                    raise AssertionError("video routing backend stopped before startup")
                await asyncio.sleep(0.01)
        yield SimpleNamespace(
            controller=controller,
            calls=calls,
            entered=entered,
            release=release,
        )
    finally:
        for gate in release:
            gate.set()
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except asyncio.TimeoutError:
            server.force_exit = True
            await asyncio.wait_for(task, timeout=5)
        listener.close()
        await storage.aclose()


async def _wait_for_gate(gate: asyncio.Event, running: asyncio.Task) -> None:
    async with asyncio.timeout(20):
        while not gate.is_set():
            if running.done():
                result = await running
                pytest.fail(
                    "video job ended before the backend gate: "
                    f"status={getattr(result, 'status', None)}"
                )
            await asyncio.sleep(0.025)


async def _routing_scope(session_factory, owner: str):
    async with session_factory() as db:
        row = (
            (
                await db.execute(
                    text(
                        f"SELECT id, registry_id, pool_id, project_id, owner, operation, "
                        "state, closed, heartbeat_deadline FROM "
                        f"{_ROUTING_SCHEMA}.scopes WHERE owner=:owner"
                    ),
                    {"owner": owner},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


async def _wait_for_heartbeat(session_factory, owner: str, previous_deadline):
    async with asyncio.timeout(8):
        while True:
            current = await _routing_scope(session_factory, owner)
            if current["heartbeat_deadline"] > previous_deadline:
                return current
            await asyncio.sleep(0.05)


async def _verify_restricted_routing_dsn(dsn: str, expected_database: str) -> None:
    connection = await asyncpg.connect(
        dsn.replace("postgresql+asyncpg://", "postgresql://", 1),
        timeout=5,
        command_timeout=10,
    )
    try:
        role, database = await connection.fetchrow(
            "SELECT current_user, current_database()"
        )
        assert role == _ROUTING_ROLE
        assert database == expected_database
    finally:
        await connection.close(timeout=2)


@pytest.mark.parametrize("binding", ("embedded", "dsn", "http"))
async def test_committed_video_job_heartbeats_cancels_and_closes_one_route_scope(
    test_engine, tmp_path, monkeypatch, binding
):
    ownership = await assert_owned_test_database(test_engine)
    routing_dsn = os.environ.get("AAP_MODULE_DATA_INFERENCE_ROUTING_URL")
    assert routing_dsn, "the module-data verifier must provide the routing LOGIN DSN"
    await _verify_restricted_routing_dsn(routing_dsn, ownership["database"])

    async with _video_runtime(test_engine, tmp_path) as media:
        video = await asyncio.to_thread(_video_bytes, tmp_path)
        listener, backend_url = _reserve_loopback_listener()
        backend_token = f"video-route-control-{uuid4().hex}"
        owner_runtime = None
        owner_server = None
        running = None
        try:
            (
                actor_id,
                project_id,
                _item_id,
                task_id,
                job_id,
                _source_key,
                _record,
            ) = await _seed_tracker(media, video, backend_url=backend_url)
            async with media.sessions.begin() as db:
                pool_id = await db.scalar(
                    text("SELECT ml_backend_pool_id FROM public.projects WHERE id=:id"),
                    {"id": project_id},
                )
                registry_id = await db.scalar(
                    text(
                        "SELECT registry_id FROM public.ml_backend_pool_members "
                        "WHERE pool_id=:pool AND traffic_state='active'"
                    ),
                    {"pool": pool_id},
                )
                assert pool_id is not None and registry_id is not None
                await db.execute(
                    text(
                        "UPDATE public.ml_backend_registry SET auth_method='token', "
                        "auth_token=:token, state='connected', last_checked_at="
                        "pg_catalog.clock_timestamp(), extra_params=CAST(:extra AS jsonb) "
                        "WHERE id=:registry"
                    ),
                    {
                        "token": backend_token,
                        "extra": json.dumps({"device": "cpu", "max_concurrency": 2}),
                        "registry": registry_id,
                    },
                )

            async with _routed_video_backend(
                listener,
                registry_id=registry_id,
                control_token=backend_token,
                storage_origin=media.endpoint,
                expected_sha256=sha256(video).hexdigest(),
            ) as backend:
                listener = None
                # Changing transport configuration invalidates prior health.
                # Re-observe this actual backend through the existing owner.
                async with media.sessions.begin() as health_db:
                    health_service = MLBackendService(health_db)
                    assert await health_service.check_health(registry_id)
                    resolved = (
                        await health_service.get_tracker_backend_for_capabilities(
                            project_id, ["sam2_video"]
                        )
                    )
                    assert resolved is not None and resolved.id == registry_id
                input_client, input_request, ready = await _prepared(
                    media, tmp_path, actor_id, task_id, job_id
                )
                reference = await input_client.reference(
                    TrackerSourceReferenceRequest(
                        **input_request.model_dump(), plan_digest=ready.plan_digest
                    )
                )
                source_response = await media.http.get(reference.url)
                source_response.raise_for_status()
                assert source_response.content == video
                await source_response.aclose()
                assert reference.source_version.sha256 == sha256(video).hexdigest()
                assert reference.source_version.byte_size == len(video)

                monkeypatch.setattr(settings, "assets_media_url", media.origin)
                monkeypatch.setattr(settings, "assets_media_token", media.token)
                monkeypatch.setattr(settings, "inference_routing_url", "")
                monkeypatch.setattr(settings, "inference_routing_token", "")
                monkeypatch.setattr(settings, "inference_routing_token_file", "")
                monkeypatch.setattr(
                    settings,
                    "inference_routing_database_url",
                    routing_dsn if binding == "dsn" else "",
                )
                monkeypatch.setattr(settings, "inference_routing_database_url_file", "")
                monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
                monkeypatch.setattr(
                    settings, "ml_backend_router_heartbeat_interval_seconds", 1
                )
                monkeypatch.setattr(settings, "ml_backend_router_lease_ttl_seconds", 30)
                monkeypatch.setattr(settings, "video_tracker_window_size_frames", 2)

                owner_runtime = RoutingRuntime(routing_dsn)
                assert await owner_runtime.readiness()
                if binding == "http":
                    owner_listener, owner_url = _reserve_loopback_listener()
                    owner_port = owner_listener.getsockname()[1]
                    owner_listener.close()
                    owner_token = uuid4().hex
                    owner_server = PipelineHttpServer(
                        manager=None,
                        data=None,
                        control_token=None,
                        host="127.0.0.1",
                        port=owner_port,
                        routing_manager=owner_runtime.manager,
                        routing_control_token=owner_token,
                        scope_retirement_manager=owner_runtime.scope_retirement_manager,
                    )
                    await owner_server.start()
                    monkeypatch.setattr(settings, "inference_routing_url", owner_url)
                    monkeypatch.setattr(
                        settings, "inference_routing_token", owner_token
                    )
                reconcile = await owner_runtime.execute(
                    make_routing_command(
                        "reconcile",
                        {
                            "registry_id": str(registry_id),
                            "control_id": str(uuid4()),
                        },
                    )
                )
                assert reconcile.state == "committed"
                assert reconcile.result.phase == "completed"
                assert reconcile.result.registry_id == registry_id
                assert backend.controller.phase == "bound"

                owner_name = f"tracker:{job_id}"
                async with media.sessions() as db:
                    job = await db.get(VideoTrackerJob, job_id)
                    assert job is not None and job.status == "queued"
                    running = asyncio.create_task(
                        run_tracker_job(
                            db,
                            job_id,
                            publisher=_silent_event,
                            shadow_session_factory=media.sessions,
                        )
                    )
                    try:
                        await _wait_for_gate(backend.entered[0], running)
                        first_scope = await _routing_scope(media.sessions, owner_name)
                        assert first_scope["registry_id"] == registry_id
                        assert first_scope["pool_id"] == pool_id
                        assert first_scope["project_id"] == project_id
                        assert first_scope["operation"] == "tracker"
                        assert first_scope["state"] == "active"
                        async with media.sessions() as observed:
                            command = await observed.scalar(
                                text(
                                    "SELECT public.pgp_sym_decrypt("
                                    "intent.command_ciphertext,pg_catalog.encode("
                                    "aap_jobs_v1._video_execution_secret(),'hex')) "
                                    "FROM aap_jobs_v1.video_execution_route_intents intent "
                                    "JOIN aap_jobs_v1.video_executions execution "
                                    "ON execution.route_request_id=intent.request_id "
                                    "AND execution.job_id=intent.job_id "
                                    "AND execution.generation=intent.generation "
                                    "WHERE intent.job_id=:job AND intent.state='bound'"
                                ),
                                {"job": job_id},
                            )
                        assert command is not None
                        replay = await owner_runtime.execute(command)
                        assert replay.state == "committed"
                        assert replay.result.id == first_scope["id"]
                        assert len(backend.calls) == 1
                        heartbeated = await _wait_for_heartbeat(
                            media.sessions,
                            owner_name,
                            first_scope["heartbeat_deadline"],
                        )
                        assert heartbeated["id"] == first_scope["id"]

                        backend.release[0].set()
                        await _wait_for_gate(backend.entered[1], running)
                        async with media.sessions.begin() as cancel_db:
                            await cancel_tracker_job(
                                cancel_db, job_id, actor_id=owner_name
                            )
                        cancelling_scope = await _routing_scope(
                            media.sessions, owner_name
                        )
                        await _wait_for_heartbeat(
                            media.sessions,
                            owner_name,
                            cancelling_scope["heartbeat_deadline"],
                        )
                        backend.release[1].set()
                        result = await asyncio.wait_for(running, timeout=60)
                    finally:
                        for gate in backend.release:
                            gate.set()
                        if not running.done():
                            with suppress(BaseException):
                                await asyncio.wait_for(running, timeout=20)
                        if not running.done():
                            running.cancel()
                            await asyncio.gather(running, return_exceptions=True)

                assert result.status == "cancelled"
                assert [
                    item["frame_index"] for item in result.staged_result["results"]
                ] == [0, 1]
                assert len(backend.calls) == 2
                assert [
                    (item["from_frame"], item["to_frame"]) for item in backend.calls
                ] == [(0, 1), (2, 3)]
                assert all(
                    item["source_sha256"] == sha256(video).hexdigest()
                    for item in backend.calls
                )
                assert all(item["source_size"] == len(video) for item in backend.calls)
                assert {item["source_version_id"] for item in backend.calls} == {
                    reference.source_version.version_id
                }
                assert all(item["job_id"] == str(job_id) for item in backend.calls)
                assert all(item["admission_header_present"] for item in backend.calls)
                assert all(item["authorization_bound"] for item in backend.calls)
                assert backend.controller.active_requests == 0

                # A definite stale/terminal preflight cannot recover an old
                # success through the ambiguous-commit status fallback.
                from app.services.ml_routing import authority as routing_authority

                replay_command = decode_routing_command(command)
                with monkeypatch.context() as replay_settings:
                    replay_settings.setattr(
                        routing_authority,
                        "make_routing_command",
                        lambda *_args: replay_command,
                    )
                    with pytest.raises(RoutingUnavailable):
                        await routing_authority.RoutingAuthority(test_engine).execute(
                            "video_acquire", replay_command.body
                        )
                assert len(backend.calls) == 2

                async with media.sessions() as db:
                    scopes = (
                        (
                            await db.execute(
                                text(
                                    f"SELECT id, registry_id, pool_id, project_id, owner, "
                                    f"operation, state, closed FROM {_ROUTING_SCHEMA}.scopes "
                                    "WHERE owner=:owner"
                                ),
                                {"owner": owner_name},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    assert len(scopes) == 1
                    scope = scopes[0]
                    assert scope["id"] == heartbeated["id"]
                    assert scope["registry_id"] == registry_id
                    assert scope["pool_id"] == pool_id
                    assert scope["project_id"] == project_id
                    assert scope["operation"] == "tracker"
                    assert scope["state"] == "reconciled" and scope["closed"] is True
                    assert (
                        await db.scalar(
                            text(
                                f"SELECT phase FROM {_ROUTING_SCHEMA}.scope_retirements WHERE scope_id=:scope"
                            ),
                            {"scope": scope["id"]},
                        )
                        == "completed"
                    )
                    journal = (
                        await db.execute(
                            text(
                                "SELECT writer_terminal,writer_terminal_outcome,execution_retired,"
                                "plan_document #>> '{source,source_version_id}' AS source_version "
                                "FROM aap_jobs_v1.video_execution_journals WHERE job_id=:job"
                            ),
                            {"job": job_id},
                        )
                    ).one()
                    assert tuple(journal) == (
                        True,
                        "cancelled",
                        True,
                        reference.source_version.version_id,
                    )
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM aap_jobs_v1.video_execution_windows WHERE job_id=:job"
                            ),
                            {"job": job_id},
                        )
                        == 1
                    )
                    attempts = (
                        (
                            await db.execute(
                                text(
                                    f"SELECT scope_id, method, path, state, status_code "
                                    f"FROM {_ROUTING_SCHEMA}.attempts WHERE scope_id=:scope "
                                    "ORDER BY created_at,id"
                                ),
                                {"scope": scope["id"]},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    assert len(attempts) == 2
                    assert all(item["scope_id"] == scope["id"] for item in attempts)
                    assert all(item["method"] == "POST" for item in attempts)
                    assert all(item["path"] == "/predict" for item in attempts)
                    assert all(
                        item["state"] == "response" and item["status_code"] == 200
                        for item in attempts
                    )
        finally:
            if listener is not None:
                listener.close()
            if running is not None and not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            if owner_server is not None:
                await owner_server.close()
            if owner_runtime is not None:
                await owner_runtime.close()
