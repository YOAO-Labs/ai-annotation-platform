"""Track HTTP completion without replacing HTTPX transport or proxy selection."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
import logging
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)
GPU_UNLOAD_ROUTE_EXTENSION = "aap.gpu_unload_route"


class BackendRequestGrant(Protocol):
    @property
    def headers(self) -> Mapping[str, str]: ...

    async def response(self, status_code: int) -> None: ...

    async def uncertain(self, reason: str) -> None: ...


RequestContext = Callable[
    [httpx.Request], AbstractAsyncContextManager[BackendRequestGrant]
]


class _Attempt:
    def __init__(self, context, grant):
        self.context = context
        self.grant = grant
        self.settlement: asyncio.Task | None = None

    async def finish(self, *, status_code: int | None = None, reason=None):
        if self.settlement is None:
            self.settlement = asyncio.create_task(self._settle(status_code, reason))
        await asyncio.shield(self.settlement)

    async def _settle(self, status_code, reason):
        try:
            if status_code is None:
                await self.grant.uncertain(reason or "transport_error")
            else:
                await self.grant.response(status_code)
        except Exception:
            # A missing acknowledgement leaves durable work unresolved. It must
            # not discard an already received inference result or repeat HTTP.
            logger.warning("backend_route_settlement_unacknowledged")
        finally:
            try:
                await self.context.__aexit__(None, None, None)
            except Exception:
                logger.warning("backend_route_context_close_unacknowledged")


class _TrackedStream(httpx.AsyncByteStream):
    def __init__(self, stream, attempt, status_code):
        self.stream = stream
        self.attempt = attempt
        self.status_code = status_code

    async def __aiter__(self):
        try:
            async for chunk in self.stream:
                yield chunk
        except BaseException:
            await self.attempt.finish(reason="incomplete_response")
            raise
        else:
            await self.attempt.finish(status_code=self.status_code)

    async def aclose(self):
        try:
            await self.stream.aclose()
        finally:
            await self.attempt.finish(reason="early_response_close")


class TrackedHttpClient:
    """Normal AsyncClient configuration, with one grant per actual HTTP send."""

    def __init__(
        self,
        request_context: RequestContext,
        *,
        on_not_sent=None,
        on_send=None,
        **kwargs,
    ):
        self.request_context = request_context
        self.on_not_sent = on_not_sent
        self.on_send = on_send
        self.admission_passed = False
        self.attempts: dict[httpx.Request, _Attempt] = {}
        hooks = dict(kwargs.pop("event_hooks", {}) or {})
        # Admission comes last so the final request method/path is authenticated.
        hooks["request"] = [*hooks.get("request", []), self._before_request]
        hooks["response"] = [self._after_response, *hooks.get("response", [])]
        self.client = httpx.AsyncClient(event_hooks=hooks, **kwargs)

    async def _before_request(self, request):
        attempt = None
        try:
            context = self.request_context(request)
            grant = await context.__aenter__()
            attempt = _Attempt(context, grant)
            self.attempts[request] = attempt
            request.headers.update(grant.headers)
            if self.on_send is not None:
                self.on_send()
        except BaseException:
            if not self.admission_passed and self.on_not_sent is not None:
                try:
                    self.on_not_sent()
                except Exception:
                    logger.warning("backend_route_no_send_unacknowledged")
            if attempt is not None:
                await attempt.finish(reason="invalid_admission_headers")
            raise
        # A later rejected redirect cannot prove that this dispatch never sent.
        self.admission_passed = True

    async def _after_response(self, response):
        attempt = self.attempts.get(response.request)
        if attempt is not None:
            if response.is_stream_consumed:
                await attempt.finish(status_code=response.status_code)
            else:
                response.stream = _TrackedStream(
                    response.stream, attempt, response.status_code
                )

    async def __aenter__(self):
        return await self.client.__aenter__()

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            return await self.client.__aexit__(exc_type, exc, traceback)
        finally:
            for attempt in self.attempts.values():
                await attempt.finish(
                    reason="request_cancelled"
                    if exc_type is not None
                    else "incomplete_response"
                )


def http_client(
    request_context: RequestContext | None = None,
    *,
    on_not_sent=None,
    on_send=None,
    **kwargs,
):
    if request_context is None:
        return httpx.AsyncClient(**kwargs)
    return TrackedHttpClient(
        request_context, on_not_sent=on_not_sent, on_send=on_send, **kwargs
    )
