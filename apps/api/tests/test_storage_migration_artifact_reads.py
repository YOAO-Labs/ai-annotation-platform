"""Closed saved-artifact transports, without database or provider substitutes."""

import asyncio
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace
import io
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy.exc import DBAPIError

from app.services import storage_access
from app.services import storage_migration_artifact_reads as service
from app.services import storage_migration_copy as copy
from app.services import storage_migration_read_publications as publications
from app.services.content_snapshot import ContentError
from app.services.original_read_plan import ReadRoute


def route():
    value = ReadRoute(
        publication_id=uuid.UUID(int=1),
        publication_revision=2,
        source_object_id="a" * 64,
        source_location_id=uuid.UUID(int=3),
        source_endpoint_url="http://source.test:9000",
        source_bucket="source-bucket",
        source_object_key="saved/object.bin",
        source_version_id="source-version",
        sha256="b" * 64,
        size_bytes=4,
        source_etag='"source-etag"',
        is_latest=False,
        target_location_id=uuid.UUID(int=4),
        target_location_revision=5,
        target_connection_id=uuid.UUID(int=6),
        target_connection_revision=7,
        target_endpoint_url="http://target.test:9000",
        target_bucket="target-bucket",
        target_object_key="_aap_migration/frozen/object",
        target_version_id="target-version",
        target_etag='"target-etag"',
        target_content_sha256="c" * 64,
        creation_nonce="copy-nonce",
        creation_plan_uuid=str(uuid.UUID(int=8)),
        maintenance_revision=9,
    )
    return replace(
        value,
        target_content_sha256=copy.digest(copy.content_target(response(value))),
    )


def response(value, **changes):
    return {
        "ResponseMetadata": {"HTTPStatusCode": 200},
        "VersionId": value.target_version_id,
        "ETag": value.target_etag,
        "ContentLength": value.size_bytes,
        "ContentType": "application/octet-stream",
        "CacheControl": "private, max-age=900",
        "Metadata": {
            "aap-copy-plan": value.creation_plan_uuid,
            "aap-copy-object": value.source_object_id,
            "aap-copy-nonce": value.creation_nonce,
            "business-owner": "saved-owner",
        },
        **changes,
    }


def logical(value, **changes):
    return {
        "Bucket": value.source_bucket,
        "Key": value.source_object_key,
        **changes,
    }


class Body(io.BytesIO):
    def __init__(self, *, close_failure=False):
        super().__init__(b"data")
        self.closes, self.close_failure = 0, close_failure

    def close(self):
        self.closes += 1
        if self.close_failure:
            raise RuntimeError("body close unknown")
        super().close()


