"""Real CPU HTTP boundary and durable routing on a disposable module database."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import os
import sys
import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference.backend_endpoint import BackendEndpointManager
from aap_inference.backend_endpoint_postgres import BackendEndpointPostgresData
from aap_inference.model_execution import BackendTransportModelExecutionProvider
from aap_inference.owner_transport import RoutingModelRequestContextFactory
from aap_inference.routing_runtime import RoutingRuntime
from aap_inference_contract.execution import (
    FrozenModelOperation,
    ModelExecutionError,
    ModelExecutionRouteBinding,
)
from aap_inference_contract.plugin_assets import PluginAssetDescriptor
from aap_inference_contract.routing import RoutingScopeView, make_routing_command
from app.services.inference_backend_control import capture_backend_endpoint_subject
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_routing_native import _connect
from tests.test_inference_routing_owner_integration import _seed_world, _stop_process

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


@pytest.mark.parametrize("operation", ["batch_predict", "interactive_predict"])
async def test_revoked_approval_after_cpu_admission_wait_sends_no_http(
    test_engine, tmp_path, operation
):
    await assert_owned_test_database(test_engine)
    routing_dsn = os.environ["AAP_MODULE_DATA_INFERENCE_ROUTING_URL"]
    endpoint_dsn = os.environ["AAP_MODULE_DATA_INFERENCE_ENDPOINT_EXECUTION_URL"]
    counter = tmp_path / "model-posts"
    server_code = """
