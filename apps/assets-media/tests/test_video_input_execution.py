import json
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError
import pytest

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs import values as v
from video_input_fixtures import (
    CURRENT_ACTOR,
    FRAME_LEASE,
    LEASE,
    claim,
    frame,
    manifest,
    member,
    record,
    source,
    time_entries,
)


def reply(operation, request, result):
    return c.parse_native(
        operation,
        request,
        {
            "schema": c.EXECUTION_SCHEMA,
            "ok": True,
            "data": result.model_dump(mode="json"),
        },
    )


def test_execution_artifact_fixes_exact_native_signatures_roles_and_budgets():
    artifact = Path(c.__file__).with_name("execution.schema.json")
    assert json.loads(artifact.read_text()) == c.contract_document()
    assert len(c.OPERATIONS) == 20
    counts = {}
    for operation in c.OPERATIONS.values():
        assert len(operation.arguments) == len(operation.sql_types)
        assert set(operation.arguments) == set(operation.request.model_fields)
        counts[operation.role] = counts.get(operation.role, 0) + 1
    assert counts == {
        "workflow": 1,
        "control": 8,
        "worker": 9,
        "labeling": 1,
        "system": 1,
    }


def test_missing_capture_link_is_fixed_failure_not_a_ready_video_binding():
    raw = member("failed").model_dump(mode="json")
    raw["task"]["dataset_item_id"] = None
    raw["source"] = None
    failed = c.VideoManifestMember.model_validate(raw)
    assert failed.error == "source_missing" and failed.input is None
    with pytest.raises(ValidationError):
        failed.task.binding()
    raw["state"], raw["error"] = "unprepared", None
    with pytest.raises(ValidationError):
        c.VideoManifestMember.model_validate(raw)


