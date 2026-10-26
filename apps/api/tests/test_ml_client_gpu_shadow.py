from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
import gc
import json
from typing import Any
import uuid
import weakref

import pytest
from aap_protocol_v2.lifecycle import AdmissionScope
from structlog.testing import capture_logs

from aap_inference.backend_endpoint import (
    BackendEndpointManager,
    BackendEndpointSnapshot,
)
from aap_inference.backend_transport import get_semaphore
from aap_inference.gpu.backend_dispatch import BackendGPUDispatchContextFactory
from aap_inference.gpu.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
    GPUDispatchGrant,
    GPUDispatchRequest,
)
from aap_inference.gpu.native import GPUAuthorityData, GPUAuthorityDataProvider
from aap_inference.gpu.settings import (
    GPUArbiterMode,
    GPUAuthoritySettings,
    parse_gpu_resources,
)
from aap_inference_contract.backend_control import BackendControlRequest
from aap_inference_contract.backend_endpoint import (
    BackendEndpointBinding,
    BackendEndpointPurpose,
)
from aap_inference_contract.execution import (
    FrozenModelOperation,
    ModelExecutionRouteBinding,
)
from aap_inference_contract.gpu import GPUAuthorityAction, GPUAuthorityUnavailable


RESOURCE_ID = "node-a/index:0"
CARD_B_ID = "node-a/index:1"
BACKEND_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
BACKEND_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


class _EndpointData:
    """Return one sealed endpoint snapshot through the owner endpoint API."""

    def __init__(
        self,
        *,
        resource_id: str | None = RESOURCE_ID,
        vram_budget_mb: int | None = 8_000,
    ) -> None:
        self.resource_id = resource_id
        self.vram_budget_mb = vram_budget_mb

    async def capture_subject(
        self,
        registry_id: uuid.UUID,
        request_id: uuid.UUID,
        purpose: BackendEndpointPurpose,
        binding: BackendEndpointBinding | None,
    ) -> dict[str, str]:
        return {"status": "ok", "subject": "v1.YQ=="}

    async def open_subject(
        self,
        subject: str,
        registry_id: uuid.UUID,
        request_id: uuid.UUID,
        purpose: BackendEndpointPurpose,
        authority: object,
    ) -> str:
        return json.dumps(
            {
                "schema": "aap-inference-backend-endpoint-snapshot/v1",
                "request_id": str(request_id),
                "registry_id": str(registry_id),
                "purpose": purpose,
                "authority": (
                    None if authority is None else authority.model_dump(mode="json")
                ),
                "endpoint_sha256": "a" * 64,
                "url": "http://gpu-backend:9090",
                "auth_method": "none",
                "auth_token": None,
                "gpu_resource_id": self.resource_id,
                "vram_budget_mb": self.vram_budget_mb,
                "eviction_priority": 0,
                "extra_params": {"max_concurrency": 4},
            }
        )

    async def readiness(self) -> bool:
        return True


async def _opened_model_endpoint(
    request: FrozenModelOperation,
    *,
    resource_id: str | None = RESOURCE_ID,
    vram_budget_mb: int | None = 8_000,
) -> BackendEndpointSnapshot:
    assert request.endpoint_registry_id is not None
    assert request.route_binding is not None
    manager = BackendEndpointManager(
        _EndpointData(resource_id=resource_id, vram_budget_mb=vram_budget_mb)
    )
    subject = await manager.capture(
        request.endpoint_registry_id,
        request_id=request.request_id,
        purpose="execution",
        binding=request.route_binding.endpoint_binding(),
    )
    return await manager.open(
        subject,
        request_id=request.request_id,
        purpose="execution",
        authority=subject.authority,
    )


