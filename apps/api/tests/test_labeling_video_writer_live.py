"""Real worker publication through each Labeling video-writer binding."""

from __future__ import annotations

from contextlib import asynccontextmanager
from hashlib import sha256
import asyncio
import json
import math
import os
from pathlib import Path
import secrets
import socket
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import asyncpg
from aap_assets_video_inputs.tracker import TrackerSourceReferenceRequest
from aap_inference.pipeline_server import PipelineHttpServer
from aap_inference.routing_runtime import RoutingRuntime
from aap_inference_contract.routing import make_routing_command
from aap_labeling.video_writer_client import VideoWriterHttpClient
from aap_labeling.video_writer_contract import video_writer_operation_id
from aap_protocol_v2.routing import ROUTE_ADMISSION_HEADER
from aap_protocol_v2.routing_runtime import install_backend_routing_controller
from fastapi import FastAPI, HTTPException, Request
import httpx
import pytest
import uvicorn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.ml_backend import MLBackendService
from app.services.video_tracking.execution import execution_proof
from app.services.video_tracking.runner import run_tracker_job
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_assets_media_live import (
    _free_loopback_port,
    _process_log,
    _start_process,
    _stop_process,
)
from tests.test_inference_routing_video import _verify_restricted_routing_dsn
from tests.test_mask_content_live import (
    _owned_resources,
    _start_runtime as _start_mask_content,
    _stop_runtime as _stop_mask_content,
)
from tests.test_tracker_input_live import _prepared, _seed_tracker
from tests.test_video_input_live import _video_bytes, _video_runtime

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_MASK_CONTENT_ACCEPTANCE") != "1",
        reason="requires the owned module-data and Mask content environments",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
_JOBS_SCHEMA = "aap_jobs_v1"
_ROUTING_SCHEMA = "aap_inference_routing_v1"
_WRITER_ROLE = "labeling_video_writer_client"


