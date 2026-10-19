from __future__ import annotations

from fastapi.testclient import TestClient

from aap_assets_mask_content import codec
from aap_assets_mask_content.contract import (
    CONTENT_SCHEMA,
    CandidatePage,
    DeleteRequest,
    DeleteResult,
    GetRequest,
    GetResult,
    ListRequest,
    PutRequest,
    PutResult,
    RleContent,
)
from aap_assets_mask_content_runtime.api import create_app


class FakeStore:
    def __init__(self):
        self.calls = []
        self.reference = codec.build_rle_reference(
            RleContent(encoding="coco_rle", size=(2, 2), counts=(1, 3))
        )

    def put(self, request: PutRequest) -> PutResult:
        self.calls.append("put")
        return PutResult(reference=self.reference)

    def get(self, request: GetRequest) -> GetResult:
        self.calls.append("get")
        return GetResult(
            reference=request.reference,
            content=RleContent(encoding="coco_rle", size=(2, 2), counts=(1, 3)),
        )

    def list(self, request: ListRequest) -> CandidatePage:
        self.calls.append("list")
        return CandidatePage(items=(), cursor=None)

    def delete(self, request: DeleteRequest) -> DeleteResult:
        self.calls.append("delete")
        return DeleteResult(object_key=request.object_key, deleted=False)

    def check_ready(self):
        self.calls.append("ready")


def _rle_payload():
    return {"encoding": "coco_rle", "size": [2, 2], "counts": [1, 3]}


def test_process_roles_expose_only_their_two_operations():
    store = FakeStore()
    content = TestClient(create_app(store, "t" * 40, "content"))
    cleanup = TestClient(create_app(store, "c" * 40, "cleanup"))
    reference = store.reference.model_dump(mode="json")

    assert (
        content.post(
            "/v1/mask-content/put",
            json={"content": _rle_payload(), "storage_encoding": "identity"},
            headers={"Authorization": "Bearer " + "t" * 40},
        ).status_code
        == 200
    )
    assert content.post(
        "/v1/mask-content/list",
        json={"limit": 10},
        headers={"Authorization": "Bearer " + "t" * 40},
    ).json() == {
        "schema": CONTENT_SCHEMA,
        "ok": False,
        "error": {"code": "permission_denied"},
    }
    assert (
        cleanup.post(
            "/v1/mask-content/get",
            json={"reference": reference},
            headers={"Authorization": "Bearer " + "c" * 40},
        ).status_code
        == 403
    )
    assert store.calls == ["put"]


def test_service_token_is_required_before_dispatch():
    store = FakeStore()
    client = TestClient(create_app(store, "t" * 40, "content"))

    response = client.post(
        "/v1/mask-content/get",
        json={"reference": store.reference.model_dump(mode="json")},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "permission_denied"
    assert store.calls == []


def test_bad_payload_and_storage_failure_are_closed_envelopes():
    store = FakeStore()

    def fail(_request):
        raise RuntimeError("secret endpoint and access key must not escape")

    store.get = fail
    client = TestClient(create_app(store, "t" * 40, "content"))
    headers = {"Authorization": "Bearer " + "t" * 40}
    bad = client.post(
        "/v1/mask-content/put", json={"content": {"bad": True}}, headers=headers
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_request"

    unavailable = client.post(
        "/v1/mask-content/get",
        json={"reference": store.reference.model_dump(mode="json")},
        headers=headers,
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "schema": CONTENT_SCHEMA,
        "ok": False,
        "error": {"code": "storage_unavailable"},
    }


def test_runtime_enforces_request_budget_before_json_parse():
    from aap_assets_mask_content.contract import MAX_REQUEST_BYTES

    store = FakeStore()
    client = TestClient(create_app(store, "t" * 40, "content"))
    response = client.post(
        "/v1/mask-content/put",
        content=b"x" * (MAX_REQUEST_BYTES + 1),
        headers={
            "Authorization": "Bearer " + "t" * 40,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "resource_limit"
    assert store.calls == []


def test_health_routes_and_bad_role_configuration():
    store = FakeStore()
    client = TestClient(create_app(store, "t" * 40, "content"))
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert store.calls == ["ready"]
    try:
        create_app(store, "t" * 40, "worker")
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported process role was accepted")