async def _opened_control_request(
    operation: str = "reload",
    *,
    registry_id: uuid.UUID = BACKEND_A,
    resource_id: str | None = RESOURCE_ID,
    vram_budget_mb: int | None = 8_000,
) -> tuple[BackendControlRequest, BackendEndpointSnapshot]:
    request_id = uuid.uuid4()
    if operation in {"unload", "reload"}:
        binding = BackendEndpointBinding(
            kind="control",
            scope_id=None,
            control_id=uuid.uuid4(),
            attempt_id=request_id,
            capability="0" * 64,
        )
        purpose: BackendEndpointPurpose = "lifecycle"
    else:
        binding = BackendEndpointBinding(
            kind="scope",
            scope_id=uuid.uuid4(),
            control_id=None,
            attempt_id=request_id,
            capability="0" * 64,
        )
        purpose = "execution"
    manager = BackendEndpointManager(
        _EndpointData(resource_id=resource_id, vram_budget_mb=vram_budget_mb)
    )
    subject = await manager.capture(
        registry_id,
        request_id=request_id,
        purpose=purpose,
        binding=binding,
    )
    request = BackendControlRequest(
        kind="registered",
        schema="aap-inference-backend-control/v1",
        operation=operation,
        request_id=request_id,
        registry_id=registry_id,
        endpoint_subject=subject,
        binding=binding,
        body={} if operation == "warmup" else None,
    )
    endpoint = await manager.open(
        subject,
        request_id=request_id,
        purpose=purpose,
        authority=subject.authority,
    )
    return request, endpoint


def _model_operation(
    operation: str = "batch_predict",
    *,
    registry_id: uuid.UUID = BACKEND_A,
    request_id: uuid.UUID | None = None,
) -> FrozenModelOperation:
    request_id = request_id or uuid.uuid4()
    task_input = {"id": "task-1", "file_path": "https://example.invalid/image.jpg"}
    return FrozenModelOperation(
        request_id=request_id,
        operation=operation,
        task_inputs=(task_input,),
        context=None,
        project_id=None,
        requested_registry_id=registry_id,
        requested_pool_id=None,
        endpoint_registry_id=registry_id,
        required_instance_id=None,
        route_owner="gpu-dispatch-policy-tests",
        route_operation="predict",
        route_binding=ModelExecutionRouteBinding(
            scope_id=uuid.uuid4(),
            attempt_id=request_id,
            capability="a" * 64,
        ),
    )


def _settings(
    *,
    mode: str = "observe",
    resources: tuple[tuple[str, int, str | None], ...] = (
        (RESOURCE_ID, 20_000, "observe"),
    ),
    rollout_enabled: bool = False,
) -> GPUAuthoritySettings:
    rows: dict[str, dict[str, Any]] = {}
    for resource_id, allocatable_mb, resource_mode in resources:
        node_id, physical_device_token = resource_id.split("/", 1)
        row: dict[str, Any] = {
            "node_id": node_id,
            "physical_device_token": physical_device_token,
            "allocatable_mb": allocatable_mb,
        }
        if resource_mode is not None:
            row["mode"] = resource_mode
        rows[resource_id] = row
    resource_map, errors = parse_gpu_resources(json.dumps(rows))
    return GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode(mode),
        gpu_arbiter_resources=resource_map,
        gpu_arbiter_config_errors=errors,
        gpu_arbiter_rollout_enabled=rollout_enabled,
    )


def _backend_fact(
    backend_id: uuid.UUID,
    *,
    resource_id: str | None = RESOURCE_ID,
    budget_mb: int | None = 8_000,
    state: str = "connected",
    checked_at: datetime = NOW,
    health_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "backend_id": str(backend_id),
        "gpu_resource_id": resource_id,
        "vram_budget_mb": budget_mb,
        "eviction_priority": 0,
        "extra_params": {"max_concurrency": 4},
        "state": state,
        "last_checked_at": checked_at.isoformat(),
        "health_meta": dict(health_meta or {}),
    }


def _rollout(
    resource_id: str = RESOURCE_ID,
    *,
    state: str = "enforcing",
    effective_mode: str = "enforce",
    target_mode: str = "enforce",
    blocker_reason: str | None = None,
) -> dict[str, Any]:
    transition_id = None if state in {"off", "enforcing"} else str(uuid.uuid4())
    return {
        "resource_id": resource_id,
        "state": state,
        "effective_mode": effective_mode,
        "target_mode": target_mode,
        "transition_id": transition_id,
        "last_transition_id": None,
        "blocker_reason": blocker_reason,
        "revision": 2,
    }