@pytest.mark.parametrize(
    "fault",
    ["missing_plan", "input_id", "created_at", "sampling", "status", "error", "link"],
)
def test_input_records_require_original_ready_plan_and_explicit_failure(fault):
    raw = record("ready").model_dump(mode="json")
    if fault == "missing_plan":
        raw["plan"] = None
    elif fault == "input_id":
        raw["input_id"] = str(uuid4())
    elif fault == "created_at":
        raw["created_at"] = "2026-10-20T00:00:00Z"
    elif fault == "sampling":
        raw["sampling"]["max_frames"] += 1
    elif fault == "status":
        raw["status"] = "materializing"
    elif fault == "error":
        raw["error"] = "processing_failed"
    else:
        raw["task"]["dataset_item_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        c.VideoInputRecord.model_validate(raw)


@pytest.mark.parametrize(
    "fault",
    [
        "gap",
        "source",
        "timebase",
        "sampling",
        "unfinished",
        "missing",
        "cursor",
        "counts",
    ],
)
def test_manifest_pages_reject_changed_membership_or_incomplete_barrier(fault):
    raw = c.VideoManifestPage(
        manifest=manifest(sealed=True),
        cursor=0,
        next_cursor=None,
        members=(member("ready"),),
    ).model_dump(mode="json")
    if fault == "gap":
        raw["members"][0]["ordinal"] = 2
    elif fault == "source":
        raw["members"][0]["source"]["source_generation"] += 1
    elif fault == "timebase":
        raw["members"][0]["source"]["timetable_revision"] += 1
    elif fault == "sampling":
        raw["manifest"]["selection"]["sampling"]["width"] += 1
    elif fault == "unfinished":
        raw["members"] = [member().model_dump(mode="json")]
    elif fault == "missing":
        raw["members"] = []
    elif fault == "cursor":
        raw["next_cursor"] = 1
    else:
        raw["manifest"]["failed_count"] = 1
    with pytest.raises(ValidationError):
        c.VideoManifestPage.model_validate(raw)


def test_original_actor_is_attribution_and_current_actor_is_separate():
    original = record("ready")
    request = c.ReadVideoInputRequest(
        input_id=original.input_id,
        job_id=original.job_id,
        task_id=original.task.task_id,
        actor_id=CURRENT_ACTOR,
    )
    assert reply("read_accepted_video_input", request, original) == original
    read = c.VideoManifestScope(
        job_id=original.job_id,
        actor_id=CURRENT_ACTOR,
        project_id=original.task.project_id,
    )
    assert (
        reply("read_accepted_video_manifest", read, manifest()).actor_id
        == original.actor_id
    )
    capture = c.CaptureVideoManifestRequest(
        **read.model_dump(), selection=manifest().selection
    )
    with pytest.raises(c.VideoInputFailure, match="invalid_response"):
        reply("capture_accepted_video_manifest", capture, manifest())


def test_original_unknown_identity_can_be_observed_once_without_retargeting():
    pending = member("preparing")
    assert pending.source.object_identity is None
    assert pending.input.source.object_identity.byte_size == 4096
    raw = pending.model_dump(mode="json")
    raw["source"]["object_identity"] = {
        "etag": "replacement",
        "byte_size": 4096,
        "version_id": None,
    }
    with pytest.raises(ValidationError):
        c.VideoManifestMember.model_validate(raw)
    with pytest.raises(ValidationError):
        c.VideoInputWork(input=record(), source=source(observed=False))


@pytest.mark.parametrize(
    "fault", ["empty", "both", "missing_revision", "missing_snapshot"]
)
def test_observation_requires_one_complete_identity_or_fixed_failure(fault):
    header = manifest()
    raw = dict(
        job_id=header.job_id,
        actor_id=CURRENT_ACTOR,
        project_id=header.project_id,
        selection_digest=header.selection_digest,
        ordinal=1,
        task_id=member().task.task_id,
        source_snapshot=source().source_snapshot,
        timetable_revision=source().timetable_revision,
        object_identity=source().object_identity,
        terminal_error=None,
    )
    if fault == "empty":
        raw["object_identity"] = None
    elif fault == "both":
        raw["terminal_error"] = "source_changed"
    elif fault == "missing_revision":
        raw["timetable_revision"] = None
    else:
        raw["source_snapshot"] = None
    with pytest.raises(ValidationError):
        c.ObserveVideoMemberRequest(**raw)


@pytest.mark.parametrize("fault", ["generation", "lease", "status", "source"])
def test_claim_never_changes_original_source_or_writer_attempt(fault):
    raw = claim().model_dump(mode="json")
    if fault == "generation":
        raw["generation"] += 1
    elif fault == "lease":
        raw["lease_token"] = str(uuid4())
    elif fault == "status":
        raw["input"]["status"] = "pending"
    else:
        raw["source"]["object_identity"]["etag"] = "replacement"
    with pytest.raises(ValidationError):
        c.VideoInputClaim.model_validate(raw)


def test_time_staging_is_bounded_contiguous_and_attempt_scoped():
    accepted = record()
    request = c.StageVideoTimesRequest(
        input_id=accepted.input_id,
        generation=1,
        lease_token=LEASE,
        cursor=0,
        entries=time_entries(),
    )
    receipt = c.StagedVideoTimes(
        input_id=accepted.input_id, generation=1, lease_token=LEASE, next_cursor=3
    )
    assert reply("stage_accepted_video_times", request, receipt) == receipt
    for changed in (
        {"generation": 2},
        {"lease_token": uuid4()},
        {"input_id": uuid4()},
        {"next_cursor": 2},
    ):
        with pytest.raises(c.VideoInputFailure, match="invalid_response"):
            reply(
                "stage_accepted_video_times",
                request,
                receipt.model_copy(update=changed),
            )
    for changed in ({"cursor": 1}, {"entries": ()}, {"entries": time_entries() * 34}):
        with pytest.raises(ValidationError):
            c.StageVideoTimesRequest(**{**request.model_dump(), **changed})


@pytest.mark.parametrize(
    "fault", ["input", "rendition", "entry", "missing_blob", "failed"]
)
def test_ready_frame_receipt_binds_retained_input_and_exact_rendition(fault):
    raw = frame("ready").model_dump(mode="json")
    if fault == "input":
        raw["blob"]["key"] = raw["blob"]["key"].replace(
            str(record().input_id), str(uuid4())
        )
    elif fault == "rendition":
        raw["blob"]["key"] = raw["blob"]["key"].replace("w1280-jpeg", "w640-webp")
    elif fault == "entry":
        raw["entry"]["frame_index"] = 1
    elif fault == "missing_blob":
        raw["blob"] = None
    else:
        raw["status"] = "failed"
    with pytest.raises(ValidationError):
        c.VideoFrameRecord.model_validate(raw)


def test_frame_publication_and_final_write_fence_reject_changed_bytes_or_time():
    ready = frame("ready")
    finish = c.FinishVideoFrameRequest(
        frame_id=ready.frame_id, generation=2, lease_token=FRAME_LEASE, blob=ready.blob
    )
    assert reply("finish_accepted_video_frame", finish, ready) == ready
    with pytest.raises(ValidationError):
        c.FinishVideoFrameRequest(**{**finish.model_dump(), "generation": 1})
    video = ready.plan.input
    fence = c.LockVideoFrameRequest(
        actor_id=CURRENT_ACTOR,
        job_id=video.job_id,
        task_id=video.task.task_id,
        input_id=video.input_id,
        plan_digest=ready.plan.plan_digest,
        ordinal=ready.entry.ordinal,
        expected_entry=ready.entry,
        expected_blob=ready.blob,
    )
    assert reply("lock_accepted_video_frame", fence, ready) == ready
    for changed in (
        {"entry": ready.entry.model_copy(update={"pts_ms": ready.entry.pts_ms + 1})},
        {"blob": ready.blob.model_copy(update={"version_id": "replacement"})},
        {"status": "expired"},
    ):
        with pytest.raises(c.VideoInputFailure, match="invalid_response"):
            reply("lock_accepted_video_frame", fence, ready.model_copy(update=changed))


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_schema",
        "unknown_error",
        "numeric_ok",
        "extra",
        "nonfinite",
        "oversize",
        "uncanonical_selection",
    ],
)
def test_native_reply_parser_rejects_malformed_or_unbounded_envelopes(fault):
    header = manifest()
    request = c.VideoManifestScope(
        job_id=header.job_id, actor_id=CURRENT_ACTOR, project_id=header.project_id
    )
    raw = {
        "schema": c.EXECUTION_SCHEMA,
        "ok": True,
        "data": header.model_dump(mode="json"),
    }
    if fault == "wrong_schema":
        raw["schema"] = v.VIDEO_INPUT_SCHEMA
    elif fault == "unknown_error":
        raw = {
            "schema": c.EXECUTION_SCHEMA,
            "ok": False,
            "error": {"code": "unchecked"},
        }
    elif fault == "numeric_ok":
        raw["ok"] = 1
    elif fault == "extra":
        raw["extra"] = 1
    elif fault == "nonfinite":
        raw["data"]["candidate_count"] = float("inf")
    elif fault == "oversize":
        raw = " " * (v.REPLY_BYTES + 1)
    else:
        del raw["data"]["selection"]["sampling"]["width"]
    with pytest.raises(c.VideoInputFailure, match="invalid_response"):
        c.parse_native("read_accepted_video_manifest", request, raw)


