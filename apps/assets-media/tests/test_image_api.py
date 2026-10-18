from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from aap_assets_images import contract as c
from aap_assets_images.transport import ImageSourceReference
from aap_assets_images_runtime.api import error_status, install_image_routes


def source(item_id=None):
    item_id = item_id or uuid4()
    return c.ImageSource(
        owner={"kind": "dataset_item", "id": item_id},
        task_id=None,
        project_id=uuid4(),
        dataset_id=uuid4(),
        item_id=item_id,
        source_generation=1,
        source_snapshot="a" * 64,
        object_identity={"etag": "etag", "byte_size": 12},
        source_drifted=False,
        upload_ready=True,
        file_path="source/image.png",
        file_name="image.png",
        file_type="image",
        bucket_kind="datasets",
        file_size=12,
        content_hash=None,
        width=2,
        height=1,
        thumbnail_path=None,
        blurhash=None,
        thumbnail_source_generation=None,
        thumbnail_error=None,
    )


class FakeControl:
    def __init__(self, current):
        self.current = current
        self.calls = []
        self.source_error = None

    async def call(self, operation, request):
        self.calls.append((operation, request))
        if operation == "read_image_source":
            return self.current
        if operation == "claim_image_media":
            raise AssertionError("HTTP exposed image worker capability")
        raise c.ImageFailure("invalid_request")

    async def source_reference(self, request):
        if self.source_error:
            raise c.ImageFailure(self.source_error)
        return ImageSourceReference(
            actor_id=request.actor_id,
            source=self.current,
            original_url="https://images.local/source",
            thumbnail_url=None,
            expires_in=300,
        )

    async def raw_source_reference(self, request):
        return await self.source_reference(request)


def make_app(control, token):
    app = FastAPI()

    @app.get("/health/live")
    async def existing_health():
        return {"status": "existing"}

    install_image_routes(app, control, token)
    return app


@pytest.mark.asyncio
async def test_installs_on_existing_app_and_serves_authenticated_image_source():
    current = source()
    control = FakeControl(current)
    token = "i" * 40
    app = make_app(control, token)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://media.local"
    ) as client:
        health = await client.get("/health/live")
        assert health.json() == {"status": "existing"}
        reply = await client.post(
            "/v1/images/commands/read_image_source",
            json={"actor_id": str(uuid4()), "dataset_item_id": str(current.item_id)},
            headers={"Authorization": "Bearer " + token},
        )
    assert reply.status_code == 200
    assert reply.json()["schema"] == c.IMAGE_SCHEMA
    assert reply.json()["ok"] is True


@pytest.mark.asyncio
async def test_auth_request_limit_and_worker_command_are_closed():
    current = source()
    token = "i" * 40
    app = make_app(FakeControl(current), token)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://media.local"
    ) as client:
        unauthorized = await client.post(
            "/v1/images/commands/read_image_source",
            content=b"{}",
            headers={"Authorization": "Bearer " + "secret"},
        )
        worker = await client.post(
            "/v1/images/commands/claim_image_media",
            json={"worker_id": str(uuid4())},
            headers={"Authorization": "Bearer " + token},
        )
        invalid = await client.post(
            "/v1/images/commands/read_image_source",
            content='{"actor_id":"SECRET"}',
            headers={"Authorization": "Bearer " + token},
        )
    assert unauthorized.status_code == 401
    assert "secret" not in unauthorized.text
    assert worker.status_code == 403
    assert invalid.status_code == 422
    assert "SECRET" not in invalid.text


@pytest.mark.asyncio
async def test_request_budget_and_source_not_ready_status():
    current = source()
    control = FakeControl(current)
    control.source_error = "source_not_ready"
    token = "i" * 40
    app = make_app(control, token)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://media.local"
    ) as client:
        source_not_ready = await client.post(
            "/v1/images/source-reference",
            json={"actor_id": str(uuid4()), "dataset_item_id": str(current.item_id)},
            headers={"Authorization": "Bearer " + token},
        )
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(c, "REQUEST_BYTES", 32)
            oversized = await client.post(
                "/v1/images/commands/read_image_source",
                content=b" " * 33,
                headers={"Authorization": "Bearer " + token},
            )
        assert error_status("source_not_ready") == 503
    assert source_not_ready.status_code == 503
    assert source_not_ready.json()["error"]["code"] == "source_not_ready"
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "resource_budget_exceeded"


@pytest.mark.asyncio
async def test_reference_scope_failure_is_not_returned_as_a_success():
    requested = source()
    control = FakeControl(source(uuid4()))
    token = "i" * 40
    app = make_app(control, token)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://media.local"
    ) as client:
        response = await client.post(
            "/v1/images/source-reference",
            json={"actor_id": str(uuid4()), "dataset_item_id": str(requested.item_id)},
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 502
    assert response.json()["ok"] is False
    assert response.json()["error"]["code"] == "invalid_response"


def test_retry_error_statuses_are_explicit():
    assert error_status("retry_cooldown") == 429
    assert error_status("already_ready") == 409
    assert error_status("not_eligible") == 409
    assert error_status("not_image") == 422