class SDK:
    def __init__(self, value):
        self.route = value
        self.meta = SimpleNamespace(endpoint_url=value.target_endpoint_url)
        self.calls, self.bodies = [], []
        self.changes = {}
        self.body_close_failure = self.close_failure = False
        self.closes = 0

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        return response(self.route, **deepcopy(self.changes))

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        body = Body(close_failure=self.body_close_failure)
        self.bodies.append(body)
        return response(self.route, Body=body, **deepcopy(self.changes))

    def generate_presigned_url(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        return "https://target.test/signed"

    def close(self):
        self.closes += 1
        if self.close_failure:
            raise RuntimeError("client close unknown")


class Target:
    def __init__(self, sdk):
        self.sdk, self.opens = sdk, 0

    @contextmanager
    def client(self):
        self.opens += 1
        try:
            yield self.sdk
        finally:
            storage_access.close_registered(self.sdk)


@pytest.mark.parametrize(
    "source_version", [publications.ANY_VERSION, None, "null", "v1"]
)
async def test_resolve_keeps_full_saved_address_hash_size_and_version_mode(
    monkeypatch, source_version
):
    selected = route()
    lookup = AsyncMock(return_value=selected)
    monkeypatch.setattr(publications, "lookup", lookup)
    ref = {
        "location_id": str(selected.source_location_id),
        "endpoint_url": selected.source_endpoint_url,
        "bucket": selected.source_bucket,
    }
    original = deepcopy(ref)
    assert (
        await service.resolve(
            object(),
            ref,
            selected.source_object_key,
            selected.sha256,
            source_version_id=source_version,
        )
        == selected
    )
    assert ref == original
    assert lookup.await_args.kwargs == {
        "source_location_id": selected.source_location_id,
        "endpoint_url": selected.source_endpoint_url,
        "bucket": selected.source_bucket,
        "object_key": selected.source_object_key,
        "sha256": selected.sha256,
        "size_bytes": None,
        "source_version_id": source_version,
    }


@pytest.mark.parametrize(
    "sha,size",
    [(None, 4), ("b" * 63, 4), ("B" * 64, 4), ("b" * 64, -1), ("b" * 64, True)],
)
async def test_missing_content_evidence_performs_no_database_lookup(
    monkeypatch, sha, size
):
    lookup = AsyncMock()
    monkeypatch.setattr(publications, "lookup", lookup)
    assert await service.resolve(object(), {}, "key", sha, size) is None
    lookup.assert_not_awaited()


def test_exact_target_transport_preserves_actual_version_and_business_metadata():
    selected = route()
    sdk = SDK(selected)
    target = Target(sdk)
    with service.client(selected, target) as reader:
        head = reader.head_object(
            **logical(selected, VersionId=selected.source_version_id)
        )
        assert head["VersionId"] == selected.target_version_id
        assert head["VersionId"] != selected.source_version_id
        assert head["ETag"] == selected.target_etag
        assert head["Metadata"] == {"business-owner": "saved-owner"}
        assert reader.meta.endpoint_url == selected.target_endpoint_url
        result = reader.get_object(
            **logical(selected, VersionId=head["VersionId"]), IfMatch=head["ETag"]
        )
        assert result["Body"].read() == b"data"
        storage_access.close_registered(result["Body"])
        assert sdk.bodies[0].closes == 1
    assert sdk.bodies[0].closes == 1
    assert sdk.closes == 1
    assert sdk.calls == [
        (
            "head",
            {
                "Bucket": selected.target_bucket,
                "Key": selected.target_object_key,
                "VersionId": selected.target_version_id,
            },
        ),
        (
            "get",
            {
                "Bucket": selected.target_bucket,
                "Key": selected.target_object_key,
                "VersionId": selected.target_version_id,
                "IfMatch": selected.target_etag,
            },
        ),
    ]


@pytest.mark.parametrize("method", ["head_object", "get_object"])
@pytest.mark.parametrize(
    "changes",
    [
        {"Bucket": "other"},
        {"Key": "other"},
        {"VersionId": "other"},
        {"VersionId": None},
        {"Range": "bytes=0-1"},
        {"PartNumber": 1},
    ],
)
def test_facade_rejects_other_object_version_or_partial_reads_before_io(
    method, changes
):
    selected = route()
    sdk = SDK(selected)
    with pytest.raises(ContentError, match="read_publication_changed"):
        getattr(service.ReadClient(sdk, selected), method)(
            **logical(selected, **changes)
        )
    assert sdk.calls == []


def test_get_rejects_changed_condition_without_provider_io():
    selected = route()
    sdk = SDK(selected)
    with pytest.raises(ContentError):
        service.ReadClient(sdk, selected).get_object(
            **logical(selected), IfMatch='"other"'
        )
    assert sdk.calls == []


def test_signed_url_pins_target_version_and_preserves_expiry():
    selected = route()
    sdk = SDK(selected)
    reader = service.ReadClient(sdk, selected)
    assert (
        reader.generate_presigned_url(
            "get_object",
            Params=logical(selected, VersionId=selected.source_version_id),
            ExpiresIn=79,
        )
        == "https://target.test/signed"
    )
    assert sdk.calls == [
        (
            "get_object",
            {
                "Params": {
                    "Bucket": selected.target_bucket,
                    "Key": selected.target_object_key,
                    "VersionId": selected.target_version_id,
                },
                "ExpiresIn": 79,
            },
        )
    ]
    with pytest.raises(ContentError):
        reader.generate_presigned_url(
            "put_object", Params=logical(selected), ExpiresIn=79
        )
    with pytest.raises(ContentError):
        reader.generate_presigned_url(
            "get_object", Params=logical(selected), ExpiresIn=79, HttpMethod="PUT"
        )
    for name in (
        "put_object",
        "delete_object",
        "list_objects_v2",
        "list_object_versions",
        "upload_fileobj",
    ):
        assert not hasattr(reader, name)


@pytest.mark.parametrize(
    "changes",
    [
        {"ResponseMetadata": {"HTTPStatusCode": 206}},
        {"VersionId": "other"},
        {"ETag": '"other"'},
        {"ContentLength": 5},
        {"ContentLength": True},
        {"ContentRange": "bytes 0-3/4"},
        {"DeleteMarker": True},
        {"ContentType": "text/plain"},
        {"CacheControl": "public"},
        {"Metadata": {}},
        {"Metadata": {"aap-copy-nonce": "other"}},
    ],
)
def test_unqualified_success_response_closes_body_and_rejects(changes):
    selected = route()
    sdk = SDK(selected)
    sdk.changes = changes
    with service.client(selected, Target(sdk)) as reader:
        with pytest.raises(ContentError):
            reader.get_object(**logical(selected))
    assert sdk.bodies[0].closes == sdk.closes == 1


@pytest.mark.parametrize(
    "field", ["aap-copy-plan", "aap-copy-object", "aap-copy-nonce", "business-owner"]
)
def test_every_saved_metadata_fact_is_verified_before_creation_fields_are_removed(
    field,
):
    selected = route()
    sdk = SDK(selected)
    metadata = response(selected)["Metadata"]
    metadata[field] = "changed"
    sdk.changes = {"Metadata": metadata}
    with service.client(selected, Target(sdk)) as reader:
        with pytest.raises(ContentError):
            reader.get_object(**logical(selected))
    assert sdk.bodies[0].closes == 1


def test_callback_failure_closes_every_adopted_body_before_client():
    selected = route()
    sdk = SDK(selected)
    with pytest.raises(RuntimeError, match="callback failed"):
        with service.client(selected, Target(sdk)) as reader:
            reader.get_object(**logical(selected))
            reader.get_object(**logical(selected))
            raise RuntimeError("callback failed")
    assert [body.closes for body in sdk.bodies] == [1, 1]
    assert sdk.closes == 1


class Permit:
    def __init__(self, world, purpose, revision):
        self.world, self.purpose, self.revision = world, purpose, revision
        self.settled = False

    async def __aenter__(self):
        self.world.active.append(self)
        return self

    async def __aexit__(self, kind, failure, traceback):
        if not getattr(failure, "storage_operation_unsettled", False):
            self.world.active.remove(self)
            self.settled = True
        return False


@pytest.fixture
def runtime(monkeypatch):
    selected = route()
    sdk = SDK(selected)
    world = SimpleNamespace(
        route=selected, sdk=sdk, target=Target(sdk), active=[], permits=[]
    )

    async def acquire(db, location, purpose, *, expected_revision=None, node_id=None):
        assert location == selected.target_location_id
        assert expected_revision == selected.maintenance_revision
        permit = Permit(world, purpose, expected_revision)
        world.permits.append(permit)
        return permit

    monkeypatch.setattr(storage_access, "acquire", acquire)
    monkeypatch.setattr(
        publications, "target_for_route", AsyncMock(return_value=world.target)
    )
    monkeypatch.setattr(publications, "recheck", AsyncMock())
    return world


async def test_actual_run_keeps_both_purposes_through_body_and_client_close(runtime):
    world = runtime
    seen = []

    async def acquired(permit):
        assert len(world.active) == 2
        assert permit.purpose == "saved_owner_read"
        assert world.target.opens == 0
        seen.append("authority")

    def physical(reader):
        assert len(world.active) == 2
        assert [permit.purpose for permit in world.active] == [
            world.route.purpose,
            "saved_owner_read",
        ]
        seen.append("read")
        return reader.get_object(**logical(world.route))["Body"].read()

    assert (
        await service.run(
            object(), world.route, "saved_owner_read", physical, on_acquired=acquired
        )
        == b"data"
    )
    assert seen == ["authority", "read"]
    assert world.sdk.bodies[0].closes == world.sdk.closes == 1
    assert world.active == [] and all(permit.settled for permit in world.permits)
    assert publications.recheck.await_count == 2


@pytest.mark.parametrize(
    "stage", ["authority", "initial_route", "final_route", "client_close"]
)
async def test_run_rejections_never_deliver_result_and_close_completed_local_result(
    runtime, stage
):
    world = runtime
    result = io.BytesIO(b"staged bytes")
    closed = []

    def close_result(value):
        assert value is result
        value.close()
        closed.append(value)

    async def authority(permit):
        if stage == "authority":
            raise ContentError("authority_changed")

    if stage == "initial_route":
        publications.recheck.side_effect = ContentError("route_changed")
    elif stage == "final_route":
        publications.recheck.side_effect = [None, ContentError("route_changed")]
    elif stage == "client_close":
        world.sdk.close_failure = True

    with pytest.raises((ContentError, RuntimeError)):
        await service.run(
            object(),
            world.route,
            "saved_owner_read",
            lambda reader: result,
            on_acquired=authority,
            close_result=close_result,
        )
    assert len(closed) == (1 if stage in {"final_route", "client_close"} else 0)
    assert world.target.opens == (1 if stage in {"final_route", "client_close"} else 0)
    assert len(world.active) == (2 if stage == "client_close" else 0)
    result.close()


async def test_body_close_unknown_retains_both_actual_permits(runtime):
    world = runtime
    world.sdk.body_close_failure = True

    def physical(reader):
        return reader.get_object(**logical(world.route))["Body"].read()

    with pytest.raises(RuntimeError) as caught:
        await service.run(object(), world.route, "saved_owner_read", physical)
    assert caught.value.storage_operation_unsettled is True
    assert len(world.active) == 2
    assert world.sdk.closes == 1
    # Release the test's in-memory handle; production retains its durable record.
    world.sdk.bodies[0].close_failure = False
    world.sdk.bodies[0].close()


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
async def test_interruption_drains_real_executor_and_closes_late_local_result(
    runtime, mode
):
    world = runtime
    entered, released = threading.Event(), threading.Event()
    result, closed = io.BytesIO(b"late staged result"), []

    def physical(reader):
        body = reader.get_object(**logical(world.route))["Body"]
        entered.set()
        assert released.wait(3)
        assert body.read() == b"data"
        return result

    def close_result(value):
        assert value is result
        closed.append(value)
        value.close()

    task = asyncio.create_task(
        service.run(
            object(),
            world.route,
            "saved_owner_read",
            physical,
            timeout=0.01 if mode == "timeout" else None,
            close_result=close_result,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if mode == "cancel":
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done()
        assert len(world.active) == 2
        assert world.sdk.bodies[0].closes == world.sdk.closes == 0
    finally:
        released.set()
        with pytest.raises(
            asyncio.CancelledError if mode == "cancel" else TimeoutError
        ):
            await task
    assert closed == [result] and result.closed
    assert world.sdk.bodies[0].closes == world.sdk.closes == 1
    assert world.active == []


@pytest.mark.parametrize(
    "error", [HTTPException(409, {"code": "changed"}), HTTPException(503, "private")]
)
async def test_read_boundary_exposes_only_closed_http_codes(error):
    @publications._read_boundary
    async def failed(db):
        raise error

    with pytest.raises(ContentError) as caught:
        await failed(SimpleNamespace(no_autoflush=nullcontext()))
    assert caught.value.code == (
        "changed" if isinstance(error.detail, dict) else "read_publication_changed"
    )
    assert caught.value.status == error.status_code


async def test_read_boundary_converts_nowait_lock_conflict_without_provider_text():
    failure = DBAPIError("private SQL", {}, SimpleNamespace(sqlstate="55P03"), False)

    @publications._read_boundary
    async def failed(db):
        raise failure

    with pytest.raises(ContentError, match="read_publication_busy"):
        await failed(SimpleNamespace(no_autoflush=nullcontext()))
