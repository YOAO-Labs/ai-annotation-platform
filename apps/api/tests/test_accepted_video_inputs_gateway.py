"""Application bridge checks using the frozen independent video fixture."""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs import values as v
from app.services.accepted_video_inputs import (
    SqlAlchemyVideoInputDatabase,
    VideoInputGateway,
    get_video_input_manifest_capture,
)

pytestmark = pytest.mark.asyncio
VECTOR = (
    Path(__file__).resolve().parents[2]
    / "assets-media/tests/fixtures/video-input-manifest-vector.json"
)


def facts():
    raw = json.loads(VECTOR.read_text())
    header = c.VideoManifestHeader.model_validate(raw["header"])
    members = tuple(c.VideoManifestMember.model_validate(row) for row in raw["members"])
    sealed = header.model_copy(
        update={
            "status": "sealed",
            "ready_count": 1,
            "failed_count": 1,
            "sealed_digest": raw["sealed_digest"],
            "sealed_at": header.created_at,
        }
    )
    scope = c.VideoManifestScope.model_validate(raw["scope"])
    return scope, header, members, sealed


def provider(header, members, sealed):
    return SimpleNamespace(
        manifest=AsyncMock(return_value=header),
        members=AsyncMock(
            return_value=c.VideoManifestPage(
                manifest=header, cursor=0, next_cursor=None, members=members
            )
        ),
        seal=AsyncMock(return_value=sealed),
        prepare=AsyncMock(),
    )


async def test_seal_accounts_for_fixed_failures_without_repreparing_ready_members():
    scope, header, members, sealed = facts()
    owner = provider(header, members, sealed)
    executor = SimpleNamespace(execute_once=AsyncMock())
    result = await VideoInputGateway(owner, executor=executor).ensure_sealed(
        scope, selection_digest=header.selection_digest
    )
    assert result == sealed
    owner.prepare.assert_not_awaited()
    executor.execute_once.assert_not_awaited()
    assert owner.seal.await_args.args[0].selection_digest == header.selection_digest


async def test_local_preparation_only_materializes_the_captured_input():
    scope, header, members, sealed = facts()
    ready = members[0]
    pending = ready.model_copy(
        update={
            "state": "preparing",
            "input": ready.input.model_copy(update={"status": "pending", "plan": None}),
        }
    )
    unprepared = ready.model_copy(update={"state": "unprepared", "input": None})
    owner = provider(
        header.model_copy(update={"prepared_count": 1}),
        (unprepared, members[1]),
        sealed,
    )
    owner.prepare.return_value = c.VideoMemberReceipt(
        manifest=owner.manifest.return_value, member=pending
    )
    executor = SimpleNamespace(execute_once=AsyncMock())
    await VideoInputGateway(owner, executor=executor).ensure_sealed(
        scope, selection_digest=header.selection_digest
    )
    executor.execute_once.assert_awaited_once_with(input_id=ready.input.input_id)
    request = owner.prepare.await_args.args[0]
    assert request.task_id == ready.task.task_id and request.ordinal == 1


async def test_remote_pending_seal_defers_without_fabricating_an_input_or_local_fallback():
    scope, header, members, sealed = facts()
    pending = members[0].model_copy(
        update={
            "state": "preparing",
            "input": members[0].input.model_copy(
                update={"status": "pending", "plan": None}
            ),
        }
    )
    owner = provider(
        header.model_copy(update={"prepared_count": 1}), (pending, members[1]), sealed
    )
    owner.seal.side_effect = c.VideoInputFailure("manifest_not_ready")
    with pytest.raises(c.VideoInputFailure, match="manifest_not_ready"):
        await VideoInputGateway(owner).ensure_sealed(
            scope, selection_digest=header.selection_digest
        )
    owner.prepare.assert_not_awaited()


async def test_preparation_is_bounded_and_a_different_manifest_never_reaches_io():
    scope, header, members, sealed = facts()
    owner = provider(header, members, sealed)
    with pytest.raises(c.VideoInputFailure, match="manifest_scope_mismatch"):
        await VideoInputGateway(owner).ensure_sealed(scope, selection_digest="a" * 64)
    owner.members.assert_not_awaited()
    owner.seal.assert_not_awaited()
    with pytest.raises(ValueError):
        VideoInputGateway(owner, preparation_limit=True)


