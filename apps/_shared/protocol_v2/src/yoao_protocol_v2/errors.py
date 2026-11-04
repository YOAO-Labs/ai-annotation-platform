"""Shared HTTP error helpers for protocol v2 backends."""

from __future__ import annotations

from enum import Enum

from fastapi import HTTPException


class LifecycleErrorCode(str, Enum):
    """Backend-side managed lifecycle error vocabulary from ADR-0049."""

    BACKEND_DRAINING = "gpu_backend_draining"
    BACKEND_ACTIVE = "gpu_backend_active"
    GENERATION_CONFLICT = "gpu_generation_conflict"
    TRANSITION_CONFLICT = "gpu_transition_conflict"
    GENERATION_INVALID = "gpu_generation_invalid"
    GENERATION_MISMATCH = "gpu_generation_mismatch"
    ADMISSION_DENIED = "gpu_admission_denied"
    UNLOAD_FAILED = "gpu_unload_failed"


_LIFECYCLE_ERROR_STATUS = {
    LifecycleErrorCode.BACKEND_DRAINING: 503,
    LifecycleErrorCode.BACKEND_ACTIVE: 409,
    LifecycleErrorCode.GENERATION_CONFLICT: 409,
    LifecycleErrorCode.TRANSITION_CONFLICT: 409,
    LifecycleErrorCode.GENERATION_INVALID: 422,
    LifecycleErrorCode.GENERATION_MISMATCH: 422,
    LifecycleErrorCode.ADMISSION_DENIED: 403,
    LifecycleErrorCode.UNLOAD_FAILED: 500,
}


class LifecycleHTTPError(HTTPException):
    """Structured FastAPI error for managed backend lifecycle endpoints."""

    def __init__(
        self,
        code: LifecycleErrorCode,
        *,
        message: str | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        detail = {"error_code": code.value}
        if message is not None:
            detail["message"] = message
        headers = None
        if retry_after_s is not None:
            if retry_after_s < 0:
                raise ValueError("retry_after_s must be non-negative")
            headers = {"Retry-After": str(retry_after_s)}
        super().__init__(
            status_code=_LIFECYCLE_ERROR_STATUS[code],
            detail=detail,
            headers=headers,
        )


class VariantNotSupportedError(HTTPException):
    """Requested variant is syntactically valid but unsupported by this backend."""

    def __init__(
        self, axis: str, value: str, allowed: list[str] | tuple[str, ...]
    ) -> None:
        super().__init__(
            status_code=422,
            detail={
                "error_code": "variant_not_supported",
                "axis": axis,
                "value": value,
                "allowed": list(allowed),
            },
        )


class ModelUnavailableError(HTTPException):
    """Requested model/variant cannot be served by the backend right now."""

    def __init__(self, key: str, reason: str, retry_after_s: int = 30) -> None:
        super().__init__(
            status_code=503,
            detail={
                "error_code": "model_unavailable",
                "key": key,
                "reason": reason,
            },
            headers={"Retry-After": str(retry_after_s)},
        )
