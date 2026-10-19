from datetime import datetime, timezone
from uuid import uuid4

import pytest

from aap_assets_images.contract import ObjectIdentity
from aap_assets_input_manifests import contract as c
from aap_assets_input_manifests_runtime.database import (
    AsyncpgManifestDatabase,
    prepare_native_call,
    validate_native_result,
)


def scope():
    return {"job_id": uuid4(), "actor_id": uuid4(), "project_id": uuid4()}


def header(**changes):
    return c.ManifestHeader(
        **{
            **scope(),
            "selection": {"predict_mode": "append"},
            "selection_digest": "a" * 64,
            "candidate_count": 1,
            "selected_count": 1,
            "prepared_count": 0,
            "status": "preparing",
            "ready_count": None,
            "failed_count": None,
            "sealed_digest": None,
            "created_at": datetime.now(timezone.utc),
            "sealed_at": None,
            **changes,
        }
    )


def envelope(value):
    return {"schema": c.MANIFEST_SCHEMA, "ok": True, "data": value}


def test_binding_freezes_names_arguments_and_denies_workflow_capture():
    request = c.NativePrepareManifestMemberRequest(
        **scope(),
        selection_digest="a" * 64,
        ordinal=7,
        task_id=uuid4(),
        source_snapshot="b" * 64,
        object_identity=ObjectIdentity(etag="s3-etag", byte_size=123),
        terminal_error=None,
    )
    sql, arguments = prepare_native_call(
        "prepare_accepted_image_input_manifest_member",
        request,
        allowed_roles={"control"},
    )
    assert sql == (
        "SELECT aap_assets_v1.prepare_accepted_image_input_manifest_member("
        "$1::uuid, $2::uuid, $3::uuid, $4::text, $5::bigint, $6::uuid, "
        "$7::text, $8::jsonb, $9::text)"
    )
    assert arguments[:7] == (
        request.job_id,
        request.actor_id,
        request.project_id,
        request.selection_digest,
        request.ordinal,
        request.task_id,
        request.source_snapshot,
    )
    assert arguments[-1] is None
    with pytest.raises(PermissionError):
        prepare_native_call(
            "capture_accepted_image_input_manifest",
            c.CaptureManifestRequest(**scope(), selection={"predict_mode": "append"}),
            allowed_roles={"control"},
        )
    with pytest.raises(TypeError):
        prepare_native_call(
            "prepare_accepted_image_input_manifest_member; SELECT 1",
            request,
            allowed_roles={"control"},
        )


def test_native_reply_requires_matching_manifest_scope_and_digest():
    request = c.ManifestScope(**scope())
    value = header(**request.model_dump())
    assert (
        validate_native_result(
            "read_accepted_image_input_manifest", request, envelope(value)
        )
        == value
    )
    crossed = value.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(c.ManifestFailure, match="invalid_response"):
        validate_native_result(
            "read_accepted_image_input_manifest", request, envelope(crossed)
        )


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.events.append("begin")

    async def __aexit__(self, exc_type, *_args):
        self.connection.events.append("rollback" if exc_type else "commit")


class FakeConnection:
    def __init__(self, raw):
        self.raw = raw
        self.events = []
        self.calls = []

    def transaction(self):
        return FakeTransaction(self)

    async def execute(self, query, value):
        self.events.append("timeout")

    async def fetchval(self, query, *args, **_kwargs):
        self.calls.append((query, args))
        return self.raw


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self, **_kwargs):
        return FakeAcquire(self.connection)


@pytest.mark.asyncio
async def test_asyncpg_binding_validates_typed_reply_before_commit():
    request = c.ManifestScope(**scope())
    valid = header(**request.model_dump())
    connection = FakeConnection(envelope(valid))
    database = AsyncpgManifestDatabase(FakePool(connection))

    assert await database.call("read_accepted_image_input_manifest", request) == valid
    assert connection.events == ["begin", "timeout", "commit"]
    assert connection.calls[0][0].startswith(
        "SELECT aap_assets_v1.read_accepted_image_input_manifest($1::uuid"
    )

    connection = FakeConnection(
        envelope(valid.model_copy(update={"project_id": uuid4()}))
    )
    database = AsyncpgManifestDatabase(FakePool(connection))
    with pytest.raises(c.ManifestFailure, match="invalid_response"):
        await database.call("read_accepted_image_input_manifest", request)
    assert connection.events == ["begin", "timeout", "rollback"]
