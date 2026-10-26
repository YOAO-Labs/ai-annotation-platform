"""Terminal log downloads use exact saved reads and never create derivatives."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
import pytest

from app.api.v1 import training_runs as api
from app.services import storage_migration_artifact_reads as reads
from app.services import storage_migration_copy as copy
from app.services import training_storage_io
from app.services.content_snapshot import ContentError
from app.services.training_dataset_bridge import TrainingDatasetError
from tests import test_training_run_storage_flow as flow
from tests.test_storage_migration_artifact_reads import route as read_route


class Body(io.BytesIO):
    close_failure = False

    def close(self):
        if self.close_failure:
            raise RuntimeError("log body close unknown")
        super().close()


@pytest.fixture
def world(monkeypatch):
    value = flow.owned.__wrapped__(monkeypatch)
    raw = b"password=private https://storage.test/key?X-Amz-Signature=signed /host/private\n"
    artifact = flow.artifact(value, "train.log", "log", raw)
    value.attempt.status = "failed"
    value.attempt.artifacts = {"train.log": artifact}
    value.project = SimpleNamespace(id=value.run.project_id)
    value.user = SimpleNamespace()
    value.db = SimpleNamespace(
        scalar=AsyncMock(return_value=value.run),
        get=AsyncMock(return_value=value.attempt),
    )
    value.saved = deepcopy((value.run.storage_ref, value.attempt.artifacts))
    ref = value.run.storage_ref
    value.route = replace(
        read_route(),
        source_location_id=uuid.UUID(ref["location_id"]),
        source_endpoint_url=ref["endpoint_url"],
        source_bucket=ref["bucket"],
        source_object_key=artifact["key"],
        sha256=artifact["sha256"],
        size_bytes=len(raw),
    )
    value.metadata = {
        **value.client.metadata[value.bucket, artifact["key"]],
        "aap-copy-plan": value.route.creation_plan_uuid,
        "aap-copy-object": value.route.source_object_id,
        "aap-copy-nonce": value.route.creation_nonce,
    }
    value.response = {
        "ResponseMetadata": {"HTTPStatusCode": 200},
        "VersionId": value.route.target_version_id,
        "ETag": value.route.target_etag,
        "ContentLength": len(raw),
        "Metadata": value.metadata,
    }
    value.route = replace(
        value.route,
        target_content_sha256=copy.digest(copy.content_target(value.response)),
    )
    value.bodies, value.reads = [], []
    value.close_failure = False
    value.after_read = lambda: None
    value.withdrawn = False

    def get_object(**kwargs):
        assert kwargs == {
            "Bucket": value.route.target_bucket,
            "Key": value.route.target_object_key,
            "VersionId": value.route.target_version_id,
            "IfMatch": value.route.target_etag,
        }
        body = Body(raw)
        body.close_failure = value.close_failure
        value.bodies.append(body)
        value.reads.append(kwargs)
        return {**deepcopy(value.response), "Body": body}

    value.physical = SimpleNamespace(get_object=get_object)

    async def routed(db, route, purpose, callback, **kwargs):
        assert db is value.db and route == value.route
        assert purpose == "training_artifact_download"
        reader = reads.ReadClient(value.physical, route)
        try:
            result = callback(reader)
            value.after_read()
            if value.withdrawn:
                raise ContentError("read_publication_changed", 409)
            return result
        finally:
            reader.close()

    value.lookup = AsyncMock(return_value=value.route)
    monkeypatch.setattr(training_storage_io.artifact_reads, "resolve", value.lookup)
    monkeypatch.setattr(training_storage_io.artifact_reads, "run", routed)
    monkeypatch.setattr(
        training_storage_io,
        "run",
        AsyncMock(side_effect=AssertionError("stopped A must not open")),
    )
    value.authority = AsyncMock()
    monkeypatch.setattr(api, "lock_owner", value.authority)
    return value


async def download(world, *, content=False):
    operation = api.download_log_content if content else api.download_artifact
    return await operation(
        world.run.id,
        world.attempt.id,
        "train.log",
        world.project,
        world.user,
        world.db,
    )


async def test_mapped_log_returns_authenticated_redacted_content_without_writes(world):
    receipt = world.attempt.artifacts["train.log"]
    descriptor = await download(world)
    result = await download(world, content=True)
    assert descriptor["requires_authentication"] is True
    assert descriptor["url"] == (
        f"/projects/{world.project.id}/training-runs/{world.run.id}"
        f"/attempts/{world.attempt.id}/artifacts/train.log/content"
    )
    assert descriptor["source_sha256"] == receipt["sha256"]
    assert descriptor["sha256"] == hashlib.sha256(result.body).hexdigest()
    assert descriptor["size_bytes"] == len(result.body)
    assert result.headers["content-type"].startswith("text/plain")
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in result.headers["content-disposition"]
    for secret in (b"private", b"storage.test", b"signed", b"/host"):
        assert secret not in result.body
    assert len(world.reads) == 2
    assert all(body.closed for body in world.bodies)
    assert world.client.calls == []
    assert (world.run.storage_ref, world.attempt.artifacts) == world.saved
    assert world.authority.await_count == 4
    assert all(
        args.args
        == (
            world.db,
            world.run.storage_ref,
            receipt["key"],
            receipt["sha256"],
            receipt["size_bytes"],
        )
        for args in world.lookup.await_args_list
    )


@pytest.mark.parametrize("stage", ["before", "after"])
async def test_log_download_rechecks_current_project_authority(world, stage):
    denial = HTTPException(403, "authority changed")
    world.authority.side_effect = [denial] if stage == "before" else [None, denial]
    with pytest.raises(HTTPException) as caught:
        await download(world, content=True)
    assert caught.value.status_code == 403
    assert len(world.reads) == (0 if stage == "before" else 1)
    assert all(body.closed for body in world.bodies)


@pytest.mark.parametrize("change", ["artifact", "owner", "terminal", "attempt_owner"])
async def test_log_download_never_delivers_changed_frozen_context(world, change):
    def mutate():
        if change == "artifact":
            world.attempt.artifacts["train.log"]["sha256"] = "0" * 64
        elif change == "owner":
            world.run.storage_ref["config_revision"] += 1
        elif change == "terminal":
            world.attempt.status = "uploading"
        else:
            world.attempt.run_id = world.project.id

    world.after_read = mutate
    with pytest.raises(HTTPException) as caught:
        await download(world, content=True)
    assert caught.value.status_code in {404, 409}
    assert len(world.reads) == 1 and all(body.closed for body in world.bodies)


async def test_publication_withdrawal_does_not_deliver_redacted_content(world):
    world.withdrawn = True
    with pytest.raises(HTTPException) as caught:
        await download(world, content=True)
    assert caught.value.detail["code"] == "read_publication_changed"
    assert all(body.closed for body in world.bodies)
    assert world.authority.await_count == 1


async def test_unknown_body_close_never_delivers_content(world):
    world.close_failure = True
    with pytest.raises(RuntimeError, match="close unknown"):
        await download(world, content=True)
    assert world.authority.await_count == 1
    assert world.client.calls == []
    for body in world.bodies:
        body.close_failure = False
        body.close()


@pytest.mark.parametrize("change", ["sha", "size", "metadata", "overflow"])
async def test_corrupt_log_is_rejected_before_any_content_response(world, change):
    if change == "sha":
        world.attempt.artifacts["train.log"]["sha256"] = "0" * 64
        world.response["Metadata"]["sha256"] = "0" * 64
    elif change == "size":
        world.attempt.artifacts["train.log"]["size_bytes"] -= 1
    elif change == "metadata":
        world.response["Metadata"]["attempt-id"] = "other-attempt"
    else:
        world.attempt.artifacts["train.log"]["size_bytes"] = (
            api.observations.LOG_BYTES + 1
        )
    # Keep copied-header evidence valid so this exercises the log's own checks.
    world.route = replace(
        world.route,
        target_content_sha256=copy.digest(copy.content_target(world.response)),
    )
    world.lookup.return_value = world.route
    with pytest.raises(HTTPException) as caught:
        await download(world, content=True)
    assert caught.value.status_code == 409
    assert all(body.closed for body in world.bodies)
    assert world.client.calls == []


async def test_content_endpoint_rejects_nonlog_without_storage_access(world):
    world.attempt.artifacts["train.log"]["role"] = "weights"
    with pytest.raises(HTTPException) as caught:
        await download(world, content=True)
    assert caught.value.status_code == 404
    assert not world.reads


async def test_missing_mapping_preserves_stopped_a_gate(world, monkeypatch):
    world.lookup.return_value = None
    original = AsyncMock(side_effect=ContentError("storage_stopped", 409))
    monkeypatch.setattr(training_storage_io, "run", original)
    with pytest.raises(HTTPException) as caught:
        await download(world, content=True)
    assert caught.value.detail["code"] == "storage_stopped"
    original.assert_awaited_once()
    assert not world.reads


@pytest.mark.parametrize("endpoint", ["content", "download"])
@pytest.mark.parametrize("failure", ["withdrawn", "stopped"])
async def test_log_http_failures_are_controlled_without_database_resources(
    world, monkeypatch, endpoint, failure
):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[api.get_current_user] = lambda: world.user
    app.dependency_overrides[api.require_project_owner] = lambda: world.project
    app.dependency_overrides[api.get_db] = lambda: world.db
    if failure == "withdrawn":
        world.withdrawn = True
        expected = "read_publication_changed"
    else:
        world.lookup.return_value = None
        monkeypatch.setattr(
            training_storage_io,
            "run",
            AsyncMock(side_effect=ContentError("storage_stopped", 409)),
        )
        expected = "storage_stopped"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.get(
            f"/api/v1/projects/{world.project.id}/training-runs/{world.run.id}"
            f"/attempts/{world.attempt.id}/artifacts/train.log/{endpoint}"
        )
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == expected
    assert "private" not in result.text
    assert all(body.closed for body in world.bodies)


async def test_log_http_conversion_preserves_unsettled_responsibility(
    world, monkeypatch
):
    failure = TrainingDatasetError("storage_stopped", "stopped", status_code=409)
    failure.storage_operation_unsettled = True
    monkeypatch.setattr(training_storage_io, "read", AsyncMock(side_effect=failure))
    with pytest.raises(HTTPException) as caught:
        await download(world, content=True)
    assert caught.value.status_code == 409
    assert caught.value.storage_operation_unsettled is True


@pytest.mark.parametrize("stage", ["before", "after"])
async def test_unproven_log_owner_has_a_controlled_http_rejection(world, stage):
    if stage == "before":
        world.run.storage_ref = None
    else:
        world.after_read = lambda: setattr(world.run, "storage_ref", None)
    with pytest.raises(HTTPException) as caught:
        await download(world, content=True)
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "training_storage_unpinned"
    assert len(world.reads) == (0 if stage == "before" else 1)


def test_log_content_openapi_declares_the_actual_plaintext_response():
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    path = (
        "/api/v1/projects/{project_id}/training-runs/{run_id}"
        "/attempts/{attempt_id}/artifacts/{name}/content"
    )
    assert app.openapi()["paths"][path]["get"]["responses"]["200"]["content"] == {
        "text/plain": {}
    }
