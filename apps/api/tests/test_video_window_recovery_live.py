"""Real-owner SIGKILL recovery through the video window journal and route retirement."""

from __future__ import annotations

from contextlib import asynccontextmanager
from hashlib import sha256
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from aap_assets_video_inputs.tracker import TrackerSourceReferenceRequest
from aap_inference.pipeline_server import PipelineHttpServer
from aap_inference.routing_runtime import RoutingRuntime
from aap_inference_contract.routing import make_routing_command
from aap_protocol_v2.routing import ROUTE_ADMISSION_HEADER
from fastapi import FastAPI, HTTPException, Request
import httpx
import pytest
import uvicorn
from aap_protocol_v2.routing_runtime import install_backend_routing_controller
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.ml_backend import MLBackendService
from app.services.video_tracking.execution import TrackerExecutionDeferred
from app.services.video_tracking.runner import run_tracker_job
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_assets_media_live import (
    _free_loopback_port,
    _stop_process,
)
from tests.test_inference_routing_video import (
    _verify_restricted_routing_dsn,
)
from tests.test_tracker_input_live import (
    _prepared,
    _seed_tracker,
    _silent_event,
)
from tests.test_video_input_live import _video_bytes, _video_runtime

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified owned module-data environment",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
_ROUTING_SCHEMA = "aap_inference_routing_v1"
_JOBS_SCHEMA = "aap_jobs_v1"


