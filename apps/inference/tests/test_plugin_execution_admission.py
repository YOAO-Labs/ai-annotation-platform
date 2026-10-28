from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_inference.model_execution import BackendTransportModelExecutionProvider
from aap_inference_contract.backend_endpoint import BackendEndpointAuthority, BackendEndpointSubject
from aap_inference_contract.execution import FrozenModelOperation, ModelExecutionError, ModelExecutionRouteBinding


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["batch_predict", "interactive_predict"])
@pytest.mark.parametrize("lifecycle_only", [False, True])
async def test_inactive_plugin_is_rejected_before_dispatch(operation, lifecycle_only):
    registry_id, request_id, scope_id = uuid4(), uuid4(), uuid4()
    authority = BackendEndpointAuthority(kind="scope", scope_id=scope_id, control_id=None, attempt_id=request_id)
    subject = BackendEndpointSubject(
        schema="aap-inference-backend-endpoint/v1", request_id=request_id,
        registry_id=registry_id, purpose="execution", authority=authority, subject="v1.YQ==",
    )
    plugin = {
        "installation_id": str(uuid4()), "status": "disabled", "revision": 2,
        "resource": {"gpu_resource_id": None, "physical_gpu_uuid": None, "logical_device": "cpu"},
        "manifest": {
            "plugin_id": "detector", "version": "1", "publisher": "example",
            "license": "MIT", "artifact": "example/detector@sha256:" + "a" * 64,
            "sdk_compatibility": "1", "entrypoint": ["detector"],
            "capabilities": ["inference", "detection"],
            "resource": {"device": "cpu", "memory_mb": 1024, "gpu_memory_mb": 0},
            "asset_manifest_sha256": "b" * 64, "allowed_asset_sources": [],
        },
    }
    extra = {"plugin_lifecycle": None} if lifecycle_only else {"plugin": plugin}
    endpoint = SimpleNamespace(registry_id=registry_id, extra_params=extra, endpoint_sha256="a" * 64)

    class Source:
        async def open(self, *args, **kwargs):
            return endpoint

    provider = BackendTransportModelExecutionProvider(
        Source(), options=None,
        request_context_factory=lambda *args: None,
        dispatch_context_factory=lambda *args: None,
    )
    request = FrozenModelOperation(
        request_id=request_id, operation=operation,
        task_inputs=({"id": "one", "file_path": "https://example.invalid/a.jpg"},),
        context=None, project_id=None, requested_registry_id=registry_id,
        requested_pool_id=None, endpoint_registry_id=registry_id,
        endpoint_subject=subject, required_instance_id=None,
        route_owner="plugin-test", route_operation="predict",
        route_binding=ModelExecutionRouteBinding(scope_id=scope_id, attempt_id=request_id, capability="a" * 64),
    )
    dispatched = False

    async def before_dispatch(_request):
        nonlocal dispatched
        dispatched = True

    with pytest.raises(ModelExecutionError) as error:
        await provider.execute(request, before_dispatch=before_dispatch)
    assert error.value.failure.code == "model_execution_unavailable"
    assert not dispatched