async def test_frame_reference_materializes_only_the_retained_frame_and_checks_scope():
    _scope, _header, members, _sealed = facts()
    plan = members[0].input.plan
    request = c.VideoFrameRequest(
        actor_id=uuid4(),
        job_id=plan.input.job_id,
        task_id=plan.input.task.task_id,
        input_id=plan.input.input_id,
        plan_digest=plan.plan_digest,
        ordinal=1,
    )
    entry = v.FrameTimeEntry(ordinal=1, frame_index=0, pts_ms=-15, is_keyframe=True)
    blob = v.FrameBlob(
        key=f"accepted-video-inputs/{request.input_id}/g1/a{uuid4()}/frames/0/w1280-jpeg",
        version_id="exact-frame-version",
        etag="frame",
        byte_size=123,
        sha256="f" * 64,
    )
    frame = c.VideoFrameRecord(
        frame_id=uuid4(),
        plan=plan,
        entry=entry,
        status="pending",
        blob=None,
        error=None,
        created_at=plan.input.created_at,
    )
    reference = v.FrameReference(
        plan=plan,
        entry=entry,
        blob=blob,
        url=f"https://objects.test/accepted/{blob.key}?versionId=exact-frame-version",
        expires_in=900,
    )
    owner = SimpleNamespace(
        require_frame=AsyncMock(return_value=frame),
        reference=AsyncMock(return_value=reference),
    )
    executor = SimpleNamespace(execute_frame_once=AsyncMock())
    assert (
        await VideoInputGateway(owner, executor=executor).ensure_reference(request)
        == reference
    )
    executor.execute_frame_once.assert_awaited_once_with(frame_id=frame.frame_id)
    owner.reference.return_value = reference.model_copy(
        update={"entry": entry.model_copy(update={"ordinal": 2, "frame_index": 5})}
    )
    with pytest.raises(c.VideoInputFailure, match="invalid_response"):
        await VideoInputGateway(owner).reference(request)


class Database:
    def __init__(self, result):
        self.scalar = AsyncMock(return_value=result)
        self.flush, self.commit, self.rollback = AsyncMock(), AsyncMock(), AsyncMock()
        self.inside = False
        self.rolled_back_savepoint = False

    @asynccontextmanager
    async def begin_nested(self):
        self.inside = True
        try:
            yield
        except Exception:
            self.rolled_back_savepoint = True
            raise
        finally:
            self.inside = False


async def test_capture_joins_the_job_transaction_and_rejects_malformed_receipts():
    scope, header, _members, _sealed = facts()
    db = Database(
        {
            "schema": c.EXECUTION_SCHEMA,
            "ok": True,
            "data": header.model_dump(mode="json"),
        }
    )
    request = c.CaptureVideoManifestRequest(
        **scope.model_dump(), selection=header.selection
    )
    result = await get_video_input_manifest_capture()(db).capture(request)
    assert result == header
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()
    statement, parameters = db.scalar.await_args.args
    assert "capture_accepted_video_manifest" in str(statement)
    assert json.loads(parameters["selection"]) == header.selection.model_dump(
        mode="json"
    )
    db.scalar.return_value["data"]["job_id"] = str(uuid4())
    with pytest.raises(c.VideoInputFailure, match="invalid_response"):
        await get_video_input_manifest_capture()(db).capture(request)
    assert db.rolled_back_savepoint
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


async def test_native_role_boundary_rejects_capture_before_sql():
    scope, header, _members, _sealed = facts()
    db = Database(None)
    with pytest.raises(c.VideoInputFailure, match="invalid_request"):
        await SqlAlchemyVideoInputDatabase(db).call(
            "capture_accepted_video_manifest",
            c.CaptureVideoManifestRequest(
                **scope.model_dump(), selection=header.selection
            ),
        )
    db.scalar.assert_not_awaited()


async def test_bad_native_receipt_rolls_back_independent_transaction():
    scope, _header, _members, _sealed = facts()
    db = Database({"schema": "wrong", "ok": True, "data": {}})
    with pytest.raises(c.VideoInputFailure, match="invalid_response"):
        await SqlAlchemyVideoInputDatabase(db).call(
            "read_accepted_video_manifest", scope
        )
    assert db.rolled_back_savepoint
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