def _reserve_listener() -> tuple[socket.socket, str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    return listener, f"http://127.0.0.1:{listener.getsockname()[1]}"


def _artifact(output_geometry: str) -> tuple[bytes, dict, str]:
    value = {
        "schema": "aap-labeling-video-writer-cpu-artifact/v1",
        "model_id": f"owned-video-writer-{output_geometry}-1",
        "weights_release_id": f"owned-video-writer-{output_geometry}-2026-10-22",
        "output_geometry": output_geometry,
    }
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return raw, json.loads(raw), sha256(raw).hexdigest()


@asynccontextmanager
async def _cpu_backend(
    listener: socket.socket,
    *,
    registry_id,
    control_token: str,
    storage_origin: str,
    expected_source_sha256: str,
    output_geometry: str,
    artifact: dict,
    weights_sha256: str,
):
    app = FastAPI()
    storage = httpx.AsyncClient(timeout=15, trust_env=False)
    calls: list[dict] = []
    gate, release = asyncio.Event(), asyncio.Event()
    capability_model = {
        "id": artifact["model_id"],
        "display_name": f"Owned deterministic CPU {output_geometry} fixture",
        "task": "tracker",
        "is_interactive": True,
        "supported_prompts": ["text"],
        "supported_trackers": ["sam2_video"],
        "supported_geometric_outputs": [output_geometry],
        "max_window_frames": 2,
        "tracker_context_mode": "none",
        "weights_release_id": artifact["weights_release_id"],
        "weights_sha256": weights_sha256,
    }

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
            "name": "owned-labeling-video-writer-cpu",
            "is_interactive": True,
            "models": [capability_model],
        }

    @app.post("/predict")
    async def predict(request: Request):
        payload = await request.json()
        task, context = payload.get("task"), payload.get("context")
        source_url = task.get("file_path") if isinstance(task, dict) else None
        if not isinstance(context, dict) or not isinstance(source_url, str):
            raise HTTPException(status_code=400, detail="invalid_tracker_request")
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
            or context.get("model_id") != artifact["model_id"]
            or type(first) is not int
            or type(last) is not int
            or first < 0
            or last < first
            or source_sha256 != expected_source_sha256
        ):
            raise HTTPException(status_code=502, detail="invalid_tracker_execution")

        call = {
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
        calls.append(call)
        if len(calls) == 3:
            gate.set()
            try:
                await asyncio.wait_for(release.wait(), timeout=45)
            except TimeoutError as exc:
                raise HTTPException(
                    status_code=504, detail="acceptance_gate_timeout"
                ) from exc

        if output_geometry == "mask":
            geometry = {
                "type": "mask",
                "rle": {"encoding": "coco_rle", "size": [48, 80], "counts": [0, 3840]},
            }
        else:
            geometry = {
                "type": "bbox",
                "x": -0.0,
                "y": 1e-5,
                "w": 0.3,
                "h": 0.4,
            }
        result = [
            {
                "frame_index": frame,
                "geometry": geometry,
                "confidence": 1e-7 if output_geometry == "bbox" else 0.9,
            }
            for frame in range(first, last + 1)
        ]
        return {
            "result": result,
            "meta": {
                "artifact_sha256": weights_sha256,
                "weights_release_id": artifact["weights_release_id"],
            },
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
                    raise AssertionError("CPU video backend exited before startup")
                await asyncio.sleep(0.01)
        yield SimpleNamespace(
            controller=controller,
            calls=calls,
            gate=gate,
            release=release,
        )
    finally:
        release.set()
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except TimeoutError:
            server.force_exit = True
            await asyncio.wait_for(task, timeout=5)
        listener.close()
        await storage.aclose()


class _StandaloneWriter:
    def __init__(
        self,
        *,
        dsn: str,
        output_geometry: str,
        mask_url: str,
        mask_token: str,
        tmp_path: Path,
    ):
        self.port = _free_loopback_port()
        self.origin = f"http://127.0.0.1:{self.port}"
        self.token = secrets.token_urlsafe(36)
        self.log = tmp_path / f"video-writer-owner-{uuid4().hex}.log"
        roots = [
            ROOT / "apps" / "labeling" / "src",
            ROOT / "apps" / "preannotation-results" / "src",
            ROOT / "apps" / "assets-media" / "src",
            ROOT / "apps" / "assets-mask-content" / "src",
            *sorted((ROOT / "apps" / "_shared").glob("*/src")),
        ]
        forbidden_api_root = str(ROOT / "apps" / "api")
        assert all(forbidden_api_root not in str(path) for path in roots)
        self.environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(str(path) for path in roots),
            "LABELING_VIDEO_WRITER_DATABASE_URL": dsn,
            "LABELING_VIDEO_WRITER_CONTROL_TOKEN": self.token,
            "LABELING_VIDEO_WRITER_HOST": "127.0.0.1",
            "LABELING_VIDEO_WRITER_PORT": str(self.port),
            "LABELING_VIDEO_WRITER_TIMEOUT_SECONDS": "30",
        }
        if output_geometry == "mask":
            self.environment.update(
                {
                    "ASSETS_MASK_CONTENT_URL": mask_url,
                    "ASSETS_MASK_CONTENT_TOKEN": mask_token,
                }
            )
        assert forbidden_api_root not in self.environment["PYTHONPATH"]
        assert not {
            "DATABASE_URL",
            "ASSETS_MEDIA_URL",
            "MINIO_ACCESS_KEY",
            "MINIO_SECRET_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        }.intersection(self.environment)
        assert self.environment["LABELING_VIDEO_WRITER_DATABASE_URL"] == dsn
        self.process = None
        self.stream = None

    def start(self) -> None:
        self.process, self.stream = _start_process(
            [sys.executable, "-m", "aap_labeling.video_writer_main"],
            self.environment,
            self.log,
        )

    async def wait_ready(self) -> None:
        assert self.process is not None
        async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
            async with asyncio.timeout(25):
                while True:
                    if self.process.poll() is not None:
                        pytest.fail(
                            "Labeling writer owner exited before /ready: "
                            + _process_log(self.log)
                        )
                    try:
                        response = await client.get(self.origin + "/ready")
                        if response.status_code == 200 and response.json() == {
                            "status": "ready"
                        }:
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)

    async def restart(self) -> None:
        self.stop()
        self.start()
        await self.wait_ready()

    def stop(self) -> None:
        if self.process is not None:
            _stop_process(self.process)
            self.process = None
        if self.stream is not None:
            self.stream.close()
            self.stream = None


async def _writer_dsn_identity(dsn: str, expected_database: str) -> None:
    connection = await asyncpg.connect(
        dsn.replace("postgresql+asyncpg://", "postgresql://", 1),
        timeout=5,
        command_timeout=10,
    )
    try:
        role, database = await connection.fetchrow(
            "SELECT current_user,current_database()"
        )
        assert role == _WRITER_ROLE
        assert database == expected_database
    finally:
        await connection.close(timeout=2)


async def _wait_last_window(peer, running) -> None:
    async with asyncio.timeout(25):
        while not peer.gate.is_set():
            if running.done():
                result = await running
                pytest.fail(
                    "tracker finished before the final controlled model window: "
                    f"status={getattr(result, 'status', None)}"
                )
            await asyncio.sleep(0.025)


async def _snapshot_windows(sessions_factory, job_id):
    async with sessions_factory() as db:
        windows = (
            (
                await db.execute(
                    text(
                        f"SELECT window_ordinal,manifest_digest,manifest FROM "
                        f"{_JOBS_SCHEMA}.video_execution_windows "
                        "WHERE job_id=:job ORDER BY window_ordinal"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .all()
        )
        parts = (
            await db.execute(
                text(
                    f"SELECT window_ordinal,part_index,part_digest,part_bytes FROM "
                    f"{_JOBS_SCHEMA}.video_execution_window_parts "
                    "WHERE job_id=:job ORDER BY window_ordinal,part_index"
                ),
                {"job": job_id},
            )
        ).all()
    by_window = {}
    for part in parts:
        by_window.setdefault(part.window_ordinal, []).append(
            (part.part_index, part.part_digest, bytes(part.part_bytes))
        )
    return {
        row["window_ordinal"]: (dict(row), by_window[row["window_ordinal"]])
        for row in windows
    }


def _assert_candidate_fidelity(row: dict) -> None:
    geometry = row["geometry"]
    geometry_digest = sha256(
        json.dumps(
            {"geometry": geometry, "outside": row["outside"]},
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    expected_digest = f"sha256:{geometry_digest}"
    assert row["geometry_digest"] == expected_digest
    assert row["candidate_key"] == (
        f"{row['instance_id'] or '1'}:{row['frame_index']}:{expected_digest}"
    )


@pytest.mark.parametrize("output_geometry", ["bbox", "mask"])
@pytest.mark.parametrize("binding", ["borrowed", "dsn", "http"])
async def test_video_job_finalizes_through_real_labeling_writer_owner(
    test_engine, tmp_path, monkeypatch, output_geometry, binding
):
    ownership = await assert_owned_test_database(test_engine)
    legacy_dsn = os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"]
    routing_dsn = os.environ.get("AAP_MODULE_DATA_INFERENCE_ROUTING_URL")
    writer_dsn = os.environ.get("AAP_MODULE_DATA_LABELING_VIDEO_WRITER_URL")
    assert routing_dsn, "the verifier must provide the restricted routing DSN"
    assert writer_dsn, "the verifier must provide the Labeling writer LOGIN DSN"
    await _verify_restricted_routing_dsn(routing_dsn, ownership["database"])
    await _writer_dsn_identity(writer_dsn, ownership["database"])

    mask_owner = mask_storage = None
    mask_url = mask_token = ""
    async with _video_runtime(test_engine, tmp_path) as media:
        video = await asyncio.to_thread(_video_bytes, tmp_path)
        artifact_bytes, artifact, artifact_digest = _artifact(output_geometry)
        listener, backend_url = _reserve_listener()
        backend_token = "owned-writer-backend-" + uuid4().hex
        route_runtime = route_server = None
        execution_engine = None
        execution_sessions = None
        owner = None
        owner_runtime_receipt = None
        try:
            execution_engine = create_async_engine(legacy_dsn, hide_parameters=True)
            execution_sessions = async_sessionmaker(
                execution_engine, expire_on_commit=False
            )
            if output_geometry == "mask":
                _, mask_bucket, _, mask_storage, mask_credentials, _ = _owned_resources(
                    test_engine
                )
                mask_owner = await asyncio.to_thread(
                    _start_mask_content,
                    "content",
                    bucket=mask_bucket,
                    credentials=mask_credentials,
                    tmp_path=tmp_path,
                )
                _, _, mask_url, mask_token = mask_owner

            (
                actor_id,
                project_id,
                item_id,
                task_id,
                job_id,
                _source_key,
                _input_record,
            ) = await _seed_tracker(
                media,
                video,
                backend_url=backend_url,
                output_geometry=output_geometry,
            )
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
                        "auth_token=:token,state='connected',last_checked_at="
                        "pg_catalog.clock_timestamp(),"
                        "extra_params=CAST(:extra AS jsonb) "
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

            async with _cpu_backend(
                listener,
                registry_id=registry_id,
                control_token=backend_token,
                storage_origin=media.endpoint,
                expected_source_sha256=sha256(video).hexdigest(),
                output_geometry=output_geometry,
                artifact=artifact,
                weights_sha256=artifact_digest,
            ) as peer:
                listener = None
                async with media.sessions.begin() as health_db:
                    health = MLBackendService(health_db)
                    assert await health.check_health(registry_id)
                    selected = await health.get_tracker_backend_for_capabilities(
                        project_id, ["sam2_video"]
                    )
                    assert selected is not None and selected.id == registry_id

                input_client, input_request, ready = await _prepared(
                    media, tmp_path, actor_id, task_id, job_id
                )
                source_reference = await input_client.reference(
                    TrackerSourceReferenceRequest(
                        **input_request.model_dump(), plan_digest=ready.plan_digest
                    )
                )
                assert (
                    source_reference.source_version.sha256 == sha256(video).hexdigest()
                )
                assert source_reference.source_version.byte_size == len(video)
                assert (await media.http.get(source_reference.url)).content == video

                monkeypatch.setattr(settings, "assets_media_url", media.origin)
                monkeypatch.setattr(settings, "assets_media_token", media.token)
                monkeypatch.setattr(settings, "inference_routing_url", "")
                monkeypatch.setattr(settings, "inference_routing_token", "")
                monkeypatch.setattr(settings, "inference_routing_token_file", "")
                monkeypatch.setattr(settings, "inference_routing_database_url", "")
                monkeypatch.setattr(settings, "inference_routing_database_url_file", "")
                monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
                monkeypatch.setattr(
                    settings, "ml_backend_router_heartbeat_interval_seconds", 1
                )
                monkeypatch.setattr(settings, "ml_backend_router_lease_ttl_seconds", 30)
                monkeypatch.setattr(settings, "video_tracker_window_size_frames", 2)
                monkeypatch.setattr(
                    settings, "video_tracker_low_confidence_outside_threshold", 0.0
                )
                monkeypatch.setattr(settings, "assets_mask_content_url", mask_url)
                monkeypatch.setattr(settings, "assets_mask_content_token", mask_token)
                monkeypatch.setattr(settings, "assets_mask_content_timeout_seconds", 20)

                route_runtime = RoutingRuntime(routing_dsn)
                assert await route_runtime.readiness()
                route_token = secrets.token_urlsafe(36)
                route_port = _free_loopback_port()
                route_server = PipelineHttpServer(
                    manager=None,
                    data=None,
                    control_token=None,
                    host="127.0.0.1",
                    port=route_port,
                    routing_manager=route_runtime.manager,
                    routing_control_token=route_token,
                    scope_retirement_manager=route_runtime.scope_retirement_manager,
                )
                await route_server.start()
                route_url = f"http://127.0.0.1:{route_port}"
                monkeypatch.setattr(settings, "inference_routing_url", route_url)
                monkeypatch.setattr(settings, "inference_routing_token", route_token)
                reconciled = await route_runtime.execute(
                    make_routing_command(
                        "reconcile",
                        {"registry_id": str(registry_id), "control_id": str(uuid4())},
                    )
                )
                assert reconciled.state == "committed"
                assert reconciled.result.phase == "completed"
                assert peer.controller.phase == "bound"

                monkeypatch.setattr(settings, "labeling_video_writer_url", "")
                monkeypatch.setattr(settings, "labeling_video_writer_database_url", "")
                monkeypatch.setattr(
                    settings, "labeling_video_writer_database_url_file", ""
                )
                monkeypatch.setattr(settings, "labeling_video_writer_control_token", "")
                monkeypatch.setattr(
                    settings, "labeling_video_writer_control_token_file", ""
                )
                if binding == "dsn":
                    monkeypatch.setattr(
                        settings, "labeling_video_writer_database_url", writer_dsn
                    )
                elif binding == "http":
                    owner = _StandaloneWriter(
                        dsn=writer_dsn,
                        output_geometry=output_geometry,
                        mask_url=mask_url,
                        mask_token=mask_token,
                        tmp_path=tmp_path,
                    )
                    owner.start()
                    await owner.wait_ready()
                    monkeypatch.setattr(
                        settings, "labeling_video_writer_url", owner.origin
                    )
                    monkeypatch.setattr(
                        settings, "labeling_video_writer_control_token", owner.token
                    )
                assert sha256(artifact_bytes).hexdigest() == artifact_digest

                completed_events: list[dict] = []

                async def publish(_channel, event):
                    nonlocal owner_runtime_receipt
                    if event["type"] == "job_completed" and binding == "http":
                        proof = execution_proof(job_id)
                        async with media.sessions() as owner_db:
                            receipt_row = (
                                (
                                    await owner_db.execute(
                                        text(
                                            "SELECT operation_id,run_id,"
                                            "request_digest FROM "
                                            "aap_labeling_v1.video_writer_receipts "
                                            "WHERE job_id=:job"
                                        ),
                                        {"job": job_id},
                                    )
                                )
                                .mappings()
                                .one()
                            )
                        assert receipt_row["run_id"] == proof.run_id
                        assert receipt_row["operation_id"] == video_writer_operation_id(
                            proof.run_id
                        )
                        assert owner is not None
                        await owner.restart()
                        async with VideoWriterHttpClient(
                            owner.origin, owner.token, timeout_seconds=20
                        ) as client:
                            status_receipt = await client.status(
                                receipt_row["operation_id"],
                                proof,
                                receipt_row["request_digest"],
                            )
                        assert status_receipt is not None
                        assert (
                            status_receipt.request_digest
                            == receipt_row["request_digest"]
                        )
                        assert status_receipt.outcome == "completed"
                        assert status_receipt.status == "pending_review"
                        owner_runtime_receipt = status_receipt
                    if event["type"] == "job_completed":
                        completed_events.append(event)

                assert execution_sessions is not None
                async with execution_sessions() as execution_db:
                    running = asyncio.create_task(
                        run_tracker_job(
                            execution_db,
                            job_id,
                            publisher=publish,
                            shadow_session_factory=execution_sessions,
                        )
                    )
                    try:
                        await _wait_last_window(peer, running)
                        before = await _snapshot_windows(media.sessions, job_id)
                        assert set(before) == {0, 1}
                        if output_geometry == "mask":
                            async with media.sessions() as db:
                                pins = (
                                    (
                                        await db.execute(
                                            text(
                                                f"SELECT {_JOBS_SCHEMA}."
                                                "video_execution_checkpoint_mask_retained("
                                                "checkpoint_id) FROM "
                                                f"{_JOBS_SCHEMA}.video_execution_windows "
                                                "WHERE job_id=:job AND window_ordinal < 2 "
                                                "ORDER BY window_ordinal"
                                            ),
                                            {"job": job_id},
                                        )
                                    )
                                    .scalars()
                                    .all()
                                )
                            assert pins == [True, True]
                        from aap_preannotation_results.video_window_checkpoint import (
                            decode_video_window_checkpoint_payload,
                        )

                        first_payload_bytes = b"".join(part[2] for part in before[0][1])
                        first_payload = decode_video_window_checkpoint_payload(
                            first_payload_bytes
                        )
                        peer.release.set()
                        result = await asyncio.wait_for(running, timeout=90)
                    finally:
                        peer.release.set()
                        if not running.done():
                            running.cancel()
                            await asyncio.gather(running, return_exceptions=True)

                assert result is not None and result.status == "pending_review", (
                    result.error_message
                    if result is not None
                    else "worker returned no job"
                )
                assert len(peer.calls) == 3
                assert {call["source_sha256"] for call in peer.calls} == {
                    sha256(video).hexdigest()
                }
                assert all(call["source_size"] == len(video) for call in peer.calls)
                assert all(call["admission_header_present"] for call in peer.calls)
                assert all(call["authorization_bound"] for call in peer.calls)
                assert all(
                    call["source_version_id"]
                    == source_reference.source_version.version_id
                    for call in peer.calls
                )
                assert peer.controller.active_requests == 0
                assert len(completed_events) == 1
                final_event = completed_events[0]
                assert set(final_event) == {
                    "type",
                    "job_id",
                    "task_id",
                    "dataset_item_id",
                    "annotation_id",
                    "at",
                }
                assert final_event["type"] == "job_completed"
                assert final_event["job_id"] == str(job_id)
                assert isinstance(final_event["at"], str)
                if binding == "http":
                    assert owner_runtime_receipt is not None

                async with media.sessions() as db:
                    row = (
                        (
                            await db.execute(
                                text(
                                    "SELECT status,revision,staged_result FROM "
                                    "public.video_tracker_jobs WHERE id=:job"
                                ),
                                {"job": job_id},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    receipts = (
                        (
                            await db.execute(
                                text(
                                    "SELECT operation_id,run_id,request_digest,"
                                    "outcome,status,staged_sha256,staged_byte_size FROM "
                                    "aap_labeling_v1.video_writer_receipts "
                                    "WHERE job_id=:job"
                                ),
                                {"job": job_id},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    journal = (
                        (
                            await db.execute(
                                text(
                                    f"SELECT writer_terminal,writer_terminal_outcome,"
                                    f"execution_retired FROM {_JOBS_SCHEMA}."
                                    "video_execution_journals WHERE job_id=:job"
                                ),
                                {"job": job_id},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    plan = (
                        await db.execute(
                            text(
                                f"SELECT plan_document FROM {_JOBS_SCHEMA}."
                                "video_execution_journals "
                                "WHERE job_id=:job"
                            ),
                            {"job": job_id},
                        )
                    ).scalar_one()
                    route_state = (
                        (
                            await db.execute(
                                text(
                                    "SELECT scope.registry_id,scope.closed,scope.state,"
                                    "scope.authority_epoch,state.backend_incarnation "
                                    f"FROM {_ROUTING_SCHEMA}.scopes AS scope "
                                    f"JOIN {_ROUTING_SCHEMA}.registry_state AS state "
                                    "ON state.registry_id=scope.registry_id "
                                    "WHERE scope.owner=:owner"
                                ),
                                {"owner": f"tracker:{job_id}"},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    retirements = (
                        (
                            await db.execute(
                                text(
                                    "SELECT phase FROM "
                                    f"{_ROUTING_SCHEMA}.scope_retirements "
                                    "WHERE scope_id IN (SELECT id FROM "
                                    f"{_ROUTING_SCHEMA}.scopes WHERE owner=:owner)"
                                ),
                                {"owner": f"tracker:{job_id}"},
                            )
                        )
                        .scalars()
                        .all()
                    )
                    mask_roots = (
                        await db.execute(
                            text(
                                "SELECT root_kind,object_key FROM public.mask_gc_roots "
                                "WHERE aap_labeling_v1._mask_gc_root_entry_live("
                                "root_kind,root_id,valid_until,true) AND "
                                "((root_kind='video_tracker_jobs' "
                                "AND root_id=:job) "
                                "OR (root_kind='video_tracker_running_outputs' "
                                "AND root_id=:job))"
                            ),
                            {"job": str(job_id)},
                        )
                    ).all()
                    running_mask_live = await db.scalar(
                        text(
                            "SELECT "
                            f"{_JOBS_SCHEMA}.video_execution_running_mask_roots_live("
                            ":job)"
                        ),
                        {"job": job_id},
                    )
                    input_retained = await db.scalar(
                        text(
                            "SELECT "
                            f"{_JOBS_SCHEMA}.video_execution_input_retained(:input)"
                        ),
                        {"input": ready.input_id},
                    )
                    source_retained = await db.scalar(
                        text(
                            "SELECT "
                            f"{_JOBS_SCHEMA}.video_execution_source_item_retained("
                            ":item)"
                        ),
                        {"item": item_id},
                    )
                    checkpoint_mask_pins = (
                        (
                            await db.execute(
                                text(
                                    "SELECT "
                                    f"{_JOBS_SCHEMA}."
                                    "video_execution_checkpoint_mask_retained("
                                    "checkpoint_id) FROM "
                                    f"{_JOBS_SCHEMA}.video_execution_windows "
                                    "WHERE job_id=:job ORDER BY window_ordinal"
                                ),
                                {"job": job_id},
                            )
                        )
                        .scalars()
                        .all()
                    )
                assert row["status"] == "pending_review"
                assert row["revision"] == 1
                staged = row["staged_result"]
                assert len(staged["results"]) == 6
                assert [item["frame_index"] for item in staged["results"]] == list(
                    range(6)
                )
                assert staged["output_geometry"] == output_geometry
                evidence = staged["tracker_input_evidence"]
                assert evidence["input_id"] == str(ready.input_id)
                assert evidence["request_digest"] == ready.request_digest
                assert evidence["plan_digest"] == ready.plan_digest
                assert (
                    evidence["source_version_id"]
                    == source_reference.source_version.version_id
                )
                assert evidence["source_sha256"] == sha256(video).hexdigest()
                assert not ready.execution.source_annotations
                assert staged["source_instance_map"] == {}
                assert len(receipts) == 1
                receipt = dict(receipts[0])
                assert receipt["outcome"] == "completed"
                assert receipt["status"] == "pending_review"
                assert receipt["operation_id"] == video_writer_operation_id(
                    receipt["run_id"]
                )
                assert receipt["staged_sha256"] is not None
                after = await _snapshot_windows(media.sessions, job_id)
                canonical_results = [
                    result.model_dump(mode="json", by_alias=True)
                    for ordinal in sorted(after)
                    for result in decode_video_window_checkpoint_payload(
                        b"".join(part[2] for part in after[ordinal][1])
                    ).frame_results
                ]
                assert staged["results"] == canonical_results
                # Legacy JSONB normalizes signed zero; the receipt hashes the
                # pre-storage candidate bytes preserved by canonical windows.
                staged_bytes = json.dumps(
                    {**staged, "results": canonical_results},
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                assert sha256(staged_bytes).hexdigest() == receipt["staged_sha256"]
                assert len(staged_bytes) == receipt["staged_byte_size"]
                assert journal["writer_terminal"] is True
                assert journal["writer_terminal_outcome"] == "completed"
                assert journal["execution_retired"] is True
                assert input_retained is False
                assert source_retained is False
                assert len(route_state) == 1
                plan_identity = plan["backend_compatibility"][0]
                assert plan_identity["registry_id"] == str(registry_id)
                assert plan_identity["model_id"] == artifact["model_id"]
                assert plan_identity["weights_sha256"] == artifact_digest
                assert plan_identity["backend_incarnation"] == str(
                    route_state[0]["backend_incarnation"]
                )
                assert route_state[0]["closed"] is True
                assert route_state[0]["state"] == "reconciled"
                assert retirements == ["completed"]
                assert running_mask_live is False
                if output_geometry == "mask":
                    assert checkpoint_mask_pins == [False, False, False]

                assert set(after) == {0, 1, 2}
                for ordinal in (0, 1):
                    assert before[ordinal] == after[ordinal]
                first_rows = [
                    row.model_dump(mode="json", by_alias=True)
                    for row in first_payload.frame_results
                ]
                assert staged["results"][: len(first_rows)] == first_rows
                if output_geometry == "bbox":
                    assert math.copysign(1.0, canonical_results[0]["geometry"]["x"]) < 0
                    assert staged["results"][0]["geometry"]["y"] == 1e-5
                    assert staged["results"][0]["confidence"] == 1e-7
                    for candidate in canonical_results:
                        _assert_candidate_fidelity(candidate)
                else:
                    references = {
                        item["geometry"]["mask"]["object_key"]: item["geometry"]["mask"]
                        for item in staged["results"]
                    }
                    assert len(references) == 1
                    assert {
                        root.object_key
                        for root in mask_roots
                        if root.root_kind == "video_tracker_jobs"
                    } == set(references)
                    assert not any(
                        root.root_kind == "video_tracker_running_outputs"
                        for root in mask_roots
                    )
                    from aap_assets_mask_content.client import MaskContentClient
                    from aap_assets_mask_content.contract import (
                        GetRequest,
                        MaskReference,
                    )

                    def verify_mask_reference(reference):
                        with MaskContentClient(
                            mask_url, mask_token, timeout_seconds=15
                        ) as client:
                            content = client.get(
                                GetRequest(
                                    reference=MaskReference.model_validate(reference)
                                )
                            ).content
                        assert content.size == (48, 80)
                        assert sum(content.counts) == 3840

                    for reference in references.values():
                        await asyncio.to_thread(verify_mask_reference, reference)
        finally:
            if listener is not None:
                listener.close()
            if owner is not None:
                owner.stop()
            if execution_engine is not None:
                await execution_engine.dispose()
            if route_server is not None:
                await route_server.close()
            if route_runtime is not None:
                await route_runtime.close()
            if mask_owner is not None:
                await asyncio.to_thread(
                    _stop_mask_content, mask_owner[0], mask_owner[1]
                )
            if mask_storage is not None:
                mask_storage.close()