def _reserve_port() -> tuple[socket.socket, str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    return listener, f"http://127.0.0.1:{listener.getsockname()[1]}"


def _bbox_artifact() -> tuple[bytes, dict, str]:
    artifact = {
        "schema": "aap-test-cpu-bbox-artifact/v1",
        "model_id": "owned-cpu-bbox-1",
        "weights_release_id": "owned-cpu-bbox-fixture-2026-10-22",
        "bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
    }
    raw = json.dumps(
        artifact,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return raw, json.loads(raw), sha256(raw).hexdigest()


@asynccontextmanager
async def _checkpoint_cpu_backend(
    listener: socket.socket,
    *,
    registry_id,
    control_token: str,
    storage_origin: str,
    expected_source_sha256: str,
    context_mode: str,
    artifact: dict,
    weights_sha256: str,
):
    app = FastAPI()
    storage = httpx.AsyncClient(timeout=15, trust_env=False)
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls: list[dict] = []
    model_windows: list[dict] = []
    sessions: dict[str, dict] = {}
    capability_model = {
        "id": artifact["model_id"],
        "display_name": "Owned deterministic CPU bbox fixture",
        "task": "tracker",
        "is_interactive": True,
        "supported_prompts": ["text"],
        "supported_trackers": ["sam2_video"],
        "supported_geometric_outputs": ["bbox"],
        "max_window_frames": 2,
        "tracker_context_mode": context_mode,
        "weights_release_id": artifact["weights_release_id"],
        "weights_sha256": weights_sha256,
    }
    if context_mode == "session":
        capability_model["max_context_frames"] = 4

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
            "name": "owned-cpu-window-checkpoint",
            "is_interactive": True,
            "models": [capability_model],
        }

    @app.post("/predict")
    async def predict(request: Request):
        payload = await request.json()
        task, context = payload.get("task"), payload.get("context")
        if not isinstance(task, dict) or not isinstance(context, dict):
            raise HTTPException(status_code=400, detail="invalid_tracker_request")
        if context.get("model_id") != artifact["model_id"]:
            raise HTTPException(status_code=409, detail="model_identity_mismatch")
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
            or source_sha256 != expected_source_sha256
        ):
            raise HTTPException(status_code=502, detail="invalid_tracker_execution")

        tracker_context = context.get("tracker_context") or {}
        action = tracker_context.get("action") if context_mode == "session" else None
        token = tracker_context.get("token") if context_mode == "session" else None
        if context_mode == "session":
            if action == "start":
                if token is not None:
                    raise HTTPException(status_code=409, detail="start_has_old_token")
                token = uuid4().hex
                sessions[token] = {
                    "job_id": context.get("job_id"),
                    "direction": context.get("direction"),
                    "span_start_frame": tracker_context.get("span_start_frame"),
                    "span_end_frame": tracker_context.get("span_end_frame"),
                }
            elif action in {"continue", "close"}:
                active = sessions.get(token) if isinstance(token, str) else None
                if active is None or active["job_id"] != context.get("job_id"):
                    raise HTTPException(status_code=409, detail="unknown_context_token")
            else:
                raise HTTPException(status_code=409, detail="invalid_context_action")
            if action == "close":
                calls.append(
                    {
                        "kind": "close",
                        "action": action,
                        "token": token,
                        "source_sha256": source_sha256,
                        "source_version_id": query["versionId"][0],
                    }
                )
                sessions.pop(token, None)
                return {"result": [], "meta": {"tracker_context": {}}}

        call_index = len(model_windows)
        call = {
            "kind": "model",
            "action": action,
            "token": token,
            "from_frame": first,
            "to_frame": last,
            "direction": context.get("direction"),
            "job_id": context.get("job_id"),
            "source_sha256": source_sha256,
            "source_size": len(source_body),
            "source_version_id": query["versionId"][0],
            "admission_header_present": bool(
                request.headers.get(ROUTE_ADMISSION_HEADER)
            ),
            "authorization_bound": request.headers.get("authorization")
            == f"Bearer {control_token}",
        }
        calls.append(call)
        model_windows.append(call)
        if call_index < len(entered):
            entered[call_index].set()
            try:
                await asyncio.wait_for(release[call_index].wait(), timeout=45)
            except TimeoutError as exc:
                raise HTTPException(
                    status_code=504, detail="acceptance_gate_timeout"
                ) from exc

        result = [
            {
                "frame_index": frame,
                "geometry": {"type": "bbox", **artifact["bbox"]},
                "confidence": 1.0,
            }
            for frame in range(first, last + 1)
        ]
        meta = {
            "artifact_sha256": weights_sha256,
            "weights_release_id": artifact["weights_release_id"],
        }
        if context_mode == "session":
            meta["tracker_context"] = {"token": token}
        return {"result": result, "meta": meta}

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
    stopped = False

    async def stop_peer() -> None:
        nonlocal stopped
        if stopped:
            return
        for gate in release:
            gate.set()
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except TimeoutError:
            server.force_exit = True
            await asyncio.wait_for(task, timeout=5)
        listener.close()
        await storage.aclose()
        stopped = True

    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                    raise AssertionError("CPU video backend stopped before startup")
                await asyncio.sleep(0.01)
        yield SimpleNamespace(
            controller=controller,
            calls=calls,
            model_windows=model_windows,
            entered=entered,
            release=release,
            sessions=sessions,
            origin=f"http://127.0.0.1:{listener.getsockname()[1]}",
            server_task=task,
            stop=stop_peer,
            is_stopped=lambda: stopped,
        )
    finally:
        await stop_peer()


