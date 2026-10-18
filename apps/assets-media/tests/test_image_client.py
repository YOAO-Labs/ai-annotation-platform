from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from aap_assets_images import contract as c
from aap_assets_images.client import ImageClient
from aap_assets_images.transport import (
    ImageSourceReference,
    ImageSourceReferences,
    ImageSourceReferencesRequest,
)
from aap_assets_images_runtime.api import install_image_routes


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


class ReferenceControl:
    def __init__(self, *, split=False, omit=False, duplicate=False):
        self.split = split
        self.omit = omit
        self.duplicate = duplicate
        self.calls = []

    async def call(self, operation, request):
        if operation == "claim_image_media":
            raise AssertionError("worker capability reached the control")
        raise c.ImageFailure("invalid_request")

    def _reference(self, request):
        current = source(request.dataset_item_id)
        return ImageSourceReference(
            actor_id=request.actor_id,
            source=current,
            original_url=f"https://image.local/source/{current.item_id}",
            thumbnail_url=None,
            expires_in=60,
        )

    async def source_reference(self, request):
        return self._reference(request)

    async def raw_source_reference(self, request):
        return self._reference(request)

    async def source_references(self, request):
        self.calls.append(list(request.requests))
        if self.split and len(request.requests) > 4:
            raise c.ImageFailure("resource_budget_exceeded")
        references = [self._reference(item) for item in request.requests]
        if self.omit:
            references = references[:-1]
        if self.duplicate and references:
            references[-1] = references[0]
        return ImageSourceReferences(references=references)


def image_app(control, token):
    app = FastAPI()
    install_image_routes(app, control, token)
    return app


@pytest.mark.asyncio
async def test_client_uses_image_namespace_and_refuses_worker_capabilities():
    token = "i" * 40
    app = image_app(ReferenceControl(), token)
    client = ImageClient(
        "http://image.local",
        token,
        transport=httpx.ASGITransport(app=app),
    )
    request = c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=uuid4())
    result = await client.source_reference(request)
    assert result.source.item_id == request.dataset_item_id
    with pytest.raises(c.ImageFailure, match="invalid_request"):
        await client.call(
            "claim_image_media", c.ClaimImageMediaRequest(worker_id=uuid4())
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_preview", [True, False])
async def test_schedule_client_preserves_dimensions_preview_intent(reply_preview):
    current = source()
    owner = current.owner
    operation_id = uuid4()
    request = c.ScheduleImageMediaRequest(
        operation_id=operation_id,
        owner_kind=owner.kind,
        owner_id=owner.id,
        spec=c.DimensionsWorkSpec(
            expected_source_snapshot=current.source_snapshot,
            generate_preview=True,
        ),
        force=False,
    )
    accepted = c.AcceptedImageWork(
        operation_id=operation_id,
        source=current,
        work=c.ImageWorkReference(
            job_id=uuid4(),
            generation=1,
            owner=owner,
            source_generation=current.source_generation,
            kind="dimensions",
            status="pending",
        ),
        spec=c.DimensionsWorkSpec(
            expected_source_snapshot=current.source_snapshot,
            policy=request.spec.policy,
            generate_preview=reply_preview,
        ),
    )

    async def reply(http_request):
        assert http_request.url.path == "/v1/images/commands/schedule_image_media"
        return httpx.Response(
            200,
            content=c.json_bytes(
                {
                    "schema": c.IMAGE_SCHEMA,
                    "ok": True,
                    "data": accepted.model_dump(mode="json", by_alias=True),
                }
            ),
        )

    client = ImageClient(
        "http://image.local",
        "i" * 40,
        transport=httpx.MockTransport(reply),
    )
    if reply_preview:
        assert await client.call("schedule_image_media", request) == accepted
    else:
        with pytest.raises(c.ImageFailure, match="invalid_response"):
            await client.call("schedule_image_media", request)


@pytest.mark.asyncio
async def test_source_reference_batch_splits_and_preserves_every_input_order():
    token = "i" * 40
    control = ReferenceControl(split=True)
    app = image_app(control, token)
    client = ImageClient(
        "http://image.local",
        token,
        transport=httpx.ASGITransport(app=app),
    )
    requests = [
        c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=uuid4())
        for _ in range(52)
    ]
    response = await client.source_references(
        ImageSourceReferencesRequest(requests=requests)
    )
    assert [item.source.item_id for item in response.references] == [
        item.dataset_item_id for item in requests
    ]
    assert len(control.calls[0]) == 50
    assert len(control.calls[1]) in {25, 26}
    assert max(map(len, control.calls[1:])) <= 50
    assert sum(len(batch) for batch in control.calls[1:]) >= 52


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["omit", "duplicate"])
async def test_source_reference_client_rejects_provider_omissions_and_duplicates(mode):
    token = "i" * 40
    control = ReferenceControl(**{mode: True})
    app = image_app(control, token)
    client = ImageClient(
        "http://image.local",
        token,
        transport=httpx.ASGITransport(app=app),
    )
    requests = [
        c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=uuid4())
        for _ in range(2)
    ]
    with pytest.raises(c.ImageFailure, match="invalid_response"):
        await client.source_references(ImageSourceReferencesRequest(requests=requests))


