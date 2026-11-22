"""Read responses preserve missing legacy provenance without changing write defaults."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from app.schemas.annotation import AnnotationCreate, AnnotationOut, AnnotationUpdate


def geometry(kind):
    payload = {
        "bbox": {"bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}},
        "polygon": {"points": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4]]},
        "polyline": {"points": [[0.1, 0.1], [0.4, 0.4]]},
        "mask": {
            "mask": {
                "encoding": "coco_rle_ref",
                "size": [4, 4],
                "object_key": f"raster-masks/sha256/aa/aa/{'a' * 64}.json",
                "sha256": "a" * 64,
                "runs": 3,
                "bytes": 12,
            }
        },
    }[kind]
    return {
        "type": f"video_track_{kind}",
        "track_id": "trk_provenance",
        "keyframes": [
            {"frame_index": 0, **payload},
            {"frame_index": 10, **payload, "source": "manual"},
            {"frame_index": 20, **payload, "source": "prediction"},
        ],
    }


@pytest.mark.parametrize("kind", ["bbox", "polygon", "polyline", "mask"])
def test_annotation_json_keeps_only_recorded_keyframe_sources(kind):
    raw = geometry(kind)
    annotation = AnnotationOut.model_validate(
        {
            "id": uuid4(),
            "task_id": uuid4(),
            "source": "prediction_based",
            "annotation_type": raw["type"],
            "class_name": "car",
            "geometry": raw,
            "is_active": True,
            "created_at": datetime.now(timezone.utc),
        }
    )
    output = TypeAdapter(list[AnnotationOut]).dump_python([annotation], mode="json")[0]
    keyframes = output["geometry"]["keyframes"]
    assert "source" not in keyframes[0]
    assert keyframes[1]["source"] == "manual"
    assert keyframes[2]["source"] == "prediction"
    assert output["source"] == "prediction_based"
    assert "source" not in raw["keyframes"][0]
    assert keyframes[0]["frame_index"] == 0
    assert output["geometry"]["track_id"] == raw["track_id"]


@pytest.mark.parametrize("kind", ["bbox", "polygon", "polyline", "mask"])
def test_keyframe_write_defaults_and_partial_update_are_unchanged(kind):
    raw = geometry(kind)
    created = AnnotationCreate(class_name="car", geometry=raw)
    assert created.geometry.model_dump()["keyframes"][0]["source"] == "manual"
    updated = AnnotationUpdate(geometry=raw)
    assert (
        "source"
        not in updated.model_dump(exclude_unset=True)["geometry"]["keyframes"][0]
    )