def test_source_protocol_has_safe_integer_utf8_and_boolean_boundaries():
    for changed in (
        {"timetable_revision": v.SAFE_INTEGER + 1},
        {"file_size": True},
        {"file_path": "bad\ud800"},
        {"source_drifted": 1},
    ):
        with pytest.raises(ValidationError):
            c.VideoCaptureSource(**{**source().model_dump(), **changed})


@pytest.mark.parametrize(
    "fault", ["workbench_batch", "explicit_subset", "implicit_subset"]
)
def test_admission_never_silently_drops_selector_members_or_workbench_scope(fault):
    raw = manifest().model_dump(mode="json")
    if fault == "workbench_batch":
        raw["selection"]["execution_scope"] = "workbench"
        raw["selection"]["batch_id"] = str(uuid4())
        with pytest.raises(ValidationError):
            c.CaptureVideoManifestRequest(
                **{
                    name: raw[name]
                    for name in ("job_id", "actor_id", "project_id", "selection")
                }
            )
    elif fault == "explicit_subset":
        raw["candidate_count"] = raw["selected_count"] = 0
    else:
        raw["selection"]["task_ids"] = None
        raw["selected_count"] = 0
    with pytest.raises(ValidationError):
        c.VideoManifestHeader.model_validate(raw)


@pytest.mark.parametrize(
    "fault", ["pending_failure", "changed_failure", "expired_plan", "drifted_source"]
)
def test_member_outcomes_keep_historical_plan_and_coherent_terminal_errors(fault):
    if fault in {"pending_failure", "changed_failure"}:
        raw = member("preparing").model_dump(mode="json")
        raw["state"], raw["error"] = "failed", "source_changed"
        if fault == "changed_failure":
            raw["input"]["status"], raw["input"]["error"] = "failed", "source_missing"
        model = c.VideoManifestMember
    elif fault == "expired_plan":
        raw = record("expired").model_dump(mode="json")
        raw["plan"] = None
        model = c.VideoInputRecord
    else:
        raw = member().model_dump(mode="json")
        raw["source"]["source_drifted"] = True
        model = c.VideoManifestMember
    with pytest.raises(ValidationError):
        model.model_validate(raw)


def test_sealed_ready_outcome_survives_byte_expiry_without_granting_a_live_fence():
    historic = member("ready").model_copy(update={"input": record("expired")})
    assert (
        c.VideoManifestPage(
            manifest=manifest(sealed=True),
            cursor=0,
            next_cursor=None,
            members=(historic,),
        )
        .members[0]
        .input.plan
        is not None
    )
    with pytest.raises(ValidationError):
        c.VideoManifestPage(
            manifest=manifest(), cursor=0, next_cursor=None, members=(historic,)
        )


def test_linked_task_raw_path_is_independent_of_assets_transport_path():
    accepted = member("preparing")
    assert accepted.task.file_path == ""
    assert accepted.source.file_path == "dataset/original.mp4"
    assert accepted.task.dataset_item_id == accepted.source.dataset_item_id
