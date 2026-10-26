"""Owned PostgreSQL and real-HTTP acceptance for the Inference routing owner."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
import uuid
from collections import Counter
from pathlib import Path

import asyncpg
from fastapi import FastAPI
import httpx
import pytest
import uvicorn
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference.pipeline_server import PipelineHttpServer
from aap_inference.routing_backend_control import BackendRoutingControl
from aap_inference.routing_runtime import RoutingRuntime
from aap_inference_contract.routing import (
    make_routing_command,
    make_routing_quiescence_request,
    make_routing_runtime_request,
    RoutingRejectedResult,
    RoutingUnavailable,
)
from aap_inference_contract.routing_client import RoutingHttpClient
from aap_protocol_v2.routing import ROUTE_ADMISSION_HEADER
from aap_protocol_v2.routing_runtime import install_backend_routing_controller
from app.db.models.user import User
from tests.labeling_class_query_helpers import assert_owned_test_database


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified disposable module-data environment",
    ),
]

_ROUTING_SCHEMA = "aap_inference_routing_v1"
_ROUTING_ROLE = "inference_routing_client"
_ROUTING_STEP = f"{_ROUTING_SCHEMA}.routing_step"
_COMMAND_PATH = "/v1/routes/command"
_STATUS_PATH = "/v1/routes/status"


def _owner_dsn(test_engine) -> str:
    value = test_engine.url.set(drivername="postgresql")
    assert value.database and value.database.endswith(("_test", "_e2e"))
    return value.render_as_string(hide_password=False)


def _available_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


async def _start_uvicorn(app: FastAPI):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
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
                    raise AssertionError("backend HTTP server stopped before startup")
                await asyncio.sleep(0.01)
    except BaseException:
        server.should_exit = True
        listener.close()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, timeout=5)
        raise
    return server, task, listener, f"http://127.0.0.1:{port}"


async def _stop_uvicorn(server, task, listener) -> None:
    if server is not None:
        server.should_exit = True
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=10)
        except asyncio.TimeoutError:
            server.force_exit = True
            await asyncio.wait_for(task, timeout=5)
    if listener is not None:
        listener.close()


async def _stop_process(process) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def _start_model_execution_owner(tmp_path, routing_dsn: str):
    from app.config import settings

    role_sources = {
        "INFERENCE_BACKEND_ENDPOINT_EXECUTION_DATABASE_URL": (
            "AAP_MODULE_DATA_INFERENCE_ENDPOINT_EXECUTION_URL"
        ),
        "INFERENCE_GPU_DATABASE_URL": "AAP_MODULE_DATA_INFERENCE_GPU_URL",
        "INFERENCE_GPU_ENDPOINT_HEALTH_OBSERVATION_DATABASE_URL": (
            "AAP_MODULE_DATA_INFERENCE_GPU_HEALTH_ENDPOINT_URL"
        ),
        "INFERENCE_GPU_ENDPOINT_GPU_CONTROL_DATABASE_URL": (
            "AAP_MODULE_DATA_INFERENCE_GPU_CONTROL_ENDPOINT_URL"
        ),
        "INFERENCE_HEALTH_DATA_DATABASE_URL": (
            "AAP_MODULE_DATA_INFERENCE_HEALTH_DATA_URL"
        ),
    }
    if any(not os.environ.get(name) for name in role_sources.values()):
        raise AssertionError(
            "module-data verifier is missing Inference execution role URLs"
        )
    if not settings.redis_url:
        raise AssertionError("test-owned Redis URL is required by the execution owner")

    port = _available_port()
    execution_token = f"execution-owner-{uuid.uuid4().hex}"
    environment = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "TMPDIR")
        if name in os.environ
    }
    python_path = [path for path in sys.path if path]
    if python_path:
        environment["PYTHONPATH"] = os.pathsep.join(python_path)
    environment.update(
        {
            **{
                runtime_name: os.environ[role_name]
                for runtime_name, role_name in role_sources.items()
            },
            "INFERENCE_ROUTING_DATABASE_URL": routing_dsn,
            "INFERENCE_ROUTING_TOKEN": f"execution-routing-{uuid.uuid4().hex}",
            "INFERENCE_MODEL_EXECUTION_TOKEN": execution_token,
            "INFERENCE_GPU_REDIS_URL": settings.redis_url,
            "INFERENCE_HOST": "127.0.0.1",
            "INFERENCE_PORT": str(port),
            "GPU_ARBITER_MODE": "off",
            "GPU_ARBITER_RESOURCES_JSON": "{}",
        }
    )
    log_path = tmp_path / "model-execution-owner.log"
    process = None
    try:
        with log_path.open("xb") as log_file:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "aap_inference",
                cwd=Path(__file__).resolve().parents[3],
                env=environment,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
            )
        base_url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            async with asyncio.timeout(30):
                while True:
                    if process.returncode is not None:
                        raise AssertionError(
                            "model execution owner exited before readiness"
                        )
                    try:
                        response = await client.get(f"{base_url}/health")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.025)
        return process, base_url, execution_token
    except BaseException:
        if process is not None:
            await _stop_process(process)
        raise


class _OwnerHttpProxy:
    """Forward real owner HTTP and drop one selected command acknowledgement."""

    def __init__(self, target_port: int, *, drop_path: str = _COMMAND_PATH) -> None:
        self.target_port = target_port
        self.drop_path = drop_path
        self.counts: Counter[str] = Counter()
        self.errors: list[str] = []
        self.drop_next_command_ack = False
        self._server = None

    async def start(self) -> str:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, limit=64 * 1024
        )
        port = int(self._server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{port}"

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer) -> None:
        upstream_writer = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            if len(head) > 64 * 1024:
                raise ValueError("oversized local owner request")
            lines = head.split(b"\r\n")
            _method, path, _version = lines[0].split(b" ", 2)
            content_length = 0
            for line in lines[1:]:
                if not line:
                    continue
                name, separator, value = line.partition(b":")
                if not separator:
                    raise ValueError("malformed local owner header")
                if name.strip().lower() == b"content-length":
                    content_length = int(value.strip())
            if content_length < 0 or content_length > 1_048_576:
                raise ValueError("invalid local owner content length")
            body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5)
            path_text = path.decode("ascii")
            self.counts[path_text] += 1
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", self.target_port
            )
            forwarded = [
                line
                for line in lines
                if line and not line.lower().startswith(b"connection:")
            ]
            forwarded.extend((b"Connection: close", b"", b""))
            upstream_writer.write(b"\r\n".join(forwarded) + body)
            await upstream_writer.drain()
            response = await asyncio.wait_for(upstream_reader.read(), timeout=15)
            if path_text == self.drop_path and self.drop_next_command_ack:
                self.drop_next_command_ack = False
            else:
                writer.write(response)
                await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                with contextlib.suppress(ConnectionError, OSError):
                    await upstream_writer.wait_closed()
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()


async def _insert_user(test_engine, user_id: uuid.UUID) -> str:
    email = f"routing-owner-{user_id.hex}@example.test"
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            User(
                id=user_id,
                email=email,
                name="routing owner transaction baseline",
                password_hash="not-used-by-this-test",
                role="employee",
                is_active=True,
            )
        )
        await session.commit()
    return "routing owner transaction baseline"


async def _seed_world(
    owner,
    *,
    registry_id: uuid.UUID,
    pool_id: uuid.UUID,
    member_id: uuid.UUID,
    backend_url: str,
    backend_token: str,
) -> dict:
    extra_params = {"max_concurrency": 2}
    async with owner.transaction():
        await owner.execute(
            """INSERT INTO public.ml_backend_registry (
                   id, name, url, state, is_interactive, auth_method, auth_token,
                   extra_params, gpu_resource_id, vram_budget_mb, eviction_priority,
                   health_meta, source, error_message, last_checked_at
               ) VALUES (
                   $1, $2, $3, 'connected', false, 'token', $4,
                   $5::jsonb, NULL, NULL, 0, $6::jsonb, 'manual', NULL, now()
               )""",
            registry_id,
            f"Routing owner HTTP {registry_id}",
            backend_url,
            backend_token,
            json.dumps(extra_params),
            json.dumps(
                {
                    "compute": {"configured_device": "cpu"},
                    "capabilities": {"model_version": "routing-owner-test"},
                }
            ),
        )
        await owner.execute(
            """INSERT INTO public.ml_backend_service_pools (
                   id, name, enabled, routing_policy, legacy_instance_id,
                   routing_generation, capability_fingerprint, capability_snapshot
               ) VALUES (
                   $1, $2, true, 'smooth_weighted_round_robin', $3, 1, NULL, NULL
               )""",
            pool_id,
            f"Routing owner pool {pool_id}",
            registry_id,
        )
        await owner.execute(
            """INSERT INTO public.ml_backend_pool_members (
                   id, pool_id, registry_id, traffic_state, weight
               ) VALUES ($1, $2, $3, 'active', 1)""",
            member_id,
            pool_id,
            registry_id,
        )
    return {
        "registry_id": registry_id,
        "pool_id": pool_id,
        "member_id": member_id,
        "backend_url": backend_url,
        "backend_token": backend_token,
        "extra_params": extra_params,
    }


async def _capture_subject(
    connection,
    *,
    scope_id: uuid.UUID,
    capability: str,
    attempt_id: uuid.UUID,
    identity: dict,
    control: bool = False,
) -> str:
    arguments = {
        "parent_kind": "control" if control else "scope",
        "parent_id": str(scope_id),
        "capability": capability,
        "attempt_id": str(attempt_id),
        "transport_identity": identity,
    }
    raw = await connection.fetchval(
        f"SELECT {_ROUTING_STEP}($1::text, $2::text)",
        "capture_transport_subject",
        json.dumps(
            arguments, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ),
    )
    reply = json.loads(raw)
    assert reply["schema"] == "aap-inference-routing-data/v1"
    assert reply["status"] == "ok"
    assert set(reply) == {"schema", "status", "subject"}
    assert type(reply["subject"]) is str and reply["subject"].startswith("v1.")
    return reply["subject"]


async def _cleanup_world(owner, world: dict, receipt_ids: list[uuid.UUID]) -> None:
    async with owner.transaction():
        registry_id = world["registry_id"]
        pool_id = world["pool_id"]
        await owner.execute(
            "DELETE FROM aap_inference_routing_v1.control_attempts "
            "WHERE control_id IN (SELECT id FROM aap_inference_routing_v1.controls "
            "WHERE registry_id=$1)",
            registry_id,
        )
        await owner.execute(
            "DELETE FROM aap_inference_routing_v1.attempts "
            "WHERE scope_id IN (SELECT id FROM aap_inference_routing_v1.scopes "
            "WHERE registry_id=$1)",
            registry_id,
        )
        await owner.execute(
            "DELETE FROM aap_inference_routing_v1.scopes WHERE registry_id=$1",
            registry_id,
        )
        await owner.execute(
            "DELETE FROM aap_inference_routing_v1.controls WHERE registry_id=$1",
            registry_id,
        )
        await owner.execute(
            "DELETE FROM aap_inference_routing_v1.command_receipts WHERE id=ANY($1::uuid[])",
            receipt_ids,
        )
        await owner.execute(
            "DELETE FROM aap_inference_routing_v1.weights WHERE registry_id=$1",
            registry_id,
        )
        await owner.execute(
            "DELETE FROM aap_inference_routing_v1.registry_state WHERE registry_id=$1",
            registry_id,
        )
        await owner.execute(
            "DELETE FROM public.project_ml_backend_pool WHERE pool_id=$1", pool_id
        )
        await owner.execute(
            "DELETE FROM public.ml_backend_pool_members WHERE registry_id=$1",
            registry_id,
        )
        await owner.execute(
            "DELETE FROM public.ml_backend_service_pools WHERE id=$1", pool_id
        )
        await owner.execute(
            "DELETE FROM public.ml_backend_registry WHERE id=$1", registry_id
        )


def _command(receipt_ids: list[uuid.UUID], operation: str, body: dict):
    command = make_routing_command(operation, body)
    receipt_ids.append(command.request_id)
    return command


async def test_routing_owner_real_database_http_and_captured_transaction(
    test_engine, monkeypatch, tmp_path
):
    ownership = await assert_owned_test_database(test_engine)
    routing_dsn = os.environ.get("AAP_MODULE_DATA_INFERENCE_ROUTING_URL")
    assert routing_dsn, (
        "module-data verifier must provide the restricted routing LOGIN DSN"
    )

    # Verify the runtime URL logs in as the provisioned function-only identity. The
    # runtime below receives this URL unchanged; it never emulates the role with SET ROLE.
    restricted_probe = await asyncpg.connect(
        routing_dsn.replace("postgresql+asyncpg://", "postgresql://", 1),
        timeout=5,
        command_timeout=10,
    )
    try:
        current_user, database = await restricted_probe.fetchrow(
            "SELECT current_user, current_database()"
        )
        assert current_user == _ROUTING_ROLE
        assert database == ownership["database"]
    finally:
        await restricted_probe.close()

    owner = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=15
    )
    world = {
        "registry_id": uuid.uuid4(),
        "pool_id": uuid.uuid4(),
        "member_id": uuid.uuid4(),
    }
    receipt_ids: list[uuid.UUID] = []
    user_id = uuid.uuid4()
    baseline_user_name = None
    borrowed = None
    borrowed_transaction = None
    backend_server = backend_task = backend_listener = None
    owner_runtime = owner_server = proxy = None
    execution_process = None
    native_trace = []

    try:
        baseline_user_name = await _insert_user(test_engine, user_id)
        backend_token = f"backend-control-{uuid.uuid4().hex}"
        route_owner_token = f"routing-owner-{uuid.uuid4().hex}"
        backend_calls: list[dict] = []
        app = FastAPI()
        unload_entered, unload_finish = asyncio.Event(), asyncio.Event()
        unload_calls = []

        @app.post("/predict")
        async def predict(payload: dict):
            backend_calls.append(payload)
            reply = {"call": len(backend_calls), "received": payload}
            if "tasks" in payload:
                reply["results"] = [
                    {"task": task["id"], "result": []} for task in payload["tasks"]
                ]
            return reply

        @app.post("/unload")
        async def unload():
            unload_calls.append(True)
            unload_entered.set()
            await unload_finish.wait()
            return {"ok": True, "unloaded": True}

        controller = install_backend_routing_controller(
            app,
            registry_id=world["registry_id"],
            environ={
                "AAP_ROUTE_CONTROL_TOKEN": backend_token,
                "AAP_ROUTE_ALLOW_LEGACY_EXECUTION": "0",
            },
        )
        (
            backend_server,
            backend_task,
            backend_listener,
            backend_url,
        ) = await _start_uvicorn(app)
        await _seed_world(
            owner,
            registry_id=world["registry_id"],
            pool_id=world["pool_id"],
            member_id=world["member_id"],
            backend_url=backend_url,
            backend_token=backend_token,
        )

        owner_runtime = RoutingRuntime(
            routing_dsn,
            backend_control=BackendRoutingControl(timeout_seconds=8),
        )
        transaction_factory = owner_runtime._transaction

        @contextlib.asynccontextmanager
        async def observed_transaction():
            async with transaction_factory() as data:
                step = data.step

                async def observed_step(action, arguments):
                    try:
                        reply = await step(action, arguments)
                    except Exception as error:
                        cause = error
                        while cause.__context__ is not None:
                            cause = cause.__context__
                        # Fixed action names and database error types only; never
                        # record subjects, SQL arguments, credentials or replies.
                        native_trace.append(
                            (
                                action,
                                type(cause).__name__,
                                getattr(cause, "sqlstate", None),
                            )
                        )
                        raise
                    native_trace.append((action, reply.get("status"), sorted(reply)))
                    return reply

                data.step = observed_step
                yield data

        owner_runtime.manager._data_factory = observed_transaction
        assert await owner_runtime.readiness()
        owner_port = _available_port()
        owner_server = PipelineHttpServer(
            manager=None,
            data=None,
            control_token=None,
            host="127.0.0.1",
            port=owner_port,
            routing_manager=owner_runtime,
            routing_control_token=route_owner_token,
        )
        await owner_server.start()
        proxy = _OwnerHttpProxy(owner_port)
        proxy_url = await proxy.start()
        routing_client = RoutingHttpClient(
            proxy_url, route_owner_token, timeout_seconds=20
        )
        quiescence = await routing_client.quiescence(
            make_routing_quiescence_request(world["registry_id"])
        )
        assert quiescence.status == "rejected"
        assert quiescence.reason == "member_not_draining"

        reconcile = _command(
            receipt_ids,
            "reconcile",
            {"registry_id": str(world["registry_id"]), "control_id": str(uuid.uuid4())},
        )
        reconciled = await routing_client.execute(reconcile)
        assert reconciled.state == "committed"
        assert reconciled.result.phase == "completed"
        assert reconciled.result.registry_id == world["registry_id"]
        assert controller.phase == "bound"

        acquire = _command(
            receipt_ids,
            "acquire",
            {
                "pool_id": None,
                "registry_id": str(world["registry_id"]),
                "project_id": None,
                "mode": "direct",
                "required_instance_id": str(world["registry_id"]),
                "owner": "api:routing-owner-integration",
                "operation": "predict",
            },
        )
        acquired = await routing_client.execute(acquire)
        replayed_acquire = await routing_client.execute(acquire)
        assert acquired.state == replayed_acquire.state == "committed"
        assert acquired.result.id == replayed_acquire.result.id
        receipt_count = await owner.fetchval(
            "SELECT count(*) FROM aap_inference_routing_v1.command_receipts"
        )
        runtime = await routing_client.runtime(
            make_routing_runtime_request([world["registry_id"]])
        )
        assert runtime.registries[0].open_scopes == 1
        assert runtime.registries[0].continuity == "ready"
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM aap_inference_routing_v1.command_receipts"
            )
            == receipt_count
        )
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM aap_inference_routing_v1.scopes WHERE registry_id=$1",
                world["registry_id"],
            )
            == 1
        )

        attempt_id = uuid.uuid4()
        borrowed = await asyncpg.connect(
            _owner_dsn(test_engine), timeout=5, command_timeout=15
        )
        owner_name = await borrowed.fetchval(
            "SELECT current_user FROM pg_catalog.pg_roles WHERE rolname=current_user"
        )
        assert owner_name != _ROUTING_ROLE
        borrowed_transaction = borrowed.transaction()
        await borrowed_transaction.start()
        await borrowed.execute(
            "UPDATE public.users SET name=$2 WHERE id=$1",
            user_id,
            "uncommitted caller-owned business change",
        )
        origin_xid = await borrowed.fetchval("SELECT pg_current_xact_id()::text")
        registry = await borrowed.fetchrow(
            """SELECT url, auth_method, auth_token, gpu_resource_id, extra_params
               FROM public.ml_backend_registry WHERE id=$1""",
            world["registry_id"],
        )
        extra_params = registry["extra_params"]
        if type(extra_params) is str:
            extra_params = json.loads(extra_params)
        transport_identity = {
            "url": registry["url"],
            "auth_method": registry["auth_method"],
            "auth_token": registry["auth_token"],
            "gpu_resource_id": registry["gpu_resource_id"],
            "extra_params": extra_params,
        }
        subject = await _capture_subject(
            borrowed,
            scope_id=acquired.result.id,
            capability=acquired.result.capability,
            attempt_id=attempt_id,
            identity=transport_identity,
        )
        begin = _command(
            receipt_ids,
            "begin",
            {
                "scope_id": str(acquired.result.id),
                "capability": acquired.result.capability,
                "attempt_id": str(attempt_id),
                "method": "POST",
                "path": "/predict",
                "transport_subject": subject,
            },
        )
        commands_before_begin = proxy.counts[_COMMAND_PATH]
        statuses_before_begin = proxy.counts[_STATUS_PATH]
        proxy.drop_next_command_ack = True
        began = await routing_client.execute(begin)
        assert began.state == "committed"
        assert began.result.attempt_id == attempt_id
        assert proxy.drop_next_command_ack is False
        assert proxy.counts[_COMMAND_PATH] == commands_before_begin + 1
        assert proxy.counts[_STATUS_PATH] == statuses_before_begin + 1
        assert (
            await borrowed.fetchval("SELECT pg_xact_status($1::xid8)", int(origin_xid))
            == "in progress"
        )
        route_headers = began.result.headers
        admission_token = next(
            value
            for key, value in route_headers.items()
            if key.lower() == ROUTE_ADMISSION_HEADER
        )

        # The captured source transaction remains alive through Native open/begin,
        # then rolls back before the actual backend compute request starts.
        await borrowed_transaction.rollback()
        borrowed_transaction = None
        await borrowed.close()
        borrowed = None
        assert (
            await owner.fetchval("SELECT name FROM public.users WHERE id=$1", user_id)
            == baseline_user_name
        )

        payload = {"frame": 7, "label": "car"}
        async with httpx.AsyncClient(trust_env=False, timeout=10) as backend_client:
            denied = await backend_client.post(f"{backend_url}/predict", json=payload)
            assert denied.status_code == 401
            response = await backend_client.post(
                f"{backend_url}/predict",
                json=payload,
                headers={ROUTE_ADMISSION_HEADER: admission_token},
            )
        assert response.status_code == 200
        assert response.json() == {"call": 1, "received": payload}
        assert backend_calls == [payload]
        assert controller.phase == "bound"
        assert controller.active_requests == 0

        replayed_begin = await routing_client.execute(begin)
        assert replayed_begin.state == "committed"
        assert replayed_begin.result.attempt_id == attempt_id
        assert replayed_begin.result.headers == began.result.headers
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM aap_inference_routing_v1.attempts "
                "WHERE id=$1 AND scope_id=$2",
                attempt_id,
                acquired.result.id,
            )
            == 1
        )
        assert backend_calls == [payload]

        settled = await routing_client.execute(
            _command(
                receipt_ids,
                "settle",
                {
                    "scope_id": str(acquired.result.id),
                    "capability": acquired.result.capability,
                    "attempt_id": str(attempt_id),
                    "kind": "response",
                    "status_code": response.status_code,
                    "reason": None,
                },
            )
        )
        assert settled.state == "committed"
        assert settled.result.state == "response"
        assert settled.result.status_code == 200
        closed = await routing_client.execute(
            _command(
                receipt_ids,
                "close",
                {
                    "scope_id": str(acquired.result.id),
                    "capability": acquired.result.capability,
                    "outcome": "success",
                },
            )
        )
        assert closed.state == "committed"
        assert closed.result.closed is True
        assert closed.result.state == "finished"

        # The canonical API gateway uses the remote execution provider and routing
        # owner while preserving this caller's uncommitted business update.
        from app.config import GPUArbiterMode, settings
        from app.services.inference_model_execution import model_execution_port
        from aap_inference_contract.execution import FrozenModelOperation

        (
            execution_process,
            execution_url,
            execution_token,
        ) = await _start_model_execution_owner(tmp_path, routing_dsn)
        for name, value in {
            "inference_routing_url": proxy_url,
            "inference_routing_token": route_owner_token,
            "inference_routing_token_file": "",
            "inference_model_execution_url": execution_url,
            "inference_model_execution_token": execution_token,
            "inference_model_execution_token_file": "",
            "gpu_arbiter_mode": GPUArbiterMode.OFF,
            "gpu_arbiter_rollout_enabled": False,
        }.items():
            monkeypatch.setattr(settings, name, value)
        before_api_receipts = set(
            await owner.fetchval(
                "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) FROM aap_inference_routing_v1.command_receipts"
            )
        )
        try:
            async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
                user = await db.get(User, user_id)
                user.name = "uncommitted real API gateway change"
                await db.flush()
                request = FrozenModelOperation(
                    request_id=uuid.uuid4(),
                    operation="batch_predict",
                    task_inputs=(
                        {
                            "id": "api-one",
                            "file_path": "https://fixture.invalid/api-one.jpg",
                            "data": {},
                        },
                    ),
                    context=None,
                    project_id=None,
                    requested_registry_id=world["registry_id"],
                    requested_pool_id=None,
                    endpoint_registry_id=None,
                    required_instance_id=world["registry_id"],
                    route_owner="api:routing-owner-integration",
                    route_operation="batch_predict",
                )
                before_backend_calls = len(backend_calls)
                result = await model_execution_port(db).execute(request)
                assert len(result.results) == 1
                assert result.results[0].task_id == "api-one"
                assert result.provenance.registry_id == world["registry_id"]
                assert result.provenance.attempt_id == request.request_id
                assert result.provenance.route_scope_id is not None
                assert len(backend_calls) == before_backend_calls + 1
                assert (
                    await owner.fetchval(
                        "SELECT name FROM public.users WHERE id=$1", user_id
                    )
                    == baseline_user_name
                )
                await db.rollback()
            assert (
                await owner.fetchval(
                    "SELECT name FROM public.users WHERE id=$1", user_id
                )
                == baseline_user_name
            )
        finally:
            after_api_receipts = set(
                await owner.fetchval(
                    "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) FROM aap_inference_routing_v1.command_receipts"
                )
            )
            receipt_ids.extend(after_api_receipts - before_api_receipts)
        # An unknown destructive response retains its hold. Recovery reuses that
        # exact control ID and waits for actual backend work, not database zero.
        await owner.execute(
            "UPDATE public.ml_backend_pool_members SET traffic_state='draining' WHERE registry_id=$1",
            world["registry_id"],
        )
        control_id = uuid.uuid4()
        prepared_control = await routing_client.execute(
            _command(
                receipt_ids,
                "prepare_control",
                {
                    "registry_id": str(world["registry_id"]),
                    "control_id": str(control_id),
                    "kind": "unload",
                    "expected_topology": None,
                },
            )
        )
        management = prepared_control.result
        assert management.phase == "prepared"
        control_attempt = uuid.uuid4()
        async with owner.transaction():
            subject = await _capture_subject(
                owner,
                scope_id=control_id,
                capability=management.capability,
                attempt_id=control_attempt,
                identity=transport_identity,
                control=True,
            )
            control_begin = await routing_client.execute(
                _command(
                    receipt_ids,
                    "begin_control",
                    {
                        "control_id": str(control_id),
                        "capability": management.capability,
                        "attempt_id": str(control_attempt),
                        "method": "POST",
                        "path": "/unload",
                        "transport_subject": subject,
                    },
                )
            )
        control_token = control_begin.result.headers["X-AAP-Route-Admission"]
        async with httpx.AsyncClient(trust_env=False, timeout=0.1) as backend_client:
            with pytest.raises(httpx.ReadTimeout):
                await backend_client.post(
                    f"{backend_url}/unload",
                    headers={ROUTE_ADMISSION_HEADER: control_token},
                )
        assert unload_entered.is_set() and controller.active_requests == 1
        await routing_client.execute(
            _command(
                receipt_ids,
                "settle_control",
                {
                    "control_id": str(control_id),
                    "capability": management.capability,
                    "attempt_id": str(control_attempt),
                    "kind": "uncertain",
                    "status_code": None,
                    "reason": "transport_timeout",
                },
            )
        )
        recovery = await routing_client.execute(
            _command(
                receipt_ids,
                "reconcile",
                {
                    "registry_id": str(world["registry_id"]),
                    "control_id": str(control_id),
                },
            )
        )
        assert recovery.state == "committed" and recovery.result.phase == "draining"
        assert controller.active_requests == 1
        observed = await routing_client.runtime(
            make_routing_runtime_request([world["registry_id"]])
        )
        assert observed.registries[0].control_id == control_id
        unload_finish.set()
        async with asyncio.timeout(5):
            while controller.active_requests:
                await asyncio.sleep(0.01)
        recovered = await routing_client.execute(
            _command(
                receipt_ids,
                "reconcile",
                {
                    "registry_id": str(world["registry_id"]),
                    "control_id": str(control_id),
                },
            )
        )
        assert recovered.result.phase == "completed"
        assert (
            await owner.fetchval(
                "SELECT state FROM aap_inference_routing_v1.control_attempts WHERE id=$1",
                control_attempt,
            )
            == "reconciled"
        )
        assert (
            await owner.fetchval(
                "SELECT recovered_kind FROM aap_inference_routing_v1.controls WHERE id=$1",
                control_id,
            )
            == "unload"
        )
        async with httpx.AsyncClient(trust_env=False, timeout=5) as backend_client:
            retired = await backend_client.post(
                f"{backend_url}/unload", headers={ROUTE_ADMISSION_HEADER: control_token}
            )
        assert retired.status_code == 401 and unload_calls == [True]
        late = await routing_client.execute(
            _command(
                receipt_ids,
                "settle_control",
                {
                    "control_id": str(control_id),
                    "capability": management.capability,
                    "attempt_id": str(control_attempt),
                    "kind": "response",
                    "status_code": 200,
                    "reason": None,
                },
            )
        )
        assert isinstance(late.result, RoutingRejectedResult)

        assert proxy.errors == []
    except RoutingUnavailable as error:
        raise AssertionError(
            f"routing owner failed at Native phases: {native_trace}"
        ) from error
    finally:
        if "unload_finish" in locals():
            unload_finish.set()
        try:
            if borrowed is not None:
                try:
                    if borrowed_transaction is not None:
                        with contextlib.suppress(Exception):
                            await borrowed_transaction.rollback()
                finally:
                    await borrowed.close()
            if execution_process is not None:
                await _stop_process(execution_process)
            if proxy is not None:
                await proxy.close()
            if owner_server is not None:
                await owner_server.close()
            if owner_runtime is not None:
                await owner_runtime.close()
            await _stop_uvicorn(backend_server, backend_task, backend_listener)
        finally:
            try:
                await _cleanup_world(owner, world, receipt_ids)
                await owner.execute("DELETE FROM public.users WHERE id=$1", user_id)
            finally:
                await owner.close()