class _NativeData:
    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        rollouts: Mapping[str, dict[str, Any] | None] | None = None,
        boundary_active: bool = False,
    ) -> None:
        self.rows = rows or []
        self.rollouts = dict(rollouts or {})
        self.boundary_active = boundary_active
        self.calls: list[tuple[GPUAuthorityAction, dict[str, Any]]] = []
        self.fail_actions: set[GPUAuthorityAction] = set()
        self.block_action: GPUAuthorityAction | None = None
        self.block_started = asyncio.Event()
        self.cancelled = False

    async def step(
        self,
        action: GPUAuthorityAction,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        args = dict(arguments)
        self.calls.append((action, args))
        if action in self.fail_actions:
            raise GPUAuthorityUnavailable()
        if action == self.block_action:
            self.block_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if action == "shadow_snapshot":
            backend_id = args["backend_id"]
            resource_id = args["resource_id"]
            selected = self.rows
            if backend_id is not None:
                selected = [row for row in selected if row["backend_id"] == backend_id]
            elif resource_id is not None:
                selected = [
                    row for row in selected if row["gpu_resource_id"] == resource_id
                ]
            return {"backends": sorted(selected, key=lambda row: row["backend_id"])}
        if action == "rollout_read":
            return {"snapshot": self.rollouts.get(args["resource_id"])}
        if action == "rollout_boundary":
            return {"active": self.boundary_active}
        raise AssertionError(f"unexpected GPU Native action: {action}")


class _Provider:
    def __init__(self, data: _NativeData) -> None:
        self.data = data
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[GPUAuthorityData]:
        self.transactions += 1
        yield self.data


class _Clock:
    def time(self) -> float:
        return NOW.timestamp()


def _factory(
    provider: GPUAuthorityDataProvider,
    *,
    config: GPUAuthoritySettings,
    authority_dispatch_factory=None,
) -> BackendGPUDispatchContextFactory:
    return BackendGPUDispatchContextFactory(
        provider,
        config=config,
        authority_dispatch_factory=authority_dispatch_factory,
        clock=_Clock(),
    )


def _authority(requests: list[GPUDispatchRequest], grants: list[GPUDispatchGrant]):
    @asynccontextmanager
    async def dispatch(request: GPUDispatchRequest):
        requests.append(request)
        grant = GPUDispatchGrant(generation="7", admission_token="signed-token")
        grants.append(grant)
        yield grant

    return dispatch


@pytest.mark.asyncio
async def test_observe_records_structured_unknown_claim_and_fails_open() -> None:
    requester = _backend_fact(BACKEND_A, resource_id=None, budget_mb=None)
    data = _NativeData([requester])
    provider = _Provider(data)
    request = _model_operation()
    endpoint = await _opened_model_endpoint(
        request, resource_id=None, vram_budget_mb=None
    )
    dispatch = _factory(provider, config=_settings(), authority_dispatch_factory=None)
    http_calls: list[str] = []

    with capture_logs() as logs:
        async with dispatch(request, endpoint)(
            "predict", AdmissionScope.PREDICT
        ) as grant:
            assert grant is None
            http_calls.append("predict")

    assert http_calls == ["predict"]
    assert data.calls[0][0] == "shadow_snapshot"
    record = next(
        item for item in logs if item["event"] == "gpu_arbiter_shadow_decision"
    )
    decision = record["gpu_arbiter"]
    assert decision["decision"] == "would-reject"
    assert decision["reason"] == "gpu_claim_missing_or_unverified"
    assert decision["authoritative"] is False
    assert decision["business_request_blocked"] is False


@pytest.mark.asyncio
async def test_observer_query_failure_is_fail_open() -> None:
    data = _NativeData([_backend_fact(BACKEND_A)])
    data.fail_actions.add("shadow_snapshot")
    provider = _Provider(data)
    request = _model_operation()
    endpoint = await _opened_model_endpoint(request)
    dispatch = _factory(provider, config=_settings())
    http_calls: list[str] = []

    async with dispatch(request, endpoint)("predict", AdmissionScope.PREDICT) as grant:
        assert grant is None
        http_calls.append("predict")

    assert http_calls == ["predict"]
    assert provider.transactions == 1


@pytest.mark.asyncio
async def test_observer_timeout_is_250ms_fail_open() -> None:
    data = _NativeData([_backend_fact(BACKEND_A)])
    data.block_action = "shadow_snapshot"
    provider = _Provider(data)
    request = _model_operation()
    endpoint = await _opened_model_endpoint(request)
    dispatch = _factory(provider, config=_settings())
    http_calls: list[str] = []
    started = asyncio.get_running_loop().time()

    async with dispatch(request, endpoint)("predict", AdmissionScope.PREDICT) as grant:
        assert grant is None
        http_calls.append("predict")
    elapsed = asyncio.get_running_loop().time() - started

    assert data.block_started.is_set()
    assert data.cancelled is True
    assert 0.20 <= elapsed < 1.0
    assert http_calls == ["predict"]


@pytest.mark.asyncio
async def test_off_mode_skips_native_shadow_and_authority() -> None:
    data = _NativeData()
    provider = _Provider(data)
    request = _model_operation()
    endpoint = await _opened_model_endpoint(request)
    dispatch = _factory(
        provider,
        config=_settings(mode="off"),
        authority_dispatch_factory=None,
    )

    async with dispatch(request, endpoint)("predict", AdmissionScope.PREDICT) as grant:
        assert grant is None

    assert provider.transactions == 0
    assert data.calls == []


@pytest.mark.asyncio
async def test_disabled_rollout_does_not_promote_desired_enforce() -> None:
    data = _NativeData([_backend_fact(BACKEND_A)])
    provider = _Provider(data)
    request = _model_operation()
    endpoint = await _opened_model_endpoint(request)
    dispatch = _factory(
        provider,
        config=_settings(
            mode="enforce",
            resources=((RESOURCE_ID, 20_000, "enforce"),),
            rollout_enabled=False,
        ),
    )

    async with dispatch(request, endpoint)("predict", AdmissionScope.PREDICT) as grant:
        assert grant is None

    assert provider.transactions == 0
    assert data.calls == []


@pytest.mark.asyncio
async def test_fresh_explicit_cpu_is_exempt_but_stale_cpu_is_rechecked() -> None:
    cpu_health = {"compute": {"configured_device": "cpu", "effective_device": "cpu"}}
    cpu_row = _backend_fact(
        BACKEND_A,
        resource_id=None,
        budget_mb=None,
        health_meta=cpu_health,
    )
    data = _NativeData([cpu_row], boundary_active=True)
    provider = _Provider(data)
    config = _settings(
        mode="enforce",
        resources=((RESOURCE_ID, 20_000, "enforce"),),
        rollout_enabled=True,
    )
    dispatch = _factory(provider, config=config)

    fresh_request = _model_operation()
    fresh_endpoint = await _opened_model_endpoint(
        fresh_request,
        resource_id=None,
        vram_budget_mb=None,
    )
    async with dispatch(fresh_request, fresh_endpoint)(
        "predict", AdmissionScope.PREDICT
    ) as grant:
        assert grant is None
    assert [action for action, _ in data.calls] == ["shadow_snapshot"]

    cpu_row["last_checked_at"] = (NOW - timedelta(minutes=4)).isoformat()
    stale_request = _model_operation()
    stale_endpoint = await _opened_model_endpoint(
        stale_request,
        resource_id=None,
        vram_budget_mb=None,
    )
    with pytest.raises(GPUArbiterDispatchError) as exc_info:
        async with dispatch(stale_request, stale_endpoint)(
            "predict", AdmissionScope.PREDICT
        ):
            pass

    assert exc_info.value.error_code == GPUArbiterErrorCode.CONFIG_INVALID.value
    assert [action for action, _ in data.calls] == [
        "shadow_snapshot",
        "shadow_snapshot",
        "rollout_boundary",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource_id", "budget_mb"),
    [
        (None, None),
        ("node-z/index:9", 8_000),
    ],
)
async def test_unclaimed_and_unknown_claims_fail_closed_at_active_rollout_boundary(
    resource_id: str | None,
    budget_mb: int | None,
) -> None:
    requester = _backend_fact(
        BACKEND_A,
        resource_id=resource_id,
        budget_mb=budget_mb,
    )
    data = _NativeData([requester], boundary_active=True)
    provider = _Provider(data)
    request = _model_operation()
    endpoint = await _opened_model_endpoint(
        request,
        resource_id=resource_id,
        vram_budget_mb=budget_mb,
    )
    authority_requests: list[GPUDispatchRequest] = []
    grants: list[GPUDispatchGrant] = []
    dispatch_with_authority = _factory(
        provider,
        config=_settings(
            mode="enforce",
            resources=((RESOURCE_ID, 20_000, "enforce"),),
            rollout_enabled=True,
        ),
        authority_dispatch_factory=_authority(authority_requests, grants),
    )

    with pytest.raises(GPUArbiterDispatchError) as exc_info:
        async with dispatch_with_authority(request, endpoint)(
            "predict", AdmissionScope.PREDICT
        ):
            pass

    assert exc_info.value.error_code == GPUArbiterErrorCode.CONFIG_INVALID.value
    assert authority_requests == []
    assert grants == []
    assert "rollout_boundary" in [action for action, _ in data.calls]


@pytest.mark.asyncio
async def test_enforcing_rollout_uses_model_request_id_and_same_grant_channel() -> None:
    data = _NativeData(
        [_backend_fact(BACKEND_A)],
        rollouts={RESOURCE_ID: _rollout()},
    )
    provider = _Provider(data)
    request = _model_operation()
    endpoint = await _opened_model_endpoint(request)
    authority_requests: list[GPUDispatchRequest] = []
    grants: list[GPUDispatchGrant] = []
    dispatch = _factory(
        provider,
        config=_settings(
            mode="enforce",
            resources=((RESOURCE_ID, 20_000, "enforce"),),
            rollout_enabled=True,
        ),
        authority_dispatch_factory=_authority(authority_requests, grants),
    )

    async with dispatch(request, endpoint)("predict", AdmissionScope.PREDICT) as grant:
        assert grant is grants[0]
        grant.request_started()
        assert grant.report_response(200)

    assert len(authority_requests) == 1
    assert authority_requests[0].request_id == request.request_id
    assert authority_requests[0].backend_id == str(BACKEND_A)
    assert authority_requests[0].gpu_resource_id == RESOURCE_ID
    assert authority_requests[0].operation == "predict"
    assert authority_requests[0].scope is AdmissionScope.PREDICT
    assert grants[0].outcome is not None
    assert grants[0].outcome.kind == "response_received"
    assert grants[0].outcome.status_code == 200


@pytest.mark.asyncio
async def test_backend_control_request_opens_and_dispatches_its_exact_request_identity() -> (
    None
):
    request, endpoint = await _opened_control_request("reload")
    data = _NativeData(
        [_backend_fact(request.registry_id)],
        rollouts={RESOURCE_ID: _rollout()},
    )
    provider = _Provider(data)
    authority_requests: list[GPUDispatchRequest] = []
    grants: list[GPUDispatchGrant] = []
    dispatch = _factory(
        provider,
        config=_settings(
            mode="enforce",
            resources=((RESOURCE_ID, 20_000, "enforce"),),
            rollout_enabled=True,
        ),
        authority_dispatch_factory=_authority(authority_requests, grants),
    )

    async with dispatch(request, endpoint)("reload", AdmissionScope.RELOAD) as grant:
        assert grant is grants[0]
        grant.report_response(200)

    assert authority_requests[0].request_id == request.request_id
    assert authority_requests[0].backend_id == str(request.registry_id)
    assert authority_requests[0].operation == "reload"
    assert authority_requests[0].scope is AdmissionScope.RELOAD
    assert grants[0].outcome is not None
    assert grants[0].outcome.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rollout", "error_code", "message"),
    [
        (None, GPUArbiterErrorCode.NOT_READY, "gpu_rollout_not_initialized"),
        (
            _rollout(state="promoting", effective_mode="off", target_mode="enforce"),
            GPUArbiterErrorCode.NOT_READY,
            "gpu_rollout_promoting",
        ),
    ],
)
async def test_missing_or_transitioning_rollout_blocks_before_authority(
    rollout: dict[str, Any] | None,
    error_code: GPUArbiterErrorCode,
    message: str,
) -> None:
    data = _NativeData(
        [_backend_fact(BACKEND_A)],
        rollouts={RESOURCE_ID: rollout},
    )
    provider = _Provider(data)
    request = _model_operation()
    endpoint = await _opened_model_endpoint(request)
    authority_requests: list[GPUDispatchRequest] = []
    grants: list[GPUDispatchGrant] = []
    dispatch = _factory(
        provider,
        config=_settings(
            mode="enforce",
            resources=((RESOURCE_ID, 20_000, "enforce"),),
            rollout_enabled=True,
        ),
        authority_dispatch_factory=_authority(authority_requests, grants),
    )

    with pytest.raises(GPUArbiterDispatchError) as exc_info:
        async with dispatch(request, endpoint)("predict", AdmissionScope.PREDICT):
            pass

    assert exc_info.value.error_code == error_code.value
    assert exc_info.value.detail["message"] == message
    assert authority_requests == []


