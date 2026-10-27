"""Published cache Range delivery keeps saved identity and both live permits."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

from app.services import media_cache_response as service
from app.services import storage_migration_copy as copy
from app.services.content_snapshot import ContentError
from tests.test_media_cache_response import Body, Harness, Permit, RAW, SHA, collect
from tests.test_storage_migration_artifact_reads import response as copied_response
from tests.test_storage_migration_artifact_reads import route as copied_route


class MigratedHarness(Harness):
    def __init__(self, monkeypatch, *, saved_version="version-1"):
        super().__init__(monkeypatch, saved_version=saved_version)
        selected = replace(
            copied_route(),
            source_location_id=self.identifier,
            source_endpoint_url=self.ref["endpoint_url"],
            source_bucket=self.ref["bucket"],
            source_object_key=self.ref["prefix"] + "thumbnail.webp",
            source_version_id=self.row.version_id,
            source_etag=self.row.etag,
            sha256=SHA,
            size_bytes=len(RAW),
            maintenance_revision=7,
        )
        self.ref["location_id"] = str(selected.source_location_id)
        self.route = replace(
            selected,
            target_content_sha256=copy.digest(
                copy.content_target(copied_response(selected, ContentType="image/webp"))
            ),
        )
        self.publication_permit = Permit(self)
        self.revoked = False
        self.target = SimpleNamespace(client=self.client)

        async def resolve(db, ref, key, sha, size, **kwargs):
            assert (ref, key, sha, size) == (
                self.ref,
                self.route.source_object_key,
                SHA,
                len(RAW),
            )
            assert kwargs == {
                "source_version_id": self.row.version_id
                if self.row.version_id is not None
                else service.publications.ANY_VERSION,
                "source_etag": self.row.etag,
            }
            return self.route

        async def acquire(db, location_id, purpose, **kwargs):
            assert not self.transaction
            assert location_id == self.route.target_location_id
            assert kwargs == {"expected_revision": 7}
            if purpose == self.route.purpose:
                return self.publication_permit
            assert purpose == service._purpose(self.capability())
            return self.permit

        async def recheck(db, route, **kwargs):
            assert route == self.route
            if self.revoked:
                raise ContentError("read_publication_changed")

        monkeypatch.setattr(service.artifact_reads, "resolve", resolve)
        monkeypatch.setattr(
            service.publications,
            "target_for_route",
            AsyncMock(return_value=self.target),
        )
        monkeypatch.setattr(service.publications, "recheck", recheck)
        monkeypatch.setattr(service.storage_access, "acquire", acquire)
        monkeypatch.setattr(
            service.media_cache_storage,
            "target_for_ref",
            AsyncMock(side_effect=AssertionError("stopped A was opened")),
        )

    def metadata(self, *, length=len(RAW), status=200, content_range=None):
        return copied_response(
            self.route,
            ContentType="image/webp",
            ContentLength=length,
            ResponseMetadata={"HTTPStatusCode": status},
            **({"ContentRange": content_range} if content_range else {}),
        )

    def get_object(self, **kwargs):
        self.gets.append(kwargs)
        assert kwargs["IfMatch"] == self.route.target_etag
        assert kwargs["VersionId"] == self.route.target_version_id
        data, content_range, status = RAW, None, 200
        if "Range" in kwargs:
            start, end = map(int, kwargs["Range"].removeprefix("bytes=").split("-"))
            data = RAW[start : end + 1]
            content_range, status = f"bytes {start}-{end}/{len(RAW)}", 206
        self.body = Body(data, self)
        return self.metadata(
            length=len(data), status=status, content_range=content_range
        ) | {"Body": self.body}


@pytest.mark.parametrize(
    "method,range_header,expected,status",
    [
        ("GET", None, RAW, 200),
        ("HEAD", "bytes=5-9", b"", 200),
        ("GET", "bytes=5-9", RAW[5:10], 206),
        ("GET", "bytes=-7", RAW[-7:], 206),
    ],
)
async def test_mapped_full_head_and_ranges_use_exact_b_without_changing_saved_receipt(
    monkeypatch, method, range_header, expected, status
):
    harness = MigratedHarness(monkeypatch)
    saved = deepcopy(harness.row.__dict__)
    response = await service.response(
        harness.capability(), method=method, range_header=range_header
    )
    messages = await collect(response)
    assert messages[0]["status"] == status
    assert b"".join(m.get("body", b"") for m in messages) == expected
    assert harness.heads == [
        {
            "Bucket": harness.route.target_bucket,
            "Key": harness.route.target_object_key,
            "VersionId": harness.route.target_version_id,
        }
    ]
    assert harness.row.__dict__ == saved
    assert harness.permit.settled and harness.publication_permit.settled
    assert harness.events[-3:] == ["client-close", "permit-exit", "permit-exit"]


@pytest.mark.parametrize("source_version", [None, "null"])
async def test_explicit_null_source_version_is_not_an_unknown_version_lookup(
    monkeypatch, source_version
):
    harness = MigratedHarness(monkeypatch, saved_version=source_version)
    response = await service.response(harness.capability(), method="HEAD")
    assert response.transfer.pinned.source_version_id == source_version
    await collect(response)
    assert harness.permit.settled and harness.publication_permit.settled


@pytest.mark.parametrize("failure", ["body", "client"])
async def test_mapped_unknown_close_retains_owner_and_publication_permits(
    monkeypatch, failure
):
    harness = MigratedHarness(monkeypatch)
    setattr(harness, f"fail_{failure}_close", True)
    response = await service.response(harness.capability())
    with pytest.raises(HTTPException):
        await collect(response)
    assert harness.permit.unsettled and harness.publication_permit.unsettled
    harness.fail_body_close = False
    if harness.body is not None and not harness.body.closed:
        harness.body.close()


@pytest.mark.parametrize("interruption", ["cancel", "withdraw"])
async def test_pending_mapped_stream_is_drained_before_settlement_and_not_delivered_after_withdraw(
    monkeypatch, interruption
):
    harness = MigratedHarness(monkeypatch)
    harness.block_read = True
    response = await service.response(harness.capability())
    task = asyncio.create_task(collect(response))
    assert await asyncio.to_thread(harness.body.started.wait, 2)
    if interruption == "cancel":
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    else:
        harness.revoked = True
    await asyncio.sleep(0.02)
    assert not task.done() and not harness.body.closed
    assert not harness.permit.settled and not harness.publication_permit.settled
    harness.body.resume.set()
    with pytest.raises(
        asyncio.CancelledError if interruption == "cancel" else ContentError
    ):
        await task
    assert harness.body.closed
    assert harness.permit.settled and harness.publication_permit.settled


async def test_withdraw_before_response_headers_sends_no_response_and_closes(
    monkeypatch,
):
    harness = MigratedHarness(monkeypatch)
    response = await service.response(harness.capability())
    harness.revoked = True
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        await asyncio.Event().wait()

    with pytest.raises(ContentError):
        await response(
            {"type": "http", "method": "GET", "asgi": {"spec_version": "2.4"}},
            receive,
            send,
        )
    assert sent == [] and harness.body.closed
    assert harness.permit.settled and harness.publication_permit.settled
