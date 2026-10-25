"""Actual HTTPX send/body lifecycle evidence for the injected execution port."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from aap_inference.request_tracking import http_client


class Stream(httpx.AsyncByteStream):
    def __init__(self, trace, *, fail=False, wait=None):
        self.trace = trace
        self.fail = fail
        self.wait = wait

    async def __aiter__(self):
        self.trace.append("body")
        yield b"hello"
        if self.wait:
            await self.wait.wait()
        if self.fail:
            raise httpx.ReadError("body interrupted")
        yield b" world"

    async def aclose(self):
        self.trace.append("stream.close")


def context(trace, *, fail_settlement=False):
    class Grant:
        headers = {"X-AAP-Route-Admission": "admitted"}

        async def response(self, status):
            trace.append(("response", status))
            if fail_settlement:
                raise OSError("lost acknowledgement")

        async def uncertain(self, reason):
            trace.append(("uncertain", reason))

    @asynccontextmanager
    async def grant(request):
        trace.append(("begin", request.method, request.url.path))
        try:
            yield Grant()
        finally:
            trace.append("context.close")

    return grant


@pytest.mark.asyncio
async def test_consumed_response_settles_after_body_before_context_close():
    trace = []

    async def handler(request):
        assert request.headers["X-AAP-Route-Admission"] == "admitted"
        trace.append("send")
        return httpx.Response(503, stream=Stream(trace))

    async with http_client(
        context(trace), transport=httpx.MockTransport(handler)
    ) as client:
        response = await client.post("https://example.test/predict")
        assert response.content == b"hello world"
    assert trace == [
        ("begin", "POST", "/predict"),
        "send",
        "body",
        ("response", 503),
        "context.close",
        "stream.close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["early_close", "body_error", "network", "cancel"])
async def test_incomplete_requests_remain_uncertain_once(failure):
    trace = []
    wait = asyncio.Event() if failure == "cancel" else None

    async def handler(_request):
        trace.append("send")
        if failure == "network":
            raise httpx.ConnectError("connection lost")
        return httpx.Response(
            200, stream=Stream(trace, fail=failure == "body_error", wait=wait)
        )

    async def request():
        async with http_client(
            context(trace),
            transport=httpx.MockTransport(handler),
            on_not_sent=lambda: trace.append("not_sent"),
        ) as client:
            async with client.stream(
                "POST", "https://example.test/predict"
            ) as response:
                if failure != "early_close":
                    await response.aread()

    if failure == "cancel":
        task = asyncio.create_task(request())
        while "body" not in trace:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif failure == "early_close":
        await request()
    else:
        with pytest.raises(httpx.HTTPError):
            await request()
    assert (
        sum(isinstance(item, tuple) and item[0] == "uncertain" for item in trace) == 1
    )
    assert not any(isinstance(item, tuple) and item[0] == "response" for item in trace)
    assert trace.count("context.close") == 1
    assert "not_sent" not in trace


@pytest.mark.asyncio
async def test_lost_settlement_ack_keeps_successful_result_without_resend():
    trace = []

    async def handler(_request):
        trace.append("send")
        return httpx.Response(200, stream=Stream(trace))

    async with http_client(
        context(trace, fail_settlement=True), transport=httpx.MockTransport(handler)
    ) as client:
        assert (
            await client.post("https://example.test/predict")
        ).content == b"hello world"
    assert trace.count("send") == 1
    assert trace.count(("response", 200)) == 1
    assert trace.count("context.close") == 1


@pytest.mark.asyncio
async def test_failed_admission_never_sends_http():
    trace = []

    @asynccontextmanager
    async def denied(_request):
        raise RuntimeError("scope revoked")
        yield

    async def handler(_request):
        trace.append("send")
        return httpx.Response(200)

    with pytest.raises(RuntimeError, match="scope revoked"):
        async with http_client(
            denied,
            transport=httpx.MockTransport(handler),
            on_not_sent=lambda: trace.append("not_sent"),
        ) as client:
            await client.post("https://example.test/predict")
    assert trace == ["not_sent"]


@pytest.mark.asyncio
async def test_rejected_redirect_cannot_claim_the_initial_request_was_not_sent():
    trace = []

    @asynccontextmanager
    async def admit(request):
        if request.url.path != "/predict":
            raise RuntimeError("redirect denied")
        async with context(trace)(request) as grant:
            yield grant

    async def handler(_request):
        trace.append("send")
        return httpx.Response(307, headers={"Location": "/redirect"})

    with pytest.raises(RuntimeError, match="redirect denied"):
        async with http_client(
            admit,
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            on_not_sent=lambda: trace.append("not_sent"),
        ) as client:
            await client.post("https://example.test/predict")
    assert trace.count("send") == 1
    assert "not_sent" not in trace
