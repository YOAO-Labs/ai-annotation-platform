"""Bounded authenticated HTTP contract for retained video input control."""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs import http_contract as h
from aap_assets_video_inputs import values as v
from aap_assets_video_inputs.client import VideoInputClient
from aap_assets_video_inputs_runtime.api import install_video_input_routes
from video_input_fixtures import (
    CURRENT_ACTOR,
    frame,
    manifest,
    member,
    record,
    time_entries,
)

TOKEN = "video-control-test-" + "x" * 40


def _fixtures():
    header = manifest(sealed=True)
    selected = member("ready")
    video = record("ready")
    frame_record = frame("ready")
    reference = v.FrameReference(
        plan=frame_record.plan,
        entry=frame_record.entry,
        blob=frame_record.blob,
        url=(
            "https://objects.example/accepted-inputs/"
            + frame_record.blob.key
            + "?"
            + urlencode({"versionId": frame_record.blob.version_id})
        ),
        expires_in=300,
    )
    entries = tuple(time_entries())
    times = v.FrameTimePage(
        plan=video.plan,
        cursor=0,
        next_cursor=None,
        entries=entries,
    )
    page = c.VideoManifestPage(
        manifest=header,
        cursor=0,
        next_cursor=None,
        members=(selected,),
    )
    receipt = c.VideoMemberReceipt(manifest=header, member=selected)
    return SimpleNamespace(
        header=header,
        member=selected,
        manifest_page=page,
        receipt=receipt,
        video=video,
        times=times,
        frame=frame_record,
        reference=reference,
    )


def _requests(items):
    header = items.header
    scope = c.VideoManifestScope(
        job_id=header.job_id,
        actor_id=CURRENT_ACTOR,
        project_id=header.project_id,
    )
    return {
        "manifest": scope,
        "members": c.VideoManifestPageRequest(
            **scope.model_dump(),
            selection_digest=header.selection_digest,
            cursor=0,
            page_size=100,
        ),
        "prepare": c.PrepareVideoMemberRequest(
            **scope.model_dump(),
            selection_digest=header.selection_digest,
            ordinal=items.member.ordinal,
            task_id=items.member.task.task_id,
        ),
        "seal": c.SealVideoManifestRequest(
            **scope.model_dump(), selection_digest=header.selection_digest
        ),
        "input": c.ReadVideoInputRequest(
            input_id=items.video.input_id,
            job_id=items.video.job_id,
            task_id=items.video.task.task_id,
            actor_id=CURRENT_ACTOR,
        ),
        "verify": c.LockVideoInputRequest(
            input_id=items.video.input_id,
            job_id=items.video.job_id,
            task_id=items.video.task.task_id,
            actor_id=CURRENT_ACTOR,
            plan_digest=items.video.plan.plan_digest,
        ),
        "times": v.FramePageRequest(
            actor_id=CURRENT_ACTOR,
            job_id=items.video.job_id,
            task_id=items.video.task.task_id,
            input_id=items.video.input_id,
            plan_digest=items.video.plan.plan_digest,
            cursor=0,
            page_size=v.PAGE_SIZE,
        ),
        "require-frame": c.VideoFrameRequest(
            actor_id=CURRENT_ACTOR,
            job_id=items.video.job_id,
            task_id=items.video.task.task_id,
            input_id=items.video.input_id,
            plan_digest=items.video.plan.plan_digest,
            ordinal=items.frame.entry.ordinal,
        ),
    }


class FakeControl:
    def __init__(self, items):
        self.items = items
        self.calls = []

    async def _call(self, operation, request):
        self.calls.append((operation, request))
        return {
            "manifest": self.items.header,
            "members": self.items.manifest_page,
            "prepare": self.items.receipt,
            "seal": self.items.header,
            "input": self.items.video,
            "verify": self.items.video,
            "times": self.items.times,
            "require_frame": self.items.frame,
            "frame": self.items.frame,
            "reference": self.items.reference,
        }[operation]

    async def manifest(self, request):
        return await self._call("manifest", request)

    async def members(self, request):
        return await self._call("members", request)

    async def prepare(self, request):
        return await self._call("prepare", request)

    async def seal(self, request):
        return await self._call("seal", request)

    async def input(self, request):
        return await self._call("input", request)

    async def verify(self, request):
        return await self._call("verify", request)

    async def times(self, request):
        return await self._call("times", request)

    async def require_frame(self, request):
        return await self._call("require_frame", request)

    async def frame(self, request):
        return await self._call("frame", request)

    async def reference(self, request):
        return await self._call("reference", request)