def _worker_environment(
    *,
    database_url: str,
    media_url: str,
    media_token: str,
    routing_url: str,
    routing_token: str,
) -> dict[str, str]:
    source_paths = [
        ROOT / "apps" / "api",
        ROOT / "apps" / "assets-video-inputs" / "src",
        ROOT / "apps" / "preannotation-results" / "src",
        ROOT / "apps" / "assets-media" / "src",
        ROOT / "apps" / "media-probe" / "src",
        ROOT / "apps" / "inference" / "src",
        *sorted((ROOT / "apps" / "_shared").glob("*/src")),
    ]
    inherited_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath = os.pathsep.join(
        [str(path) for path in source_paths]
        + ([inherited_pythonpath] if inherited_pythonpath else [])
    )
    return {
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": pythonpath,
        "PYTHONUNBUFFERED": "1",
        "DATABASE_URL": database_url,
        "ASSETS_MEDIA_URL": media_url,
        "ASSETS_MEDIA_TOKEN": media_token,
        "ASSETS_MEDIA_TOKEN_FILE": "",
        "INFERENCE_ROUTING_URL": routing_url,
        "INFERENCE_ROUTING_TOKEN": routing_token,
        "INFERENCE_ROUTING_TOKEN_FILE": "",
        "ML_BACKEND_ROUTER_MODE": "enforce",
        "ML_BACKEND_ROUTER_LEASE_TTL_SECONDS": "30",
        "ML_BACKEND_ROUTER_HEARTBEAT_INTERVAL_SECONDS": "1",
        "VIDEO_TRACKER_WINDOW_SIZE_FRAMES": "2",
        "VIDEO_TRACKER_SAM3_WINDOW_SIZE_FRAMES": "2",
        "VIDEO_TRACKER_LOW_CONFIDENCE_OUTSIDE_THRESHOLD": str(
            settings.video_tracker_low_confidence_outside_threshold
        ),
        "INFERENCE_ROUTING_TIMEOUT_SECONDS": "20",
    }


