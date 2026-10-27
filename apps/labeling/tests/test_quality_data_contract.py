"""Focused request and owner routing checks for Mask QC data commands."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from aap_labeling.quality_data_contract import (
    QUALITY_DATA_PROTOCOL,
    QualityDataUnavailable,
    ReadMaskQCRun,
    InsertMaskReviewScope,
    UpsertMaskQCIssue,
    RecordRepairRollbackShard,
    decode_quality_data_command,
    quality_data_command_json,
)
from aap_labeling.quality_data_data import QualityDataClient


def test_quality_command_is_closed_and_round_trips() -> None:
    command = ReadMaskQCRun(
        schema=QUALITY_DATA_PROTOCOL,
        operation="read_run",
        run_id=uuid.uuid4(),
        lock=True,
    )
    encoded = quality_data_command_json(command)
    assert json.loads(encoded)["operation"] == "read_run"
    assert decode_quality_data_command(encoded) == command
    with pytest.raises(QualityDataUnavailable):
        decode_quality_data_command(
            json.dumps({**json.loads(encoded), "unexpected": True})
        )


def test_issue_command_requires_typed_source_versions_and_timestamps() -> None:
    annotation_id = uuid.uuid4()
    command = UpsertMaskQCIssue(
        schema=QUALITY_DATA_PROTOCOL,
        operation="upsert_issue",
        issue={
            "id": uuid.uuid4(),
            "run_id": uuid.uuid4(),
            "last_seen_run_id": uuid.uuid4(),
            "project_id": uuid.uuid4(),
            "task_id": uuid.uuid4(),
            "annotation_id": annotation_id,
            "annotation_version": 1,
            "related_annotation_ids": [annotation_id],
            "source_versions": {str(annotation_id): 1},
            "code": "small_island",
            "severity": "warning",
            "severity_rank": 1,
            "status": "open",
            "frame_start": None,
            "frame_end": None,
            "metric": {},
            "threshold": {},
            "region_bbox": None,
            "region_mask_ref": None,
            "region_digest": None,
            "dedupe_key": uuid.uuid4().hex + uuid.uuid4().hex,
            "source": {},
            "suggestion": None,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    assert decode_quality_data_command(quality_data_command_json(command)) == command


def test_review_scope_write_requires_bound_actor_issue_and_job() -> None:
    command = InsertMaskReviewScope(
        schema=QUALITY_DATA_PROTOCOL,
        operation="insert_review_scope",
        scope_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        annotation_id=uuid.uuid4(),
        qc_issue_id=uuid.uuid4(),
        source_job_id=uuid.uuid4(),
        expected_segment_id=None,
        reviewer_id=uuid.uuid4(),
        source_annotation_version=2,
        result_annotation_version=3,
        source_job_revision=4,
        frame_start=0,
        frame_end=0,
        region_mask_ref={"sha256": "a" * 64},
        region_digest="a" * 64,
        candidate_digest="sha256:" + "b" * 64,
        decision="accept",
    )
    raw = json.loads(quality_data_command_json(command))
    assert decode_quality_data_command(raw) == command
    raw["qc_issue_id"] = None
    with pytest.raises(QualityDataUnavailable):
        decode_quality_data_command(raw)


def test_rollback_shard_command_is_closed_and_carries_the_rollback_checkpoint() -> None:
    annotation_id = uuid.uuid4()
    command = RecordRepairRollbackShard(
        schema=QUALITY_DATA_PROTOCOL,
        operation="record_rollback_shard",
        batch_id=uuid.uuid4(),
        rollback_job_id=uuid.uuid4(),
        shard_id="a" * 24,
        result_value={
            "status": "completed",
            "mutation_count": 2,
            "versions": {str(annotation_id): 3},
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "annotation_facts": {},
        },
    )
    encoded = quality_data_command_json(command)
    assert json.loads(encoded)["operation"] == "record_rollback_shard"
    assert decode_quality_data_command(encoded) == command
    raw = json.loads(encoded)
    raw["shard_id"] = "A" * 24
    with pytest.raises(QualityDataUnavailable):
        decode_quality_data_command(raw)
    raw = json.loads(encoded)
    raw.pop("rollback_job_id")
    with pytest.raises(QualityDataUnavailable):
        decode_quality_data_command(raw)
    raw = json.loads(encoded)
    raw["result_value"] = []
    with pytest.raises(QualityDataUnavailable):
        decode_quality_data_command(raw)


@pytest.mark.asyncio
async def test_client_routes_retention_and_qc_to_their_closed_functions() -> None:
    calls: list[tuple[str, dict]] = []

    async def execute(function: str, payload: str) -> object:
        request = json.loads(payload)
        calls.append((function, request))
        return {
            "schema": QUALITY_DATA_PROTOCOL,
            "ok": True,
            "request": request,
            "data": ["raster-masks/sha256/abc"],
        }

    client = QualityDataClient(execute)
    from aap_labeling.quality_data_contract import ReadRasterMaskReferences

    command = ReadRasterMaskReferences(
        schema=QUALITY_DATA_PROTOCOL,
        operation="read_all_references",
    )
    assert await client.command(command) == ["raster-masks/sha256/abc"]
    assert calls[0][0] == "raster_mask_retention_query"
    assert calls[0][1]["schema"] == QUALITY_DATA_PROTOCOL
