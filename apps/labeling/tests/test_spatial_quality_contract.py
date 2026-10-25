from __future__ import annotations

import uuid

import pytest

from aap_labeling.spatial_quality_contract import (
    SPATIAL_QUALITY_PROTOCOL,
    SpatialQualityUnavailable,
    decode_spatial_quality_reply,
    spatial_quality_json_schema,
    spatial_quality_request,
    spatial_quality_request_json,
)


def test_spatial_quality_requests_are_closed_and_action_typed() -> None:
    project_id = uuid.uuid4()
    request = spatial_quality_request("read_point_source", project_id=project_id)

    assert spatial_quality_request_json(request) == (
        '{"schema":"aap-labeling-spatial-quality/v1",'
        f'"action":"read_point_source","project_id":"{project_id}"}}'
    )
    with pytest.raises(ValueError, match="invalid Labeling spatial quality request"):
        spatial_quality_request(
            "read_point_source", project_id=project_id, extra="not allowed"
        )
    with pytest.raises(ValueError, match="invalid Labeling spatial quality request"):
        spatial_quality_request("read_point_source", project_id="not-a-uuid")


def test_spatial_quality_schema_keeps_native_actions_distinct() -> None:
    schema = spatial_quality_json_schema()
    definitions = schema["$defs"]
    upsert = definitions["QualityCommand_upsert_point_issue"]
    assert upsert["additionalProperties"] is False
    assert "new_issue_id" in upsert["required"]
    assert upsert["properties"]["action"]["const"] == "upsert_point_issue"
    assert schema["discriminator"]["mapping"]["read_video_boundary"] == (
        "#/$defs/ReadVideoBoundary"
    )


def test_spatial_quality_reply_is_bound_to_its_request() -> None:
    request = spatial_quality_request("read_point_source", project_id=uuid.uuid4())
    reply = {
        "schema": SPATIAL_QUALITY_PROTOCOL,
        "action": "read_point_source",
        "data": {"tasks": [], "track_scene_ids": [], "dataset_ids": []},
    }
    assert decode_spatial_quality_reply(request, reply) == reply["data"]

    with pytest.raises(SpatialQualityUnavailable):
        decode_spatial_quality_reply(request, {**reply, "action": "read_video_runs"})
    with pytest.raises(SpatialQualityUnavailable):
        decode_spatial_quality_reply(request, {**reply, "unexpected": True})
    with pytest.raises(SpatialQualityUnavailable):
        decode_spatial_quality_reply(
            request, {**reply, "data": {"tasks": [], "unexpected": []}}
        )
