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

    async def finish(
        self, *, status_code: int | None = None, reason=None, not_sent=False
    ):
        if self.settlement is None:
            self.settlement = asyncio.create_task(
                self._settle(status_code, reason, not_sent)
            )
        await asyncio.shield(self.settlement)

    async def _settle(self, status_code, reason, not_sent):
        try:
            if not_sent:
                try:
                    confirmed = await self.grant.not_sent()
                except Exception:
                    confirmed = False
                if not confirmed:
                    await self.grant.uncertain("dispatch_start_unacknowledged")
            elif status_code is None:
                await self.grant.uncertain(reason or "transport_error")
            else:
                await self.grant.response(status_code)
        except Exception:
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


def _attach_pinned_transport(
    client: httpx.AsyncClient,
    pinned_transport_factory: Callable[[], httpx.AsyncBaseTransport] | None,
) -> None:
    if pinned_transport_factory is None:
        return
    if isinstance(getattr(client, "_transport", None), httpx.AsyncHTTPTransport):
        client._transport = pinned_transport_factory()


class TrackedHttpClient:
    """Normal AsyncClient configuration, with one grant per actual HTTP send."""

    def __init__(
        self,
        request_context: RequestContext,
        *,
        on_not_sent=None,
        on_send=None,
        pinned_transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        **kwargs,
    ):
        self.request_context = request_context
        self.on_not_sent = on_not_sent
        self.on_send = on_send
        self.admission_passed = False
        self.attempts: dict[httpx.Request, _Attempt] = {}
        hooks = dict(kwargs.pop("event_hooks", {}) or {})
        hooks["request"] = [*hooks.get("request", []), self._before_request]
        hooks["response"] = [self._after_response, *hooks.get("response", [])]
        self.client = httpx.AsyncClient(event_hooks=hooks, **kwargs)
        if "transport" not in kwargs:
            _attach_pinned_transport(self.client, pinned_transport_factory)

    async def _before_request(self, request):
        attempt = None
        try:
            context = self.request_context(request)
            grant = await context.__aenter__()
            attempt = _Attempt(context, grant)
            self.attempts[request] = attempt
            request.headers.update(grant.headers)
            authorize = getattr(grant, "authorize_send", None)
            if authorize is not None:
                await authorize()
            if self.on_send is not None:
                self.on_send()
        except BaseException:
            if not self.admission_passed and self.on_not_sent is not None:
                try:
                    self.on_not_sent()
                except Exception:
                    logger.warning("backend_route_no_send_unacknowledged")
            if attempt is not None:
                await attempt.finish(
                    reason="invalid_admission_headers",
                    not_sent=bool(getattr(attempt.grant, "can_prove_not_sent", False)),
                )
            raise
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
    pinned_transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    **kwargs,
):
    if request_context is None:
        client = httpx.AsyncClient(**kwargs)
        if "transport" not in kwargs:
            _attach_pinned_transport(client, pinned_transport_factory)
        return client
    return TrackedHttpClient(
        request_context,
        on_not_sent=on_not_sent,
        on_send=on_send,
        pinned_transport_factory=pinned_transport_factory,
        **kwargs,
    )
