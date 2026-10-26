from __future__ import annotations

import json
import uuid
from importlib.resources import files
from pathlib import Path

import pytest

from aap_labeling.e2e_fixture_contract import (
    LABELING_E2E_FILTERING_RENDER_FUNCTION,
    LABELING_E2E_FIXTURE_FUNCTION,
    LABELING_E2E_FIXTURE_PROTOCOL,
    InsertFixtureBatches,
    LabelingE2EFixtureUnavailable,
    PrepareFilteringRendering,
    TaskBatchFixtureRow,
    decode_fixture_write_reply,
    e2e_fixture_schema,
    fixture_command_json,
)


def test_fixture_schema_matches_the_current_contract_and_migration():
    root = Path(__file__).resolve().parents[3]
    migration = root / "apps/api/alembic/sql/0367_labeling_filtering_rendering.schema.json"
    resource = files("aap_labeling")
    packaged_schema = json.loads(
        resource.joinpath("e2e-fixtures.schema.json").read_text(encoding="utf-8")
    )

    assert packaged_schema == json.loads(migration.read_text(encoding="utf-8"))
    assert packaged_schema == e2e_fixture_schema()
    assert packaged_schema["$id"] == LABELING_E2E_FIXTURE_PROTOCOL
    functions = packaged_schema["x-postgresql-functions"]
    assert functions[LABELING_E2E_FIXTURE_FUNCTION]["arguments"] == "jsonb"
    assert functions[LABELING_E2E_FILTERING_RENDER_FUNCTION] == {
        "arguments": "jsonb",
        "request": {"$ref": "#/$defs/PrepareFilteringRendering"},
        "returns": "jsonb",
        "reply": "aap-labeling-e2e-fixtures/v1",
        "volatility": "volatile",
        "parallel": "unsafe",
    }


def test_filtering_rendering_command_has_a_closed_owner_reply():
    project_id, low_id, at_id, above_id = (uuid.uuid4() for _ in range(4))
    request = PrepareFilteringRendering(
        schema=LABELING_E2E_FIXTURE_PROTOCOL,
        operation="prepare_filtering_rendering",
        project_id=project_id,
        low_prediction_id=low_id,
        at_prediction_id=at_id,
        above_prediction_id=above_id,
    )
    command = json.loads(fixture_command_json(request))
    assert command == {
        "schema": LABELING_E2E_FIXTURE_PROTOCOL,
        "operation": "prepare_filtering_rendering",
        "project_id": str(project_id),
        "low_prediction_id": str(low_id),
        "at_prediction_id": str(at_id),
        "above_prediction_id": str(above_id),
    }
    frame_indices = (0, 0, 10)
    scores = (0.2, 0.5, 0.9)
    ids = (low_id, at_id, above_id)
    rendered_predictions = [
        {
            "id": str(prediction_id),
            "result": [
                {
                    "type": "video_bbox",
                    "class_name": "car",
                    "geometry": {
                        "type": "video_bbox",
                        "frame_index": frame_index,
                        "x": 0.62,
                        "y": 0.62,
                        "w": 0.12,
                        "h": 0.15,
                    },
                    "confidence": score,
                    "tool_unit_id": "bbox",
                    "attributes": {},
                }
            ],
        }
        for prediction_id, frame_index, score in zip(
            ids, frame_indices, scores, strict=True
        )
    ]
    reply = {
        "schema": LABELING_E2E_FIXTURE_PROTOCOL,
        "operation": "prepare_filtering_rendering",
        "ok": True,
        "data": {
            "ids": [str(value) for value in ids],
            "rendered_predictions": rendered_predictions,
        },
    }
    data = decode_fixture_write_reply(request, reply)
    assert data.ids == list(ids)
    assert [row.id for row in data.rendered_predictions or []] == list(ids)
    with pytest.raises(LabelingE2EFixtureUnavailable):
        decode_fixture_write_reply(
            request,
            {
                **reply,
                "data": {
                    "ids": [str(low_id), str(at_id)],
                    "rendered_predictions": rendered_predictions[:2],
                },
            },
        )


def test_fixture_request_preserves_omitted_defaults_and_explicit_null():
    project_id = uuid.uuid4()
    batch_id = uuid.uuid4()

    def request(**fields):
        return InsertFixtureBatches(
            schema=LABELING_E2E_FIXTURE_PROTOCOL,
            operation="insert_batches",
            project_id=project_id,
            rows=[
                TaskBatchFixtureRow(
                    id=batch_id,
                    project_id=project_id,
                    display_id="B-E2E-DEFAULTS",
                    name="Default semantics",
                    **fields,
                )
            ],
        )

    omitted = json.loads(fixture_command_json(request()))["rows"][0]
    explicit_null = json.loads(
        fixture_command_json(request(status=None, reviewer_id=None))
    )["rows"][0]

    assert "status" not in omitted
    assert "reviewer_id" not in omitted
    assert explicit_null["status"] is None
    assert explicit_null["reviewer_id"] is None

    reply = {
        "schema": LABELING_E2E_FIXTURE_PROTOCOL,
        "operation": "insert_batches",
        "ok": True,
        "data": {"ids": [str(batch_id)]},
    }
    assert decode_fixture_write_reply(request(), reply).ids == [batch_id]


def test_fixture_reply_rejects_wrong_operation_and_row_count():
    request = InsertFixtureBatches(
        schema=LABELING_E2E_FIXTURE_PROTOCOL,
        operation="insert_batches",
        project_id=uuid.uuid4(),
        rows=[
            TaskBatchFixtureRow(
                id=uuid.uuid4(),
                project_id=uuid.uuid4(),
                display_id="B-E2E-REPLY",
                name="Reply binding",
            )
        ],
    )
    with pytest.raises(LabelingE2EFixtureUnavailable):
        decode_fixture_write_reply(
            request,
            {
                "schema": LABELING_E2E_FIXTURE_PROTOCOL,
                "operation": "patch_batches",
                "ok": True,
                "data": {"ids": []},
            },
        )