@pytest.mark.asyncio
async def test_rollout_unavailability_blocks_before_authority() -> None:
    data = _NativeData([_backend_fact(BACKEND_A)])
    data.fail_actions.add("rollout_read")
    provider = _Provider(data)
    request = _model_operation()
    endpoint = await _opened_model_endpoint(request)
    authority_requests: list[GPUDispatchRequest] = []
    grants: list[GPUDispatchGrant] = []
    dispatch = _factory(
        provider,
        config=_settings(
            mode="enforce",
            resources=((RESOURCE_ID, 20_000, "enforce"),),
            rollout_enabled=True,
        ),
        authority_dispatch_factory=_authority(authority_requests, grants),
    )

    with pytest.raises(GPUArbiterDispatchError) as exc_info:
        async with dispatch(request, endpoint)("predict", AdmissionScope.PREDICT):
            pass

    assert exc_info.value.error_code == GPUArbiterErrorCode.UNAVAILABLE.value
    assert authority_requests == []
    assert grants == []


@pytest.mark.asyncio
async def test_two_enforced_cards_share_authority_without_cross_card_identity() -> None:
    data = _NativeData(
        [
            _backend_fact(BACKEND_A, resource_id=RESOURCE_ID),
            _backend_fact(BACKEND_B, resource_id=CARD_B_ID),
        ],
        rollouts={
            RESOURCE_ID: _rollout(RESOURCE_ID),
            CARD_B_ID: _rollout(CARD_B_ID),
        },
    )
    provider = _Provider(data)
    authority_requests: list[GPUDispatchRequest] = []
    grants: list[GPUDispatchGrant] = []
    dispatch = _factory(
        provider,
        config=_settings(
            mode="enforce",
            resources=(
                (RESOURCE_ID, 20_000, "enforce"),
                (CARD_B_ID, 20_000, "enforce"),
            ),
            rollout_enabled=True,
        ),
        authority_dispatch_factory=_authority(authority_requests, grants),
    )

    expected_request_ids: list[uuid.UUID] = []
    for backend_id, resource_id in ((BACKEND_A, RESOURCE_ID), (BACKEND_B, CARD_B_ID)):
        request = _model_operation(registry_id=backend_id)
        expected_request_ids.append(request.request_id)
        endpoint = await _opened_model_endpoint(request, resource_id=resource_id)
        async with dispatch(request, endpoint)(
            "predict", AdmissionScope.PREDICT
        ) as grant:
            grant.report_response(200)

    assert [item.backend_id for item in authority_requests] == [
        str(BACKEND_A),
        str(BACKEND_B),
    ]
    assert [item.gpu_resource_id for item in authority_requests] == [
        RESOURCE_ID,
        CARD_B_ID,
    ]
    assert [item.request_id for item in authority_requests] == expected_request_ids
    assert all(grant.outcome is not None for grant in grants)


