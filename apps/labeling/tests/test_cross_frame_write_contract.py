from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
import json
from math import radians
from pathlib import Path
from uuid import uuid4

import pytest

from aap_labeling.cross_frame_write_contract import (
    CROSS_FRAME_WRITE_PROTOCOL,
    CrossFrameAnnotationSnapshot,
    CrossFrameSourceSnapshot,
    CrossFrameSourceFence,
    CrossFrameTargetWrite,
    CrossFrameTaskFence,
    CrossFrameWriteUnavailable,
    cross_frame_write_command,
    cross_frame_write_command_json,
    cross_frame_write_schema,
    decode_cross_frame_write_command,
)
from aap_labeling.cross_frame_write_policy import interpolate_psr
from aap_labeling.cross_frame_write_data import CrossFrameWriteDataClient


def _command():
    project_id, source_task_id, target_task_id = uuid4(), uuid4(), uuid4()
    source_id = uuid4()
    return cross_frame_write_command(
        "propagate",
        actor_id=uuid4(),
        task_fences=[
            CrossFrameTaskFence(
                task_id=source_task_id, project_id=project_id, expected_version=1
            ),
            CrossFrameTaskFence(
                task_id=target_task_id, project_id=project_id, expected_version=1
            ),
        ],
        source_annotations=[
            CrossFrameSourceFence(
                annotation_id=source_id,
                task_id=source_task_id,
                project_id=project_id,
                expected_version=1,
                geometry_type="bbox",
                class_name="car",
                expected_track_id=None,
                target_track_id="trk_cross_frame",
                expected_scene_track_id=None,
                expected_temporal_role="sample",
            )
        ],
        target_annotations=[
            CrossFrameTargetWrite(
                annotation_id=uuid4(),
                source_annotation_id=source_id,
                task_id=target_task_id,
                project_id=project_id,
                video_segment_id=None,
                source="manual",
                annotation_type="bbox",
                tool_unit_id="bbox",
                class_name="car",
                geometry={"type": "bbox", "x": 1, "y": 2, "w": 3, "h": 4},
                track_id="trk_cross_frame",
                attributes={"雪": True},
                temporal_role="sample",
            )
        ],
        scene_tracks=[],
    )


def test_contract_round_trips_closed_command_and_schema():
    request = _command()
    encoded = cross_frame_write_command_json(request)
    assert decode_cross_frame_write_command(encoded) == request
    assert json.loads(encoded)["schema"] == CROSS_FRAME_WRITE_PROTOCOL
    assert cross_frame_write_schema()["$id"] == CROSS_FRAME_WRITE_PROTOCOL
    assert (
        json.loads(
            files("aap_labeling").joinpath("cross-frame-writes.schema.json").read_text()
        )
        == cross_frame_write_schema()
    )
    repository = Path(__file__).resolve().parents[3]
    assert (
        files("aap_labeling")
        .joinpath("sql/0344_labeling_cross_frame_writes.sql")
        .read_text()
        == (
            repository / "apps/api/alembic/sql/0344_labeling_cross_frame_writes.sql"
        ).read_text()
    )
    assert (
        files("aap_labeling").joinpath("cross-frame-writes.schema.json").read_text()
        == (
            repository
            / "apps/api/alembic/sql/0344_labeling_cross_frame_writes.schema.json"
        ).read_text()
    )

    payload = json.loads(encoded)
    payload["unexpected"] = True
    with pytest.raises(CrossFrameWriteUnavailable):
        decode_cross_frame_write_command(json.dumps(payload))


def test_batch_contract_keeps_duplicate_source_targets_in_input_order():
    single = _command()
    duplicate = CrossFrameTargetWrite(
        **{
            **single.target_annotations[0].model_dump(mode="python"),
            "annotation_id": uuid4(),
        }
    )
    batch = cross_frame_write_command(
        "propagate_batch",
        actor_id=single.actor_id,
        task_fences=single.task_fences,
        source_annotations=single.source_annotations,
        target_annotations=[single.target_annotations[0], duplicate],
        scene_tracks=[],
    )
    assert len(batch.source_annotations) == 1
    assert [item.source_annotation_id for item in batch.target_annotations] == [
        single.source_annotations[0].annotation_id,
        single.source_annotations[0].annotation_id,
    ]


@pytest.mark.asyncio
async def test_data_client_decodes_owner_snapshots():
    request = _command()
    target = request.target_annotations[0]
    source = request.source_annotations[0]
    now = datetime.now(timezone.utc)
    annotation = CrossFrameAnnotationSnapshot(
        id=target.annotation_id,
        task_id=target.task_id,
        project_id=target.project_id,
        video_segment_id=None,
        user_id=request.actor_id,
        source="manual",
        annotation_type=target.annotation_type,
        tool_unit_id=target.tool_unit_id,
        class_name=target.class_name,
        geometry=target.geometry,
        confidence=None,
        parent_prediction_id=None,
        parent_annotation_id=None,
        track_id=target.track_id,
        scene_track_id=None,
        sensor_dataset_item_id=None,
        sensor_role=None,
        sensor_visibility=None,
        calibration_revision=None,
        calibration_digest=None,
        temporal_role="sample",
        lead_time=None,
        was_cancelled=False,
        ground_truth=False,
        is_active=True,
        attributes=target.attributes,
        attributes_meta={},
        z_order=0,
        is_locked=False,
        is_hidden=False,
        version=1,
        created_at=now,
        updated_at=now,
    )
    source_snapshot = CrossFrameSourceSnapshot(
        annotation_id=source.annotation_id,
        version=2,
        track_id=source.target_track_id,
        scene_track_id=None,
        temporal_role="sample",
        updated_at=now,
        written_fields=["track_id", "version", "updated_at"],
    )
    reply = {
        "schema": CROSS_FRAME_WRITE_PROTOCOL,
        "ok": True,
        "request": json.loads(cross_frame_write_command_json(request)),
        "data": {
            "annotations": [annotation.model_dump(mode="json")],
            "sources": [source_snapshot.model_dump(mode="json")],
            "scene_tracks": [],
        },
    }

    async def execute(request_json: str):
        assert json.loads(request_json) == reply["request"]
        return reply

    result = await CrossFrameWriteDataClient(execute).command(request)
    assert result.annotations == [annotation]
    assert result.sources == [source_snapshot]
    assert result.scene_tracks == []


@dataclass(frozen=True)
class _Pose:
    ego_rotation: list[float]
    ego_translation: list[float]


def test_interpolation_keeps_short_arc_slerp_and_missing_pose_fallback():
    before = {
        "center": [0.0, 1.0, 2.0],
        "size": [2.0, 4.0, 6.0],
        "rotation": [0.0, 0.0, radians(170)],
    }
    after = {
        "center": [10.0, 3.0, 4.0],
        "size": [4.0, 6.0, 8.0],
        "rotation": [0.0, 0.0, radians(-170)],
    }
    identity = _Pose([1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
    midpoint, compensated = interpolate_psr(
        before,
        after,
        0.5,
        pose_a=identity,
        pose_b=identity,
        pose_mid=identity,
    )
    assert compensated is True
    assert midpoint["center"] == [5.0, 2.0, 3.0]
    assert midpoint["size"] == [3.0, 5.0, 7.0]
    assert abs(abs(midpoint["rotation"][2]) - 3.141592653589793) < 1e-6

    fallback, compensated = interpolate_psr(
        before, after, 0.5, pose_a=identity, pose_b=None, pose_mid=identity
    )
    assert compensated is False
    assert fallback["center"] == [5.0, 2.0, 3.0]
