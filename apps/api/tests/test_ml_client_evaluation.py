"""Bounded inference/cache transport and the exact controlled routing whitelist."""

import hashlib
import json
import uuid
from types import SimpleNamespace

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
    request_id = uuid.uuid4()
    await client.managed_evaluation_request(
        f"requests/{request_id}", params={"binding_digest": "a" * 64}
    )
    await client.managed_evaluation_request(
        f"requests/{request_id}/seal", payload={"binding_digest": "a" * 64}
    )
    assert [r.method for r in seen] == ["GET", "GET", "POST"]
    assert all(r.headers["authorization"] == "Bearer private-proof-token" for r in seen)


@pytest.mark.parametrize("seal", [False, True])
async def test_large_segment_completed_receipt_recovers_through_read_and_seal(
    monkeypatch, seal
):
    from app.services import managed_inference as managed
    from app.services.training_dataset_bridge import content_digest
    from aap_protocol_v2.mask_interaction import CocoRlePayload, canonical_rle_bytes
    from training_contract.model_return import (
        SEGMENTATION_OUTPUT_CONTRACT,
        SEGMENTATION_RUNTIME,
    )

    request_id, storage_id = uuid.uuid4(), uuid.uuid4()
    rle = {
        "encoding": "coco_rle",
        "size": [512, 512],
        "counts": [0] + [1] * (512 * 512),
    }
    raw = canonical_rle_bytes(CocoRlePayload.model_validate(rle))
    sha = hashlib.sha256(raw).hexdigest()
    identity = {
        "model_version_id": str(uuid.uuid4()),
        "sha256": "d" * 64,
        "classes_digest": content_digest(["car"]),
    }
    response = {
        "results": [
            {
                "task": "sample",
                "result": [
                    {
                        "tool_unit_id": "region",
                        "class_name": "car",
                        "native_class_index": 0,
                        "instance_index": 0,
                        "score": 0.8,
                        "confidence": 0.8,
                        "geometry": {
                            "type": "raster_mask",
                            "mask": {
                                "encoding": "coco_rle_ref",
                                "size": [512, 512],
                                "sha256": sha,
                                "bytes": len(raw),
                                "runs": len(rle["counts"]),
                                "object_key": f"raster-masks/sha256/{sha[:2]}/{sha[2:4]}/{sha}.json",
                            },
                        },
                    }
                ],
                "meta": {
                    "trained_model": identity,
                    "mask_objects": {sha: rle},
                    "segmentation": {
                        "output_contract": SEGMENTATION_OUTPUT_CONTRACT,
                        "dimensions": [512, 512],
                        "decoder": SEGMENTATION_RUNTIME["decoder"],
                        "mask_processing": SEGMENTATION_RUNTIME["mask_processing"],
                        "runtime_qualification": SEGMENTATION_RUNTIME,
                    },
                },
            }
        ]
    }
    request = {
        "request_id": str(request_id),
        "binding_digest": "b" * 64,
        "storage_identity": str(storage_id),
        "profile_digest": "c" * 64,
        "issued_at": 1,
        "expires_at": 86401,
    }
    completed = {
        **request,
        "schema": 1,
        "state": "completed",
        "response": response,
        "response_digest": content_digest(response),
    }
    assert len(json.dumps(completed).encode()) > 256 * 1024
    seen = transport_factory(monkeypatch, lambda _: httpx.Response(200, json=completed))
    client = _transport(_backend())
    row = SimpleNamespace(
        request_id=request_id, request={"task": "segment", "evaluation": request}
    )
    path = f"segment/requests/{request_id}"
    if seal:
        recovered = await client.managed_evaluation_request(
            path + "/seal",
            payload={
                key: request[key]
                for key in (
                    "binding_digest",
                    "profile_digest",
                    "storage_identity",
                    "issued_at",
                    "expires_at",
                )
            },
        )
    else:
        recovered = await client.managed_evaluation_request(
            path,
            params={
                "binding_digest": request["binding_digest"],
                "storage_identity": request["storage_identity"],
            },
        )
    assert managed.checked_receipt(row, recovered) == completed
    assert recovered["response_digest"] == content_digest(recovered["response"])
    from app.services.sample_evaluation_masks import normalize_predictions

    frozen = normalize_predictions(
        recovered["response"]["results"][0], [0], width=512, height=512
    )
    assert frozen[0]["area_pixels"] == 512 * 512 // 2
    assert (
        seen[0].url.path
        == f"/managed-evaluation/segment/requests/{request_id}{'/seal' if seal else ''}"
    )
    assert seen[0].method == ("POST" if seal else "GET")