@pytest.mark.asyncio
async def test_unreported_grant_outcome_becomes_uncertain_and_caller_error_propagates() -> (
    None
):
    data = _NativeData(
        [_backend_fact(BACKEND_A)],
        rollouts={RESOURCE_ID: _rollout()},
    )
    provider = _Provider(data)
    config = _settings(
        mode="enforce",
        resources=((RESOURCE_ID, 20_000, "enforce"),),
        rollout_enabled=True,
    )
    grants: list[GPUDispatchGrant] = []

    @asynccontextmanager
    async def authority(_request: GPUDispatchRequest):
        grant = GPUDispatchGrant(generation="7", admission_token="signed-token")
        grants.append(grant)
        yield grant

    dispatch = _factory(provider, config=config, authority_dispatch_factory=authority)
    request = _model_operation()
    endpoint = await _opened_model_endpoint(request)
    async with dispatch(request, endpoint)("predict", AdmissionScope.PREDICT):
        pass
    assert grants[0].outcome is not None
    assert grants[0].outcome.kind == "uncertain"
    assert grants[0].outcome.reason == "response_not_reported"

    request = _model_operation()
    endpoint = await _opened_model_endpoint(request)
    with pytest.raises(RuntimeError, match="caller failed"):
        async with dispatch(request, endpoint)("predict", AdmissionScope.PREDICT):
            raise RuntimeError("caller failed")
    assert grants[1].outcome is not None
    assert grants[1].outcome.kind == "uncertain"
    assert grants[1].outcome.reason == "request_aborted"


