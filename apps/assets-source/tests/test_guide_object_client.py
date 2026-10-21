from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from aap_assets_source.guide_object_client import GuideObjectHttpClient
from aap_assets_source.guide_object_contract import (
    GUIDE_OBJECT_SCHEMA,
    GuideObjectFailure,
    GuideObjectRequest,
    guide_object_failure_reply,
    guide_object_success_reply,
)


def command(operation="delete"):
    project_id = uuid4()
    return GuideObjectRequest(
        schema=GUIDE_OBJECT_SCHEMA,
        request_id=uuid4(),
        operation=operation,
        project_id=project_id,
        key=f"projects/{project_id}/guide/{uuid4()}-image.png",
        content_type=None,
    )


@pytest.mark.asyncio
async def test_scoped_delete_is_sent_once_without_bucket_or_version_selection():
    request = command()
    calls = []

    def serve(incoming):
        calls.append(incoming)
        assert incoming.url == "http://assets.test/v1/guide-objects/execute"
        assert incoming.headers["authorization"] == "Bearer " + "t" * 40
        assert json.loads(incoming.content) == request.model_dump(
            mode="json", by_alias=True
        )
        reply = guide_object_success_reply(request, {"deleted": True})
        return httpx.Response(200, json=reply.model_dump(mode="json", by_alias=True))

    client = GuideObjectHttpClient(
        "http://assets.test/", "t" * 40, transport=httpx.MockTransport(serve)
    )
    reply = await client.execute(request)
    assert reply.result.deleted is True
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["read_timeout", "disconnect", "cancel"])
async def test_uncertain_transport_is_never_retried(fault):
    calls = []

    async def serve(incoming):
        calls.append(incoming)
        if fault == "cancel":
            raise asyncio.CancelledError
        error = httpx.ReadTimeout if fault == "read_timeout" else httpx.ReadError
        raise error("sensitive-provider-detail", request=incoming)

    client = GuideObjectHttpClient(
        "http://assets.test", "t" * 40, transport=httpx.MockTransport(serve)
    )
    if fault == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await client.execute(command())
    else:
        with pytest.raises(GuideObjectFailure) as error:
            await client.execute(command())
        assert error.value.code == "unavailable"
        assert "sensitive" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [(500, "storage_error"), (401, "unavailable"), (307, "unavailable")],
)
async def test_http_failure_is_bounded_and_does_not_follow_redirects(status, expected):
    calls = []

    def serve(incoming):
        calls.append(incoming)
        return httpx.Response(
            status,
            content=b"sensitive-storage-details",
            headers={"Location": "http://untrusted.test/"},
        )

    client = GuideObjectHttpClient(
        "http://assets.test", "t" * 40, transport=httpx.MockTransport(serve)
    )
    with pytest.raises(GuideObjectFailure) as error:
        await client.execute(command())
    assert error.value.code == expected
    assert "sensitive" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_valid_failure_keeps_storage_classification_and_binds_scope():
    request = command()
    payload = guide_object_failure_reply(request, "storage_error").model_dump(
        mode="json", by_alias=True
    )

    def serve(_incoming):
        return httpx.Response(200, json=payload)

    client = GuideObjectHttpClient(
        "http://assets.test", "t" * 40, transport=httpx.MockTransport(serve)
    )
    with pytest.raises(GuideObjectFailure) as error:
        await client.execute(request)
    assert error.value.code == "storage_error"

    payload["request_id"] = str(uuid4())
    with pytest.raises(GuideObjectFailure) as error:
        await client.execute(request)
    assert error.value.code == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [b"not-json", b'{"ok":true,"ok":false}', b"x" * 257])
async def test_malformed_or_oversized_reply_fails_closed(bad):
    client = GuideObjectHttpClient(
        "http://assets.test",
        "t" * 40,
        max_response_bytes=256,
        transport=httpx.MockTransport(
            lambda _incoming: httpx.Response(200, content=bad)
        ),
    )
    with pytest.raises(GuideObjectFailure) as error:
        await client.execute(command())
    assert error.value.code == "unavailable"


@pytest.mark.asyncio
async def test_request_budget_rejects_before_transport():
    calls = []
    client = GuideObjectHttpClient(
        "http://assets.test",
        "t" * 40,
        max_request_bytes=1,
        transport=httpx.MockTransport(lambda incoming: calls.append(incoming)),
    )
    with pytest.raises(GuideObjectFailure):
        await client.execute(command())
    assert calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"origin": "http://user:secret@assets.test"},
        {"origin": "http://assets.test/prefix"},
        {"origin": "http://assets.test?secret=value"},
        {"origin": "http://assets.test\n"},
        {"token": "short"},
        {"token": "t" * 40 + "\n"},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": True},
        {"max_response_bytes": True},
        {"max_request_bytes": 0},
    ],
)
def test_invalid_binding_configuration_is_not_echoed(overrides):
    options = {"origin": "http://assets.test", "token": "t" * 40, **overrides}
    with pytest.raises(ValueError) as error:
        GuideObjectHttpClient(**options)
    assert "user:secret" not in str(error.value)