class FakeResponse:
    def __init__(self, raw, *, status=200, chunks=None):
        self.raw = raw
        self.status_code = status
        self.chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aiter_raw(self):
        for chunk in self.chunks or (self.raw,):
            yield chunk


class FakeTransport:
    def __init__(self, raw, *, status=200, chunks=None):
        self.raw, self.status, self.chunks = raw, status, chunks
        self.calls = []

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.raw, status=self.status, chunks=self.chunks)


def _envelope(result, *, schema=h.CONTROL_SCHEMA):
    return h.json_bytes(
        {
            "schema": schema,
            "ok": True,
            "data": result.model_dump(mode="json"),
        }
    )


def _error_envelope(code):
    return h.json_bytes(
        {"schema": h.CONTROL_SCHEMA, "ok": False, "error": {"code": code}}
    )


@pytest.mark.asyncio
async def test_http_client_and_server_round_trip_all_ten_private_routes():
    items = _fixtures()
    requests = _requests(items)
    control = FakeControl(items)
    app = install_video_input_routes(FastAPI(), control, TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://media.example"
    ) as http:
        async with VideoInputClient(
            "http://media.example", TOKEN, client=http
        ) as client:
            assert await client.manifest(requests["manifest"]) == items.header
            assert await client.members(requests["members"]) == items.manifest_page
            assert await client.prepare(requests["prepare"]) == items.receipt
            assert await client.seal(requests["seal"]) == items.header
            assert await client.input(requests["input"]) == items.video
            assert await client.verify(requests["verify"]) == items.video
            assert await client.times(requests["times"]) == items.times
            assert await client.require_frame(requests["require-frame"]) == items.frame
            assert await client.frame(requests["require-frame"]) == items.frame
            assert await client.reference(requests["require-frame"]) == items.reference
            hidden = await http.post(
                "/v1/video-inputs/capture",
                headers={"Authorization": "Bearer " + TOKEN},
                json={},
            )
            assert hidden.status_code == 404

    expected = {
        "manifest",
        "members",
        "prepare",
        "seal",
        "input",
        "verify",
        "times",
        "require-frame",
        "frame",
        "reference",
    }
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert set(h.HTTP_OPERATIONS) == expected
    assert {f"{h.HTTP_PREFIX}/{operation}" for operation in expected} <= paths
    assert [operation for operation, _ in control.calls] == [
        "manifest",
        "members",
        "prepare",
        "seal",
        "input",
        "verify",
        "times",
        "require_frame",
        "frame",
        "reference",
    ]
    assert all(request.actor_id == CURRENT_ACTOR for _, request in control.calls)
    assert items.video.actor_id != CURRENT_ACTOR


def test_http_control_schema_and_openapi_snapshot_are_frozen_and_closed():
    package = Path(h.__file__).parent
    contract = h.contract_document()
    document = h.http_document()
    assert json.loads((package / "control.schema.json").read_text()) == contract
    assert json.loads((package / "openapi.snapshot.json").read_text()) == document
    assert contract["$id"] == h.CONTROL_SCHEMA
    assert set(contract["x-http"]) == set(h.HTTP_OPERATIONS)
    for request_type, response_type in h.HTTP_OPERATIONS.values():
        assert request_type.__name__ in contract["$defs"]
        assert response_type.__name__ in contract["$defs"]
    assert set(document["paths"]) == {
        f"{h.HTTP_PREFIX}/{operation}" for operation in h.HTTP_OPERATIONS
    }
    assert len(document["paths"]) == 10
    exposed_names = set(contract["x-http"]) | set(document["paths"])
    assert not any(
        name.endswith("/capture")
        or "read_accepted_video_input_work" in name
        or "claim_accepted_video" in name
        for name in exposed_names
    )

    def check_refs(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "$ref":
                    assert item.startswith("#/components/schemas/")
                    assert item.rsplit("/", 1)[1] in document["components"]["schemas"]
                else:
                    check_refs(item)
        elif isinstance(value, list):
            for item in value:
                check_refs(item)

    check_refs(document)


@pytest.mark.asyncio
async def test_reference_reply_is_bound_to_requested_input_and_plan_but_keeps_origin_actor():
    items = _fixtures()
    request = _requests(items)["require-frame"]
    valid = await VideoInputClient(
        "http://media.example",
        TOKEN,
        client=FakeTransport(_envelope(items.reference)),
    ).reference(request)
    assert valid == items.reference
    assert valid.plan.input.actor_id != request.actor_id

    changed = items.reference.model_copy(
        update={
            "plan": items.reference.plan.model_copy(
                update={
                    "input": items.reference.plan.input.model_copy(
                        update={"input_id": uuid4()}
                    )
                }
            )
        }
    )
    client = VideoInputClient(
        "http://media.example", TOKEN, client=FakeTransport(_envelope(changed))
    )
    with pytest.raises(c.VideoInputFailure, match="invalid_response"):
        await client.reference(request)


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", uuid4()),
        ("task_id", uuid4()),
        ("input_id", uuid4()),
        ("plan_digest", "d" * 64),
        ("ordinal", 2),
    ],
)
@pytest.mark.asyncio
async def test_reference_rejects_each_crossed_request_identity(field, value):
    items = _fixtures()
    request = _requests(items)["require-frame"].model_copy(update={field: value})
    client = VideoInputClient(
        "http://media.example",
        TOKEN,
        client=FakeTransport(_envelope(items.reference)),
    )
    with pytest.raises(c.VideoInputFailure, match="invalid_response"):
        await client.reference(request)


