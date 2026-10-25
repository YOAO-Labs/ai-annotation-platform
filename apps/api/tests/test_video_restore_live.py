"""Owned restore acceptance with the real CPU route-control HTTP endpoint."""

from __future__ import annotations

import asyncio
from collections import Counter
import hashlib
import json
import os
import socket
import uuid

import asyncpg
from aap_inference.restore import RestoreUnavailable, VideoRestoreManager
from aap_inference.restore_postgres import VideoRestorePostgresProvider
from aap_inference.routing_backend_control import BackendRoutingControl
from aap_inference.routing_runtime import RoutingRuntime
from aap_inference_contract.routing import make_routing_command
from aap_labeling.video_writer import VideoWriterManager
from aap_labeling.video_writer_contract import (
    make_video_writer_command,
    video_writer_command_digest,
)
from aap_labeling.video_writer_postgres import VideoWriterPostgresProvider
from aap_preannotation_results.video_execution import (
    RenewRequest,
    VideoExecutionFailure,
)
from aap_preannotation_results.video_execution_plan import decode_video_execution_plan
from aap_preannotation_results.video_window_checkpoint import (
    decode_video_window_checkpoint_manifest,
)
from aap_preannotation_results_runtime.video_execution_database import (
    VideoExecutionPostgresData,
)
from aap_protocol_v2.routing import ROUTE_ADMISSION_HEADER, ROUTE_STATUS_PATH
from aap_protocol_v2.routing_runtime import install_backend_routing_controller
from fastapi import FastAPI, Request
import httpx
import pytest
import uvicorn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.video_tracking.execution import execution_proof, tracker_execution
from app.services.video_tracking.recovery import recover_tracker_execution
from tests.test_inference_routing_owner_integration import (
    _capture_subject,
    _seed_world,
    _start_uvicorn,
    _stop_uvicorn,
)
from tests.test_labeling_video_writer_restore import (
    _seal_window_with_lexemes,
)
from tests.test_video_restore_native import (
    _RESTORE_ROLE,
    _command,
    _create_restored_database,
    _create_source_database,
    _json_value,
    _role_dsn,
)
from tests.test_video_window_journal_native import _prepared_execution
from tests.test_identity_recovery import _postgres_tool
from tests.test_inference_backend_dependency_reads_restore import (
    _database_acl,
    _drop_owned_clone,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verifier-owned disposable PostgreSQL resources",
    ),
]

_WRITER_ROLE = "labeling_video_writer_client"
_VIDEO_EXECUTION_ROLE = "video_execution_client"
_ROUTING_ROLE = "inference_routing_client"


class _RequestCounter:
    def __init__(self, app):
        self.app = app
        self.paths: Counter[str] = Counter()

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            self.paths[scope.get("path", "")] += 1
        await self.app(scope, receive, send)


async def _start_uvicorn_on_port(app, port: int):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(128)
    listener.setblocking(False)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
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
                    raise AssertionError("cold backend stopped before startup")
                await asyncio.sleep(0.01)
    except BaseException:
        server.should_exit = True
        listener.close()
        await asyncio.gather(task, return_exceptions=True)
        raise
    return server, task, listener, f"http://127.0.0.1:{port}"


def _backend_app(registry_id, token, *, entered=None, release=None):
    app = FastAPI()
    calls: list[dict] = []

    @app.post("/predict")
    async def predict(request: Request):
        body = await request.json()
        calls.append(body)
        if entered is not None:
            entered.set()
        if release is not None:
            await release.wait()
        return {"accepted": True}

    controller = install_backend_routing_controller(
        app,
        registry_id=registry_id,
        environ={
            "AAP_ROUTE_CONTROL_TOKEN": token,
            "AAP_ROUTE_ALLOW_LEGACY_EXECUTION": "0",
        },
    )
    return app, controller, calls


def _owner_dsn(engine, database: str) -> str:
    return engine.url.set(drivername="postgresql", database=database).render_as_string(
        hide_password=False
    )


