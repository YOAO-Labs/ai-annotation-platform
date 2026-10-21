from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from aap_inference.pipeline_client import PipelineHttpClient
from aap_inference.pipeline_contract import (
    PIPELINE_PROTOCOL,
    PipelineErrorDetails,
    PipelineFailure,
    PipelinePostCommitFailure,
    PipelineQueryFailure,
    PipelineResult,
    PipelineStatusSuccess,
    PipelineSuccess,
    PipelineUnavailable,
    decode_pipeline_status_request,
    pipeline_command_request,
    pipeline_query_request,
)


def _command():
    return pipeline_command_request(
        "create_pipeline",
        actor_id=uuid.UUID("0f9210aa-91c3-4a26-a78f-16367d4f811d"),
        actor_role="project_admin",
        payload={},
    )


def _success(command, resource_id=None):
    return PipelineSuccess(
        schema=PIPELINE_PROTOCOL,
        ok=True,
        operation_id=command.operation_id,
        operation=command.operation,
        actor_id=command.admitted_actor.id,
        target_id=command.target_id,
        result=PipelineResult(resource_id=resource_id or uuid.uuid4()),
        commit_state="committed",
    )


def test_execute_uses_authenticated_endpoint_and_returns_bound_success():
    command = _command()
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=_success(command).model_dump(mode="json", by_alias=True),
        )

    async def run():
        client = PipelineHttpClient(
            "http://pipeline-owner:8768/",
            "control-secret",
            transport=httpx.MockTransport(handler),
        )
        try:
            reply = await client.execute(command)
        finally:
            await client.close()
        assert reply.ok is True
        assert len(seen) == 1
        assert seen[0].url.path == "/v1/pipelines/execute"
        assert seen[0].headers["authorization"] == "Bearer control-secret"

    asyncio.run(run())


