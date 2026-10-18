from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from aap_assets_media.contract import (
    MEDIA_SCHEMA,
    OPERATIONS,
    BackfillSelection,
    BackfillSpec,
    ClaimedWork,
    ClaimRequest,
    InvalidateRequest,
    Invalidation,
    InternalSourceRequest,
    MediaClaim,
    MediaFailure,
    ReadSourceRequest,
    RequestWorkRequest,
    ScheduleWorkRequest,
    MetadataSpec,
    WorkTiming,
)
from aap_assets_media_runtime.database import (
    AsyncpgMediaDatabase,
    prepare_native_call,
    validate_native_result,
)


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.transactions.append("rollback" if exc_type else "commit")
        return False


class _Connection:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []
        self.transactions = []

    def transaction(self):
        return _Transaction(self)

    async def fetchval(self, statement, *parameters):
        self.calls.append((statement, parameters))
        return self.raw


class _Pool:
    def __init__(self, raw):
        self.connection = _Connection(raw)
        self.acquisitions = 0

    @asynccontextmanager
    async def acquire(self):
        self.acquisitions += 1
        yield self.connection


def _ok(data):
    return {"schema": MEDIA_SCHEMA, "ok": True, "data": data}


@pytest.mark.asyncio
async def test_asyncpg_binding_uses_exact_signature_and_commits_before_return():
    raw = _ok({"claim": None})
    pool = _Pool(raw)
    database = AsyncpgMediaDatabase(pool, allowed_roles={"worker"})
    request = ClaimRequest(worker_id=uuid4(), kinds=["frame"])

    result = await database.call("claim_video_media", request)

    statement, parameters = pool.connection.calls[0]
    operation = OPERATIONS["claim_video_media"]
    assert "aap_assets_v1.claim_video_media(" in statement
    assert all(
        f"${i}::{sql_type}" in statement
        for i, sql_type in enumerate(operation.sql_types, 1)
    )
    assert parameters[0] == request.worker_id
    assert result.claim is None
    assert pool.connection.transactions == ["commit"]


def test_json_arguments_are_serialized_under_frozen_function_signature():
    request = ScheduleWorkRequest(
        operation_id=uuid4(),
        dataset_item_id=None,
        spec=BackfillSpec(selection=BackfillSelection(dataset_id=uuid4())),
    )
    statement, parameters = prepare_native_call("schedule_video_media", request)
    assert statement == (
        "SELECT aap_assets_v1.schedule_video_media("
        "$1::uuid, $2::uuid, $3::jsonb, $4::boolean)"
    )
    assert parameters[0] == request.operation_id
    assert parameters[1] is None
    assert '"kind": "video_backfill"' in parameters[2]
    assert parameters[3] is False


def test_optional_jsonb_none_is_bound_as_sql_null():
    request = InternalSourceRequest(dataset_item_id=uuid4())
    statement, parameters = prepare_native_call(
        "resolve_internal_media_source", request
    )
    assert statement == (
        "SELECT aap_assets_v1.resolve_internal_media_source("
        "$1::uuid, $2::text, $3::jsonb)"
    )
    assert parameters == (request.dataset_item_id, None, None)


@pytest.mark.asyncio
async def test_wrong_login_scope_is_rejected_before_database_access():
    pool = _Pool(_ok({}))
    database = AsyncpgMediaDatabase(pool, allowed_roles={"worker"})
    request = ReadSourceRequest(actor_id=uuid4(), dataset_item_id=uuid4())
    with pytest.raises(PermissionError):
        await database.call("read_media_source", request)
    assert pool.acquisitions == 0


@pytest.mark.asyncio
async def test_invalid_native_reply_rolls_back_before_returning_error():
    pool = _Pool({"schema": "wrong/v1", "ok": True, "data": {"claim": None}})
    database = AsyncpgMediaDatabase(pool, allowed_roles={"worker"})
    with pytest.raises(MediaFailure, match="invalid_response"):
        await database.call("claim_video_media", ClaimRequest(worker_id=uuid4()))
    assert pool.connection.transactions == ["rollback"]


def test_result_scope_rejects_source_substitution():
    request = ReadSourceRequest(actor_id=uuid4(), dataset_item_id=uuid4())
    wrong = {
        "schema": MEDIA_SCHEMA,
        "ok": True,
        "data": {
            "dataset_item_id": str(uuid4()),
            "dataset_id": str(uuid4()),
            "source_generation": 1,
            "source_snapshot": "a" * 64,
            "timetable_revision": 0,
            "materialized_generation": 1,
            "object_identity": None,
            "source_drifted": False,
            "file_name": "clip.mp4",
            "file_path": "dataset/clip.mp4",
            "file_type": "video",
            "file_size": None,
            "content_hash": None,
            "width": None,
            "height": None,
            "thumbnail_path": None,
            "blurhash": None,
            "video_metadata": {},
        },
    }
    with pytest.raises(MediaFailure, match="invalid_response"):
        validate_native_result("read_media_source", request, wrong)


def test_claim_result_must_match_requested_job_and_work_kind():
    request = ClaimRequest(worker_id=uuid4(), job_id=uuid4(), kinds=["video_backfill"])
    claim = MediaClaim(
        job_id=uuid4(),
        generation=1,
        lease_token=uuid4(),
        lease_expires_at="2026-10-19T00:00:00Z",
        source=None,
        spec=BackfillSpec(selection=BackfillSelection(dataset_id=uuid4())),
        output_prefix=None,
        timetable_revision=0,
        timing=WorkTiming(),
    )
    result = ClaimedWork(claim=claim)
    reply = _ok(result.model_dump(mode="json"))

    with pytest.raises(MediaFailure, match="invalid_response"):
        validate_native_result("claim_video_media", request, reply)


def test_request_budget_is_checked_before_call():
    request = InvalidateRequest(
        actor_id=uuid4(),
        dataset_item_id=uuid4(),
        invalidation=Invalidation(
            source_snapshot="a" * 64,
            expected_key="media-assets/" + "k" * (8 * 1024 * 1024),
            kind="playback",
        ),
    )
    with pytest.raises(MediaFailure, match="resource_budget_exceeded"):
        prepare_native_call("invalidate_video_media", request)


def test_request_pair_must_match_registered_contract_model():
    request = RequestWorkRequest(
        operation_id=uuid4(),
        actor_id=uuid4(),
        dataset_item_id=uuid4(),
        spec=MetadataSpec(),
    )
    with pytest.raises(TypeError, match="operation/request"):
        prepare_native_call("retry_video_media", request)