@pytest.mark.asyncio
async def test_client_request_and_reply_budgets_are_checked_before_decode(monkeypatch):
    current = source()
    request = c.ReadImageSourceRequest(
        actor_id=uuid4(), dataset_item_id=current.item_id
    )
    client = ImageClient("http://image.local", "i" * 40)
    monkeypatch.setattr(c, "REQUEST_BYTES", 1)
    with pytest.raises(c.ImageFailure, match="resource_budget_exceeded"):
        await client.source_reference(request)

    body = c.json_bytes(
        {
            "schema": c.IMAGE_SCHEMA,
            "ok": True,
            "data": ImageSourceReference(
                actor_id=request.actor_id,
                source=current,
                original_url="https://image.local/source",
                thumbnail_url=None,
                expires_in=60,
            ),
        }
    )

    async def oversized(_request):
        return httpx.Response(200, content=body + b" " * 50)

    client = ImageClient(
        "http://image.local",
        "i" * 40,
        transport=httpx.MockTransport(oversized),
    )
    monkeypatch.setattr(c, "REQUEST_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(c, "REPLY_BYTES", len(body) + 1)
    with pytest.raises(c.ImageFailure, match="resource_budget_exceeded"):
        await client.source_reference(request)


@pytest.mark.asyncio
async def test_service_token_is_not_forwarded_through_proxy_or_redirect(monkeypatch):
    token = "i" * 40
    current = source()
    redirect_hits = []
    proxy_hits = []
    origin_hits = []

    async def read_request(reader):
        headers = await reader.readuntil(b"\r\n\r\n")
        content_length = next(
            int(line.split(b":", 1)[1])
            for line in headers.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        )
        await reader.readexactly(content_length)
        return headers

    async def attacker(reader, writer):
        redirect_hits.append(await read_request(reader))
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(attacker, "127.0.0.1", 0) as attacker_server:
        attacker_port = attacker_server.sockets[0].getsockname()[1]

        async def proxy(reader, writer):
            proxy_hits.append(await read_request(reader))
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(proxy, "127.0.0.1", 0) as proxy_server:
            proxy_port = proxy_server.sockets[0].getsockname()[1]

            async def origin(reader, writer):
                headers = await read_request(reader)
                origin_hits.append(headers)
                writer.write(
                    b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:"
                    + str(attacker_port).encode()
                    + b"/steal\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            async with await asyncio.start_server(
                origin, "127.0.0.1", 0
            ) as origin_server:
                origin_port = origin_server.sockets[0].getsockname()[1]
                monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
                monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
                monkeypatch.setenv("NO_PROXY", "")
                monkeypatch.setenv("no_proxy", "")
                client = ImageClient(f"http://127.0.0.1:{origin_port}", token)
                with pytest.raises(c.ImageFailure):
                    await client.source_reference(
                        c.ReadImageSourceRequest(
                            actor_id=uuid4(), dataset_item_id=current.item_id
                        )
                    )
    assert len(origin_hits) == 1
    assert not proxy_hits
    assert not redirect_hits
    assert b"Authorization: Bearer " + token.encode() in origin_hits[0]