@pytest.mark.parametrize("operation", ["manifest", "members", "prepare"])
@pytest.mark.asyncio
async def test_client_rejects_normalized_duplicate_manifest_membership(operation):
    items = _fixtures()
    result = {
        "manifest": items.header,
        "members": items.manifest_page,
        "prepare": items.receipt,
    }[operation]
    raw = json.loads(_envelope(result))
    header = raw["data"] if operation == "manifest" else raw["data"]["manifest"]
    header["selection"]["task_ids"] *= 2
    client = VideoInputClient(
        "http://media.example", TOKEN, client=FakeTransport(h.json_bytes(raw))
    )
    with pytest.raises(c.VideoInputFailure, match="invalid_response"):
        await getattr(client, operation)(_requests(items)[operation])


@pytest.mark.parametrize(
    "fault",
    [
        "schema",
        "extra_key",
        "nan",
        "unknown_error",
        "bad_version_url",
        "crossed_ordinal",
    ],
)
@pytest.mark.asyncio
async def test_client_rejects_malformed_or_crossed_responses_without_echoing(fault):
    items = _fixtures()
    request = _requests(items)["require-frame"]
    if fault == "schema":
        body = _envelope(items.reference, schema="other/v1")
    elif fault == "extra_key":
        envelope = json.loads(_envelope(items.reference))
        envelope["debug"] = "PRIVATE"
        body = json.dumps(envelope).encode()
    elif fault == "nan":
        body = b'{"schema":"aap-assets-video-input-control/v1","ok":true,"data":NaN}'
    elif fault == "unknown_error":
        body = h.json_bytes(
            {"schema": h.CONTROL_SCHEMA, "ok": False, "error": {"code": "secret"}}
        )
    elif fault == "bad_version_url":
        raw = json.loads(_envelope(items.reference))
        raw["data"]["url"] = "https://objects.example/" + items.reference.blob.key
        body = json.dumps(raw).encode()
    else:
        changed = items.reference.model_copy(
            update={"entry": items.reference.entry.model_copy(update={"ordinal": 2})}
        )
        body = _envelope(changed)
    client = VideoInputClient("http://media.example", TOKEN, client=FakeTransport(body))
    with pytest.raises(c.VideoInputFailure, match="invalid_response") as failure:
        await client.reference(request)
    assert "PRIVATE" not in str(failure.value)


@pytest.mark.asyncio
async def test_client_enforces_request_reply_budgets_and_redirects_fail_closed(
    monkeypatch,
):
    items = _fixtures()
    request = _requests(items)["manifest"]
    transport = FakeTransport(_envelope(items.header))
    client = VideoInputClient("http://media.example", TOKEN, client=transport)
    monkeypatch.setattr(v, "REQUEST_BYTES", 1)
    with pytest.raises(c.VideoInputFailure, match="resource_budget_exceeded"):
        await client.manifest(request)
    assert transport.calls == []
    monkeypatch.setattr(v, "REQUEST_BYTES", 65536)

    monkeypatch.setattr(v, "REPLY_BYTES", 5)
    oversized = VideoInputClient(
        "http://media.example",
        TOKEN,
        client=FakeTransport(b"", chunks=[b"123", b"456"]),
    )
    with pytest.raises(c.VideoInputFailure, match="resource_budget_exceeded"):
        await oversized.manifest(request)
    monkeypatch.setattr(v, "REPLY_BYTES", 2 * 1024 * 1024)

    redirected = FakeTransport(_envelope(items.header), status=307)
    client = VideoInputClient("http://media.example", TOKEN, client=redirected)
    with pytest.raises(c.VideoInputFailure, match="invalid_response"):
        await client.manifest(request)
    assert redirected.calls[0][2]["follow_redirects"] is False