@pytest.mark.asyncio
async def test_reported_backend_rejection_survives_context_exception() -> None:
    data = _NativeData(
        [_backend_fact(BACKEND_A)],
        rollouts={RESOURCE_ID: _rollout()},
    )
    provider = _Provider(data)
    grant = GPUDispatchGrant(generation="7", admission_token="signed-token")

    @asynccontextmanager
    async def authority(_request: GPUDispatchRequest):
        yield grant

    dispatch = _factory(
        provider,
        config=_settings(
            mode="enforce",
            resources=((RESOURCE_ID, 20_000, "enforce"),),
            rollout_enabled=True,
        ),
        authority_dispatch_factory=authority,
    )
    request = _model_operation()
    endpoint = await _opened_model_endpoint(request)

    with pytest.raises(RuntimeError, match="backend returned 503"):
        async with dispatch(request, endpoint)(
            "predict", AdmissionScope.PREDICT
        ) as active:
            active.request_started()
            active.report_response(503)
            raise RuntimeError("backend returned 503")

    assert grant.outcome is not None
    assert grant.outcome.kind == "response_received"
    assert grant.outcome.status_code == 503


@pytest.mark.asyncio
async def test_enforced_rollout_requires_authority_and_rejects_invalid_grants() -> None:
    data = _NativeData(
        [_backend_fact(BACKEND_A)],
        rollouts={RESOURCE_ID: _rollout()},
    )
    provider = _Provider(data)
    request = _model_operation()
    endpoint = await _opened_model_endpoint(request)
    config = _settings(
        mode="enforce",
        resources=((RESOURCE_ID, 20_000, "enforce"),),
        rollout_enabled=True,
    )

    with pytest.raises(GPUArbiterDispatchError) as missing:
        async with _factory(provider, config=config, authority_dispatch_factory=None)(
            request, endpoint
        )("predict", AdmissionScope.PREDICT):
            pass
    assert missing.value.error_code == GPUArbiterErrorCode.NOT_READY.value

    @asynccontextmanager
    async def invalid_authority(_request: GPUDispatchRequest):
        yield object()

    with pytest.raises(GPUArbiterDispatchError) as invalid:
        async with _factory(
            provider,
            config=config,
            authority_dispatch_factory=invalid_authority,
        )(request, endpoint)("predict", AdmissionScope.PREDICT):
            pass
    assert invalid.value.error_code == GPUArbiterErrorCode.UNAVAILABLE.value