async def _json_route_status(url: str, token: str) -> dict:
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        response = await client.get(
            f"{url.rstrip('/')}{ROUTE_STATUS_PATH}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.json()


async def _native_snapshot(engine, job_id: uuid.UUID, request_id: uuid.UUID) -> dict:
    async with engine.connect() as connection:
        job = (
            (
                await connection.execute(
                    text(
                        "SELECT id::text,status,revision,completed_at,"
                        "staged_result::text AS staged_result,error_message "
                        "FROM public.video_tracker_jobs WHERE id=:job"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .one()
        )
        journal = (
            (
                await connection.execute(
                    text(
                        "SELECT run_id::text,plan_digest,"
                        "plan_document::text AS plan_document,"
                        "writer_terminal,writer_terminal_outcome,execution_retired "
                        "FROM aap_jobs_v1.video_execution_journals WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .one()
        )
        execution = (
            (
                await connection.execute(
                    text(
                        "SELECT run_id::text,worker_id::text,generation,state,realm,"
                        "route_request_id::text FROM aap_jobs_v1.video_executions "
                        "WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .one()
        )
        windows = (
            (
                await connection.execute(
                    text(
                        "SELECT window_ordinal,checkpoint_id,manifest_digest,"
                        "manifest::text AS manifest FROM "
                        "aap_jobs_v1.video_execution_windows WHERE job_id=:job "
                        "ORDER BY window_ordinal"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .all()
        )
        parts = (
            (
                await connection.execute(
                    text(
                        "SELECT window_ordinal,part_index,part_digest,part_bytes "
                        "FROM aap_jobs_v1.video_execution_window_parts "
                        "WHERE job_id=:job "
                        "ORDER BY window_ordinal,part_index"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .all()
        )
        route_intent = (
            (
                await connection.execute(
                    text(
                        "SELECT request_id::text,scope_id::text,generation,"
                        "command_sha256,state,realm,public.pgp_sym_decrypt("
                        "command_ciphertext,pg_catalog.encode("
                        "(SELECT secret FROM aap_jobs_v1.video_execution_authority "
                        "WHERE singleton),'hex')) AS command "
                        "FROM aap_jobs_v1.video_execution_route_intents "
                        "WHERE request_id=:request"
                    ),
                    {"request": request_id},
                )
            )
            .mappings()
            .one()
        )
        route_intent = dict(route_intent)
        route_intent["command_payload_sha256"] = hashlib.sha256(
            route_intent.pop("command").encode("utf-8")
        ).hexdigest()
        route_receipt = (
            await connection.execute(
                text(
                    "SELECT to_jsonb(receipt)::text FROM "
                    "aap_inference_routing_v1.command_receipts AS receipt "
                    "WHERE id=:request"
                ),
                {"request": request_id},
            )
        ).scalar_one()
        old_route_scope = (
            (
                await connection.execute(
                    text(
                        "SELECT id::text,registry_id::text,authority_epoch::text,"
                        "realm,state,closed FROM aap_inference_routing_v1.scopes "
                        "WHERE id=:scope"
                    ),
                    {"scope": route_intent["scope_id"]},
                )
            )
            .mappings()
            .one()
        )
        route_receipt_digest = hashlib.sha256(route_receipt.encode("utf-8")).hexdigest()
        accepted = await connection.execute(
            text(
                "SELECT (to_jsonb(input)-ARRAY['lease_token','last_lease_token',"
                "'lease_owner'])::text "
                "FROM public.asset_accepted_video_inputs AS input "
                "WHERE input.job_id=:job"
            ),
            {"job": job_id},
        )
        accepted_input = accepted.scalar_one()
    return {
        "job": dict(job),
        "journal": dict(journal),
        "execution": dict(execution),
        "windows": [dict(row) for row in windows],
        "parts": [
            (
                row["window_ordinal"],
                row["part_index"],
                row["part_digest"],
                bytes(row["part_bytes"]),
            )
            for row in parts
        ],
        "route_intent": route_intent,
        "route_receipt_digest": route_receipt_digest,
        "old_route_scope": dict(old_route_scope),
        "accepted_input": accepted_input,
    }


def _assert_restored_frozen_state(before: dict, after: dict, *, target_realm: str):
    assert before["job"] == after["job"]
    assert before["journal"] == after["journal"]
    assert before["windows"] == after["windows"]
    assert before["parts"] == after["parts"]
    assert before["accepted_input"] == after["accepted_input"]
    assert before["route_receipt_digest"] == after["route_receipt_digest"]
    for key in (
        "request_id",
        "scope_id",
        "generation",
        "command_sha256",
        "state",
        "command_payload_sha256",
    ):
        assert before["route_intent"][key] == after["route_intent"][key]
    assert before["route_intent"]["realm"] != after["route_intent"]["realm"]
    assert after["route_intent"]["realm"] == target_realm
    for key in ("id", "registry_id", "authority_epoch", "realm"):
        assert before["old_route_scope"][key] == after["old_route_scope"][key]
    assert after["old_route_scope"]["state"] == "reconciled"
    assert after["old_route_scope"]["closed"] is True


async def _create_post_backup_active_request(runtime, owner, registry_id):
    command = make_routing_command(
        "acquire",
        {
            "pool_id": None,
            "registry_id": str(registry_id),
            "project_id": None,
            "mode": "direct",
            "required_instance_id": str(registry_id),
            "owner": f"api:restore-post-backup:{uuid.uuid4()}",
            "operation": "predict",
        },
    )
    scope = (await runtime.execute(command)).result
    attempt_id = uuid.uuid4()
    registry = await owner.fetchrow(
        "SELECT url,auth_method,auth_token,gpu_resource_id,extra_params "
        "FROM public.ml_backend_registry WHERE id=$1",
        registry_id,
    )
    identity = {
        "url": registry["url"],
        "auth_method": registry["auth_method"],
        "auth_token": registry["auth_token"],
        "gpu_resource_id": registry["gpu_resource_id"],
        "extra_params": (
            json.loads(registry["extra_params"])
            if isinstance(registry["extra_params"], str)
            else registry["extra_params"]
        ),
    }
    async with owner.transaction():
        subject = await _capture_subject(
            owner,
            scope_id=scope.id,
            capability=scope.capability,
            attempt_id=attempt_id,
            identity=identity,
        )
        begin = make_routing_command(
            "begin",
            {
                "scope_id": str(scope.id),
                "capability": scope.capability,
                "attempt_id": str(attempt_id),
                "method": "POST",
                "path": "/predict",
                "transport_subject": subject,
            },
        )
        admitted = (await runtime.execute(begin)).result
    admission = next(
        value
        for key, value in admitted.headers.items()
        if key.lower() == ROUTE_ADMISSION_HEADER.lower()
    )
    return scope, attempt_id, admission


async def test_restored_video_execution_resumes_after_real_global_route_drain(
    test_engine, tmp_path, monkeypatch, request
):
    """A copied bound Native checkpoint resumes behind real global backend fencing.

    This case uses the portable Native input/plan/checkpoint helpers and the
    independent Labeling writer. It does not start the production video runner
    or retrieve source bytes from S3; storage restore remains a separate gate.
    """
    request.node.user_properties.extend(
        [
            ("video_restore_worker", "native_checkpoint_fixture"),
            ("video_restore_source_bytes", "not_retrieved"),
            ("video_restore_s3_recovery", "not_tested"),
        ]
    )
    source_database = f"aap_video_restore_live_source_{uuid.uuid4().hex[:8]}_test"
    restored_database = f"aap_video_restore_live_target_{uuid.uuid4().hex[:8]}_test"
    source_marker = f"video-restore-live-source:{uuid.uuid4()}"
    restored_marker = f"video-restore-live-target:{uuid.uuid4()}"
    created: set[str] = set()
    source_engine = restored_engine = admin = None
    backend_server = backend_task = backend_listener = None
    request_task = None
    backend_http = None
    release_request = asyncio.Event()
    request_entered = asyncio.Event()
    owner = None
    operator = None
    completed_before = None
    dump = tmp_path / "video-restore-live.dump"

    try:
        admin = create_async_engine(
            test_engine.url.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,
        )
        source_engine = await _create_source_database(
            test_engine,
            admin,
            source_database,
            source_marker,
            await _database_acl(test_engine),
            created,
        )
        backend_token = f"video-restore-live-{uuid.uuid4().hex}"
        registry_id, pool_id, member_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        backend_app, backend_controller, backend_calls = _backend_app(
            registry_id,
            backend_token,
            entered=request_entered,
            release=release_request,
        )
        counter = _RequestCounter(backend_app)
        (
            backend_server,
            backend_task,
            backend_listener,
            backend_url,
        ) = await _start_uvicorn(counter)
        owner = await asyncpg.connect(
            _owner_dsn(test_engine, source_database), timeout=5, command_timeout=20
        )
        await _seed_world(
            owner,
            registry_id=registry_id,
            pool_id=pool_id,
            member_id=member_id,
            backend_url=backend_url,
            backend_token=backend_token,
        )
        source_routing_dsn = _role_dsn(
            "AAP_MODULE_DATA_INFERENCE_ROUTING_URL", _ROUTING_ROLE, source_database
        )
        source_runtime = RoutingRuntime(
            source_routing_dsn,
            backend_control=BackendRoutingControl(timeout_seconds=8),
        )
        initial_reconcile = make_routing_command(
            "reconcile",
            {"registry_id": str(registry_id), "control_id": str(uuid.uuid4())},
        )
        initial_control = await source_runtime.execute(initial_reconcile)
        assert initial_control.state == "committed"
        assert backend_controller.phase == "bound"

        source_sessions = async_sessionmaker(source_engine, expire_on_commit=False)
        async with source_sessions.begin() as db:
            job, old_proof, plan = await _prepared_execution(db)
            await _seal_window_with_lexemes(db, old_proof, plan, 0)
            project_id = await db.scalar(
                text(
                    "SELECT project_id FROM public.video_tracker_input_scopes "
                    "WHERE tracker_job_id=:job"
                ),
                {"job": job.id},
            )
        video_acquire = make_routing_command(
            "video_acquire",
            {
                "pool_id": None,
                "registry_id": str(registry_id),
                "project_id": str(project_id),
                "mode": "direct",
                "required_instance_id": str(registry_id),
                "owner": f"tracker:{job.id}",
                "operation": "tracker",
                "execution": old_proof.model_dump(mode="json"),
            },
            request_id=uuid.uuid4(),
        )
        old_route = await source_runtime.execute(video_acquire)
        assert old_route.state == "committed"
        assert old_route.result.registry_id == registry_id
        assert old_route.result.owner == f"tracker:{job.id}"

        source_before = await _native_snapshot(
            source_engine, job.id, video_acquire.request_id
        )
        assert source_before["job"]["status"] == "running"
        assert source_before["journal"]["plan_digest"] == plan.plan_digest
        assert [row["window_ordinal"] for row in source_before["windows"]] == [0]
        assert source_before["parts"]
        old_backend_epoch = source_before["old_route_scope"]["authority_epoch"]
        old_incarnation = str(backend_controller.backend_incarnation)
        source_routing_state = await owner.fetchrow(
            "SELECT authority_epoch::text,backend_incarnation::text "
            "FROM aap_inference_routing_v1.registry_state WHERE registry_id=$1",
            registry_id,
        )
        assert source_routing_state["authority_epoch"] == old_backend_epoch
        assert source_routing_state["backend_incarnation"] == old_incarnation

        dump.touch(mode=0o600, exist_ok=False)
        dump.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", source_database],
            )
        )
        assert dump.read_bytes().startswith(b"PGDMP")

        restored_engine = await _create_restored_database(
            test_engine,
            admin,
            source_engine,
            restored_database,
            restored_marker,
            created,
            dump,
        )
        restored_before = await _native_snapshot(
            restored_engine, job.id, video_acquire.request_id
        )
        assert restored_before == source_before
        assert (
            restored_before["execution"]["realm"] == source_before["execution"]["realm"]
        )

        # Change the controller epoch after the backup. The restore must observe
        # the live HTTP controller state instead of trusting the copied snapshot.
        retired_source = await source_runtime.retire_scope(
            {
                "schema": "aap-inference-scope-retirement/v1",
                "retirement_id": str(uuid.uuid4()),
                "scope_id": str(old_route.result.id),
                "capability": old_route.result.capability,
            }
        )
        assert retired_source.state == "committed"
        assert retired_source.result.phase == "completed"
        current_reconcile = make_routing_command(
            "reconcile",
            {"registry_id": str(registry_id), "control_id": str(uuid.uuid4())},
        )
        changed_control = await source_runtime.execute(current_reconcile)
        assert changed_control.state == "committed"
        assert changed_control.result.phase == "completed"
        assert backend_controller.phase == "bound"
        current_live_status = await _json_route_status(backend_url, backend_token)
        assert current_live_status["phase"] == "bound"
        assert current_live_status["backend_incarnation"] == old_incarnation
        assert current_live_status["authority_epoch"] != old_backend_epoch

        # This admission is created after pg_dump, so it is absent from the
        # restored database. The real backend controller still counts its work.
        (
            post_backup_scope,
            post_backup_attempt,
            admission,
        ) = await _create_post_backup_active_request(source_runtime, owner, registry_id)
        backend_http = httpx.AsyncClient(trust_env=False, timeout=30)
        request_task = asyncio.create_task(
            backend_http.post(
                f"{backend_url}/predict",
                json={"operation": "omitted-after-backup"},
                headers={ROUTE_ADMISSION_HEADER: admission},
            )
        )
        async with asyncio.timeout(10):
            await request_entered.wait()
        assert backend_controller.active_requests == 1
        async with restored_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM aap_inference_routing_v1.scopes "
                        "WHERE id=:id"
                    ),
                    {"id": post_backup_scope.id},
                )
                == 0
            )

        target_routing_dsn = _role_dsn(
            "AAP_MODULE_DATA_INFERENCE_ROUTING_URL", _ROUTING_ROLE, restored_database
        )
        target_runtime = RoutingRuntime(
            target_routing_dsn,
            backend_control=BackendRoutingControl(timeout_seconds=8),
        )
        writer_dsn = _role_dsn(
            "AAP_MODULE_DATA_LABELING_VIDEO_WRITER_URL", _WRITER_ROLE, restored_database
        )
        execution_dsn = _role_dsn(
            "AAP_MODULE_DATA_VIDEO_EXECUTION_URL",
            _VIDEO_EXECUTION_ROLE,
            restored_database,
        )
        operator_dsn = _role_dsn(
            "AAP_MODULE_DATA_VIDEO_RESTORE_URL", _RESTORE_ROLE, restored_database
        )
        operator = VideoRestorePostgresProvider(operator_dsn)
        manager = VideoRestoreManager(
            operator, backend_control=BackendRoutingControl(timeout_seconds=8)
        )
        identity = await manager.inspect()
        command = _command(identity, dump)

        # Even quarantined policy reads retain the shared transaction barrier.
        # Restore must wait for that transaction before committing its fence.
        barrier_connection = await asyncpg.connect(
            execution_dsn, timeout=5, command_timeout=15
        )
        barrier = barrier_connection.transaction()
        await barrier.start()
        try:
            policy = _json_value(
                await barrier_connection.fetchval(
                    "SELECT aap_jobs_v1.video_execution_step('policy','{}')"
                )
            )
            assert policy["ready"] is False
            busy = _json_value(
                await operator.step("prepare", {"command": command.as_dict()})
            )
            assert busy["status"] == "rejected"
            assert busy["reason"] == "execution_busy"
        finally:
            await barrier.rollback()
            await barrier_connection.close()
        no_partial_prepare = _json_value(
            await operator.step("status", {"restore_id": command.restore_id})
        )
        assert no_partial_prepare["status"] == "rejected"
        assert no_partial_prepare["reason"] == "restore_missing"

        pending = await manager.execute(command)
        assert pending.state == "pending"
        assert pending.record is not None and pending.record["phase"] == "prepared"
        progress = _json_value(
            await operator.step("status", {"restore_id": command.restore_id})
        )
        assert progress["restore"]["phase"] == "prepared"
        controller_context = progress["contexts"][0]
        assert controller_context["phase"] == "prepared"
        assert (
            controller_context["retired_epoch"]
            == current_live_status["authority_epoch"]
        )
        assert controller_context["retired_epoch"] != old_backend_epoch

        release_request.set()
        response = await asyncio.wait_for(request_task, timeout=15)
        assert response.status_code == 200
        await backend_http.aclose()
        backend_http = None
        request_task = None
        await source_runtime.execute(
            make_routing_command(
                "settle",
                {
                    "scope_id": str(post_backup_scope.id),
                    "capability": post_backup_scope.capability,
                    "attempt_id": str(post_backup_attempt),
                    "kind": "response",
                    "status_code": response.status_code,
                    "reason": None,
                },
            )
        )
        assert backend_controller.active_requests == 0

        # Inject a late owner-side failure after physical bind/record_bound.
        # The operator transaction must roll back, leaving the same prepared
        # receipt and copied Job facts available for the same command to retry.
        async with restored_engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE FUNCTION public.video_restore_test_fail_registry_update() "
                    "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                    "RAISE EXCEPTION 'restore_finish_fault'; END $$"
                )
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER video_restore_test_fail_registry_update "
                    "BEFORE UPDATE ON aap_inference_routing_v1.registry_state "
                    "FOR EACH ROW EXECUTE FUNCTION "
                    "public.video_restore_test_fail_registry_update()"
                )
            )
        try:
            failed_finish = await manager.execute(command)
        except RestoreUnavailable:
            failed_finish = None
        assert failed_finish is None or failed_finish.state != "completed"
        after_failed_finish = await manager.status(command.restore_id)
        assert after_failed_finish.state == "pending"
        assert after_failed_finish.record is not None
        assert after_failed_finish.record["phase"] == "prepared"
        after_rollback = await _native_snapshot(
            restored_engine, job.id, video_acquire.request_id
        )
        assert after_rollback["job"] == restored_before["job"]
        assert after_rollback["journal"] == restored_before["journal"]
        assert (
            after_rollback["execution"]["run_id"]
            == restored_before["execution"]["run_id"]
        )
        assert (
            after_rollback["execution"]["generation"]
            == restored_before["execution"]["generation"]
        )
        assert after_rollback["windows"] == restored_before["windows"]
        assert after_rollback["parts"] == restored_before["parts"]
        assert (
            after_rollback["route_receipt_digest"]
            == restored_before["route_receipt_digest"]
        )
        async with restored_engine.begin() as connection:
            await connection.execute(
                text(
                    "DROP TRIGGER video_restore_test_fail_registry_update "
                    "ON aap_inference_routing_v1.registry_state"
                )
            )
            await connection.execute(
                text("DROP FUNCTION public.video_restore_test_fail_registry_update()")
            )

        completed = await manager.execute(command)
        assert completed.state == "completed", completed.reason
        assert completed.record is not None and completed.record["phase"] == "completed"
        completed_before = completed.record
        after_restore = await _native_snapshot(
            restored_engine, job.id, video_acquire.request_id
        )
        _assert_restored_frozen_state(
            restored_before,
            after_restore,
            target_realm=identity["target_jobs_realm"],
        )
        assert after_restore["execution"]["run_id"] == str(old_proof.run_id)
        assert after_restore["execution"]["generation"] == old_proof.generation

        # A later legitimate epoch change must not erase a completed retirement.
        old_scope_id = after_restore["old_route_scope"]["id"]
        async with restored_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT aap_inference_routing_v1.video_execution_scope_retired("
                        "CAST(:scope AS uuid))"
                    ),
                    {"scope": old_scope_id},
                )
                is True
            )
        next_epoch = await target_runtime.execute(
            make_routing_command(
                "reconcile",
                {"registry_id": str(registry_id), "control_id": str(uuid.uuid4())},
            )
        )
        assert next_epoch.state == "committed"
        assert next_epoch.result.phase == "completed"
        async with restored_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT aap_inference_routing_v1.video_execution_scope_retired("
                        "CAST(:scope AS uuid))"
                    ),
                    {"scope": old_scope_id},
                )
                is True
            )

        # Rotation invalidates the copied bearer proof even when a database
        # restore reused the same cluster and the old token was otherwise live.
        old_role_connection = await asyncpg.connect(
            execution_dsn, timeout=5, command_timeout=10
        )
        try:
            async with old_role_connection.transaction():
                execution_data = await VideoExecutionPostgresData.bind(
                    old_role_connection
                )
                with pytest.raises(VideoExecutionFailure) as old_proof_error:
                    await execution_data.step(
                        "renew",
                        RenewRequest(**old_proof.model_dump(), lease_seconds=60),
                    )
            assert old_proof_error.value.code == "lease_lost"
        finally:
            await old_role_connection.close()

        # The same run now obtains a normal fresh claim, observes the global
        # retirement proof, enters recovery_ready, seals the remaining canonical
        # windows, and publishes through the independent Labeling owner.
        monkeypatch.setattr(settings, "inference_routing_url", "")
        monkeypatch.setattr(settings, "inference_routing_token", "")
        monkeypatch.setattr(settings, "inference_routing_token_file", "")
        monkeypatch.setattr(settings, "inference_routing_database_url", "")
        monkeypatch.setattr(settings, "inference_routing_database_url_file", "")
        restored_sessions = async_sessionmaker(restored_engine, expire_on_commit=False)
        writer = VideoWriterManager(VideoWriterPostgresProvider(writer_dsn))
        receipt = None
        async with restored_sessions() as db:
            async with tracker_execution(db, job.id) as claim:
                assert claim.generation == old_proof.generation + 1
                assert claim.proof().run_id == old_proof.run_id
                assert claim.state == "recovery_required"
                assert await recover_tracker_execution(db, job.id)
                new_proof = execution_proof(job.id)
                restored_route_status = await target_runtime.video_execution_status(
                    new_proof.model_dump(mode="json")
                )
                assert restored_route_status["scope"] is None
                journal = (
                    await db.execute(
                        text(
                            "SELECT plan_document FROM "
                            "aap_jobs_v1.video_execution_journals WHERE job_id=:job"
                        ),
                        {"job": job.id},
                    )
                ).scalar_one()
                frozen_plan = decode_video_execution_plan(journal)
                assert frozen_plan.plan_digest == plan.plan_digest
                previous = source_before["windows"][0]["manifest"]
                prior = decode_video_window_checkpoint_manifest(previous)
                for ordinal in range(1, len(frozen_plan.windows)):
                    prior = await _seal_window_with_lexemes(
                        db, new_proof, frozen_plan, ordinal, prior
                    )
                await db.commit()

                writer_command = make_video_writer_command(
                    new_proof,
                    outcome="completed",
                    expected_input={
                        "input_id": str(frozen_plan.input_id),
                        "request_digest": frozen_plan.request_digest,
                        "plan_digest": frozen_plan.source_plan_digest,
                        "source_version_id": frozen_plan.source.source_version_id,
                        "source_sha256": frozen_plan.source.source_sha256,
                    },
                    frame_results=(),
                )
                receipt = await writer.execute(writer_command)
                assert receipt.outcome == "completed"
                assert receipt.status == "pending_review"
                assert (
                    await writer.status(
                        writer_command.operation_id,
                        writer_command.execution,
                        video_writer_command_digest(writer_command),
                    )
                    == receipt
                )

        async with restored_engine.connect() as connection:
            final_job = (
                (
                    await connection.execute(
                        text(
                            "SELECT status,staged_result FROM public.video_tracker_jobs "
                            "WHERE id=:job"
                        ),
                        {"job": job.id},
                    )
                )
                .mappings()
                .one()
            )
            final_windows = (
                (
                    await connection.execute(
                        text(
                            "SELECT window_ordinal,manifest_digest,"
                            "manifest::text AS manifest "
                            "FROM aap_jobs_v1.video_execution_windows WHERE job_id=:job "
                            "ORDER BY window_ordinal"
                        ),
                        {"job": job.id},
                    )
                )
                .mappings()
                .all()
            )
            final_parts = (
                (
                    await connection.execute(
                        text(
                            "SELECT window_ordinal,part_index,part_digest,part_bytes "
                            "FROM aap_jobs_v1.video_execution_window_parts "
                            "WHERE job_id=:job "
                            "ORDER BY window_ordinal,part_index"
                        ),
                        {"job": job.id},
                    )
                )
                .mappings()
                .all()
            )
            label_receipt = (
                await connection.execute(
                    text(
                        "SELECT to_jsonb(receipt)::text FROM "
                        "aap_labeling_v1.video_writer_receipts AS receipt "
                        "WHERE job_id=:job"
                    ),
                    {"job": job.id},
                )
            ).scalar_one()
        assert final_job["status"] == "pending_review"
        assert [row["window_ordinal"] for row in final_windows] == [0, 1, 2]
        assert (
            final_windows[0]["manifest_digest"]
            == source_before["windows"][0]["manifest_digest"]
        )
        assert final_windows[0]["manifest"] == source_before["windows"][0]["manifest"]
        assert [
            (
                row["window_ordinal"],
                row["part_index"],
                row["part_digest"],
                bytes(row["part_bytes"]),
            )
            for row in final_parts
            if row["window_ordinal"] == 0
        ] == [part for part in source_before["parts"] if part[0] == 0]
        assert json.loads(label_receipt)["operation_id"] == str(receipt.operation_id)
        assert json.loads(label_receipt)["outcome"] == "completed"

        # A completed restore replays as an immutable receipt and makes no new
        # backend request, including the pre-finish safety status reads.
        backend_control_counts = (
            counter.paths[ROUTE_STATUS_PATH],
            counter.paths["/v1/aap-routing/control"],
        )
        requests_before_replay = tuple(backend_calls)
        assert requests_before_replay == ({"operation": "omitted-after-backup"},)
        replayed_restore = await manager.execute(command)
        assert replayed_restore.state == "completed"
        assert replayed_restore.record == completed_before
        assert backend_control_counts == (
            counter.paths[ROUTE_STATUS_PATH],
            counter.paths["/v1/aap-routing/control"],
        )
        assert tuple(backend_calls) == requests_before_replay
    finally:
        release_request.set()
        if request_task is not None:
            await asyncio.gather(request_task, return_exceptions=True)
        if backend_http is not None:
            await backend_http.aclose()
        if owner is not None:
            await owner.close()
        if backend_server is not None:
            await _stop_uvicorn(backend_server, backend_task, backend_listener)
        if restored_engine is not None:
            await restored_engine.dispose()
        if source_engine is not None:
            await source_engine.dispose()
        if admin is not None:
            if restored_database in created:
                await _drop_owned_clone(
                    admin, test_engine, restored_database, restored_marker
                )
            if source_database in created:
                await _drop_owned_clone(
                    admin, test_engine, source_database, source_marker
                )
            await admin.dispose()


async def test_restore_refuses_backend_cold_restart_with_changed_incarnation(
    test_engine, tmp_path
):
    """A restarted CPU controller cannot retire the old backend incarnation."""
    source_database = f"aap_video_restore_cold_source_{uuid.uuid4().hex[:8]}_test"
    restored_database = f"aap_video_restore_cold_target_{uuid.uuid4().hex[:8]}_test"
    source_marker = f"video-restore-cold-source:{uuid.uuid4()}"
    restored_marker = f"video-restore-cold-target:{uuid.uuid4()}"
    created: set[str] = set()
    source_engine = restored_engine = admin = None
    backend_server = backend_task = backend_listener = None
    replacement_server = replacement_task = replacement_listener = None
    owner = None
    dump = tmp_path / "video-restore-cold.dump"
    provider = None
    try:
        admin = create_async_engine(
            test_engine.url.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,
        )
        source_engine = await _create_source_database(
            test_engine,
            admin,
            source_database,
            source_marker,
            await _database_acl(test_engine),
            created,
        )
        token = f"video-restore-cold-{uuid.uuid4().hex}"
        registry_id, pool_id, member_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        first_app, first_controller, _ = _backend_app(registry_id, token)
        first_counter = _RequestCounter(first_app)
        (
            backend_server,
            backend_task,
            backend_listener,
            backend_url,
        ) = await _start_uvicorn(first_counter)
        owner = await asyncpg.connect(
            _owner_dsn(test_engine, source_database), timeout=5, command_timeout=20
        )
        await _seed_world(
            owner,
            registry_id=registry_id,
            pool_id=pool_id,
            member_id=member_id,
            backend_url=backend_url,
            backend_token=token,
        )
        source_runtime = RoutingRuntime(
            _role_dsn(
                "AAP_MODULE_DATA_INFERENCE_ROUTING_URL", _ROUTING_ROLE, source_database
            ),
            backend_control=BackendRoutingControl(timeout_seconds=8),
        )
        reconciled = await source_runtime.execute(
            make_routing_command(
                "reconcile",
                {"registry_id": str(registry_id), "control_id": str(uuid.uuid4())},
            )
        )
        assert reconciled.state == "committed"
        old_incarnation = str(first_controller.backend_incarnation)
        dump.touch(mode=0o600, exist_ok=False)
        dump.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", source_database],
            )
        )
        restored_engine = await _create_restored_database(
            test_engine,
            admin,
            source_engine,
            restored_database,
            restored_marker,
            created,
            dump,
        )
        await _stop_uvicorn(backend_server, backend_task, backend_listener)
        backend_server = backend_task = backend_listener = None

        cold_app, cold_controller, _ = _backend_app(registry_id, token)
        cold_counter = _RequestCounter(cold_app)
        port = int(httpx.URL(backend_url).port)
        (
            replacement_server,
            replacement_task,
            replacement_listener,
            replacement_url,
        ) = await _start_uvicorn_on_port(cold_counter, port)
        assert replacement_url == backend_url
        assert str(cold_controller.backend_incarnation) != old_incarnation

        provider = VideoRestorePostgresProvider(
            _role_dsn(
                "AAP_MODULE_DATA_VIDEO_RESTORE_URL", _RESTORE_ROLE, restored_database
            )
        )
        manager = VideoRestoreManager(
            provider, backend_control=BackendRoutingControl(timeout_seconds=8)
        )
        identity = await manager.inspect()
        assert (
            identity["source_jobs_realm"].split(":", 2)[2]
            == identity["target_jobs_realm"].split(":", 2)[2]
        )
        command = _command(identity, dump)
        try:
            result = await manager.execute(command)
        except RestoreUnavailable:
            pass
        else:
            assert result.state in {"pending", "unknown"}, result.reason
        held = await manager.status(command.restore_id)
        assert held.state == "pending", held.reason
        assert held.record is not None and held.record["phase"] == "prepared"
        native_status = _json_value(
            await provider.step("status", {"restore_id": command.restore_id})
        )
        assert native_status["restore"]["phase"] == "prepared"
        assert cold_counter.paths[ROUTE_STATUS_PATH] >= 1
    finally:
        if owner is not None:
            await owner.close()
        if backend_server is not None:
            await _stop_uvicorn(backend_server, backend_task, backend_listener)
        if replacement_server is not None:
            await _stop_uvicorn(
                replacement_server, replacement_task, replacement_listener
            )
        if restored_engine is not None:
            await restored_engine.dispose()
        if source_engine is not None:
            await source_engine.dispose()
        if admin is not None:
            if restored_database in created:
                await _drop_owned_clone(
                    admin, test_engine, restored_database, restored_marker
                )
            if source_database in created:
                await _drop_owned_clone(
                    admin, test_engine, source_database, source_marker
                )
            await admin.dispose()
