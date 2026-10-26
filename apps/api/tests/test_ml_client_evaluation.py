"""Bounded inference/cache transport and the exact controlled routing whitelist."""

import json

import httpx
import pytest
from fastapi import HTTPException

from aap_inference import backend_transport
from tests.test_ml_client_metrics import _backend, _transport


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.consumed = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def transport_factory(monkeypatch, response):
    real = httpx.AsyncClient
    seen = []

    def handler(request):
        seen.append(request)
        return response(request) if callable(response) else response

    def factory(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(backend_transport.httpx, "AsyncClient", factory)
    return seen


@pytest.mark.parametrize("declared", [True, False])
async def test_batch_budget_closes_stream_and_does_not_parse_oversized_json(
    monkeypatch, declared
):
    stream = Stream([b"x" * 8, b"y" * 8, b"z" * 8])
    transport_factory(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-length": "24"} if declared else {},
            stream=stream,
            request=request,
        ),
    )
    with pytest.raises(HTTPException) as error:
        await _transport(_backend()).predict([{"id": "sample"}], max_response_bytes=10)
    assert error.value.status_code == 413
    assert error.value.detail["reason"] == "evaluation_response_too_large"
    assert stream.closed and stream.consumed == (0 if declared else 2)


async def test_bounded_batch_keeps_structured_busy_error_and_metadata(monkeypatch):
    def handler(request):
        payload = json.loads(request.content)
        if payload["tasks"][0]["id"] == "busy":
            return httpx.Response(
                409,
                json={
                    "detail": {
                        "reason": "evaluation_request_busy",
                        "message": "old native still running",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "task": "sample",
                        "result": [],
                        "meta": {"sample_evaluation": {"request_id": "stable"}},
                    }
                ]
            },
        )

    transport_factory(monkeypatch, handler)
    client = _transport(_backend())
    with pytest.raises(HTTPException) as error:
        await client.predict([{"id": "busy"}], max_response_bytes=1024)
    assert (
        error.value.status_code == 409
        and error.value.detail["reason"] == "evaluation_request_busy"
    )
    results = await client.predict([{"id": "sample"}], max_response_bytes=1024)
    assert results[0].meta == {"sample_evaluation": {"request_id": "stable"}}


@pytest.mark.parametrize("payload", [[], {}, {"results": {}}, {"results": [None]}])
async def test_invalid_bounded_batch_cannot_become_empty_success(monkeypatch, payload):
    transport_factory(monkeypatch, lambda _: httpx.Response(200, json=payload))
    with pytest.raises(ValueError):
        await _transport(_backend()).predict(
            [{"id": "sample"}], max_response_bytes=1024
        )


async def test_private_profile_cache_and_seal_never_enter_gpu_dispatch(monkeypatch):
    seen = transport_factory(
        monkeypatch, lambda _: httpx.Response(200, json={"state": "completed"})
    )
    backend = _backend()
    backend.auth_method, backend.auth_token = "token", "private-proof-token"
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def forbidden_dispatch(_operation, _scope):
        raise AssertionError("Private receipt and seal cannot request GPU admission")
        yield

    client = _transport(backend, dispatch_context=forbidden_dispatch)
    assert await client.managed_evaluation_request("profile") == {"state": "completed"}
    await client.managed_evaluation_request(
        "requests/request", params={"binding_digest": "a" * 64}
    )
    await client.managed_evaluation_request(
        "requests/request/seal", payload={"binding_digest": "a" * 64}
    )
    assert [r.method for r in seen] == ["GET", "GET", "POST"]
    assert all(r.headers["authorization"] == "Bearer private-proof-token" for r in seen)
