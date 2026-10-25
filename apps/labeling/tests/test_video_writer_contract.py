"""Pure contract and legacy-transform checks for the private video writer."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

import aap_labeling.video_writer_contract as contract
from aap_assets_video_inputs.tracker import (
    TrackerExecutionSpec,
    TrackerVideoInputEvidence,
)
from aap_preannotation_results.video_execution import ExecutionProof


_FIXTURE = Path(__file__).parent / "fixtures" / "video-writer-legacy.json"
_RUN_ID = UUID("00000000-0000-0000-0000-0000000003f1")
_WORKER_ID = UUID("00000000-0000-0000-0000-0000000003f2")
_LEASE_ID = UUID("00000000-0000-0000-0000-0000000003f3")


def _corpus():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _proof(job_id: UUID) -> ExecutionProof:
    return ExecutionProof(
        job_id=job_id,
        run_id=_RUN_ID,
        worker_id=_WORKER_ID,
        generation=1,
        lease_token=_LEASE_ID,
    )


def _case_values(case: dict):
    evidence = TrackerVideoInputEvidence.model_validate(case["evidence"])
    execution = TrackerExecutionSpec.model_validate(case["execution"])
    expected_input = {
        "input_id": str(evidence.input_id),
        "request_digest": evidence.request_digest,
        "plan_digest": evidence.plan_digest,
        "source_version_id": evidence.source_version_id,
        "source_sha256": evidence.source_sha256,
    }
    command = contract.make_video_writer_command(
        _proof(evidence.tracker_job_id),
        outcome="completed",
        expected_input=expected_input,
        frame_results=case["frame_results"],
    )
    facts = SimpleNamespace(
        job=SimpleNamespace(
            job_kind=execution.job_kind,
            annotation_id=(
                UUID(case["job_annotation_id"])
                if case["job_annotation_id"] is not None
                else None
            ),
        ),
        effective_outcome="completed",
        acceptance=SimpleNamespace(execution=execution, evidence=evidence),
        execution_plan=SimpleNamespace(
            output=SimpleNamespace(geometry=case["output_geometry"]),
            windows=(),
        ),
    )
    return command, facts


def test_saved_legacy_transform_corpus_is_preserved(monkeypatch):
    corpus = _corpus()
    assert corpus["schema"] == "aap-video-writer-legacy-corpus/v1"
    assert len(corpus["cases"]) == 20

    for case in corpus["cases"]:
        command, facts = _case_values(case)
        try:
            if case["name"] == "candidate-size-refusal":
                with monkeypatch.context() as context:
                    context.setattr(
                        contract,
                        "MAX_VIDEO_WRITER_STAGED_BYTES",
                        case["staged_byte_limit"],
                    )
                    contract._staged_result(command, facts, command.frame_results)
                pytest.fail("the candidate-size fixture must be rejected")
            staged = contract._staged_result(command, facts, command.frame_results)
        except contract.VideoWriterRejected as error:
            assert case.get("expected_error") is not None, case["name"]
            expected_code = (
                "staged_result_too_large"
                if case["name"] == "candidate-size-refusal"
                else "source_map_invalid"
            )
            assert error.code == expected_code, case["name"]
            continue
        assert case.get("expected_error") is None, case["name"]
        assert staged == case["expected_staged_result"], case["name"]


def test_staged_budget_keeps_legacy_ascii_escape_accounting(monkeypatch):
    case = next(
        case for case in _corpus()["cases"] if case["name"] == "unicode-instance"
    )
    command, facts = _case_values(case)
    previous_bytes = json.dumps(
        case["expected_staged_result"], separators=(",", ":")
    ).encode("utf-8")
    monkeypatch.setattr(
        contract, "MAX_VIDEO_WRITER_STAGED_BYTES", len(previous_bytes) - 1
    )
    with pytest.raises(contract.VideoWriterRejected, match="staged_result_too_large"):
        contract._staged_result(command, facts, command.frame_results)


def test_correction_order_and_operation_digest_are_stable():
    case = next(
        item for item in _corpus()["cases"] if item["name"] == "both-correction-order"
    )
    command, _facts = _case_values(case)
    assert [row.frame_index for row in command.frame_results] == [3, 2, 5, 6]
    retry, _facts = _case_values(case)
    assert retry.operation_id == command.operation_id
    assert contract.video_writer_request_bytes(
        retry
    ) == contract.video_writer_request_bytes(command)
    assert contract.video_writer_command_digest(
        retry
    ) == contract.video_writer_command_digest(command)


def test_command_decoder_rejects_duplicate_nonfinite_and_extra_fields():
    case = _corpus()["cases"][0]
    command, _facts = _case_values(case)
    raw = contract.video_writer_command_bytes(command)
    duplicate = raw.replace(
        b'"outcome":"completed"',
        b'"outcome":"completed","outcome":"completed"',
        1,
    )
    with pytest.raises(contract.VideoWriterInvalidRequest):
        contract.decode_video_writer_command(duplicate)

    nonfinite = raw.replace(b'"confidence":0.9', b'"confidence":NaN', 1)
    with pytest.raises(contract.VideoWriterInvalidRequest):
        contract.decode_video_writer_command(nonfinite)

    payload = json.loads(raw)
    payload["actor_id"] = str(_WORKER_ID)
    with pytest.raises(contract.VideoWriterInvalidRequest):
        contract.decode_video_writer_command(payload)


def test_cyclic_python_values_are_rejected_without_serialization_leaks():
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(contract.VideoWriterInvalidRequest):
        contract.video_writer_request_bytes(cyclic)


def test_nested_mask_references_are_validated_and_deduplicated_by_value():
    case = next(item for item in _corpus()["cases"] if item["name"] == "mask")
    references = contract.collect_video_writer_mask_references(
        case["expected_staged_result"]
    )
    assert len(references) == 1
    assert references[0].object_key.startswith("raster-masks/sha256/")

    malformed = json.loads(json.dumps(case["expected_staged_result"]))
    malformed["results"][0]["geometry"]["mask"]["object_key"] = "outside/namespace"
    with pytest.raises(contract.VideoWriterRejected) as raised:
        contract.collect_video_writer_mask_references(malformed)
    assert raised.value.code == "candidate_invalid"


def test_checked_in_json_schema_matches_the_contract():
    from importlib.resources import files

    schema_path = files("aap_labeling").joinpath("video-writer.schema.json")
    assert (
        json.loads(schema_path.read_text(encoding="utf-8"))
        == contract.video_writer_schema()
    )


def test_native_reply_error_set_is_closed():
    request = contract.VideoWriterPolicyArguments()
    envelope = {
        "schema": contract.LABELING_VIDEO_WRITER_DATA_SCHEMA,
        "ok": False,
        "error": {"code": "permission_denied"},
    }
    with pytest.raises(contract.VideoWriterRejected) as raised:
        contract.decode_native_reply("policy", request, envelope)
    assert raised.value.code == "permission_denied"

    envelope["error"]["code"] = "provider_secret"
    with pytest.raises(contract.VideoWriterUnavailable):
        contract.decode_native_reply("policy", request, envelope)


def test_apply_receipt_must_echo_staged_digest_and_size():
    command = _case_values(_corpus()["cases"][0])[0]
    request_digest = contract.video_writer_command_digest(command)
    arguments = contract.VideoWriterApplyArguments.model_validate(
        {
            "operation_id": str(command.operation_id),
            "execution": command.execution.model_dump(mode="json", by_alias=True),
            "outcome": command.outcome,
            "request_digest": request_digest,
            "expected_input": command.expected_input.model_dump(
                mode="json", by_alias=True
            ),
            "error_message": None,
            "staged_result_json": None,
            "staged_sha256": None,
            "staged_byte_size": 0,
        }
    )
    receipt = contract.VideoWriterReceipt(
        operation_id=command.operation_id,
        job_id=command.execution.job_id,
        run_id=command.execution.run_id,
        request_digest=request_digest,
        outcome="completed",
        status="pending_review",
        completed_at="2026-10-22T00:00:00Z",
        staged_sha256="a" * 64,
        staged_byte_size=1,
    )
    with pytest.raises(contract.VideoWriterUnavailable):
        contract.decode_native_reply(
            "apply",
            arguments,
            {
                "schema": contract.LABELING_VIDEO_WRITER_DATA_SCHEMA,
                "ok": True,
                "data": {"receipt": receipt.model_dump(mode="json", by_alias=True)},
            },
        )


def test_empty_cancellation_command_reconstructs_saved_prefix_from_owner_facts():
    case = next(
        case for case in _corpus()["cases"] if case.get("expected_staged_result")
    )
    original, facts = _case_values(case)
    facts.effective_outcome = "cancelled"
    command = contract.make_video_writer_command(
        original.execution, outcome="cancelled", expected_input=None, frame_results=[]
    )
    rows = original.frame_results
    staged = contract._staged_result(command, facts, rows)
    assert staged is not None
    assert staged["results"] == [
        row.model_dump(mode="json", by_alias=True) for row in rows
    ]
    assert contract._staged_result(command, facts, ()) is None
