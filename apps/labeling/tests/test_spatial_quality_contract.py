from __future__ import annotations

import json
import uuid
from pathlib import Path

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
    assert schema["discriminator"]["mapping"]["read_video_chapters"] == (
        "#/$defs/ReadVideoChapters"
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


def test_video_chapter_projection_is_frozen_before_the_labeling_command() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    projection_path = (
        repo_root / "apps/assets-media/src/aap_assets_media/sql/video-chapter-facts.sql"
    )
    frozen_path = repo_root / "apps/api/alembic/sql/0354_labeling_spatial_quality.sql"
    schema_path = (
        repo_root / "apps/labeling/src/aap_labeling/spatial-quality.schema.json"
    )
    frozen_schema_path = (
        repo_root / "apps/api/alembic/sql/0354_labeling_spatial_quality.schema.json"
    )
    projection = projection_path.read_text(encoding="utf-8")
    frozen = frozen_path.read_text(encoding="utf-8")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert projection == (
        "CREATE VIEW aap_assets_v1.video_chapter_facts\n"
        "WITH (security_barrier = true)\n"
        "AS\n"
        "SELECT id, dataset_item_id, title, start_frame, end_frame\n"
        "FROM public.video_chapters;\n"
        "-- aap-statement-break\n"
        "REVOKE ALL ON TABLE aap_assets_v1.video_chapter_facts FROM PUBLIC;\n"
    )
    assert frozen.startswith(projection + "-- aap-statement-break\nCREATE FUNCTION")
    assert schema_path.read_bytes() == frozen_schema_path.read_bytes()
    function = schema["x-postgresql-functions"][
        "aap_labeling_v1.spatial_quality_command(jsonb)"
    ]
    assert "read_video_chapters" in function["actions"]
    assert schema["x-response-data-keys"]["read_video_chapters"] == ["chapters"]
    function_sql = frozen.split("CREATE FUNCTION", maxsplit=1)[1]
    assert "FROM aap_assets_v1.video_chapter_facts AS chapter_row" in function_sql
    assert "public.video_chapters" not in function_sql
    assert "task_row.id = (p_request ->> 'task_id')::uuid" in function_sql
    assert "task_row.dataset_item_id = chapter_row.dataset_item_id" in function_sql
    assert "ORDER BY chapter_row.start_frame, chapter_row.id" in function_sql


def test_video_chapter_read_is_task_scoped_and_closed() -> None:
    task_id = uuid.uuid4()
    dataset_item_id = uuid.uuid4()
    chapter_id = uuid.uuid4()
    request = spatial_quality_request(
        "read_video_chapters", task_id=task_id, dataset_item_id=dataset_item_id
    )
    assert spatial_quality_request_json(request) == (
        '{"schema":"aap-labeling-spatial-quality/v1",'
        f'"action":"read_video_chapters","task_id":"{task_id}",'
        f'"dataset_item_id":"{dataset_item_id}"}}'
    )
    with pytest.raises(ValueError, match="invalid Labeling spatial quality request"):
        spatial_quality_request(
            "read_video_chapters",
            task_id=task_id,
            dataset_item_id=dataset_item_id,
            project_id=uuid.uuid4(),
        )

    data = {
        "chapters": [
            {
                "id": str(chapter_id),
                "title": "part",
                "start_frame": 10,
                "end_frame": 30,
            }
        ]
    }
    reply = {
        "schema": SPATIAL_QUALITY_PROTOCOL,
        "action": "read_video_chapters",
        "data": data,
    }
    assert decode_spatial_quality_reply(request, reply) == data
