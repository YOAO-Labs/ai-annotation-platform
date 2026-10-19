from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode
from uuid import uuid4

import pytest

from aap_assets_inputs.contract import InputFailure
from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs_runtime.control import VideoInputControl
from video_input_fixtures import CURRENT_ACTOR, frame, manifest, member, record, source


def setup(*, member_=None):
    header = manifest()
    selected = member_ or member()
    if selected.state in {"ready", "failed"}:
        header = header.model_copy(update={"prepared_count": 1})
    page = c.VideoManifestPage(
        manifest=header, cursor=0, next_cursor=None, members=(selected,)
    )
    preparing = c.VideoMemberReceipt(manifest=header, member=member("preparing"))
    database = SimpleNamespace(call=AsyncMock(side_effect=[page, preparing]))
    storage = SimpleNamespace(
        assert_versioning=AsyncMock(),
        head_source=AsyncMock(return_value=source().object_identity),
        verify_blob=AsyncMock(),
        sign_blob=AsyncMock(),
    )
    request = c.PrepareVideoMemberRequest(
        job_id=header.job_id,
        actor_id=CURRENT_ACTOR,
        project_id=header.project_id,
        selection_digest=header.selection_digest,
        ordinal=1,
        task_id=selected.task.task_id,
    )
    return VideoInputControl(database, storage), database, storage, request


async def test_prepare_observes_original_unknown_identity_without_rereading_a_current_source():
    control, database, storage, request = setup()
    receipt = await control.prepare(request)
    assert receipt.member.state == "preparing"
    observation = database.call.await_args_list[-1].args[1]
    assert observation.actor_id == CURRENT_ACTOR
    assert observation.source_snapshot == source().source_snapshot
    assert observation.timetable_revision == source().timetable_revision
    assert observation.object_identity == source().object_identity
    assert observation.terminal_error is None
    assert storage.head_source.await_args.args[0] == source(observed=False)


@pytest.mark.parametrize("state", ["preparing", "ready", "failed"])
async def test_prepare_replay_returns_canonical_member_before_any_source_head(state):
    control, database, storage, request = setup(member_=member(state))
    assert (await control.prepare(request)).member.state == state
    assert database.call.await_count == 1
    storage.assert_versioning.assert_not_awaited()
    storage.head_source.assert_not_awaited()


@pytest.mark.parametrize(
    "fault,error",
    [
        ("replacement", "source_changed"),
        ("missing", "source_missing"),
        ("zero_bytes", "source_missing"),
    ],
)
async def test_source_observation_records_a_fixed_failure_without_retargeting(
    fault, error
):
    selected = member().model_copy(update={"source": source()})
    control, database, storage, request = setup(member_=selected)
    if fault == "replacement":
        storage.head_source.return_value = source().object_identity.model_copy(
            update={"etag": "new"}
        )
    elif fault == "missing":
        storage.head_source.side_effect = InputFailure("object_missing")
    else:
        selected = member().model_copy(
            update={
                "source": source(observed=False).model_copy(update={"file_size": None})
            }
        )
        control, database, storage, request = setup(member_=selected)
        storage.head_source.return_value = source().object_identity.model_copy(
            update={"byte_size": 0}
        )
    receipt = c.VideoMemberReceipt(
        manifest=manifest().model_copy(update={"prepared_count": 1}),
        member=member("failed").model_copy(update={"error": error}),
    )
    database.call.side_effect = [database.call.side_effect.__next__(), receipt]
    assert (await control.prepare(request)).member.error == error
    observation = database.call.await_args_list[-1].args[1]
    assert observation.object_identity is None and observation.terminal_error == error


async def test_transient_storage_outage_is_not_frozen_as_a_member_failure():
    control, database, storage, request = setup()
    storage.head_source.side_effect = InputFailure("storage_unavailable")
    with pytest.raises(c.VideoInputFailure, match="storage_unavailable"):
        await control.prepare(request)
    assert database.call.await_count == 1


