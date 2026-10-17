"""CLI shared output, error handling, and Client setup.

``--json`` controls successful stdout; errors stay text by default, with
``--error-format json`` available for structured stderr.
"""

from __future__ import annotations

import json
import math
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, TypeVar
from uuid import UUID

import httpx
import typer
from pydantic import BaseModel, TypeAdapter, ValidationError as ModelValidationError
from rich.console import Console

from ai_annotation import Client
from ai_annotation.config import load_config
from ai_annotation.errors import (
    AAPError,
    APIStatusError,
    AuthenticationError,
    JobFailedError,
    JobTimeoutError,
    StreamError,
)

console = Console()
err_console = Console(stderr=True)
# 进度条/spinner 是瞬态 UI 而非命令输出, 走 stderr, 避免污染管道中的 stdout
progress_console = Console(stderr=True)
ModelT = TypeVar("ModelT", bound=BaseModel)
_ERROR_FORMAT: ContextVar[str] = ContextVar("aap_cli_error_format", default="text")


@contextmanager
def error_format_context(error_format: str) -> Iterator[None]:
    token = _ERROR_FORMAT.set(error_format)
    try:
        yield
    finally:
        _ERROR_FORMAT.reset(token)


def get_error_format() -> str:
    return _ERROR_FORMAT.get()


def _model_error(exc: ModelValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, error['loc'])) or 'body'}: {error['msg']}"
        for error in exc.errors(include_input=False, include_url=False)
    ).replace("\n", " ")


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number


def _reject_json_constant(_value: str) -> None:
    raise ValueError("JSON numbers must be finite")


def read_json_body(path: Path, model_type: type[ModelT]) -> ModelT:
    """Read typed request JSON without including input values in failures."""
    try:
        raw = (
            sys.stdin.read() if path == Path("-") else path.read_text(encoding="utf-8")
        )
        data = json.loads(
            raw, parse_float=_finite_json_float, parse_constant=_reject_json_constant
        )
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise AAPError("无法读取请求文件：请检查路径、UTF-8 编码和 JSON 格式") from None
    if not isinstance(data, dict):
        raise AAPError("请求文件必须包含 JSON 对象")
    try:
        return model_type.model_validate(data)
    except ModelValidationError as exc:
        error = AAPError(f"请求参数无效: {_model_error(exc)}")
        error.cli_fields = _validation_fields(exc.errors(include_url=False))
        raise error from None


def print_learning_result(result: Any, json_mode: bool) -> None:
    if isinstance(result, BaseModel):
        data = result.model_dump(mode="json", by_alias=True)
    elif isinstance(result, Path):
        data = {"path": str(result)}
    elif result is None:
        data = {"deleted": True}
    else:
        data = TypeAdapter(Any).dump_python(result, mode="json", by_alias=True)
    if json_mode:
        print_json(data)
    else:
        console.print_json(data=data)


def print_json(data: Any) -> None:
    """--json 模式输出: 裸 JSON, 无任何 rich 装饰。"""
    typer.echo(json.dumps(data, ensure_ascii=False))


_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    """unicode 块字符趋势条 (CLI 无 Textual 时画时间序列)。空 / 单值降级。"""
    nums = [float(v) for v in values]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    span = hi - lo
    if span <= 0:
        return _SPARK_CHARS[0] * len(nums)
    last = len(_SPARK_CHARS) - 1
    return "".join(_SPARK_CHARS[min(last, int((v - lo) / span * last))] for v in nums)


def _safe_text(value: Any, limit: int = 512) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(r"\bak_[A-Za-z0-9._~-]+\b", "[redacted]", text)
    text = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)([\"']?(?:api[_\s-]?key|access[_\s-]?token|refresh[_\s-]?token|password|secret)[\"']?\s*[:=]\s*[\"']?)[^\s,;\]}\"']+",
        r"\1[redacted]",
        text,
    )
    return text[:limit] or None


def _safe_location(value: Any) -> list[str | int] | None:
    if not isinstance(value, (list, tuple)):
        return None
    location: list[str | int] = []
    for part in value:
        if isinstance(part, int) and part >= 0:
            location.append(part)
        elif isinstance(part, str):
            location.append(
                part if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}", part) else "*"
            )
    return location


