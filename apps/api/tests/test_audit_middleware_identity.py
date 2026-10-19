from __future__ import annotations

import uuid
from types import SimpleNamespace

from starlette.requests import Request

from app.middleware.audit import _build_payload


def _request(*, state: dict, authorization: str | None = None) -> Request:
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/example",
            "raw_path": b"/api/v1/example",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "state": state,
        }
    )


def test_audit_uses_verified_identity_principal():
    actor_id = uuid.uuid4()
    request = _request(
        state={
            "identity_principal": SimpleNamespace(actor_id=actor_id, role="employee")
        },
        authorization="Bearer stale-token-claim",
    )

    payload = _build_payload(request, 200)

    assert payload["actor_id"] == str(actor_id)
    assert payload["actor_role"] == "employee"


def test_audit_does_not_infer_actor_from_bearer_claims():
    request = _request(state={}, authorization="Bearer not-a-verified-token")

    payload = _build_payload(request, 401)

    assert payload["actor_id"] is None
    assert payload["actor_role"] is None
