"""SDK 异常层级。后端错误体统一为 {"detail": str | dict}。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ai_annotation.models import Job


class AAPError(Exception):
    """所有 SDK 异常的基类。"""


class InstallError(AAPError):
    """An optional SDK feature requires additional installed dependencies."""


class APIStatusError(AAPError):
    """HTTP 4xx / 5xx 统一异常。"""

    def __init__(
        self,
        status_code: int,
        detail: Any,
        *,
        request_id: str | None = None,
        retry_after: str | None = None,
    ):
        self.status_code = status_code
        self.detail = detail
        self.request_id = request_id
        self.retry_after = retry_after
        super().__init__(f"HTTP {status_code}: {detail}")


class AuthenticationError(APIStatusError):
    """401 · 未认证或 API key 失效。"""


class PermissionDeniedError(APIStatusError):
    """403 · 无权限。"""


class NotFoundError(APIStatusError):
    """404 · 资源不存在。"""


class ConflictError(APIStatusError):
    """409 · 资源冲突 (如上传内容重复)。"""


class ValidationError(APIStatusError):
    """422 · 请求体 / 参数校验失败。"""


class JobFailedError(AAPError):
    """async job 以 failed / cancelled 终态结束。"""

    def __init__(self, job: "Job"):
        self.job = job
        super().__init__(
            f"async job {job.id} 终态 {job.status}: {job.error_message or '(无错误信息)'}"
        )


class JobTimeoutError(AAPError):
    """jobs.wait 超时仍未到终态。"""


class StreamError(AAPError):
    """A sanitized WebSocket failure; refresh owning REST state before reconnecting."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        close_code: int | None = None,
        status_code: int | None = None,
    ):
        self.code = code
        self.close_code = close_code
        self.status_code = status_code
        super().__init__(message)
