from __future__ import annotations

import asyncio
import json

import pytest

from aap_inference.pipeline_server import PipelineHttpServer, _PayloadTooLarge


class _Manager:
    def __init__(self, reply=None) -> None:
        self.reply = reply
        self.calls = []

    async def execute(self, body):
        self.calls.append(("execute", body))
        return self.reply

    async def query(self, body):
        self.calls.append(("query", body))
        return self.reply

    async def status(self, body):
        self.calls.append(("status", body))
        return self.reply


class _Data:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    async def readiness(self) -> bool:
        return self.ready


class _Writer:
    def __init__(self) -> None:
        self.output = bytearray()
        self.closed = False

    def write(self, value: bytes) -> None:
        self.output.extend(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def test_bounded_server_returns_503_before_dispatch_when_full():
    manager = _Manager()
    server = PipelineHttpServer(
        manager,
        _Data(True),
        "control-token",
        max_concurrency=1,
    )
    server._active = 1
    writer = _Writer()

    asyncio.run(server._handle(None, writer))

    assert manager.calls == []
    assert b"503 Service Unavailable" in writer.output
    assert writer.closed is True


def test_health_reports_unconfigured_owner_as_not_ready():
    server = PipelineHttpServer(None, None, "control-token")
    status, body = asyncio.run(server._dispatch("GET", "/health", {}, b""))

    assert status == 503
    assert json.loads(body) == {"status": "not_ready"}


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer control-token"])
async def test_model_execution_auth_precedes_unbounded_body(authorization):
    server = PipelineHttpServer(
        None,
        None,
        "control-token",
        model_execution_manager=object(),
        model_execution_control_token="execution-token",
    )
    listener = await asyncio.start_server(server._handle, "127.0.0.1", 0)
    async with listener:
        port = listener.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            headers = [
                "POST /v1/model-execution/execute HTTP/1.1",
                "Host: localhost",
                "Content-Type: application/json",
                "Content-Length: 10000000",
            ]
            if authorization is not None:
                headers.append(f"Authorization: {authorization}")
            writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=5)
            assert response.startswith(b"HTTP/1.1 401 ")
        finally:
            writer.close()
            await writer.wait_closed()


def test_authentication_rejects_wrong_bearer_before_manager_call():
    manager = _Manager()
    server = PipelineHttpServer(manager, _Data(True), "control-token")

    status, body = asyncio.run(
        server._dispatch(
            "POST",
            "/v1/pipelines/query",
            {"authorization": "Bearer wrong"},
            b"{}",
        )
    )

    assert status == 401
    assert manager.calls == []


@pytest.mark.asyncio
async def test_oversized_request_is_rejected_before_body_read():
    server = PipelineHttpServer(
        None,
        None,
        "control-token",
        max_request_bytes=8,
    )
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"POST /v1/pipelines/query HTTP/1.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 9\r\n\r\n"
    )

    with pytest.raises(_PayloadTooLarge):
        await server._read_request(reader)