def test_unknown_typed_failure_probes_status_once_without_reposting():
    command = _command()
    paths = []
    resource_id = uuid.UUID("1257b89b-3f43-4154-aa22-a2914c527fc9")
    unknown = PipelineFailure(
        schema=PIPELINE_PROTOCOL,
        ok=False,
        operation_id=command.operation_id,
        operation=command.operation,
        actor_id=command.admitted_actor.id,
        target_id=command.target_id,
        error=PipelineErrorDetails(
            status_code=503,
            detail="项目编排操作结果暂不可确认",
            headers=None,
            rollback=False,
        ),
        commit_state="unknown",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/execute"):
            return httpx.Response(
                200, json=unknown.model_dump(mode="json", by_alias=True)
            )
        status_request = decode_pipeline_status_request(request.content)
        status_reply = PipelineStatusSuccess(
            schema=PIPELINE_PROTOCOL,
            ok=True,
            request_id=status_request.request_id,
            operation=status_request.operation,
            operation_id=command.operation_id,
            command_operation=command.operation,
            actor_id=command.admitted_actor.id,
            target_id=command.target_id,
            state="committed",
            resource_id=resource_id,
        )
        return httpx.Response(
            200, json=status_reply.model_dump(mode="json", by_alias=True)
        )

    async def run():
        client = PipelineHttpClient(
            "http://pipeline-owner",
            "token",
            transport=httpx.MockTransport(handler),
        )
        try:
            reply = await client.execute(command)
        finally:
            await client.close()
        assert reply.ok is True
        assert reply.commit_state == "committed"
        assert reply.result.resource_id == resource_id
        assert paths == ["/v1/pipelines/execute", "/v1/pipelines/status"]

    asyncio.run(run())


def test_definitive_rollback_failure_does_not_probe_status():
    command = _command()
    paths = []
    failure = PipelineFailure(
        schema=PIPELINE_PROTOCOL,
        ok=False,
        operation_id=command.operation_id,
        operation=command.operation,
        actor_id=command.admitted_actor.id,
        target_id=command.target_id,
        error=PipelineErrorDetails(
            status_code=409,
            detail={"reason": "project_busy"},
            headers=None,
            rollback=True,
        ),
        commit_state="rolled_back",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=failure.model_dump(mode="json", by_alias=True))

    async def run():
        client = PipelineHttpClient(
            "http://pipeline-owner",
            "token",
            transport=httpx.MockTransport(handler),
        )
        try:
            reply = await client.execute(command)
        finally:
            await client.close()
        assert reply.ok is False
        assert reply.commit_state == "rolled_back"
        assert paths == ["/v1/pipelines/execute"]

    asyncio.run(run())


def test_query_failure_is_returned_without_status_probe():
    query = pipeline_query_request(
        "list_pipelines",
        actor_id=uuid.UUID("0f9210aa-91c3-4a26-a78f-16367d4f811d"),
        actor_role="project_admin",
        filters={"scope": None, "project_id": None, "organization_id": None},
    )
    paths = []
    failure = PipelineQueryFailure(
        schema=PIPELINE_PROTOCOL,
        ok=False,
        request_id=query.request_id,
        operation=query.operation,
        actor_id=query.admitted_actor.id,
        target_id=query.target_id,
        error=PipelineErrorDetails(
            status_code=403,
            detail="无权读取编排",
            headers=None,
            rollback=False,
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=failure.model_dump(mode="json", by_alias=True))

    async def run():
        client = PipelineHttpClient(
            "http://pipeline-owner",
            "token",
            transport=httpx.MockTransport(handler),
        )
        try:
            reply = await client.query(query)
        finally:
            await client.close()
        assert reply.ok is False
        assert paths == ["/v1/pipelines/query"]

    asyncio.run(run())


def test_malformed_execute_ack_uses_one_status_probe_and_never_reposts():
    command = _command()
    paths = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/execute"):
            return httpx.Response(200, content=b'{"ok":true,"wrong":1}')
        return httpx.Response(200, content=b"{}")

    async def run():
        client = PipelineHttpClient(
            "http://pipeline-owner",
            "token",
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(PipelineUnavailable):
                await client.execute(command)
        finally:
            await client.close()
        assert paths == ["/v1/pipelines/execute", "/v1/pipelines/status"]

    asyncio.run(run())


def test_transport_loss_uses_one_status_probe_and_never_reposts():
    command = _command()
    paths = []
    resource_id = uuid.UUID("a12284e4-94c6-4ab5-9b42-c16d4ac628d4")

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/execute"):
            raise httpx.ReadError("lost acknowledgement", request=request)
        status_request = decode_pipeline_status_request(request.content)
        reply = PipelineStatusSuccess(
            schema=PIPELINE_PROTOCOL,
            ok=True,
            request_id=status_request.request_id,
            operation=status_request.operation,
            operation_id=command.operation_id,
            command_operation=command.operation,
            actor_id=command.admitted_actor.id,
            target_id=command.target_id,
            state="committed",
            resource_id=resource_id,
        )
        return httpx.Response(200, json=reply.model_dump(mode="json", by_alias=True))

    async def run():
        client = PipelineHttpClient(
            "http://pipeline-owner",
            "token",
            transport=httpx.MockTransport(handler),
        )
        try:
            reply = await client.execute(command)
        finally:
            await client.close()
        assert reply.ok is True
        assert reply.result.resource_id == resource_id
        assert paths == ["/v1/pipelines/execute", "/v1/pipelines/status"]

    asyncio.run(run())


def test_plain_query_500_remains_generic_postcommit_failure_without_probe():
    query = pipeline_query_request(
        "list_pipelines",
        actor_id=uuid.UUID("0f9210aa-91c3-4a26-a78f-16367d4f811d"),
        actor_role="project_admin",
        filters={"scope": None, "project_id": None, "organization_id": None},
    )
    paths = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(500, text="Internal Server Error")

    async def run():
        client = PipelineHttpClient(
            "http://pipeline-owner",
            "token",
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(PipelinePostCommitFailure):
                await client.query(query)
        finally:
            await client.close()
        assert paths == ["/v1/pipelines/query"]

    import asyncio

    asyncio.run(run())
