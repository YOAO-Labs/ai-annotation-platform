from __future__ import annotations

import builtins
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from unittest.mock import AsyncMock

from aap_assets_input_manifests import contract as c
from aap_assets_input_manifests.client import HTTP_OPERATIONS, ManifestClient
from aap_assets_input_manifests.http_contract import http_document
from aap_assets_input_manifests_runtime.api import install_manifest_routes
from test_input_manifest_runtime import fixture_values

TOKEN = "manifest-control-test-" + "x" * 32


def sealed_header(header):
    return header.model_copy(
        update={
            "prepared_count": header.selected_count,
            "status": "sealed",
            "ready_count": 1,
            "failed_count": 0,
            "sealed_digest": "e" * 64,
            "sealed_at": datetime.now(timezone.utc),
        }
    )


class FakeResponse:
    def __init__(self, raw):
        self.raw = raw
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aiter_raw(self):
        yield self.raw


class FakeTransport:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.raw)


@pytest.mark.asyncio
async def test_injected_transport_needs_no_httpx_client_and_uses_only_control_paths():
    _source, header, member, page, prepare = fixture_values(state="preparing")
    body = c.json_bytes({"schema": c.MANIFEST_SCHEMA, "ok": True, "data": header})
    transport = FakeTransport(body)
    client = ManifestClient("https://media.example", TOKEN, client=transport)

    result = await client.record(
        c.ManifestScope(
            job_id=header.job_id,
            actor_id=header.actor_id,
            project_id=header.project_id,
        )
    )

    assert result == header
    assert set(HTTP_OPERATIONS) == {"record", "page", "prepare", "seal"}
    method, url, kwargs = transport.calls[0]
    assert method == "POST"
    assert url == "https://media.example/v1/input-manifests/record"
    assert kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
    assert json.loads(kwargs["content"])["job_id"] == str(header.job_id)
    assert member.state == "preparing" and page.members[0].state == "preparing"
    assert prepare.task_id == member.task_id
    assert not any("capture" in path for path in HTTP_OPERATIONS)


@pytest.mark.asyncio
async def test_manifest_client_rejects_invalid_scope_and_oversized_body(monkeypatch):
    _source, header, _member, _page, _prepare = fixture_values()
    crossed = header.model_copy(update={"job_id": uuid4()})
    transport = FakeTransport(
        c.json_bytes({"schema": c.MANIFEST_SCHEMA, "ok": True, "data": crossed})
    )
    client = ManifestClient("http://media.example", TOKEN, client=transport)
    request = c.ManifestScope(
        job_id=header.job_id,
        actor_id=header.actor_id,
        project_id=header.project_id,
    )
    with pytest.raises(c.ManifestFailure, match="invalid_response"):
        await client.record(request)

    transport = FakeTransport(b"")
    client = ManifestClient("http://media.example", TOKEN, client=transport)
    monkeypatch.setattr(c, "REQUEST_BYTES", 1)
    with pytest.raises(c.ManifestFailure, match="resource_budget_exceeded"):
        await client.record(request)
    assert transport.calls == []


@pytest.mark.asyncio
async def test_four_authenticated_routes_round_trip_and_do_not_expose_capture():
    _source, header, member, page, prepare_request = fixture_values(state="preparing")
    scope = c.ManifestScope(
        job_id=header.job_id,
        actor_id=header.actor_id,
        project_id=header.project_id,
    )
    seal_request = c.SealManifestRequest(
        **scope.model_dump(), selection_digest=header.selection_digest
    )
    receipt = c.ManifestMemberReceipt(
        job_id=header.job_id,
        actor_id=header.actor_id,
        project_id=header.project_id,
        selection_digest=header.selection_digest,
        member=member,
    )
    control = SimpleNamespace(
        record=AsyncMock(return_value=header),
        page=AsyncMock(return_value=page),
        prepare=AsyncMock(return_value=receipt),
        seal=AsyncMock(return_value=sealed_header(header)),
    )
    app = install_manifest_routes(FastAPI(), control, TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://media.example"
    ) as http:
        client = ManifestClient("http://media.example", TOKEN, client=http)
        assert await client.record(scope) == header
        assert (
            await client.page(
                c.ManifestPageRequest(
                    **scope.model_dump(),
                    selection_digest=header.selection_digest,
                    page_size=1,
                )
            )
            == page
        )
        assert await client.prepare(prepare_request) == receipt
        assert (await client.seal(seal_request)).status == "sealed"
        unauthorized = await http.post(
            "/v1/input-manifests/record", json=scope.model_dump(mode="json")
        )
        assert unauthorized.status_code == 401
        capture = await http.post(
            "/v1/input-manifests/capture",
            headers={"Authorization": "Bearer " + TOKEN},
            json={},
        )
        assert capture.status_code == 404
        too_large = await http.post(
            "/v1/input-manifests/record",
            headers={"Authorization": "Bearer " + TOKEN},
            content=b" " * (c.REQUEST_BYTES + 1),
        )
        assert too_large.status_code == 413

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert {
        "/v1/input-manifests/record",
        "/v1/input-manifests/page",
        "/v1/input-manifests/prepare",
        "/v1/input-manifests/seal",
    } <= paths
    assert control.record.await_count == 1
    assert control.page.await_count == 1
    assert control.prepare.await_count == 1
    assert control.seal.await_count == 1


def test_openapi_snapshot_is_exactly_the_four_control_routes():
    document = http_document()
    package = Path(c.__file__).parent
    assert json.loads((package / "openapi.snapshot.json").read_text()) == document
    assert set(document["paths"]) == {
        "/v1/input-manifests/record",
        "/v1/input-manifests/page",
        "/v1/input-manifests/prepare",
        "/v1/input-manifests/seal",
    }
    assert "/v1/input-manifests/capture" not in document["paths"]


@pytest.mark.asyncio
async def test_injected_client_path_does_not_import_httpx(monkeypatch):
    _source, header, _member, _page, _prepare = fixture_values()
    raw = c.json_bytes({"schema": c.MANIFEST_SCHEMA, "ok": True, "data": header})
    original_import = builtins.__import__

    def reject_httpx(name, *args, **kwargs):
        if name == "httpx":
            raise AssertionError("injected transport must not require HTTPX")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_httpx)
    client = ManifestClient("http://media.example", TOKEN, client=FakeTransport(raw))
    result = await client.record(
        c.ManifestScope(
            job_id=header.job_id,
            actor_id=header.actor_id,
            project_id=header.project_id,
        )
    )
    assert result == header