def _input_scalars(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (bool, int, float)):
        yield str(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _input_scalars(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _input_scalars(item)


def _safe_validation_message(error: dict[str, Any]) -> str | None:
    message = _safe_text(error.get("msg"))
    if message is None:
        return None
    for source in (error.get("input"), error.get("ctx")):
        for value in _input_scalars(source):
            if value:
                message = message.replace(value, "[redacted]")
    return message[:512]


def _validation_fields(details: Any) -> list[dict[str, Any]]:
    if isinstance(details, list):
        errors = details
    elif isinstance(details, dict):
        errors = next(
            (
                value
                for key in ("errors", "detail", "fields")
                if isinstance((value := details.get(key)), list)
            ),
            [details] if "loc" in details else [],
        )
    else:
        return []
    fields: list[dict[str, Any]] = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        location = _safe_location(error.get("loc"))
        if location is None:
            continue
        field: dict[str, Any] = {"loc": location}
        error_type = error.get("type")
        if isinstance(error_type, str) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}", error_type
        ):
            field["type"] = error_type
        message = _safe_validation_message(error)
        if message is not None:
            field["msg"] = message
        fields.append(field)
    return fields


def _safe_uuid(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _safe_reason(value: Any) -> str | None:
    cleaned = _safe_text(value, 64)
    return (
        cleaned if cleaned and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", cleaned) else None
    )


def _api_error_payload(error: APIStatusError) -> dict[str, Any]:
    details = error.detail
    fields = _validation_fields(details)
    code = details.get("code") if isinstance(details, dict) else None
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code):
        code = None
    detail = None
    if not fields:
        if isinstance(details, str):
            detail = _safe_text(details)
        elif isinstance(details, dict):
            detail = next(
                (
                    cleaned
                    for key in ("detail", "message", "msg")
                    if (cleaned := _safe_text(details.get(key))) is not None
                ),
                None,
            )
    return {
        "status": error.status_code,
        "code": code,
        "detail": detail,
        "fields": fields,
        "request_id": _safe_text(error.request_id, 128),
        "retry_after": _safe_text(error.retry_after, 128),
        "reason": _safe_reason(details.get("reason"))
        if isinstance(details, dict)
        else None,
        "run_id": _safe_uuid(details.get("run_id"))
        if isinstance(details, dict)
        else None,
        "job_id": _safe_uuid(details.get("async_job_id") or details.get("job_id"))
        if isinstance(details, dict)
        else None,
    }


def _safe_request_detail(error: AAPError) -> str | None:
    message = str(error)
    if message.startswith("async job "):
        return "Async job operation failed."
    if (
        message.startswith(("video frame ", "video chunk "))
        and " is not ready " in message
    ):
        return "Video asset is not ready. Read its descriptor before downloading."
    if message == "video asset URL must be absolute to avoid sending API credentials":
        return "Video asset URL must be absolute. Refresh its descriptor."
    # Fixed SDK-authored hints are safe; other AAPError text may contain IDs or server data.
    if message.startswith(
        (
            "无法读取请求文件",
            "请求文件必须包含 JSON 对象",
            "缺少 base_url:",
            "Training run creation requires a fresh preflight_digest",
            "training study creation requires a preview_digest",
            "expected_sha256 must be",
            "expected_size must be",
            "VOC export returns ZIP bytes synchronously; use ",
            "scope and lidar belong in body=ExportRequestBody(...)",
            "WebSocket streams require ai-annotation-sdk[streams]",
        )
    ):
        return _safe_text(message)
    return None


def _safe_job_status(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,31}", value):
        return value
    return None


def _job_timeout_id(error: JobTimeoutError) -> str | None:
    match = re.match(r"^async job (\S+) 等待超过 ", str(error))
    return _safe_text(match.group(1), 128) if match else None


def print_structured_error(
    kind: str,
    *,
    status: int | None = None,
    code: str | None = None,
    detail: str | None = None,
    fields: list[dict[str, Any]] | None = None,
    request_id: str | None = None,
    retry_after: str | None = None,
    job_id: str | None = None,
    job_status: str | None = None,
    reason: str | None = None,
    run_id: str | None = None,
    close_code: int | None = None,
) -> None:
    """Write one sanitized error object to stderr."""
    error: dict[str, Any] = {"kind": kind}
    for key, value in (
        ("status", status),
        ("code", code),
        ("detail", _safe_text(detail)),
        ("fields", fields or None),
        ("request_id", _safe_text(request_id, 128)),
        ("retry_after", _safe_text(retry_after, 128)),
        ("job_id", _safe_text(job_id, 128)),
        ("job_status", _safe_job_status(job_status)),
        ("reason", _safe_reason(reason)),
        ("run_id", _safe_uuid(run_id)),
        (
            "close_code",
            close_code
            if type(close_code) is int and 1000 <= close_code <= 4999
            else None,
        ),
    ):
        if value is not None:
            error[key] = value
    sys.stderr.write(json.dumps(error, ensure_ascii=False) + "\n")


