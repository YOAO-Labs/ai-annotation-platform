from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from aap_assets_media import contract as c
from aap_assets_media.client import MediaClient
from aap_assets_media.http_contract import http_document
from aap_assets_media.transport import read_all_timetable
from aap_assets_media_runtime.api import create_app
from test_contract import source


@pytest.mark.asyncio
async def test_complete_paged_timetable_and_source_change_rejection():
    s = source()
    first = c.TimetablePage(
        source=s,
        snapshot_digest="b" * 64,
        total=3,
        after_frame=None,
        next_frame=1,
        complete=False,
        frames=[
            c.FrameEntry(frame_index=i, pts_ms=i * 40, is_keyframe=i == 0)
            for i in range(2)
        ],
    )

    class Pages:
        changed = False

        async def call(self, operation, request):
            if request.after_frame is None:
                return first
            return first.model_copy(
                update={
                    "after_frame": 1,
                    "next_frame": None,
                    "complete": True,
                    "frames": [
                        c.FrameEntry(frame_index=2, pts_ms=93, is_keyframe=False)
                    ],
                    "snapshot_digest": "c" * 64
                    if self.changed
                    else first.snapshot_digest,
                }
            )

    pages = Pages()
    request = c.TimetableRequest(
        actor_id=uuid4(), dataset_item_id=s.dataset_item_id, page_size=2
    )
    assert [row.pts_ms for row in await read_all_timetable(pages, request)] == [
        0,
        40,
        93,
    ]
    pages.changed = True
    with pytest.raises(c.MediaFailure, match="source_changed"):
        await read_all_timetable(pages, request)


@pytest.mark.asyncio
async def test_truncated_complete_page_is_rejected():
    s = source()

    class Pages:
        async def call(self, operation, request):
            return c.TimetablePage(
                source=s,
                snapshot_digest="b" * 64,
                total=2,
                after_frame=None,
                next_frame=None,
                complete=True,
                frames=[],
            )

    with pytest.raises(c.MediaFailure, match="invalid_response"):
        await read_all_timetable(
            Pages(),
            c.TimetableRequest(actor_id=uuid4(), dataset_item_id=s.dataset_item_id),
        )


@pytest.mark.asyncio
async def test_http_client_uses_authenticated_owner_and_rejects_substituted_source():
    s = source()

    class Control:
        async def call(self, name, request):
            return s

    app = create_app(Control(), "t" * 32)
    client = MediaClient(
        "http://media.local", "t" * 32, transport=httpx.ASGITransport(app=app)
    )
    request = c.ReadSourceRequest(actor_id=uuid4(), dataset_item_id=s.dataset_item_id)
    assert await client.call("read_media_source", request) == s
    with pytest.raises(c.MediaFailure, match="invalid_response"):
        await client.call(
            "read_media_source", request.model_copy(update={"dataset_item_id": uuid4()})
        )
    bad = MediaClient(
        "http://media.local", "wrong" * 8, transport=httpx.ASGITransport(app=app)
    )
    with pytest.raises(c.MediaFailure, match="permission_denied"):
        await bad.call("read_media_source", request)


@pytest.mark.asyncio
async def test_http_surface_never_exposes_worker_claims_or_request_errors():
    app = create_app(None, "t" * 32)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://media.local"
    ) as client:
        denied = await client.post(
            "/v1/commands/claim_video_media",
            json={},
            headers={"Authorization": "Bearer " + "t" * 32},
        )
        assert denied.status_code == 403
        invalid = await client.post(
            "/v1/commands/read_media_source",
            content='{"actor_id":"SECRET"}',
            headers={"Authorization": "Bearer " + "t" * 32},
        )
        assert invalid.status_code == 422
        assert "SECRET" not in invalid.text


@pytest.mark.asyncio
async def test_http_request_budget_rejects_before_any_owner_call(monkeypatch):
    monkeypatch.setattr(c, "REQUEST_BYTES", 32)
    app = create_app(None, "t" * 32)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://media.local"
    ) as client:
        response = await client.post(
            "/v1/commands/read_media_source",
            content=" " * 33,
            headers={"Authorization": "Bearer " + "t" * 32},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "resource_budget_exceeded"


def test_shipped_http_contract_contains_control_routes_and_closed_schema_refs():
    document = http_document()
    path = Path(__file__).parents[1] / "src/aap_assets_media/openapi.snapshot.json"
    assert json.loads(path.read_text()) == document
    assert "/v1/commands/claim_video_media" not in document["paths"]
    assert "/v1/commands/resolve_internal_media_source" in document["paths"]
    assert len(document["paths"]) == 22

    def inspect(value):
        if isinstance(value, dict):
            for name, item in value.items():
                if name == "$ref":
                    assert item.startswith("#/components/schemas/")
                    assert item.rsplit("/", 1)[1] in document["components"]["schemas"]
                else:
                    inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)

    inspect(document)


@pytest.mark.asyncio
async def test_service_token_is_sent_directly_even_when_a_proxy_is_configured(
    monkeypatch,
):
    s = source()
    reached = []
    body = c.json_bytes({"schema": c.MEDIA_SCHEMA, "ok": True, "data": s})

    async def origin(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        reached.append("origin")
        assert b"Authorization: Bearer " + b"t" * 32 in headers
        length = next(
            int(line.split(b":", 1)[1])
            for line in headers.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        )
        await reader.readexactly(length)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def proxy(reader, writer):
        reached.append("proxy")
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(origin, "127.0.0.1", 0) as server:
        async with await asyncio.start_server(proxy, "127.0.0.1", 0) as proxy_server:
            port = server.sockets[0].getsockname()[1]
            proxy_port = proxy_server.sockets[0].getsockname()[1]
            monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
            monkeypatch.setenv("NO_PROXY", "")
            monkeypatch.setenv("no_proxy", "")
            client = MediaClient(f"http://127.0.0.1:{port}", "t" * 32)
            assert (
                await client.call(
                    "read_media_source",
                    c.ReadSourceRequest(
                        actor_id=uuid4(), dataset_item_id=s.dataset_item_id
                    ),
                )
                == s
            )
    assert reached == ["origin"]
