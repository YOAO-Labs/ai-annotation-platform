from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from aap_assets_mask_content import codec
from aap_assets_mask_content.gc_client import MaskGcClient
from aap_assets_mask_content.gc_contract import (
    GC_SCHEMA,
    DeleteOperationRequest,
    DeleteOperationStatus,
    DeleteStatusRequest,
    GcCandidate,
    GcScanPage,
    GcScanRequest,
    MaskGcFailure,
)

_TOKEN = "private-cleanup-token-with-more-than-32-characters"
_OPERATION_ID = UUID("2d639c8a-0fd8-4dcb-9f90-26cb8a752f3a")
_KEY = codec.rle_object_key("a" * 64)
_WHEN = datetime(2026, 10, 19, tzinfo=timezone.utc)


def _request():
    return DeleteOperationRequest(
        operation_id=_OPERATION_ID,
        object_key=_KEY,
        etag="opaque-etag-1",
        last_modified=_WHEN,
    )


def _status(
    request=None, *, operation_id=_OPERATION_ID, state="completed", outcome="deleted"
):
    return DeleteOperationStatus(
        operation_id=operation_id,
        request=request,
        state=state,
        outcome=outcome,
    )


@contextmanager
def _client(handler):
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    try:
        with MaskGcClient(
            "http://mask-gc.invalid",
            _TOKEN,
            client=http_client,
        ) as client:
            yield client
    finally:
        http_client.close()


def test_scan_sends_physical_cursor_and_accepts_bounded_page():
    cursor = "raster-masks/sha256/aa/aa/0-legacy-file.data"
    candidate = GcCandidate(
        object_key=_KEY,
        etag="opaque-etag-1",
        last_modified=_WHEN,
        byte_size=8,
    )
    page = GcScanPage(items=[candidate], after_key=_KEY, complete=True)

    def handler(request):
        assert request.url.path == "/v1/mask-gc/scan"
        assert request.headers["authorization"] == "Bearer " + _TOKEN
        assert request.headers["content-type"] == "application/json"
        assert request.read() == b'{"after_key":"' + cursor.encode() + b'","limit":10}'
        return httpx.Response(
            200,
            json={
                "schema": GC_SCHEMA,
                "ok": True,
                "data": page.model_dump(mode="json"),
            },
        )

    with _client(handler) as client:
        result = client.scan(GcScanRequest(after_key=cursor, limit=10))

    assert result == page


def test_readiness_authenticates_the_cleanup_scan_without_mutating():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.headers["authorization"] == "Bearer " + _TOKEN
        assert request.read() == b'{"after_key":null,"limit":1}'
        return httpx.Response(
            200,
            json={
                "schema": GC_SCHEMA,
                "ok": True,
                "data": {"items": [], "after_key": None, "complete": True},
            },
        )

    with _client(handler) as client:
        assert client.check_ready() is None
    assert calls == ["/v1/mask-gc/scan"]


def test_readiness_does_not_accept_unavailable_cleanup_permission():
    with _client(
        lambda request: httpx.Response(
            403,
            json={
                "schema": GC_SCHEMA,
                "ok": False,
                "error": {"code": "permission_denied"},
            },
        )
    ) as client:
        with pytest.raises(MaskGcFailure) as caught:
            client.check_ready()
    assert caught.value.code == "permission_denied"


def test_scan_rejects_candidate_before_the_requested_cursor():
    older_key = codec.rle_object_key("0" * 64)
    candidate = GcCandidate(
        object_key=older_key,
        etag="opaque-etag-1",
        last_modified=_WHEN,
        byte_size=8,
    )
    cursor = _KEY
    response = {
        "schema": GC_SCHEMA,
        "ok": True,
        "data": GcScanPage(items=[candidate], after_key=_KEY, complete=True).model_dump(
            mode="json"
        ),
    }
    with _client(lambda _request: httpx.Response(200, json=response)) as client:
        with pytest.raises(MaskGcFailure) as caught:
            client.scan(GcScanRequest(after_key=cursor))

    assert caught.value.code == "invalid_response"


def test_execute_and_status_require_exact_operation_identity():
    request = _request()
    other_id = UUID("9e1d88d5-5994-4d28-88af-5e1cdd55ea6b")
    other_request = request.model_copy(update={"operation_id": other_id})
    wrong_execute = _status(other_request, operation_id=other_id)
    with _client(
        lambda _request: httpx.Response(
            200,
            json={
                "schema": GC_SCHEMA,
                "ok": True,
                "data": wrong_execute.model_dump(mode="json"),
            },
        )
    ) as client:
        with pytest.raises(MaskGcFailure) as caught:
            client.execute(request)
    assert caught.value.code == "invalid_response"

    wrong_status = _status(None, operation_id=other_id, state="not_found", outcome=None)
    with _client(
        lambda _request: httpx.Response(
            200,
            json={
                "schema": GC_SCHEMA,
                "ok": True,
                "data": wrong_status.model_dump(mode="json"),
            },
        )
    ) as client:
        with pytest.raises(MaskGcFailure) as caught:
            client.status(DeleteStatusRequest(operation_id=_OPERATION_ID))
    assert caught.value.code == "invalid_response"


def test_execute_rejects_response_for_different_delete_request():
    request = _request()
    changed = request.model_copy(update={"etag": "other-etag"})
    response = _status(changed)
    with _client(
        lambda _request: httpx.Response(
            200,
            json={
                "schema": GC_SCHEMA,
                "ok": True,
                "data": response.model_dump(mode="json"),
            },
        )
    ) as client:
        with pytest.raises(MaskGcFailure) as caught:
            client.execute(request)
    assert caught.value.code == "invalid_response"


def test_closed_idempotency_error_survives_the_http_boundary():
    response = {
        "schema": GC_SCHEMA,
        "ok": False,
        "error": {"code": "idempotency_conflict"},
    }
    with _client(lambda _request: httpx.Response(409, json=response)) as client:
        with pytest.raises(MaskGcFailure) as caught:
            client.execute(_request())
    assert caught.value.code == "idempotency_conflict"


def test_invalid_url_or_token_is_closed():
    with pytest.raises(MaskGcFailure) as caught:
        MaskGcClient("https://user:secret@mask-gc.invalid", _TOKEN)
    assert caught.value.code == "storage_unavailable"
    assert "secret" not in str(caught.value)
