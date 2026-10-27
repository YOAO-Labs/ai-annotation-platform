"""Bounded mocked transport tests; no database or provider is opened."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request

from app.api.v1.media_cache import read_media_cache
from app.services import media_cache_response as service, media_cache_urls
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.managed_original_retirement import managed_storage_identity

RAW = bytes(range(256)) * 4
SHA = hashlib.sha256(RAW).hexdigest()
ETAG = '"provider-version-one"'


@pytest.fixture(autouse=True)
def isolated_admission(monkeypatch):
    # Each pytest function owns its event loop; production keeps one API loop.
    monkeypatch.setattr(service, "_slots", asyncio.BoundedSemaphore(4))
    monkeypatch.setattr(service, "_waiting", threading.BoundedSemaphore(64))


def reference(identifier, *, registered=True, family="thumbnails"):
    endpoint = (
        "http://registered.invalid"
        if registered
        else service.media_cache_storage._runtime_endpoint()
    )
    root = "team/_aap_managed/media-cache/" if registered else ""
    return {
        "schema": 2,
        "purpose": "media-cache",
        "location_id": str(uuid.uuid4() if registered else LEGACY_LOCATION_ID),
        "config_revision": 3 if registered else 1,
        "binding_generation": 2 if registered else 1,
        "endpoint_url": endpoint,
        "bucket": "saved-media-bucket",
        "prefix": root + f"{family}/objects/{identifier}/",
        "storage_identity": managed_storage_identity(endpoint, "saved-media-bucket"),
        "key_prefix": root,
    }


class Body(BytesIO):
    def __init__(self, data, harness):
        super().__init__(data)
        self.harness = harness
        self.requested = []
        self.started, self.resume = threading.Event(), threading.Event()

    def read(self, size=-1):
        self.requested.append(size)
        if self.harness.block_read:
            self.started.set()
            assert self.resume.wait(5)
        return super().read(size)

    def close(self):
        self.harness.events.append("body-close")
        if self.harness.fail_body_close:
            raise RuntimeError("private provider close failure")
        super().close()


class Permit:
    revision = 7

    def __init__(self, harness):
        self.harness = harness
        self.id = uuid.uuid4()
        self.settled = False
        self.unsettled = False

    async def __aexit__(self, kind, error, tb):
        self.harness.events.append("permit-exit")
        self.unsettled = bool(getattr(error, "storage_operation_unsettled", False))
        self.settled = not self.unsettled


class Harness:
    def __init__(self, monkeypatch, *, registered=True, saved_version="version-1"):
        self.identifier = uuid.uuid4()
        self.ref = reference(self.identifier, registered=registered)
        self.row = SimpleNamespace(
            id=self.identifier,
            state="ready",
            storage_ref=self.ref,
            owner_kind="task_thumbnail",
            uncertain_write=False,
            sha256=SHA,
            size_bytes=len(RAW),
            etag=ETAG,
            version_id=saved_version,
        )
        self.events, self.heads, self.gets, self.targets = [], [], [], []
        self.transaction = False
        self.fail_body_close = self.fail_client_close = self.block_read = False
        self.block_open = False
        self.open_started, self.open_resume = threading.Event(), threading.Event()
        self.changed_etag = self.changed_version = self.changed_size = False
        self.body = None
        self.permit = Permit(self)
        self.db_reads = 0
        harness = self

        class Database:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                harness.transaction = False
                harness.events.append("session-close")

            async def get(self, model, identifier, **kwargs):
                harness.transaction = True
                harness.db_reads += 1
                return harness.row if identifier == harness.identifier else None

            async def commit(self):
                harness.transaction = False
                harness.events.append("commit")

            async def scalar(self, statement):
                harness.transaction = True
                # The output guard is a real FOR SHARE NOWAIT query. The only
                # other scalar is its exact-purpose retiring-operation barrier.
                if statement.column_descriptions[0].get("entity") is not None:
                    assert statement._for_update_arg.read
                    assert statement._for_update_arg.nowait
                    return harness.row
                return False

        async def target_for_ref(db, ref):
            assert ref == self.ref
            # The second lookup models normal credential rotation after admission.
            target = SimpleNamespace(
                bucket=ref["bucket"],
                location_id=uuid.UUID(ref["location_id"]),
                client=self.client,
                credential_revision=len(self.targets) + 1,
            )
            self.targets.append(target)
            return target

        async def check(db, location_id, **kwargs):
            assert location_id == uuid.UUID(self.ref["location_id"])
            return 7

        async def acquire(db, location_id, purpose, **kwargs):
            assert not self.transaction
            expected = {
                "thumbnails": "thumbnail_",
                "image-pyramids": "image_pyramid_",
                "video-derivatives": "video_derivative_",
            }
            family = self.ref["prefix"].split("/objects/")[0].rsplit("/", 1)[-1]
            assert purpose == expected[family] + self.identifier.hex
            assert kwargs == {"expected_revision": 7}
            self.events.append("admit")
            return self.permit

        monkeypatch.setattr(service, "async_session", Database)
        monkeypatch.setattr(
            service.media_cache_storage, "target_for_ref", target_for_ref
        )
        monkeypatch.setattr(service.storage_access, "check", check)
        monkeypatch.setattr(service.storage_access, "acquire", acquire)
        monkeypatch.setattr(
            service.artifact_reads, "resolve", AsyncMock(return_value=None)
        )

    @contextmanager
    def client(self, *, read_timeout):
        assert read_timeout == service.READ_SECONDS
        assert not self.transaction
        assert self.events[-1] == "session-close"
        self.events.append("client-open")
        if self.block_open:
            self.open_started.set()
            assert self.open_resume.wait(5)
        try:
            yield self
        finally:
            self.events.append("client-close")
            if self.fail_client_close:
                raise RuntimeError("private provider client close failure")

    def metadata(self, *, length=len(RAW), status=200, content_range=None):
        return {
            "ETag": ETAG,
            "VersionId": "version-1",
            "ContentLength": length,
            "ResponseMetadata": {"HTTPStatusCode": status},
            **({"ContentRange": content_range} if content_range else {}),
        }

    def head_object(self, **kwargs):
        self.heads.append(kwargs)
        return self.metadata()

    def get_object(self, **kwargs):
        self.gets.append(kwargs)
        assert kwargs["IfMatch"] == ETAG
        assert kwargs["VersionId"] == "version-1"
        data, content_range, status = RAW, None, 200
        if "Range" in kwargs:
            start, end = map(int, kwargs["Range"].removeprefix("bytes=").split("-"))
            data = RAW[start : end + 1]
            content_range, status = f"bytes {start}-{end}/{len(RAW)}", 206
        self.body = Body(data, self)
        result = self.metadata(
            length=len(data), status=status, content_range=content_range
        )
        result["Body"] = self.body
        if self.changed_etag:
            result["ETag"] = '"same-size-replacement"'
        if self.changed_version:
            result["VersionId"] = "version-2"
        if self.changed_size:
            result["ContentLength"] += 1
        return result

    def capability(self, *, now=None):
        url = media_cache_urls.sign(
            "thumbnail",
            self.identifier,
            self.ref["prefix"] + "thumbnail.webp",
            self.ref,
            {"sha256": SHA, "size_bytes": len(RAW)},
            60,
            now=now,
        )
        token = parse_qs(urlsplit(url).query)["token"][0]
        return media_cache_urls.decode(
            token,
            kind="thumbnail",
            output_id=str(self.identifier),
            relative="thumbnail.webp",
            now=now,
        )


async def collect(response, *, send_failure=False):
    messages = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if send_failure and message["type"] == "http.response.body":
            raise RuntimeError("downstream closed")
        messages.append(message)

    await response(
        {"type": "http", "method": "GET", "asgi": {"spec_version": "2.4"}},
        receive,
        send,
    )
    return messages


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize(
    "method,range_header,if_range,expected,status",
    [
        ("GET", None, None, RAW, 200),
        ("HEAD", "bytes=5-9", None, b"", 200),
        ("GET", "bytes=5-9", None, RAW[5:10], 206),
        ("GET", "bytes=-7", None, RAW[-7:], 206),
        ("GET", "bytes=5-9", '"old"', RAW, 200),
        ("GET", "bytes=5-9", f'"sha256:{SHA}"', RAW[5:10], 206),
    ],
)
async def test_exact_saved_target_and_bounded_range(
    monkeypatch, registered, method, range_header, if_range, expected, status
):
    harness = Harness(monkeypatch, registered=registered)
    response = await service.response(
        harness.capability(),
        method=method,
        range_header=range_header,
        if_range=if_range,
    )
    messages = await collect(response)
    assert b"".join(message.get("body", b"") for message in messages) == expected
    assert messages[0]["status"] == status
    headers = dict(messages[0]["headers"])
    assert (
        headers[b"content-length"]
        == str(len(RAW) if method == "HEAD" else len(expected)).encode()
    )
    assert headers[b"etag"] == f'"sha256:{SHA}"'.encode()
    assert headers[b"cross-origin-resource-policy"] == b"same-origin"
    assert headers[b"content-type"] == b"image/webp"
    assert (b"content-range" in headers) == (status == 206)
    assert harness.heads == [
        {
            "Bucket": harness.ref["bucket"],
            "Key": harness.ref["prefix"] + "thumbnail.webp",
            "VersionId": "version-1",
        }
    ]
    assert harness.targets[-1].credential_revision == 2
    assert harness.events[-2:] == ["client-close", "permit-exit"]
    assert harness.permit.settled
    if method == "HEAD":
        assert not harness.gets
    else:
        assert harness.body.closed
        assert max(harness.body.requested) <= min(service.CHUNK_BYTES, len(expected))
        assert sum(harness.body.requested) == len(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["changed_etag", "changed_version", "changed_size"])
async def test_same_size_replacement_and_bad_metadata_close_every_handle(
    monkeypatch, change
):
    harness = Harness(monkeypatch)
    setattr(harness, change, True)
    with pytest.raises(HTTPException) as caught:
        await service.response(harness.capability())
    assert caught.value.detail == {"code": "media_cache_mismatch"}
    assert harness.body.closed
    assert harness.permit.settled
    assert harness.events[-3:] == ["body-close", "client-close", "permit-exit"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate", ["state", "uncertain", "reference", "sha", "size", "owner"]
)
async def test_invalid_saved_owner_never_opens_provider(monkeypatch, mutate):
    harness = Harness(monkeypatch)
    capability = harness.capability()
    if mutate == "state":
        harness.row.state = "building"
    elif mutate == "uncertain":
        harness.row.uncertain_write = True
    elif mutate == "reference":
        harness.row.storage_ref = reference(harness.identifier)
    elif mutate == "sha":
        harness.row.sha256 = "0" * 64
    elif mutate == "size":
        harness.row.size_bytes += 1
    else:
        harness.row.owner_kind = "unsupported"
    with pytest.raises(HTTPException) as caught:
        await service.response(capability)
    assert caught.value.status_code == 404
    assert not harness.heads and not harness.gets
    assert "admit" not in harness.events


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["tamper", "expired", "owner", "key", "kind"])
async def test_invalid_token_is_rejected_before_database(monkeypatch, wrong):
    harness = Harness(monkeypatch)
    now = datetime.now(timezone.utc)
    issued = now - timedelta(minutes=2) if wrong == "expired" else now
    url = media_cache_urls.sign(
        "thumbnail",
        harness.identifier,
        harness.ref["prefix"] + "thumbnail.webp",
        harness.ref,
        {"sha256": SHA, "size_bytes": len(RAW)},
        60,
        now=issued,
    )
    token = parse_qs(urlsplit(url).query)["token"][0]
    if wrong == "tamper":
        token = "A" + token[1:]
    with pytest.raises(HTTPException) as caught:
        await read_media_cache(
            "image-pyramid" if wrong == "kind" else "thumbnail",
            str(uuid.uuid4() if wrong == "owner" else harness.identifier),
            "other.webp" if wrong == "key" else "thumbnail.webp",
            Request({"type": "http", "method": "GET", "headers": []}),
            token,
        )
    assert caught.value.status_code == 404
    assert harness.db_reads == 0
    assert not harness.heads


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["body", "client"])
async def test_failed_close_retains_unresolved_operation(monkeypatch, failure):
    harness = Harness(monkeypatch)
    harness.fail_body_close = failure == "body"
    harness.fail_client_close = failure == "client"
    response = await service.response(harness.capability())
    with pytest.raises(HTTPException) as caught:
        await collect(response)
    assert caught.value.detail == {"code": "storage_operation_unsettled"}
    assert caught.value.storage_operation_unsettled
    assert harness.permit.unsettled and not harness.permit.settled
    assert harness.events[-3:] == ["body-close", "client-close", "permit-exit"]
    # The mock close failure is intentional; release its local BytesIO afterward.
    harness.fail_body_close = False
    if not harness.body.closed:
        harness.body.close()


@pytest.mark.asyncio
async def test_failed_send_still_closes_and_settles(monkeypatch):
    harness = Harness(monkeypatch)
    response = await service.response(harness.capability())
    with pytest.raises(RuntimeError, match="downstream closed"):
        await collect(response, send_failure=True)
    assert harness.body.closed and harness.permit.settled


@pytest.mark.asyncio
async def test_cancelled_pending_read_is_drained_before_close(monkeypatch):
    harness = Harness(monkeypatch)
    harness.block_read = True
    response = await service.response(harness.capability())
    running = asyncio.create_task(collect(response))
    assert await asyncio.to_thread(harness.body.started.wait, 2)
    running.cancel()
    await asyncio.sleep(0)
    assert not harness.body.closed and not harness.permit.settled
    harness.body.resume.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert harness.body.closed and harness.permit.settled


@pytest.mark.asyncio
async def test_cancelled_constructor_adopts_late_body_then_closes(monkeypatch):
    harness = Harness(monkeypatch)
    harness.block_open = True
    running = asyncio.create_task(service.response(harness.capability()))
    assert await asyncio.to_thread(harness.open_started.wait, 2)
    running.cancel()
    await asyncio.sleep(0)
    assert not harness.permit.settled
    harness.open_resume.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert harness.body.closed and harness.permit.settled


@pytest.mark.asyncio
async def test_unsatisfiable_range_settles_without_provider_open(monkeypatch):
    harness = Harness(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        await service.response(harness.capability(), range_header="bytes=99999-")
    assert caught.value.status_code == 416
    assert caught.value.headers == {"Content-Range": f"bytes */{len(RAW)}"}
    assert harness.permit.settled and not harness.heads


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image-pyramid", "video-derivative"])
@pytest.mark.parametrize("state", ["ready", "retiring"])
async def test_existing_inventory_validators_bind_exact_key(monkeypatch, kind, state):
    harness = Harness(monkeypatch)
    family = "image-pyramids" if kind == "image-pyramid" else "video-derivatives"
    harness.ref = reference(harness.identifier, family=family)
    harness.row.storage_ref, harness.row.state = harness.ref, state
    relative = "overview.webp" if kind == "image-pyramid" else "playback.mp4"
    objects = {
        harness.ref["prefix"] + relative: {"sha256": SHA, "size_bytes": len(RAW)},
        harness.ref["prefix"]
        + ("manifest.json" if kind == "image-pyramid" else "metadata.json"): {
            "sha256": "b" * 64,
            "size_bytes": 1,
        },
    }
    if kind == "image-pyramid":
        objects[harness.ref["prefix"] + "tiles/0/0/0.webp"] = {
            "sha256": "c" * 64,
            "size_bytes": 1,
        }
    harness.row.inventory = {"schema": 1, "objects": objects}
    url = media_cache_urls.sign(
        kind,
        harness.identifier,
        harness.ref["prefix"] + relative,
        harness.ref,
        objects[harness.ref["prefix"] + relative],
        60,
    )
    capability = media_cache_urls.decode(
        parse_qs(urlsplit(url).query)["token"][0],
        kind=kind,
        output_id=harness.identifier,
        relative=relative,
    )
    response = await service.response(capability, range_header="bytes=3-6")
    messages = await collect(response)
    assert b"".join(message.get("body", b"") for message in messages) == RAW[3:7]
    assert harness.gets[0]["Key"] == harness.ref["prefix"] + relative
    assert "VersionId" not in harness.heads[0]
    assert harness.gets[0]["VersionId"] == "version-1"
    assert harness.permit.settled


@pytest.mark.asyncio
async def test_unknown_client_construction_retains_operation(monkeypatch):
    harness = Harness(monkeypatch)

    @contextmanager
    def unknown_constructor(**kwargs):
        raise RuntimeError("private constructor details")
        yield  # pragma: no cover

    harness.client = unknown_constructor
    with pytest.raises(HTTPException) as caught:
        await service.response(harness.capability())
    assert caught.value.detail == {"code": "media_cache_unavailable"}
    assert harness.permit.unsettled and not harness.permit.settled
    assert not harness.heads and not harness.gets


@pytest.mark.asyncio
async def test_cancelled_read_with_failed_close_preserves_uncertainty(monkeypatch):
    harness = Harness(monkeypatch)
    harness.block_read = harness.fail_body_close = True
    response = await service.response(harness.capability())
    running = asyncio.create_task(collect(response))
    assert await asyncio.to_thread(harness.body.started.wait, 2)
    running.cancel()
    harness.body.resume.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await running
    assert caught.value.storage_operation_unsettled
    assert harness.permit.unsettled and not harness.permit.settled
    harness.fail_body_close = False
    harness.body.close()


@pytest.mark.asyncio
async def test_grid_requests_wait_for_physical_close_without_extra_io(monkeypatch):
    harness = Harness(monkeypatch)
    opened = [await service.response(harness.capability()) for _ in range(4)]
    reads = harness.db_reads
    waiting = asyncio.create_task(service.response(harness.capability()))
    await asyncio.sleep(0)
    try:
        assert not waiting.done(), (
            "A normal grid burst must wait instead of losing its fifth image"
        )
        assert harness.db_reads == reads and len(harness.gets) == 4
        await collect(opened.pop(0))
        response = await asyncio.wait_for(waiting, 1)
        assert len(harness.gets) == 5
        await collect(response)
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        for response in opened:
            await collect(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_waiting_timeout_or_cancellation_does_not_take_a_slot(
    monkeypatch, cancel
):
    harness = Harness(monkeypatch)
    opened = [await service.response(harness.capability()) for _ in range(4)]
    reads = harness.db_reads
    monkeypatch.setattr(service, "ADMISSION_SECONDS", 0.01 if not cancel else 30)
    waiting = asyncio.create_task(service.response(harness.capability()))
    try:
        if cancel:
            await asyncio.sleep(0)
            assert not waiting.done()
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
        else:
            with pytest.raises(HTTPException) as caught:
                await waiting
            assert caught.value.detail == {"code": "media_cache_busy"}
        assert harness.db_reads == reads and len(harness.gets) == 4
        await collect(opened.pop(0))
        admitted = await service.response(harness.capability())
        await collect(admitted)
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        for response in opened:
            await collect(response)


@pytest.mark.asyncio
async def test_waiting_capacity_is_bounded_before_metadata_or_provider_io(monkeypatch):
    harness = Harness(monkeypatch)
    monkeypatch.setattr(service, "_waiting", threading.BoundedSemaphore(1))
    opened = [await service.response(harness.capability()) for _ in range(4)]
    waiting = asyncio.create_task(service.response(harness.capability()))
    await asyncio.sleep(0)
    try:
        assert not waiting.done()
        reads = harness.db_reads
        with pytest.raises(HTTPException) as caught:
            await service.response(harness.capability())
        assert caught.value.detail == {"code": "media_cache_busy"}
        assert harness.db_reads == reads and len(harness.gets) == 4
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        for response in opened:
            await collect(response)


@pytest.mark.asyncio
async def test_truncated_stream_closes_before_settlement(monkeypatch):
    harness = Harness(monkeypatch)
    response = await service.response(harness.capability())
    harness.body.truncate(len(RAW) - 1)
    with pytest.raises(HTTPException) as caught:
        await collect(response)
    assert caught.value.detail == {"code": "media_cache_mismatch"}
    assert harness.body.closed and harness.permit.settled


@pytest.mark.asyncio
async def test_response_timeout_drains_pending_read(monkeypatch):
    harness = Harness(monkeypatch)
    harness.block_read = True
    monkeypatch.setattr(service, "RESPONSE_SECONDS", 0.01)
    response = await service.response(harness.capability())
    running = asyncio.create_task(collect(response))
    assert await asyncio.to_thread(harness.body.started.wait, 2)
    await asyncio.sleep(0.03)
    assert not harness.body.closed and not harness.permit.settled
    harness.body.resume.set()
    with pytest.raises(HTTPException) as caught:
        await running
    assert caught.value.status_code == 504
    assert harness.body.closed and harness.permit.settled


def test_only_get_is_in_schema_and_both_methods_share_the_route():
    from fastapi import FastAPI
    from app.api.v1.media_cache import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/media-cache")
    path = "/api/v1/media-cache/{kind}/{output_id}/{relative}"
    assert set(app.openapi()["paths"][path]) == {"get"}
    methods = {
        method
        for route in app.routes
        if "media-cache" in route.path
        for method in route.methods
    }
    assert methods == {"GET", "HEAD"}


@pytest.fixture
async def admitted_output(test_engine, monkeypatch, request):
    """Committed owners and real independent permits in the disposable DB only.

    No provider opens. Thumbnail tombstones cannot be deleted by design and are
    removed when this test mode is destroyed after the bounded regression run.
    """
    kind = request.param
    model, family = service._FAMILIES[kind]
    identifier = uuid.uuid4()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    saved = reference(identifier, registered=False, family=family)
    relative = {
        "thumbnail": "thumbnail.webp",
        "image-pyramid": "overview.webp",
        "video-derivative": "playback.mp4",
    }[kind]
    receipt = {"sha256": SHA, "size_bytes": len(RAW)}
    objects = {saved["prefix"] + relative: receipt}
    instant = datetime.now(timezone.utc)
    values = {"id": identifier, "storage_ref": saved, "state": "building"}
    publication = {}
    if kind == "thumbnail":
        values |= {
            "owner_kind": "task_thumbnail",
            "owner_id": uuid.uuid4(),
            "input_ref": {},
            "writer_until": instant,
            "next_check_at": instant,
            "uncertain_write": False,
        }
        publication = {
            "sha256": SHA,
            "size_bytes": len(RAW),
            "etag": ETAG,
            "ready_at": instant,
        }
    else:
        metadata = "manifest.json" if kind == "image-pyramid" else "metadata.json"
        objects[saved["prefix"] + metadata] = {"sha256": "b" * 64, "size_bytes": 1}
        publication = {"inventory": {"schema": 1, "objects": objects}}
        if kind == "image-pyramid":
            objects[saved["prefix"] + "tiles/0/0/0.webp"] = {
                "sha256": "c" * 64,
                "size_bytes": 1,
            }
            values |= {
                "asset_id": uuid.uuid4(),
                "generation": 1,
                "lease_token": uuid.uuid4(),
            }
        else:
            values |= {
                "dataset_item_id": uuid.uuid4(),
                "kind": "metadata",
                "spec": {},
                "original_ref": {},
                "auth_ref": {},
            }
            publication["result"] = {}
    async with factory() as db:
        await ensure_legacy_bindings(db)
        row = model(**values)
        db.add(row)
        await db.flush()
        if kind == "video-derivative":
            row.lease_token, row.lease_expires_at = uuid.uuid4(), instant
            await db.flush()
        for name, value in publication.items():
            setattr(row, name, value)
        row.state = "ready"
        await db.flush()
        if "ready_output" not in request.node.name:
            row.state = "retiring"
        await db.commit()
    url = media_cache_urls.sign(
        kind, identifier, saved["prefix"] + relative, saved, receipt, 60
    )
    capability = media_cache_urls.decode(
        parse_qs(urlsplit(url).query)["token"][0],
        kind=kind,
        output_id=identifier,
        relative=relative,
    )
    target = SimpleNamespace(
        location_id=LEGACY_LOCATION_ID,
        bucket=saved["bucket"],
        client=lambda **kwargs: pytest.fail("admission regression opened a provider"),
    )

    async def current_target(db, reference):
        assert reference == saved
        return target

    monkeypatch.setattr(service, "async_session", factory)
    monkeypatch.setattr(service.media_cache_storage, "target_for_ref", current_target)
    context = SimpleNamespace(
        capability=capability,
        factory=factory,
        model=model,
        identifier=identifier,
        purpose=service._purpose(capability),
    )
    try:
        yield context
    finally:
        async with factory() as db:
            # All test physical contexts were never entered; remove any failed
            # assertion's test-only permit, preserving no real uncertainty.
            await db.execute(
                delete(service.StorageOperation).where(
                    service.StorageOperation.purpose == context.purpose
                )
            )
            row = await db.get(model, identifier)
            if row is not None:
                row.state = "retiring"
                await db.flush()
                row.state = "deleted"
                await db.flush()
                if kind != "thumbnail":
                    await db.delete(row)
            await db.commit()


_KINDS = ["thumbnail", "image-pyramid", "video-derivative"]


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted_output", _KINDS, indirect=True)
async def test_db_read_permit_is_seen_by_collectors_locked_second_check(
    admitted_output,
):
    owned = admitted_output
    transfer = await service._metadata(owned.capability)
    try:
        async with owned.factory() as gc:
            async with await service.storage_access.acquire(
                gc, LEGACY_LOCATION_ID, owned.purpose
            ) as collecting:
                # Both existing collectors and Thumbnail GC take this exclusive
                # owner lock, then exclude their own operation before deletion.
                row = await gc.scalar(
                    select(owned.model)
                    .where(owned.model.id == owned.identifier)
                    .with_for_update()
                )
                assert row.state == "retiring"
                assert await gc.scalar(
                    select(
                        exists().where(
                            service.StorageOperation.purpose == owned.purpose,
                            service.StorageOperation.id != collecting.id,
                        )
                    )
                )
                await gc.commit()
    finally:
        await service._release(transfer)


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted_output", _KINDS, indirect=True)
async def test_db_retiring_read_after_gc_final_check_is_refused(admitted_output):
    owned = admitted_output
    async with owned.factory() as gc:
        async with await service.storage_access.acquire(
            gc, LEGACY_LOCATION_ID, owned.purpose
        ) as collecting:
            row = await gc.scalar(
                select(owned.model)
                .where(owned.model.id == owned.identifier)
                .with_for_update()
            )
            assert row.state == "retiring"
            assert not await gc.scalar(
                select(
                    exists().where(
                        service.StorageOperation.purpose == owned.purpose,
                        service.StorageOperation.id != collecting.id,
                    )
                )
            )
            await gc.commit()
            # Reproduce the exact former window: the collector committed its
            # last metadata check and retains its permit over physical deletion.
            with pytest.raises(HTTPException) as caught:
                await service._metadata(owned.capability)
            assert caught.value.detail == {"code": "media_cache_busy"}
            async with owned.factory() as check:
                operations = list(
                    await check.scalars(
                        select(service.StorageOperation.id).where(
                            service.StorageOperation.purpose == owned.purpose
                        )
                    )
                )
                assert operations == [collecting.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted_output", _KINDS, indirect=True)
async def test_db_ready_output_keeps_concurrent_read_admission(admitted_output):
    owned = admitted_output
    first = await service._metadata(owned.capability)
    second = None
    try:
        second = await service._metadata(owned.capability)
        assert first.permit.id != second.permit.id
    finally:
        if second is not None:
            await service._release(second)
        await service._release(first)