def _start_runner_peer(job_id, environment: dict[str, str]):
    helper = (
        ROOT / "apps" / "api" / "tests" / "fixtures" / "video_window_runner_peer.py"
    )
    return subprocess.Popen(
        [sys.executable, str(helper), str(job_id)],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


async def _wait_entered(backend, index: int, process) -> None:
    async with asyncio.timeout(30):
        while not backend.entered[index].is_set():
            if process.poll() is not None:
                pytest.fail("tracker worker exited before the controlled model request")
            await asyncio.sleep(0.025)


async def _first_checkpoint(sessions_factory, job_id):
    async with sessions_factory() as db:
        return (
            (
                await db.execute(
                    text(
                        f"SELECT checkpoint_id,manifest_digest,manifest FROM "
                        f"{_JOBS_SCHEMA}.video_execution_windows "
                        "WHERE job_id=:job AND window_ordinal=0"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .one_or_none()
        )


async def _wait_first_checkpoint(sessions_factory, job_id, process):
    async with asyncio.timeout(15):
        while True:
            if process.poll() is not None:
                pytest.fail("tracker worker exited before committing its first window")
            row = await _first_checkpoint(sessions_factory, job_id)
            if row is not None:
                return dict(row)
            await asyncio.sleep(0.025)


async def _first_window_parts(sessions_factory, job_id):
    async with sessions_factory() as db:
        rows = (
            await db.execute(
                text(
                    f"SELECT part_index,part_digest,part_bytes FROM "
                    f"{_JOBS_SCHEMA}.video_execution_window_parts "
                    "WHERE job_id=:job AND window_ordinal=0 ORDER BY part_index"
                ),
                {"job": job_id},
            )
        ).all()
    return [(row.part_index, row.part_digest, bytes(row.part_bytes)) for row in rows]


async def _snapshot_claim(sessions_factory, job_id):
    async with sessions_factory() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT run_id,generation,worker_id,state,lease_expires_at "
                        f"FROM {_JOBS_SCHEMA}.video_executions WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _expire_exact_claim(sessions_factory, job_id, expected_generation: int):
    async with sessions_factory.begin() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT run_id,generation,worker_id,lease_expires_at "
                        f"FROM {_JOBS_SCHEMA}.video_executions "
                        "WHERE job_id=:job FOR UPDATE"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["generation"] == expected_generation
        result = await db.execute(
            text(
                "UPDATE aap_jobs_v1.video_executions "
                "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
                "WHERE job_id=:job AND run_id=:run AND generation=:generation "
                "AND worker_id=:worker AND lease_expires_at>pg_catalog.clock_timestamp()"
            ),
            {
                "job": job_id,
                "run": row["run_id"],
                "generation": row["generation"],
                "worker": row["worker_id"],
            },
        )
        assert result.rowcount == 1


async def _scope_rows(sessions_factory, job_id):
    async with sessions_factory() as db:
        rows = (
            (
                await db.execute(
                    text(
                        f"SELECT id,registry_id,authority_epoch,state,closed FROM "
                        f"{_ROUTING_SCHEMA}.scopes WHERE owner=:owner ORDER BY created_at,id"
                    ),
                    {"owner": f"tracker:{job_id}"},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _assert_recovery_is_draining(sessions_factory, job_id, old_scope_id):
    async with sessions_factory() as db:
        row = (
            (
                await db.execute(
                    text(
                        f"SELECT scope_id,phase FROM {_ROUTING_SCHEMA}.scope_retirements "
                        "WHERE scope_id=:scope"
                    ),
                    {"scope": old_scope_id},
                )
            )
            .mappings()
            .one()
        )
        scopes = (
            (
                await db.execute(
                    text(f"SELECT id FROM {_ROUTING_SCHEMA}.scopes WHERE owner=:owner"),
                    {"owner": f"tracker:{job_id}"},
                )
            )
            .scalars()
            .all()
        )
    assert row["phase"] == "draining"
    assert list(scopes) == [old_scope_id]


async def _wait_backend_idle(backend) -> None:
    async with asyncio.timeout(20):
        while backend.controller.active_requests != 0:
            await asyncio.sleep(0.025)


async def _attempt_tracker_run(
    sessions_factory, job_id, *, lose_completion_reply=False
) -> None:
    async with asyncio.timeout(45):
        async with sessions_factory() as db:
            lost_reply = False
            if lose_completion_reply:
                original_commit = db.commit

                async def commit_then_lose_reply():
                    nonlocal lost_reply
                    finishing = (
                        await db.scalar(
                            text(
                                "SELECT status='pending_review' FROM public.video_tracker_jobs WHERE id=:job"
                            ),
                            {"job": job_id},
                        )
                        is True
                    )
                    await original_commit()
                    if finishing and not lost_reply:
                        lost_reply = True
                        raise OSError(
                            "test: final commit reply lost after server commit"
                        )

                db.commit = commit_then_lose_reply
            result = await run_tracker_job(
                db,
                job_id,
                publisher=_silent_event,
                shadow_session_factory=sessions_factory,
            )
            if lose_completion_reply:
                assert lost_reply
                assert result is not None and result.status == "pending_review"


@pytest.mark.parametrize(
    ("context_mode", "lose_completion_reply", "cancel_after_kill"),
    [
        pytest.param("none", False, False, id="none"),
        pytest.param("session", False, False, id="session"),
        pytest.param("none", True, False, id="none-True"),
        pytest.param("none", False, True, id="cancelled-prefix"),
    ],
)
async def test_sigkill_replays_only_open_video_context_after_actual_scope_retirement(
    test_engine,
    tmp_path,
    monkeypatch,
    context_mode,
    lose_completion_reply,
    cancel_after_kill,
):
    ownership = await assert_owned_test_database(test_engine)
    routing_dsn = os.environ.get("AAP_MODULE_DATA_INFERENCE_ROUTING_URL")
    assert routing_dsn, "the module-data verifier must provide the routing LOGIN DSN"
    legacy_dsn = os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"]
    await _verify_restricted_routing_dsn(routing_dsn, ownership["database"])

    async with _video_runtime(test_engine, tmp_path) as media:
        video = await asyncio.to_thread(_video_bytes, tmp_path)
        artifact_bytes, artifact, artifact_digest = _bbox_artifact()
        listener, backend_url = _reserve_port()
        backend_token = f"owned-window-backend-{uuid4().hex}"
        owner_runtime = None
        owner_server = None
        peer = None
        execution_engine = create_async_engine(legacy_dsn, hide_parameters=True)
        execution_sessions = async_sessionmaker(
            execution_engine, expire_on_commit=False
        )
        try:
            (
                actor_id,
                project_id,
                item_id,
                task_id,
                job_id,
                _source_key,
                _input_record,
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
                        "extra": json.dumps(
                            {"device": "cpu", "max_concurrency": 2},
                            separators=(",", ":"),
                        ),
                        "registry": registry_id,
                    },
                )

            async with _checkpoint_cpu_backend(
                listener,
                registry_id=registry_id,
                control_token=backend_token,
                storage_origin=media.endpoint,
                expected_source_sha256=sha256(video).hexdigest(),
                context_mode=context_mode,
                artifact=artifact,
                weights_sha256=artifact_digest,
            ) as backend:
                listener = None
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
                response = await media.http.get(reference.url)
                response.raise_for_status()
                assert response.content == video
                await response.aclose()
                assert reference.source_version.sha256 == sha256(video).hexdigest()
                assert reference.source_version.byte_size == len(video)

                monkeypatch.setattr(settings, "assets_media_url", media.origin)
                monkeypatch.setattr(settings, "assets_media_token", media.token)
                monkeypatch.setattr(settings, "inference_routing_url", "")
                monkeypatch.setattr(settings, "inference_routing_token", "")
                monkeypatch.setattr(settings, "inference_routing_token_file", "")
                monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
                monkeypatch.setattr(
                    settings, "ml_backend_router_heartbeat_interval_seconds", 1
                )
                monkeypatch.setattr(settings, "ml_backend_router_lease_ttl_seconds", 30)
                monkeypatch.setattr(settings, "video_tracker_window_size_frames", 2)

                owner_runtime = RoutingRuntime(routing_dsn)
                assert await owner_runtime.readiness()
                owner_port = _free_loopback_port()
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
                owner_url = f"http://127.0.0.1:{owner_port}"
                monkeypatch.setattr(settings, "inference_routing_url", owner_url)
                monkeypatch.setattr(settings, "inference_routing_token", owner_token)

                reconcile = await owner_runtime.execute(
                    make_routing_command(
                        "reconcile",
                        {"registry_id": str(registry_id), "control_id": str(uuid4())},
                    )
                )
                assert reconcile.state == "committed"
                assert reconcile.result.phase == "completed"
                assert reconcile.result.registry_id == registry_id
                assert backend.controller.phase == "bound"

                peer = _start_runner_peer(
                    job_id,
                    _worker_environment(
                        database_url=legacy_dsn,
                        media_url=media.origin,
                        media_token=media.token,
                        routing_url=owner_url,
                        routing_token=owner_token,
                    ),
                )

                await _wait_entered(backend, 0, peer)
                assert backend.controller.active_requests == 1
                backend.release[0].set()
                await _wait_entered(backend, 1, peer)
                assert backend.controller.active_requests == 1
                first_window = await _wait_first_checkpoint(
                    media.sessions, job_id, peer
                )
                assert first_window["manifest"]["window_ordinal"] == 0
                first_parts = await _first_window_parts(media.sessions, job_id)
                assert first_parts

                old_scope_rows = await _scope_rows(media.sessions, job_id)
                assert len(old_scope_rows) == 1
                old_scope = old_scope_rows[0]
                assert old_scope["registry_id"] == registry_id
                async with media.sessions() as db:
                    frozen = (
                        (
                            await db.execute(
                                text(
                                    "SELECT plan_digest,plan_document FROM "
                                    f"{_JOBS_SCHEMA}.video_execution_journals "
                                    "WHERE job_id=:job"
                                ),
                                {"job": job_id},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    identity = frozen["plan_document"]["backend_compatibility"][0]
                    runtime_identity = (
                        (
                            await db.execute(
                                text(
                                    f"SELECT authority_epoch,backend_incarnation,continuity "
                                    f"FROM {_ROUTING_SCHEMA}.registry_state "
                                    "WHERE registry_id=:registry"
                                ),
                                {"registry": registry_id},
                            )
                        )
                        .mappings()
                        .one()
                    )
                assert identity["model_id"] == artifact["model_id"]
                assert identity["weights_release_id"] == artifact["weights_release_id"]
                assert identity["weights_sha256"] == artifact_digest
                assert identity["backend_incarnation"] == str(
                    runtime_identity["backend_incarnation"]
                )
                assert (
                    old_scope["authority_epoch"] == runtime_identity["authority_epoch"]
                )
                assert runtime_identity["continuity"] == "ready"

                claim_before_kill = await _snapshot_claim(media.sessions, job_id)
                assert claim_before_kill["generation"] == 1
                os.killpg(peer.pid, signal.SIGKILL)
                assert peer.wait(timeout=5) == -signal.SIGKILL
                assert backend.controller.active_requests == 1

                if cancel_after_kill:
                    async with media.sessions.begin() as db:
                        await db.execute(
                            text(
                                "UPDATE public.video_tracker_jobs SET status='cancelled', "
                                "cancel_requested_at=pg_catalog.clock_timestamp() WHERE id=:job"
                            ),
                            {"job": job_id},
                        )
                await _expire_exact_claim(media.sessions, job_id, expected_generation=1)
                with pytest.raises(
                    TrackerExecutionDeferred, match="scope_retirement_pending"
                ):
                    await _attempt_tracker_run(execution_sessions, job_id)
                await _assert_recovery_is_draining(
                    media.sessions, job_id, old_scope["id"]
                )
                claim_during_drain = await _snapshot_claim(media.sessions, job_id)
                assert claim_during_drain["generation"] == 2
                assert claim_during_drain["state"] == "recovery_required"
                assert backend.controller.active_requests == 1
                assert len(backend.model_windows) == 2
                assert len(await _scope_rows(media.sessions, job_id)) == 1

                backend.release[1].set()
                await _wait_backend_idle(backend)
                await _expire_exact_claim(media.sessions, job_id, expected_generation=2)
                await _attempt_tracker_run(
                    execution_sessions,
                    job_id,
                    lose_completion_reply=lose_completion_reply,
                )

                async with media.sessions() as db:
                    job_row = (
                        (
                            await db.execute(
                                text(
                                    "SELECT status,staged_result FROM public.video_tracker_jobs "
                                    "WHERE id=:job"
                                ),
                                {"job": job_id},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    frozen_after = (
                        (
                            await db.execute(
                                text(
                                    "SELECT plan_digest,plan_document,writer_terminal,"
                                    "writer_terminal_outcome,execution_retired FROM "
                                    f"{_JOBS_SCHEMA}.video_execution_journals WHERE job_id=:job"
                                ),
                                {"job": job_id},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    windows = (
                        (
                            await db.execute(
                                text(
                                    f"SELECT window_ordinal,checkpoint_id,manifest_digest,manifest "
                                    f"FROM {_JOBS_SCHEMA}.video_execution_windows "
                                    "WHERE job_id=:job ORDER BY window_ordinal"
                                ),
                                {"job": job_id},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    attempts = (
                        (
                            await db.execute(
                                text(
                                    "SELECT generation,window_ordinal,mode,state,raw_response_digest "
                                    f"FROM {_JOBS_SCHEMA}.video_execution_window_attempts "
                                    "WHERE job_id=:job ORDER BY generation,window_ordinal"
                                ),
                                {"job": job_id},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    closures = (
                        (
                            await db.execute(
                                text(
                                    "SELECT context_span_digest,closed_generation FROM "
                                    f"{_JOBS_SCHEMA}.video_execution_span_closures "
                                    "WHERE job_id=:job ORDER BY context_span_digest"
                                ),
                                {"job": job_id},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    retained_input = await db.scalar(
                        text(
                            f"SELECT {_JOBS_SCHEMA}.video_execution_input_retained(:input)"
                        ),
                        {"input": ready.input_id},
                    )
                    retained_source = await db.scalar(
                        text(
                            f"SELECT {_JOBS_SCHEMA}.video_execution_source_item_retained(:item)"
                        ),
                        {"item": item_id},
                    )
                    final_parts = (
                        await db.execute(
                            text(
                                f"SELECT part_index,part_digest,part_bytes FROM "
                                f"{_JOBS_SCHEMA}.video_execution_window_parts "
                                "WHERE job_id=:job AND window_ordinal=0 ORDER BY part_index"
                            ),
                            {"job": job_id},
                        )
                    ).all()
                    retirements = (
                        (
                            await db.execute(
                                text(
                                    f"SELECT scope_id,phase FROM {_ROUTING_SCHEMA}.scope_retirements "
                                    "WHERE scope_id IN (SELECT id FROM "
                                    f"{_ROUTING_SCHEMA}.scopes WHERE owner=:owner) ORDER BY scope_id"
                                ),
                                {"owner": f"tracker:{job_id}"},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    route_intents = (
                        (
                            await db.execute(
                                text(
                                    "SELECT generation,scope_id,state FROM "
                                    f"{_JOBS_SCHEMA}.video_execution_route_intents "
                                    "WHERE job_id=:job ORDER BY generation"
                                ),
                                {"job": job_id},
                            )
                        )
                        .mappings()
                        .all()
                    )
                final_claim = await _snapshot_claim(media.sessions, job_id)
                all_scopes = await _scope_rows(media.sessions, job_id)

                if cancel_after_kill:
                    assert job_row["status"] == "cancelled"
                    assert [
                        row["frame_index"]
                        for row in job_row["staged_result"]["results"]
                    ] == [0, 1]
                    assert frozen_after["writer_terminal"] is True
                    assert frozen_after["writer_terminal_outcome"] == "cancelled"
                    assert frozen_after["execution_retired"] is True
                    assert retained_input is False
                    assert retained_source is False
                    assert len(windows) == 1
                    assert windows[0]["manifest"] == first_window["manifest"]
                    assert [
                        (row.part_index, row.part_digest, bytes(row.part_bytes))
                        for row in final_parts
                    ] == first_parts
                    assert len(all_scopes) == 1
                    assert all(
                        scope["closed"] is True and scope["state"] == "reconciled"
                        for scope in all_scopes
                    )
                    assert backend.controller.active_requests == 0
                    assert len(backend.model_windows) == 2
                    async with media.sessions() as db:
                        published = (
                            await db.execute(
                                text(
                                    "SELECT operation_id,request_digest,completed_at FROM "
                                    "aap_labeling_v1.video_writer_receipts WHERE job_id=:job"
                                ),
                                {"job": job_id},
                            )
                        ).one()
                    # A later delivery has a new proof and no old in-memory
                    # candidates; it inspects the original receipt unchanged.
                    await _expire_exact_claim(
                        media.sessions, job_id, expected_generation=3
                    )
                    await _attempt_tracker_run(execution_sessions, job_id)
                    async with media.sessions() as db:
                        replayed = (
                            await db.execute(
                                text(
                                    "SELECT operation_id,request_digest,completed_at FROM "
                                    "aap_labeling_v1.video_writer_receipts WHERE job_id=:job"
                                ),
                                {"job": job_id},
                            )
                        ).one()
                    assert replayed == published
                    assert len(backend.model_windows) == 2
                    return
                assert job_row["status"] == "pending_review"
                candidates = job_row["staged_result"]["results"]
                assert [row["frame_index"] for row in candidates] == list(range(6))
                assert len({row["frame_index"] for row in candidates}) == 6
                assert all(
                    row["geometry"] == {"type": "bbox", **artifact["bbox"]}
                    and row["confidence"] == 1.0
                    for row in candidates
                )
                assert frozen_after["plan_digest"] == frozen["plan_digest"]
                assert frozen_after["plan_document"] == frozen["plan_document"]
                assert frozen_after["writer_terminal"] is True
                assert frozen_after["writer_terminal_outcome"] == "completed"
                assert frozen_after["execution_retired"] is True
                assert retained_input is False
                assert retained_source is False
                assert [row["window_ordinal"] for row in windows] == [0, 1, 2]
                assert windows[0]["checkpoint_id"] == first_window["checkpoint_id"]
                assert windows[0]["manifest_digest"] == first_window["manifest_digest"]
                assert windows[0]["manifest"] == first_window["manifest"]
                final_first_parts = [
                    (row.part_index, row.part_digest, bytes(row.part_bytes))
                    for row in final_parts
                ]
                assert final_first_parts == first_parts
                assert final_claim["generation"] == 3
                assert final_claim["state"] == "ready"
                assert len(all_scopes) == 2
                assert all(scope["closed"] is True for scope in all_scopes)
                assert all(scope["state"] == "reconciled" for scope in all_scopes)
                assert all(row["phase"] == "completed" for row in retirements)
                assert {row["scope_id"] for row in retirements} == {
                    row["id"] for row in all_scopes
                }
                assert [row["generation"] for row in route_intents] == [1, 3]
                assert all(row["state"] == "bound" for row in route_intents)
                assert {row["scope_id"] for row in route_intents} == {
                    row["id"] for row in all_scopes
                }
                assert backend.controller.active_requests == 0

                old_generation_attempts = [
                    row for row in attempts if row["generation"] == 1
                ]
                new_generation_attempts = [
                    row for row in attempts if row["generation"] == 3
                ]
                assert [
                    (row["window_ordinal"], row["state"])
                    for row in old_generation_attempts
                ] == [(0, "captured"), (1, "abandoned")]
                if context_mode == "none":
                    assert [
                        (row["window_ordinal"], row["mode"], row["state"])
                        for row in new_generation_attempts
                    ] == [(1, "execute", "captured"), (2, "execute", "captured")]
                    assert closures == []
                    assert [
                        (row["from_frame"], row["to_frame"])
                        for row in backend.model_windows
                    ] == [(0, 1), (2, 3), (2, 3), (4, 5)]
                else:
                    assert [
                        (row["window_ordinal"], row["mode"], row["state"])
                        for row in new_generation_attempts
                    ] == [
                        (0, "replay", "replay_verified"),
                        (1, "execute", "captured"),
                        (2, "execute", "captured"),
                    ]
                    plan_spans = {
                        window["context_span"]["span_digest"]
                        for window in frozen_after["plan_document"]["windows"]
                        if window["context_span"]["mode"] == "session"
                    }
                    assert {
                        row["context_span_digest"] for row in closures
                    } == plan_spans
                    assert len(closures) == 2
                    assert all(row["closed_generation"] == 3 for row in closures)
                    original_raw = first_window["manifest"]["raw_response_digest"]
                    replay_observation = next(
                        row
                        for row in new_generation_attempts
                        if row["mode"] == "replay"
                    )["raw_response_digest"]
                    assert replay_observation is not None
                    assert replay_observation != original_raw
                    starts = [
                        row["token"]
                        for row in backend.model_windows
                        if row["action"] == "start"
                    ]
                    assert len(starts) == 3
                    assert len(set(starts)) == 3
                    assert [row["action"] for row in backend.calls] == [
                        "start",
                        "continue",
                        "start",
                        "continue",
                        "close",
                        "start",
                        "close",
                    ]
                assert all(
                    row["source_sha256"] == reference.source_version.sha256
                    and row["source_version_id"] == reference.source_version.version_id
                    for row in backend.model_windows
                )
                assert all(
                    row["job_id"] == str(job_id)
                    and row["admission_header_present"]
                    and row["authorization_bound"]
                    for row in backend.model_windows
                )
                assert artifact_bytes == json.dumps(
                    artifact,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
        finally:
            if listener is not None:
                listener.close()
            if peer is not None:
                _stop_process(peer)
            if owner_server is not None:
                await owner_server.close()
            if owner_runtime is not None:
                await owner_runtime.close()
            await execution_engine.dispose()