import http.server, pathlib, sys
counter = pathlib.Path(sys.argv[1])
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ready")
    def do_POST(self):
        with counter.open("ab") as out: out.write(b"post\\n")
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *_args): pass
server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
print(server.server_port, flush=True)
server.serve_forever()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        server_code,
        str(counter),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    runtime = None
    owner = None
    running = None
    release = asyncio.Event()
    try:
        port = int(await asyncio.wait_for(process.stdout.readline(), timeout=10))
        backend_url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(backend_url + "/health")).status_code == 200

        owner = await _connect(test_engine)
        registry_id, pool_id, member_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await _seed_world(
            owner,
            registry_id=registry_id,
            pool_id=pool_id,
            member_id=member_id,
            backend_url=backend_url,
            backend_token="test-backend-token",
        )
        manifest = {
            "plugin_id": "detector",
            "version": "1",
            "publisher": "test",
            "license": "MIT",
            "artifact": "example/detector@sha256:" + "a" * 64,
            "sdk_compatibility": "1",
            "entrypoint": ["detector"],
            "capabilities": ["inference", "detection"],
            "models": [
                {
                    "id": "detect",
                    "task": "detect",
                    "is_interactive": True,
                    "supported_prompts": ["none"],
                    "supported_inputs": ["full_image"],
                    "supported_geometric_outputs": ["bbox"],
                }
            ],
            "pools": {"models": {"cap": 1}},
            "resource": {"device": "cpu", "memory_mb": 512, "gpu_memory_mb": 0},
            "asset_manifest_sha256": "b" * 64,
            "allowed_asset_sources": [],
        }
        extra = {
            "max_concurrency": 1,
            "plugin": {
                "installation_id": str(registry_id),
                "manifest": manifest,
                "status": "active",
                "revision": 1,
                "resource": {
                    "gpu_resource_id": None,
                    "physical_gpu_uuid": None,
                    "logical_device": "cpu",
                },
            },
            "plugin_lifecycle": {
                "launch": {"container_id": "c" * 64, "image_id": "sha256:" + "d" * 64}
            },
        }
        health = {"capabilities": {"models": [{"id": "detect", "task": "detect"}]}}
        descriptor = PluginAssetDescriptor.model_validate(
            {
                "schema_version": 1,
                "plugin_id": "detector",
                "plugin_version": "1",
                "artifact": manifest["artifact"],
                "asset_manifest_sha256": manifest["asset_manifest_sha256"],
                "selections": [
                    {
                        "model_id": "detect",
                        "variants": {},
                        "asset_free": True,
                        "assets": [],
                    }
                ],
            }
        )
        decision = {
            "expected_revision": 0,
            "expected_installation_revision": 1,
            "descriptor": descriptor.model_dump(mode="json"),
            "descriptor_sha256": descriptor.sha256(),
            "approved": [{"model_id": "detect", "variants": {}}],
            "inventory": [],
            "container_id": "c" * 64,
            "image_id": "sha256:" + "d" * 64,
        }
        await owner.execute(
            "UPDATE public.ml_backend_registry SET extra_params=$2::jsonb, "
            "health_meta=$3::jsonb, last_checked_at=clock_timestamp() WHERE id=$1",
            registry_id,
            json.dumps(extra),
            json.dumps(health),
        )
        await owner.fetchval(
            "SELECT aap_inference_topology_v1.set_plugin_asset_approval($1,0,$2::text)",
            registry_id,
            json.dumps(decision),
        )
        await owner.execute(
            "UPDATE aap_inference_routing_v1.registry_state SET continuity='ready', "
            "backend_incarnation=gen_random_uuid() WHERE registry_id=$1",
            registry_id,
        )
        runtime = RoutingRuntime(routing_dsn)
        acquired = await runtime.execute(
            make_routing_command(
                "acquire",
                {
                    "pool_id": None,
                    "registry_id": str(registry_id),
                    "project_id": None,
                    "mode": "direct",
                    "required_instance_id": str(registry_id),
                    "owner": "asset-fence-smoke",
                    "operation": "predict",
                },
            )
        )
        assert isinstance(acquired.result, RoutingScopeView)
        scope = acquired.result
        request_id = uuid.uuid4()
        binding = ModelExecutionRouteBinding(scope.id, request_id, scope.capability)
        entered = asyncio.Event()

        @asynccontextmanager
        async def admission(_operation, _scope):
            entered.set()
            await release.wait()
            yield None

        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            subject = await capture_backend_endpoint_subject(
                db,
                registry_id,
                request_id=request_id,
                purpose="execution",
                binding=binding.endpoint_binding(),
            )
            request = FrozenModelOperation(
                request_id=request_id,
                operation=operation,
                task_inputs=(
                    {"id": "one", "file_path": "https://example.invalid/a.jpg"},
                ),
                context={"model_id": "detect"},
                project_id=None,
                requested_registry_id=registry_id,
                requested_pool_id=None,
                endpoint_registry_id=registry_id,
                endpoint_subject=subject,
                required_instance_id=registry_id,
                route_owner="asset-fence-smoke",
                route_operation="predict",
                route_binding=binding,
                health_meta=health,
            )
            source = BackendEndpointManager(
                BackendEndpointPostgresData(endpoint_dsn, "execution")
            )
            provider = BackendTransportModelExecutionProvider(
                source,
                options=SimpleNamespace(ml_health_timeout=5, ml_predict_timeout=10),
                request_context_factory=RoutingModelRequestContextFactory(runtime),
                dispatch_context_factory=lambda *_: admission,
            )
            running = asyncio.create_task(provider.execute(request))
            await asyncio.wait_for(entered.wait(), timeout=10)
            revoked = {**decision, "expected_revision": 1, "approved": []}
            await owner.fetchval(
                "SELECT aap_inference_topology_v1.set_plugin_asset_approval($1,1,$2::text)",
                registry_id,
                json.dumps(revoked),
            )
            release.set()
            with pytest.raises(ModelExecutionError) as denied:
                await asyncio.wait_for(running, timeout=20)
            assert denied.value.failure.code == "model_execution_unavailable"
            assert not denied.value.failure.uncertain
            assert not counter.exists()
            assert process.returncode is None
            row = await owner.fetchrow(
                "SELECT state FROM aap_inference_routing_v1.attempts WHERE id=$1",
                request_id,
            )
            assert row is not None and row["state"] == "not_sent"
            closed = await runtime.execute(
                make_routing_command(
                    "close",
                    {
                        "scope_id": str(scope.id),
                        "capability": scope.capability,
                        "outcome": "cancel",
                    },
                )
            )
            assert closed.result.closed and closed.result.state == "cancelled"
    finally:
        release.set()
        if running is not None and not running.done():
            running.cancel()
            with suppress(BaseException):
                await running
        if runtime is not None:
            await runtime.close()
        if owner is not None:
            await owner.close()
        await _stop_process(process)
