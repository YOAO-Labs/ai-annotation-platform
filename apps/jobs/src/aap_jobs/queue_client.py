"""HTTP clients for API dispatch and cooperative Celery revoke requests."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .queue_contract import (
    JOBS_QUEUE_PATH,
    JobsQueueContractError,
    JobsQueueUnavailable,
    QueueRevoke,
    decode_queue_reply,
    decode_queue_request,
    encode_canvas_request,
    encode_dispatch_request,
    encode_queue_request,
    encode_revoke_request,
)

_MAX_REPLY_BYTES = 4096
_REQUEST_TIMEOUT_SECONDS = 10.0


def _origin_and_token(origin: str, token: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
        token_bytes = token.encode("ascii")
    except (AttributeError, UnicodeError, ValueError):
        raise ValueError("Jobs queue client settings are invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not token
        or (port is not None and not 1 <= port <= 65535)
        or len(token_bytes) > 4096
        or any(value < 33 or value > 126 for value in token_bytes)
    ):
        raise ValueError("Jobs queue client settings are invalid")
    return f"{parsed.scheme}://{parsed.netloc}", token


def _bounded(chunks, *, limit: int) -> bytes:
    body = bytearray()
    for chunk in chunks:
        body.extend(chunk)
        if len(body) > limit:
            raise JobsQueueContractError()
    return bytes(body)


async def _bounded_async(chunks, *, limit: int) -> bytes:
    body = bytearray()
    async for chunk in chunks:
        body.extend(chunk)
        if len(body) > limit:
            raise JobsQueueContractError()
    return bytes(body)


def _post_sync(origin: str, token: str, body: bytes, execution_id: str) -> None:
    try:
        import httpx

        with httpx.Client(
            timeout=_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                "POST",
                f"{origin}{JOBS_QUEUE_PATH}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                content=body,
            ) as response:
                response_body = _bounded(response.iter_bytes(), limit=_MAX_REPLY_BYTES)
                status_code = response.status_code
        reply = decode_queue_reply(response_body)
        if (
            status_code != 200
            or not reply.accepted
            or reply.execution_id != execution_id
        ):
            raise JobsQueueUnavailable()
    except JobsQueueUnavailable:
        raise
    except Exception:
        raise JobsQueueUnavailable() from None


def _envelope_body(envelope: dict[str, Any]) -> tuple[bytes, str]:
    body = encode_queue_request(envelope)
    operation = decode_queue_request(body)
    if isinstance(operation, QueueRevoke):
        raise JobsQueueContractError()
    return body, operation.execution_id


class JobsQueueClient:
    """Dispatch to the owner; acceptance means broker publication returned."""

    def __init__(self, origin: str, token: str):
        self.origin, self.token = _origin_and_token(origin, token)

    async def _post_async(self, body: bytes, execution_id: str) -> str:
        try:
            import httpx

            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self.origin}{JOBS_QUEUE_PATH}",
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                    },
                    content=body,
                ) as response:
                    response_body = await _bounded_async(
                        response.aiter_bytes(), limit=_MAX_REPLY_BYTES
                    )
                    status_code = response.status_code
            reply = decode_queue_reply(response_body)
            if (
                status_code != 200
                or not reply.accepted
                or reply.execution_id != execution_id
            ):
                raise JobsQueueUnavailable()
            return execution_id
        except JobsQueueUnavailable:
            raise
        except Exception:
            raise JobsQueueUnavailable() from None

    async def dispatch_job(
        self,
        task_name: str,
        execution_id: str,
        args: list,
        kwargs: dict | None = None,
        options: dict | None = None,
    ) -> str:
        body = encode_dispatch_request(
            task_name,
            execution_id,
            args,
            kwargs=kwargs,
            options=options,
        )
        return await self._post_async(body, execution_id)

    def dispatch_job_sync(
        self,
        task_name: str,
        execution_id: str,
        args: list,
        kwargs: dict | None = None,
        options: dict | None = None,
    ) -> str:
        body = encode_dispatch_request(
            task_name,
            execution_id,
            args,
            kwargs=kwargs,
            options=options,
        )
        _post_sync(self.origin, self.token, body, execution_id)
        return execution_id

    async def dispatch_canvas(
        self,
        execution_id: str,
        group_id: str,
        tasks: list[dict[str, Any]],
        callback: dict[str, Any],
    ) -> str:
        body = encode_canvas_request(execution_id, group_id, tasks, callback)
        return await self._post_async(body, execution_id)

    def dispatch_canvas_sync(
        self,
        execution_id: str,
        group_id: str,
        tasks: list[dict[str, Any]],
        callback: dict[str, Any],
    ) -> str:
        body = encode_canvas_request(execution_id, group_id, tasks, callback)
        _post_sync(self.origin, self.token, body, execution_id)
        return execution_id

    async def publish_envelope(self, envelope: dict[str, Any]) -> str:
        body, execution_id = _envelope_body(envelope)
        return await self._post_async(body, execution_id)

    def publish_envelope_sync(self, envelope: dict[str, Any]) -> str:
        body, execution_id = _envelope_body(envelope)
        _post_sync(self.origin, self.token, body, execution_id)
        return execution_id


class JobsQueueExecutionControl:
    """Synchronous revoke adapter for the existing execution-control protocol."""

    def __init__(self, origin: str, token: str):
        self.origin, self.token = _origin_and_token(origin, token)

    def request_cancel(self, execution_id: str) -> None:
        body = encode_revoke_request(execution_id)
        _post_sync(self.origin, self.token, body, execution_id)
