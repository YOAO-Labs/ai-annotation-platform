"""Cycle-safe GPU arbitration contracts (no ml_client / DB / store imports).

These types, error codes, enums and dispatch dataclasses are the low-level contract
layer that ``ml_client`` and the GPU orchestration modules both depend on. Extracted
verbatim from the legacy ``gpu_arbiter.py`` so ``ml_client`` no longer needs to import
``gpu_arbiter`` (breaking the historical cycle).
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol

from aap_inference_contract.execution import ModelExecutionError
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    validate_canonical_positive_int64,
)
from uuid import UUID


class GPUArbiterErrorCode(str, Enum):
    """Stable platform-side arbitration errors frozen by ADR-0049."""

    NOT_READY = "gpu_arbiter_not_ready"
    CAPACITY_UNAVAILABLE = "gpu_capacity_unavailable"
    BACKEND_CONCURRENCY_SATURATED = "gpu_backend_concurrency_saturated"
    DRAIN_TIMEOUT = "gpu_drain_timeout"
    UNAVAILABLE = "gpu_arbiter_unavailable"
    CONFIG_INVALID = "gpu_config_invalid"
    BACKEND_RETIREMENT_REQUIRED = "gpu_backend_retirement_required"


_GPU_ARBITER_DISPATCH_ERROR_STATUS = {
    GPUArbiterErrorCode.NOT_READY: 503,
    GPUArbiterErrorCode.CAPACITY_UNAVAILABLE: 503,
    GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED: 503,
    GPUArbiterErrorCode.DRAIN_TIMEOUT: 503,
    GPUArbiterErrorCode.UNAVAILABLE: 503,
    GPUArbiterErrorCode.CONFIG_INVALID: 503,
    GPUArbiterErrorCode.BACKEND_RETIREMENT_REQUIRED: 409,
}


_GPU_ARBITER_RETRY_AFTER_REQUIRED = frozenset(
    {
        GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED,
        GPUArbiterErrorCode.DRAIN_TIMEOUT,
    }
)


@dataclass
class GPUConfigDiagnostic:
    code: str
    level: Literal["info", "warning", "critical", "blocker"]
    message: str
    field: str | None = None
    resource_id: str | None = None
    backend_id: UUID | None = None


@dataclass
class GPUBackendConfigStatus:
    status: Literal["ok", "info", "warning", "critical", "blocker"] = "ok"
    desired_mode: Literal["off", "observe", "enforce"] = "off"
    effective_mode: Literal["off", "observe", "enforce"] = "off"
    rollout_enabled: bool | None = None
    rollout_state: (
        Literal[
            "disabled",
            "uninitialized",
            "off",
            "promoting",
            "enforcing",
            "demoting",
            "blocked",
        ]
        | None
    ) = None
    rollout_revision: int | None = None
    rollout_blocker_reason: str | None = None
    allocatable_mb: int | None = None
    resource_claimed_budget_mb: int | None = None
    diagnostics: list[GPUConfigDiagnostic] = dataclass_field(default_factory=list)


class GPUArbiterDispatchError(RuntimeError):
    """Framework-neutral rejection facts projected to HTTP at the API boundary."""

    def __init__(
        self,
        code: GPUArbiterErrorCode,
        *,
        message: str | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        if code in _GPU_ARBITER_RETRY_AFTER_REQUIRED and retry_after_s is None:
            raise ValueError(f"{code.value} requires retry_after_s")
        if retry_after_s is not None and (
            not isinstance(retry_after_s, int)
            or isinstance(retry_after_s, bool)
            or retry_after_s < 0
        ):
            raise ValueError("retry_after_s must be a non-negative integer")

        self.error_code = code.value
        self.status_code = _GPU_ARBITER_DISPATCH_ERROR_STATUS[code]
        self.retry_after_s = retry_after_s
        self.detail = {"error_code": code.value}
        if message is not None:
            self.detail["message"] = message
        self.headers = (
            {"Retry-After": str(retry_after_s)} if retry_after_s is not None else {}
        )
        super().__init__(message or code.value)

    @classmethod
    def from_remote(
        cls,
        *,
        status_code: int,
        detail: dict[str, Any],
        headers: Mapping[str, str],
        error_code: str,
        retry_after_s: int | None,
    ) -> GPUArbiterDispatchError:
        if (
            not isinstance(status_code, int)
            or isinstance(status_code, bool)
            or not 100 <= status_code <= 599
            or type(detail) is not dict
            or type(error_code) is not str
            or not error_code
            or (
                retry_after_s is not None
                and (
                    not isinstance(retry_after_s, int)
                    or isinstance(retry_after_s, bool)
                    or retry_after_s < 0
                )
            )
            or any(
                type(key) is not str or type(value) is not str
                for key, value in headers.items()
            )
        ):
            raise ValueError("remote GPU dispatch error is malformed")
        message = detail.get("message")
        error = cls.__new__(cls)
        RuntimeError.__init__(
            error,
            message if type(message) is str and message else error_code,
        )
        error.status_code = status_code
        error.detail = dict(detail)
        error.headers = dict(headers)
        error.error_code = error_code
        error.retry_after_s = retry_after_s
        return error


GPUDispatchOperation = Literal[
    "predict",
    "predict_interactive",
    "warmup",
    "reload",
    "unload",
]


GPUDispatchOutcomeKind = Literal["response_received", "not_sent", "uncertain"]


GPUDispatchUncertainReason = Literal["request_aborted", "response_not_reported"]


@dataclass(frozen=True)
class GPUDispatchRequest:
    """Exact caller identity and model metadata for GPU authority dispatch."""

    request_id: UUID
    backend_id: str
    gpu_resource_id: str
    operation: GPUDispatchOperation
    scope: AdmissionScope


@dataclass(frozen=True)
class GPUDispatchOutcome:
    """One explicit transport outcome; it is not a residency assertion."""

    kind: GPUDispatchOutcomeKind
    status_code: int | None = None
    reason: GPUDispatchUncertainReason | None = None


class GPUDispatchOutcomeChannel:
    """Mutable one-shot outcome channel owned by an immutable dispatch grant."""

    def __init__(self) -> None:
        self._outcome: GPUDispatchOutcome | None = None
        self._send_started = False

    @property
    def outcome(self) -> GPUDispatchOutcome | None:
        return self._outcome

    def report_response(self, status_code: int) -> bool:
        if (
            not isinstance(status_code, int)
            or isinstance(status_code, bool)
            or not 100 <= status_code <= 599
        ):
            raise ValueError("status_code must be a valid HTTP status")
        if self._outcome is not None:
            return False
        self._outcome = GPUDispatchOutcome(
            kind="response_received",
            status_code=status_code,
        )
        return True

    def report_uncertain_if_missing(
        self,
        reason: GPUDispatchUncertainReason,
    ) -> bool:
        if self._outcome is not None:
            return False
        self._outcome = GPUDispatchOutcome(kind="uncertain", reason=reason)
        return True

    def report_not_sent(self) -> bool:
        if self._outcome is not None or self._send_started:
            return False
        self._outcome = GPUDispatchOutcome(kind="not_sent")
        return True

    def request_started(self) -> None:
        if self._outcome is not None or self._send_started:
            raise ValueError("dispatch grant already used")
        self._send_started = True


class GPUDispatchGrantPort(Protocol):
    """Structural grant shared by local owner dispatch and remote HTTP clients."""

    generation: str
    admission_token: str

    @property
    def outcome(self) -> GPUDispatchOutcome | None: ...

    def report_response(self, status_code: int) -> bool: ...

    def report_not_sent(self) -> bool: ...

    def request_started(self) -> None: ...

    def report_uncertain_if_missing(
        self,
        reason: GPUDispatchUncertainReason,
    ) -> bool: ...


@dataclass(frozen=True)
class GPUDispatchGrant:
    """Managed lifecycle headers produced after authoritative admission."""

    generation: str
    admission_token: str
    outcome_channel: GPUDispatchOutcomeChannel = dataclass_field(
        default_factory=GPUDispatchOutcomeChannel,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        validate_canonical_positive_int64(self.generation)
        if (
            not self.admission_token
            or self.admission_token.strip() != self.admission_token
        ):
            raise ValueError("admission_token must be non-empty and canonical")

    @property
    def outcome(self) -> GPUDispatchOutcome | None:
        return self.outcome_channel.outcome

    def report_response(self, status_code: int) -> bool:
        return self.outcome_channel.report_response(status_code)

    def report_not_sent(self) -> bool:
        return self.outcome_channel.report_not_sent()

    def request_started(self) -> None:
        self.outcome_channel.request_started()

    def report_uncertain_if_missing(
        self,
        reason: GPUDispatchUncertainReason,
    ) -> bool:
        return self.outcome_channel.report_uncertain_if_missing(reason)


GPUDispatchContextFactory = Callable[
    [GPUDispatchRequest], AbstractAsyncContextManager[GPUDispatchGrantPort]
]


def gpu_arbiter_failure_record(exc: BaseException) -> dict[str, Any] | None:
    """Return a stable worker record for direct or wrapped admission failures."""

    if isinstance(exc, GPUArbiterDispatchError):
        error_code = exc.error_code
        status_code = exc.status_code
        retry_after_s = exc.retry_after_s
        detail: object = exc.detail
    elif isinstance(exc, ModelExecutionError):
        failure = exc.failure
        if failure.kind != "admission":
            return None
        error_code = failure.code
        status_code = failure.status_code
        detail = failure.detail
        retry_after = failure.retry_after
        if isinstance(retry_after, int) and not isinstance(retry_after, bool):
            retry_after_s = retry_after if retry_after >= 0 else None
        elif (
            isinstance(retry_after, str)
            and retry_after.isascii()
            and retry_after.isdecimal()
        ):
            retry_after_s = int(retry_after)
        else:
            retry_after_s = None
    else:
        return None

    if not isinstance(error_code, str) or error_code not in {
        item.value for item in GPUArbiterErrorCode
    }:
        return None
    if (
        not isinstance(status_code, int)
        or isinstance(status_code, bool)
        or not 100 <= status_code <= 599
    ):
        return None
    message = detail.get("message") if isinstance(detail, Mapping) else None
    if not isinstance(message, str) or not message:
        message = error_code
    return {
        "error_code": error_code,
        "status_code": status_code,
        "retry_after_s": retry_after_s,
        "message": message,
    }


def summarize_gpu_arbiter_failures(
    failures: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate dispatch attempts by stable code into a bounded job summary."""

    buckets: dict[str, dict[str, Any]] = {}
    for failure in failures:
        error_code = failure.get("error_code")
        if not isinstance(error_code, str) or not error_code:
            continue
        count = failure.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            count = 1
        retry_after_s = failure.get("retry_after_s")
        bucket = buckets.setdefault(
            error_code,
            {
                "error_code": error_code,
                "status_code": failure.get("status_code"),
                "retry_after_s": retry_after_s,
                "count": 0,
            },
        )
        bucket["count"] += count
        if isinstance(retry_after_s, int) and not isinstance(retry_after_s, bool):
            current = bucket["retry_after_s"]
            if current is None or retry_after_s > current:
                bucket["retry_after_s"] = retry_after_s
    return [buckets[error_code] for error_code in sorted(buckets)]