@pytest.mark.asyncio
async def test_server_auth_body_limit_and_failures_do_not_echo_secrets(monkeypatch):
    items = _fixtures()
    control = FakeControl(items)
    app = install_video_input_routes(FastAPI(), control, TOKEN)
    request = _requests(items)["manifest"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://media.example"
    ) as client:
        denied = await client.post(
            f"{h.HTTP_PREFIX}/manifest",
            content=b"{}",
            headers={"Authorization": "Bearer SECRET"},
        )
        malformed = await client.post(
            f"{h.HTTP_PREFIX}/manifest",
            content=b'{"actor_id":"SECRET"}',
            headers={"Authorization": "Bearer " + TOKEN},
        )
        assert denied.status_code == 401
        assert malformed.status_code == 422
        assert "SECRET" not in denied.text + malformed.text

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(v, "REQUEST_BYTES", 16)
            oversized = await client.post(
                f"{h.HTTP_PREFIX}/manifest",
                content=b" " * 17,
                headers={"Authorization": "Bearer " + TOKEN},
            )
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "resource_budget_exceeded"

        async def unavailable(_request):
            raise c.VideoInputFailure("input_not_ready")

        control.manifest = unavailable
        failed = await client.post(
            f"{h.HTTP_PREFIX}/manifest",
            content=h.json_bytes(request),
            headers={"Authorization": "Bearer " + TOKEN},
        )
        assert failed.status_code == h.http_error_status("input_not_ready")
        assert failed.json()["error"]["code"] == "input_not_ready"

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(v, "REPLY_BYTES", 32)
            too_large = await client.post(
                f"{h.HTTP_PREFIX}/manifest",
                content=h.json_bytes(request),
                headers={"Authorization": "Bearer " + TOKEN},
            )
        assert too_large.status_code == 413
        assert too_large.json()["error"]["code"] == "resource_budget_exceeded"


@pytest.mark.asyncio
async def test_injected_client_path_does_not_import_httpx(monkeypatch):
    items = _fixtures()
    original_import = builtins.__import__

    def reject_httpx(name, *args, **kwargs):
        if name == "httpx":
            raise AssertionError("injected video client must not import HTTPX")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_httpx)
    client = VideoInputClient(
        "http://media.example",
        TOKEN,
        client=FakeTransport(_envelope(items.header)),
    )
    assert await client.manifest(_requests(items)["manifest"]) == items.header


@pytest.mark.asyncio
async def test_route_installation_does_not_resolve_a_lazy_control_proxy():
    items = _fixtures()

    class LazyControl:
        def __init__(self):
            self.ready = False
            self.resolved = []

        def __getattr__(self, name):
            self.resolved.append(name)
            if not self.ready:
                raise RuntimeError("control proxy is not initialized")
            return getattr(FakeControl(items), name)

    control = LazyControl()
    app = install_video_input_routes(FastAPI(), control, TOKEN)
    assert control.resolved == []
    control.ready = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://media.example"
    ) as http:
        client = VideoInputClient("http://media.example", TOKEN, client=http)
        assert await client.manifest(_requests(items)["manifest"]) == items.header
    assert control.resolved == ["manifest"]


@pytest.mark.parametrize(
    "base_url,token",
    [
        ("ftp://media.example", TOKEN),
        ("https://user:pass@media.example", TOKEN),
        ("https://:pass@media.example", TOKEN),
        ("https://media.example?x=1", TOKEN),
        ("https://media.example", "short"),
        ("https://media.example", "bad\ntoken" + "x" * 32),
    ],
)
def test_client_rejects_unsafe_origin_or_service_token(base_url, token):
    with pytest.raises(ValueError):
        VideoInputClient(base_url, token, client=FakeTransport(b""))
