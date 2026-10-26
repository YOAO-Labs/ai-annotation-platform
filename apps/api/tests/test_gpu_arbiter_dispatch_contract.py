from __future__ import annotations

import json

import pytest

from aap_inference_contract.gpu import (
    GPU_CALLER_DATA_PROTOCOL,
    GPU_CALLER_NATIVE_ACTIONS,
    GPU_CALLER_NATIVE_FUNCTIONS,
    GPU_CALLER_NATIVE_SCHEMA,
    GPUAuthorityUnavailable,
)
from aap_inference.gpu.native import GPUCallerPostgresData
from aap_owner_data import postgres as postgres_module

from aap_inference.gpu.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
    GPUDispatchGrant,
    GPUDispatchOutcomeChannel,
    gpu_arbiter_failure_record,
    summarize_gpu_arbiter_failures,
)
from aap_inference.gpu.diagnostics import unregistered_gpu_loading_blocked
from aap_inference.gpu.settings import (
    GPUArbiterMode,
    GPUAuthoritySettings,
    GPUResourceSettings,
)


@pytest.mark.parametrize(
    ("code", "status_code", "retry_after_s"),
    [
        (GPUArbiterErrorCode.NOT_READY, 503, None),
        (GPUArbiterErrorCode.CAPACITY_UNAVAILABLE, 503, 4),
        (GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED, 503, 4),
        (GPUArbiterErrorCode.DRAIN_TIMEOUT, 503, 4),
        (GPUArbiterErrorCode.UNAVAILABLE, 503, None),
        (GPUArbiterErrorCode.CONFIG_INVALID, 503, None),
        (GPUArbiterErrorCode.BACKEND_RETIREMENT_REQUIRED, 409, None),
    ],
)
def test_dispatch_error_contract(
    code: GPUArbiterErrorCode,
    status_code: int,
    retry_after_s: int | None,
) -> None:
    error = GPUArbiterDispatchError(
        code,
        message="stable message",
        retry_after_s=retry_after_s,
    )

    assert error.error_code == code.value
    assert error.status_code == status_code
    assert error.detail == {
        "error_code": code.value,
        "message": "stable message",
    }
    assert (error.headers or {}).get("Retry-After") == (
        str(retry_after_s) if retry_after_s is not None else None
    )
    assert gpu_arbiter_failure_record(error) == {
        "error_code": code.value,
        "status_code": status_code,
        "retry_after_s": retry_after_s,
        "message": "stable message",
    }


def test_dispatch_error_worker_record_uses_code_as_default_message() -> None:
    error = GPUArbiterDispatchError(GPUArbiterErrorCode.NOT_READY)

    assert gpu_arbiter_failure_record(error) == {
        "error_code": "gpu_arbiter_not_ready",
        "status_code": 503,
        "retry_after_s": None,
        "message": "gpu_arbiter_not_ready",
    }
    assert gpu_arbiter_failure_record(RuntimeError("ordinary")) is None


def test_dispatch_error_worker_summary_is_bounded_and_deterministic() -> None:
    assert summarize_gpu_arbiter_failures(
        [
            {
                "error_code": "gpu_capacity_unavailable",
                "status_code": 503,
                "retry_after_s": 2,
                "message": "first",
            },
            {
                "error_code": "gpu_arbiter_not_ready",
                "status_code": 503,
                "retry_after_s": None,
                "message": "not ready",
            },
            {
                "error_code": "gpu_capacity_unavailable",
                "status_code": 503,
                "retry_after_s": 5,
                "message": "second",
                "count": 2,
            },
        ]
    ) == [
        {
            "error_code": "gpu_arbiter_not_ready",
            "status_code": 503,
            "retry_after_s": None,
            "count": 1,
        },
        {
            "error_code": "gpu_capacity_unavailable",
            "status_code": 503,
            "retry_after_s": 5,
            "count": 3,
        },
    ]


@pytest.mark.parametrize(
    "code",
    [
        GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED,
        GPUArbiterErrorCode.DRAIN_TIMEOUT,
    ],
)
def test_retryable_dispatch_errors_require_retry_after(
    code: GPUArbiterErrorCode,
) -> None:
    with pytest.raises(ValueError, match="requires retry_after_s"):
        GPUArbiterDispatchError(code)