@pytest.mark.parametrize(
    "path",
    [
        "profile",
        "segment/profile",
        "requests/00000000-0000-0000-0000-000000000001",
        "requests/00000000-0000-0000-0000-000000000001/seal",
    ],
)
async def test_non_segment_receipts_and_all_profiles_keep_small_budget(
    monkeypatch, path
):
    stream = Stream([b"x" * (256 * 1024 + 1)])
    transport_factory(
        monkeypatch, lambda request: httpx.Response(200, stream=stream, request=request)
    )
    with pytest.raises(HTTPException) as error:
        await _transport(_backend()).managed_evaluation_request(
            path, payload={} if path.endswith("/seal") else None
        )
    assert error.value.detail["max_bytes"] == 256 * 1024
    assert stream.closed


@pytest.mark.parametrize("seal", [False, True])
@pytest.mark.parametrize("declared", [False, True])
async def test_segment_receipt_budget_stops_before_unbounded_json(
    monkeypatch, seal, declared
):
    limit = 17 * 1024 * 1024
    stream = Stream([b"x" * (64 * 1024)] * (limit // (64 * 1024) + 2))
    transport_factory(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-length": str(limit + 1)} if declared else {},
            stream=stream,
            request=request,
        ),
    )
    with pytest.raises(HTTPException) as error:
        await _transport(_backend()).managed_evaluation_request(
            f"segment/requests/{uuid.uuid4()}{'/seal' if seal else ''}",
            payload={} if seal else None,
        )
    assert error.value.detail["max_bytes"] == limit
    assert stream.closed and (
        stream.consumed == 0
        if declared
        else stream.consumed <= limit // (64 * 1024) + 1
    )


@pytest.mark.parametrize(
    "path,payload",
    [
        ("other", None),
        ("segment/other", None),
        ("segment/requests/request", None),
        ("segment/requests/00000000-0000-0000-0000-000000000001?unbounded=1", None),
        ("segment/requests/00000000-0000-0000-0000-000000000001/seal/extra", {}),
        ("segment/profile", {}),
        ("requests/00000000-0000-0000-0000-000000000001/seal", None),
        ("segment/requests/00000000-0000-0000-0000-000000000001", {}),
    ],
)
async def test_unknown_evaluation_paths_and_methods_are_rejected_before_http(
    monkeypatch, path, payload
):
    seen = transport_factory(
        monkeypatch, lambda _: pytest.fail("Unknown paths cannot enter transport")
    )
    with pytest.raises(ValueError):
        await _transport(_backend()).managed_evaluation_request(path, payload=payload)
    assert seen == []


async def test_controlled_route_forwards_only_qualified_context_and_byte_budget(
    monkeypatch,
):
    from aap_inference_contract.execution import FrozenModelOperation, thaw_json
    from app.services import inference_model_execution, trained_models

    target = uuid.uuid4()
    ref = {
        "model_version_id": str(uuid.uuid4()),
        "sha256": "a" * 64,
        "classes_digest": "b" * 64,
        "classes": ["car"],
        "bindings": ["car"],
    }
    gateway = inference_model_execution.model_execution_port(None)

    class FrozenReference:
        async def validate(self, request):
            assert request.project_id is not None
            return target

    monkeypatch.setattr(
        trained_models,
        "bind_frozen_model_reference",
        lambda _db: FrozenReference(),
    )
    request = {
        "request_id": str(uuid.uuid4()),
        "binding_digest": "c" * 64,
    }
    operation = FrozenModelOperation(
        request_id=uuid.uuid4(),
        operation="batch_predict",
        task_inputs=({"id": "sample"},),
        context={
            "model_id": "trained:" + ref["model_version_id"],
            "frozen_model_ref": ref,
            "params": {"conf": 0.25},
            "sample_evaluation": request,
            "unqualified_escape": True,
        },
        project_id=uuid.uuid4(),
        requested_registry_id=target,
        requested_pool_id=None,
        endpoint_registry_id=None,
        required_instance_id=target,
        route_owner="evaluation",
        route_operation="sample_evaluation",
        max_response_bytes=256 * 1024,
    )
    forwarded, reference = await gateway._controlled_context(operation)
    wire = thaw_json(forwarded.context)
    assert forwarded.required_instance_id == target
    assert forwarded.max_response_bytes == 256 * 1024
    assert wire["sample_evaluation"] == request
    assert wire["model_id"] == "trained:" + ref["model_version_id"]
    assert wire["params"] == {"conf": 0.25}
    assert wire["frozen_model_ref"] == trained_models.wire_reference(ref)
    assert reference["model_version_id"] == ref["model_version_id"]
    assert "unqualified_escape" not in wire