def print_error(
    message: str,
    json_mode: bool = False,
    *,
    kind: str = "local_error",
    status: int | None = None,
    code: str | None = None,
    detail: str | None = None,
    fields: list[dict[str, Any]] | None = None,
    request_id: str | None = None,
    retry_after: str | None = None,
    job_id: str | None = None,
    job_status: str | None = None,
    reason: str | None = None,
    run_id: str | None = None,
    close_code: int | None = None,
) -> None:
    """Write a text error by default, or one structured object when requested."""
    if get_error_format() == "json":
        print_structured_error(
            kind,
            status=status,
            code=code,
            detail=detail,
            fields=fields,
            request_id=request_id,
            retry_after=retry_after,
            job_id=job_id,
            job_status=job_status,
            reason=reason,
            run_id=run_id,
            close_code=close_code,
        )
    elif json_mode:
        print(message, file=sys.stderr)
    else:
        err_console.print(f"[red]{message}[/red]")


def confirm_destructive(message: str, yes: bool, json_mode: bool) -> None:
    """破坏性操作确认; JSON 模式禁止 prompt。"""
    if yes:
        return
    if json_mode:
        raise typer.BadParameter("--json 模式执行破坏性操作时必须传 --yes")
    try:
        typer.confirm(message, abort=True)
    except typer.Abort:
        if get_error_format() != "json":
            raise
        print_structured_error(
            "confirmation_refused", detail="Confirmation was declined."
        )
        raise typer.Exit(code=1) from None


@contextmanager
def cli_errors(json_mode: bool = False) -> Iterator[None]:
    """把 SDK / 网络异常转为一行错误 + exit 1, 不向用户喷 traceback。"""
    try:
        yield
    except AuthenticationError as e:
        print_error(
            f"认证失败 (HTTP 401): {e.detail}; 请先运行 `aap login` 配置有效 API key",
            json_mode,
            kind="api_error",
            **_api_error_payload(e),
        )
        raise typer.Exit(code=1) from None
    except APIStatusError as e:
        print_error(
            f"请求失败 (HTTP {e.status_code}): {e.detail}",
            json_mode,
            kind="api_error",
            **_api_error_payload(e),
        )
        raise typer.Exit(code=1) from None
    except JobFailedError as e:
        print_error(
            str(e),
            json_mode,
            kind="job_failed",
            job_id=str(e.job.id),
            job_status=e.job.status,
        )
        raise typer.Exit(code=1) from None
    except JobTimeoutError as e:
        print_error(
            str(e),
            json_mode,
            kind="job_timeout",
            detail="Job did not reach a terminal state before timeout.",
            job_id=_job_timeout_id(e),
        )
        raise typer.Exit(code=1) from None
    except StreamError as e:
        print_error(
            str(e),
            json_mode,
            kind="stream_error",
            code=e.code,
            status=e.status_code,
            close_code=e.close_code,
            detail=str(e),
        )
        raise typer.Exit(code=1) from None
    except AAPError as e:
        fields = getattr(e, "cli_fields", None)
        if fields is not None:
            print_error(
                str(e),
                json_mode,
                kind="validation_error",
                detail="Request validation failed.",
                fields=fields,
            )
        else:
            print_error(
                str(e),
                json_mode,
                kind="request_error",
                detail=_safe_request_detail(e),
            )
        raise typer.Exit(code=1) from None
    except ModelValidationError as e:
        fields = _validation_fields(e.errors(include_url=False))
        print_error(
            f"请求参数无效: {_model_error(e)}",
            json_mode,
            kind="validation_error",
            detail="Request validation failed.",
            fields=fields,
        )
        raise typer.Exit(code=1) from None
    except httpx.HTTPError as e:
        print_error(
            f"网络错误: {e}",
            json_mode,
            kind="network_error",
            detail="Network request failed.",
        )
        raise typer.Exit(code=1) from None
    except OSError as e:
        print_error(
            f"文件操作失败: {e.strerror or type(e).__name__}",
            json_mode,
            kind="local_error",
            detail="File operation failed.",
        )
        raise typer.Exit(code=1) from None


def get_client(json_mode: bool = False, *, require_auth: bool = True) -> Client:
    """从配置装配 Client; 未配置时提示并 exit 1。"""
    base_url, api_key = load_config()
    if not base_url or (require_auth and not api_key):
        print_error(
            "未配置平台地址 / API key: 请先运行 `aap login`, "
            "或设置环境变量 AAP_BASE_URL / AAP_API_KEY",
            json_mode,
            kind="configuration_error",
            detail="Platform URL and API key are not configured.",
        )
        raise typer.Exit(code=1)
    return Client(base_url=base_url, api_key=api_key)