@pytest.mark.parametrize("retry_after_s", [-1, True, 1.5])
def test_dispatch_error_rejects_invalid_retry_after(retry_after_s: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        GPUArbiterDispatchError(
            GPUArbiterErrorCode.CAPACITY_UNAVAILABLE,
            retry_after_s=retry_after_s,  # type: ignore[arg-type]
        )


def test_dispatch_grant_requires_canonical_generation_and_token() -> None:
    first = GPUDispatchGrant("1", "signed-token")
    second = GPUDispatchGrant("2", "another-token")

    assert first.generation == "1"
    assert first.outcome_channel is not second.outcome_channel

    with pytest.raises(ValueError, match="canonical positive int64"):
        GPUDispatchGrant("01", "signed-token")
    with pytest.raises(ValueError, match="non-empty and canonical"):
        GPUDispatchGrant("1", " token ")


def test_dispatch_outcome_channel_reports_one_response_exactly_once() -> None:
    channel = GPUDispatchOutcomeChannel()

    assert channel.report_response(503) is True

    assert channel.outcome is not None
    assert channel.outcome.kind == "response_received"
    assert channel.outcome.status_code == 503
    assert channel.outcome.reason is None
    assert channel.report_response(200) is False
    assert channel.outcome.status_code == 503


def test_dispatch_outcome_channel_defaults_missing_result_to_uncertain() -> None:
    channel = GPUDispatchOutcomeChannel()

    assert channel.report_uncertain_if_missing("request_aborted") is True
    assert channel.report_uncertain_if_missing("response_not_reported") is False

    assert channel.outcome is not None
    assert channel.outcome.kind == "uncertain"
    assert channel.outcome.status_code is None
    assert channel.outcome.reason == "request_aborted"


def test_proven_not_sent_is_one_shot_and_cannot_replace_an_observed_outcome():
    grant = GPUDispatchGrant("1", "signed-token")
    assert grant.report_not_sent() is True
    assert grant.outcome.kind == "not_sent"
    assert grant.report_uncertain_if_missing("request_aborted") is False
    assert grant.report_response(200) is False
    for report in (
        lambda channel: channel.report_response(503),
        lambda channel: channel.report_uncertain_if_missing("request_aborted"),
    ):
        channel = GPUDispatchOutcomeChannel()
        report(channel)
        assert channel.report_not_sent() is False


def test_dispatch_start_fences_late_no_send_and_grant_reuse():
    grant = GPUDispatchGrant("1", "signed-token")
    grant.request_started()
    assert grant.report_not_sent() is False
    with pytest.raises(ValueError, match="already used"):
        grant.request_started()
    assert grant.report_uncertain_if_missing("request_aborted")
    not_sent = GPUDispatchGrant("1", "other-token")
    assert not_sent.report_not_sent()
    with pytest.raises(ValueError, match="already used"):
        not_sent.request_started()


@pytest.mark.parametrize("status_code", [True, 99, 600])
def test_dispatch_outcome_channel_rejects_invalid_http_status(
    status_code: object,
) -> None:
    channel = GPUDispatchOutcomeChannel()

    with pytest.raises(ValueError, match="HTTP status"):
        channel.report_response(status_code)  # type: ignore[arg-type]


def test_unregistered_loading_gate_uses_effective_not_desired_mode() -> None:
    config = GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.ENFORCE,
        gpu_arbiter_resources={
            "node-a/index:0": GPUResourceSettings(
                resource_id="node-a/index:0",
                node_id="node-a",
                physical_device_token="index:0",
                allocatable_mb=20_000,
                mode=GPUArbiterMode.ENFORCE,
            )
        },
    )

    assert unregistered_gpu_loading_blocked(config=config) is False


def test_unregistered_loading_gate_stays_closed_during_demotion(
    monkeypatch,
) -> None:
    config = GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.OFF,
        gpu_arbiter_resources={
            "node-a/index:0": GPUResourceSettings(
                resource_id="node-a/index:0",
                node_id="node-a",
                physical_device_token="index:0",
                allocatable_mb=20_000,
                mode=GPUArbiterMode.OFF,
            )
        },
    )
    monkeypatch.setattr(
        "aap_inference.gpu.policy.effective_gpu_arbiter_mode",
        lambda _resource_id, *, config: GPUArbiterMode.ENFORCE,
    )

    assert unregistered_gpu_loading_blocked(config=config) is True


@pytest.mark.asyncio
async def test_caller_native_profile_uses_caller_functions_and_rejects_private_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = {
        "protocol": GPU_CALLER_DATA_PROTOCOL,
        "native_schema": GPU_CALLER_NATIVE_SCHEMA,
        "functions": list(GPU_CALLER_NATIVE_FUNCTIONS),
        "actions": list(GPU_CALLER_NATIVE_ACTIONS),
        "ready": True,
    }

    class Connection:
        calls: list[tuple[str, tuple[object, ...]]]

        def __init__(self) -> None:
            self.calls = []

        def is_in_transaction(self) -> bool:
            return True

        async def fetchval(self, query: str, *args: object) -> object:
            self.calls.append((query, args))
            if query.endswith("gpu_caller_policy()"):
                return json.dumps(policy)
            if query == "SELECT pg_catalog.pg_current_xact_id()::text":
                return "42"
            if query == "SELECT pg_catalog.pg_current_xact_id_if_assigned()::text":
                return "42"
            if query.endswith("gpu_caller_step($1::text,$2::text)"):
                action = args[0]
                return json.dumps(
                    {
                        "protocol": GPU_CALLER_DATA_PROTOCOL,
                        "action": action,
                        "result": {"candidates": []},
                    }
                )
            raise AssertionError(f"unexpected Native query: {query}")

    def reject_private_role_verification(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "caller profile must not verify the private authority role"
        )

    monkeypatch.setattr(
        postgres_module, "verify_function_only_role", reject_private_role_verification
    )
    connection = Connection()

    caller_data = await GPUCallerPostgresData.bind(connection)
    result = await caller_data.step("membership_candidates", {})

    assert result == {"candidates": []}
    assert [query.rsplit(".", 1)[-1] for query, _ in connection.calls] == [
        "gpu_caller_policy()",
        "pg_current_xact_id()::text",
        "pg_current_xact_id_if_assigned()::text",
        "gpu_caller_step($1::text,$2::text)",
    ]
    assert connection.calls[-1][1][0] == "membership_candidates"
    with pytest.raises(GPUAuthorityUnavailable):
        await caller_data.step("runtime_subject", {})
    assert len(connection.calls) == 4
