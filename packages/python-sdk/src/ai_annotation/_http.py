"""内部传输层: URL 拼接 / auth 注入 / 错误映射 / 幂等 GET 重试 / 流式上传下载。

不属于公开 API; 外部代码只应使用 ai_annotation.Client。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from ai_annotation.errors import (
    AAPError,
    APIStatusError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)

API_PREFIX = "/api/v1"


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _idempotency_headers(value: str | UUID | None) -> dict[str, str]:
    if value is None:
        return {}
    try:
        return {"Idempotency-Key": str(UUID(str(value)))}
    except (TypeError, ValueError) as exc:
        raise ValueError("idempotency_key must be a UUID") from exc


_STATUS_ERROR_MAP: dict[int, type[APIStatusError]] = {
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
}
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


def map_status_error(resp: httpx.Response) -> APIStatusError:
    """后端错误体为 {"detail": str|dict}; 解析失败回落原始文本。"""
    try:
        detail = resp.json().get("detail")
    except Exception:
        detail = resp.text
    cls = _STATUS_ERROR_MAP.get(resp.status_code, APIStatusError)
    return cls(
        resp.status_code,
        detail,
        request_id=resp.headers.get("x-request-id") or resp.headers.get("request-id"),
        retry_after=resp.headers.get("retry-after"),
    )


class HttpTransport:
    """httpx.Client 薄封装; 幂等 GET 在 429/5xx 时做最多 3 次指数退避重试。"""

    def __init__(self, base_url: str, api_key: str | None, timeout: float = 30.0):
        self._origin = base_url.rstrip("/")
        self._timeout = timeout
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(
            base_url=self._origin + API_PREFIX, headers=headers, timeout=timeout
        )
        self._bare_client: httpx.Client | None = None
        # 重试退避基数, 测试可调小避免真实 sleep
        self.retry_backoff = 0.5

    @property
    def bare(self) -> httpx.Client:
        """无 auth 的裸 client: 预签名 PUT / 绝对预签名下载 URL 用。"""
        if self._bare_client is None:
            self._bare_client = httpx.Client(timeout=self._timeout)
        return self._bare_client

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
        root_path: bool = False,
        accepted_statuses: tuple[int, ...] = (),
        timeout: float | None = None,
    ) -> httpx.Response:
        if root_path and (not path.startswith("/") or path.startswith("//")):
            raise ValueError("root path must be an absolute same-origin path")
        target = self._origin + path if root_path else path
        attempts = 3 if method.upper() == "GET" else 1
        for attempt in range(attempts):
            resp = self._client.request(
                method,
                target,
                params=params,
                json=json,
                content=content,
                data=data,
                files=files,
                headers=headers,
                timeout=self._timeout if timeout is None else timeout,
            )
            if (
                resp.status_code in _RETRYABLE_STATUS
                and resp.status_code not in accepted_statuses
                and attempt + 1 < attempts
            ):
                time.sleep(self.retry_backoff * (2**attempt))
                continue
            break
        if resp.status_code >= 400 and resp.status_code not in accepted_statuses:
            raise map_status_error(resp)
        return resp

    def put_presigned(self, url: str, file_path: Path, content_type: str) -> None:
        """向预签名 URL 流式 PUT 文件 (不带平台 auth header; 大文件不整读进内存)。"""
        with file_path.open("rb") as f:
            resp = self.bare.put(url, content=f, headers={"Content-Type": content_type})
        if resp.status_code >= 400:
            raise APIStatusError(resp.status_code, f"预签名上传失败: {resp.text[:200]}")
        if not 200 <= resp.status_code < 300:
            raise APIStatusError(resp.status_code, "预签名上传未完成：需要 2xx 响应")

    def stream_download(
        self,
        url: str,
        dest: Path,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> Path:
        """Stream atomically; absolute URLs bypass platform auth, relative paths use it."""
        if expected_sha256 is not None and (
            len(expected_sha256) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in expected_sha256)
        ):
            raise AAPError("expected_sha256 must be a 64-character hexadecimal digest")
        if expected_size is not None and (
            type(expected_size) is not int or expected_size < 0
        ):
            raise AAPError("expected_size must be a non-negative integer")
        if url.startswith(("http://", "https://")):
            client, target = self.bare, url
            if params is not None:
                target = str(httpx.URL(target).copy_merge_params(params))
                params = None
        else:
            client = self._client
            target = self._origin + (url if url.startswith("/") else "/" + url)
        temp = tempfile.NamedTemporaryFile(
            mode="wb",
            dir=dest.parent,
            prefix=".aap-download-",
            suffix=".part",
            delete=False,
        )
        temp_path = Path(temp.name)
        digest = hashlib.sha256()
        size = 0
        try:
            with temp:
                with client.stream(method, target, params=params) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                        raise map_status_error(resp)
                    if resp.status_code != 200:
                        raise APIStatusError(
                            resp.status_code,
                            f"下载需要 HTTP 200，收到 {resp.status_code}",
                        )
                    for chunk in resp.iter_bytes():
                        temp.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            actual_sha256 = digest.hexdigest()
            if expected_sha256 is not None and actual_sha256 != expected_sha256.lower():
                raise AAPError(
                    f"download SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
                )
            if expected_size is not None and size != expected_size:
                raise AAPError(
                    f"download size mismatch: expected {expected_size}, got {size}"
                )
            os.replace(temp_path, dest)
        finally:
            temp_path.unlink(missing_ok=True)
        return dest

    def close(self) -> None:
        self._client.close()
        if self._bare_client is not None:
            self._bare_client.close()
