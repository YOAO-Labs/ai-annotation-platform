"""Typed ready and failure provenance for retained video predictions."""

import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from aap_assets_video_inputs import evidence as e
from aap_assets_video_inputs import execution as c
from video_input_fixtures import frame


def test_evidence_contract_artifact_fixes_types_keys_and_native_owner_boundary():
    artifact = Path(e.__file__).with_name("evidence.schema.json")
    assert json.loads(artifact.read_text()) == e.contract_document()
    assert e.ASSETS_PIN_OPERATION.signature == (
        "aap_assets_v1._lock_accepted_video_input_pin(uuid,uuid,jsonb,uuid)"
    )
    assert e.ASSETS_PIN_OPERATION.role == "labeling"
    assert e.contract_document()["x-native"]["assets_pin"]["owner"] == "assets"
    assert set(e.LABELING_OPERATIONS) == {
        "attach_annotation_video_input_source",
        "copy_annotation_video_input_sources",
        "accepted_video_input_referenced",
    }
    assert e.contract_document()["x-prediction-extra-key"] == "accepted_video_input"


def test_ready_evidence_is_derived_from_the_exact_retained_frame_record():
    retained = frame("ready")
    evidence = e.ready_evidence(retained.plan, retained)
    encoded = evidence.model_dump(mode="json", by_alias=True)
    assert encoded == {
        "schema": e.EVIDENCE_SCHEMA,
        "input_id": str(retained.plan.input.input_id),
        "job_id": str(retained.plan.input.job_id),
        "task_id": str(retained.plan.input.task.task_id),
        "project_id": str(retained.plan.input.task.project_id),
        "plan_digest": retained.plan.plan_digest,
        "timebase_digest": retained.plan.timebase_digest,
        "frame_id": str(retained.frame_id),
        "entry": retained.entry.model_dump(mode="json"),
        "source_sha256": retained.plan.input.blob.sha256,
        "frame_sha256": retained.blob.sha256,
    }
    assert e.parse_prediction_evidence(encoded) == evidence
    assert e.parse_prediction_evidence(json.dumps(encoded)) == evidence
    assert e.prediction_extra(evidence) == {"accepted_video_input": encoded}


def test_prediction_evidence_rejects_untyped_image_ids_and_noncanonical_objects():
    retained = frame("ready")
    encoded = e.ready_evidence(retained.plan, retained).model_dump(
        mode="json", by_alias=True
    )
    with pytest.raises(e.VideoEvidenceFailure, match="invalid_response"):
        e.parse_prediction_evidence({"input_id": encoded["input_id"]})
    for mutate in (
        lambda value: value.update(extra=True),
        lambda value: value.update(plan_digest="F" * 64),
        lambda value: value.update(frame_id=str(UUID(encoded["frame_id"]).hex)),
    ):
        changed = json.loads(json.dumps(encoded))
        mutate(changed)
        with pytest.raises(e.VideoEvidenceFailure, match="invalid_response"):
            e.parse_prediction_evidence(changed)


def test_failed_evidence_keeps_original_plan_and_time_even_before_frame_creation():
    retained = frame("ready")
    failed = e.failure_evidence(retained.plan, retained.entry)
    encoded = failed.model_dump(mode="json", by_alias=True)
    assert encoded["schema"] == e.FAILURE_SCHEMA
    assert encoded["frame_id"] is None and encoded["frame_sha256"] is None
    assert encoded["source_sha256"] == retained.plan.input.blob.sha256
    assert e.parse_prediction_evidence(encoded) == failed
    assert e.prediction_extra(failed) == {"accepted_video_input": encoded}

    partial = e.failure_evidence(
        retained.plan, retained.entry, frame_id=retained.frame_id
    )
    assert partial.frame_sha256 is None
    with pytest.raises(ValidationError):
        e.VideoInputFailureEvidence.model_validate(
            {**encoded, "frame_sha256": "f" * 64}
        )
    with pytest.raises(ValueError, match="outside its retained input plan"):
        e.failure_evidence(
            retained.plan,
            retained.entry.model_copy(
                update={"frame_index": retained.entry.frame_index + 1}
            ),
        )


def test_assets_pin_request_is_bound_to_current_annotation_scope():
    retained = frame("ready")
    evidence = e.ready_evidence(retained.plan, retained)
    request = e.LockVideoInputPinRequest(
        task_id=retained.plan.input.task.task_id,
        project_id=retained.plan.input.task.project_id,
        evidence=evidence,
        actor_id=None,
    )
    assert request.evidence == evidence
    assert request.actor_id is None
    with pytest.raises(ValidationError):
        e.AttachAnnotationVideoInputSourceRequest(
            annotation_id=UUID(int=1), actor_id=None, evidence=evidence
        )


def test_video_frame_failure_cannot_be_promoted_to_ready_evidence():
    retained = frame("failed")
    with pytest.raises(ValueError, match="requires a retained frame blob"):
        e.ready_evidence(retained.plan, retained)


def test_labeling_native_reply_is_typed_and_scope_checked():
    retained = frame("ready")
    evidence = e.ready_evidence(retained.plan, retained)
    request = e.AttachAnnotationVideoInputSourceRequest(
        annotation_id=UUID(int=5),
        actor_id=UUID(int=6),
        evidence=evidence,
    )
    response = {
        "schema": e.LABELING_SCHEMA,
        "ok": True,
        "data": {
            "annotation_id": str(request.annotation_id),
            "input_id": str(evidence.input_id),
            "plan_digest": evidence.plan_digest,
            "ordinal": evidence.entry.ordinal,
            "attached": True,
        },
    }
    assert e.parse_labeling_native(
        "attach_annotation_video_input_source", request, response
    ).attached
    response["data"]["input_id"] = str(UUID(int=7))
    with pytest.raises(e.VideoEvidenceFailure, match="invalid_response"):
        e.parse_labeling_native(
            "attach_annotation_video_input_source", request, response
        )


def test_private_assets_pin_reply_must_match_the_annotation_evidence_exactly():
    retained = frame("ready")
    evidence = e.ready_evidence(retained.plan, retained)
    request = e.LockVideoInputPinRequest(
        task_id=evidence.task_id,
        project_id=evidence.project_id,
        evidence=evidence,
        actor_id=UUID(int=6),
    )
    response = {
        "schema": c.EXECUTION_SCHEMA,
        "ok": True,
        "data": {"frame": retained.model_dump(mode="json")},
    }
    assert e.parse_assets_pin(request, response).frame == retained
    response["data"]["frame"]["blob"]["sha256"] = "0" * 64
    with pytest.raises(e.VideoEvidenceFailure, match="invalid_response"):
        e.parse_assets_pin(request, response)


def test_existing_video_execution_contract_still_owns_frame_entry_validation():
    assert "lock_accepted_video_frame" in c.OPERATIONS
    retained = frame("ready")
    evidence = e.ready_evidence(retained.plan, retained)
    assert evidence.entry == retained.entry
