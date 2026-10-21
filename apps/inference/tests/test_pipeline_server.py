from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from aap_inference.pipeline_contract import (
    PIPELINE_PROTOCOL,
    PipelineQuerySuccess,
    pipeline_query_request,
)
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
    assert b"Inference pipeline owner busy" in writer.output
    assert writer.closed is True


def test_health_reports_unconfigured_owner_as_not_ready():
    server = PipelineHttpServer(None, None, "control-token")
    status, body = asyncio.run(server._dispatch("GET", "/health", {}, b""))

    assert status == 503
    assert json.loads(body) == {"status": "not_ready"}


def test_authenticated_query_dispatch_keeps_typed_reply_on_http_200():
    query = pipeline_query_request(
        "list_pipelines",
        actor_id=uuid.UUID("0f9210aa-91c3-4a26-a78f-16367d4f811d"),
        actor_role="project_admin",
        filters={"scope": None, "project_id": None, "organization_id": None},
    )
    reply = PipelineQuerySuccess(
        schema=PIPELINE_PROTOCOL,
        ok=True,
        request_id=query.request_id,
        operation=query.operation,
        actor_id=query.admitted_actor.id,
        target_id=query.target_id,
        rows=[],
    )
    manager = _Manager(reply)
    server = PipelineHttpServer(manager, _Data(True), "control-token")

    status, body = asyncio.run(
        server._dispatch(
            "POST",
            "/v1/pipelines/query",
            {"authorization": "Bearer control-token"},
            query.model_dump_json(by_alias=True).encode(),
        )
    )

    assert status == 200
    assert json.loads(body)["ok"] is True
    assert manager.calls == [("query", query.model_dump_json(by_alias=True).encode())]


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
    assert json.loads(body) == {"detail": "Unauthorized"}
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