def input_request(accepted):
    return c.LockVideoInputRequest(
        input_id=accepted.input_id,
        job_id=accepted.job_id,
        task_id=accepted.task.task_id,
        actor_id=CURRENT_ACTOR,
        plan_digest=record("ready").plan.plan_digest,
    )


@pytest.mark.parametrize(
    "state,error",
    [
        ("pending", "input_not_ready"),
        ("expired", "input_expired"),
        ("failed", "processing_failed"),
    ],
)
async def test_original_verification_never_uses_an_unready_or_expired_input(
    state, error
):
    control, database, storage, _ = setup()
    accepted = record(state)
    database.call = AsyncMock(
        return_value=c.VideoInputWork(input=accepted, source=source())
    )
    with pytest.raises(c.VideoInputFailure, match=error):
        await control.verify(input_request(accepted))
    storage.verify_blob.assert_not_awaited()


async def test_preoverwrite_verification_checks_exact_original_version_and_plan():
    control, database, storage, _ = setup()
    accepted = record("ready")
    database.call = AsyncMock(
        return_value=c.VideoInputWork(input=accepted, source=source())
    )
    assert await control.verify(input_request(accepted)) == accepted
    storage.verify_blob.assert_awaited_once_with(accepted.plan.input.blob)
    storage.head_source.assert_awaited_once_with(source())
    with pytest.raises(c.VideoInputFailure, match="input_scope_mismatch"):
        await control.verify(
            input_request(accepted).model_copy(update={"plan_digest": "f" * 64})
        )


async def test_frame_reference_signs_exact_retained_version_and_never_resolves_current_url():
    control, database, storage, _ = setup()
    ready = frame("ready")
    video = ready.plan.input
    request = c.VideoFrameRequest(
        actor_id=CURRENT_ACTOR,
        input_id=video.input_id,
        job_id=video.job_id,
        task_id=video.task.task_id,
        plan_digest=ready.plan.plan_digest,
        ordinal=1,
    )
    database.call = AsyncMock(return_value=ready)
    url = (
        "https://storage.example/accepted/"
        + ready.blob.key
        + "?"
        + urlencode({"versionId": ready.blob.version_id})
    )
    storage.sign_blob.return_value = url
    reference = await control.reference(request)
    assert reference.blob == ready.blob and reference.url == url
    storage.verify_blob.assert_awaited_once_with(ready.blob)
    storage.sign_blob.assert_awaited_once_with(ready.blob, expires_in=900)
    storage.head_source.assert_not_awaited()
    storage.verify_blob.side_effect = InputFailure("input_corrupt")
    with pytest.raises(c.VideoInputFailure, match="input_corrupt"):
        await control.reference(request)
    assert storage.sign_blob.await_count == 1


async def test_prepare_rejects_crossed_member_before_object_io():
    control, database, storage, request = setup()
    with pytest.raises(c.VideoInputFailure, match="manifest_scope_mismatch"):
        await control.prepare(request.model_copy(update={"task_id": uuid4()}))
    storage.head_source.assert_not_awaited()


@pytest.mark.parametrize(
    "fault,error", [("changed", "source_changed"), ("missing", "source_missing")]
)
async def test_preoverwrite_checks_captured_source_identity_without_resolving_new_task_path(
    fault, error
):
    control, database, storage, _ = setup()
    accepted = record("ready")
    database.call = AsyncMock(
        return_value=c.VideoInputWork(input=accepted, source=source())
    )
    if fault == "changed":
        storage.head_source.return_value = source().object_identity.model_copy(
            update={"etag": "new"}
        )
    else:
        storage.head_source.side_effect = InputFailure("object_missing")
    with pytest.raises(c.VideoInputFailure, match=error):
        await control.verify(input_request(accepted))
    storage.verify_blob.assert_not_awaited()
    storage.head_source.assert_awaited_once_with(source())