@pytest.mark.asyncio
async def test_context_rejects_mismatched_request_and_operation_before_native_io() -> (
    None
):
    data = _NativeData()
    provider = _Provider(data)
    request = _model_operation()
    other_request = _model_operation(registry_id=BACKEND_B)
    mismatched_endpoint = await _opened_model_endpoint(other_request)
    dispatch = _factory(provider, config=_settings())

    with pytest.raises(GPUArbiterDispatchError) as identity_error:
        dispatch(request, mismatched_endpoint)
    assert identity_error.value.error_code == GPUArbiterErrorCode.NOT_READY.value

    with pytest.raises(GPUArbiterDispatchError) as operation_error:
        async with dispatch(other_request, mismatched_endpoint)(
            "warmup", AdmissionScope.WARMUP
        ):
            pass
    assert operation_error.value.error_code == GPUArbiterErrorCode.NOT_READY.value
    assert data.calls == []


def test_contended_semaphore_cache_does_not_retain_closed_event_loop() -> None:
    loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop] | None = None

    async def exercise() -> None:
        nonlocal loop_ref
        loop = asyncio.get_running_loop()
        loop_ref = weakref.ref(loop)
        semaphore = get_semaphore("gc-regression", 1)
        assert semaphore is not None
        await semaphore.acquire()
        waiter = asyncio.create_task(semaphore.acquire())
        await asyncio.sleep(0)
        assert getattr(semaphore, "_loop", None) is loop
        waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter
        semaphore.release()

    asyncio.run(exercise())
    gc.collect()

    assert loop_ref is not None
    assert loop_ref() is None
