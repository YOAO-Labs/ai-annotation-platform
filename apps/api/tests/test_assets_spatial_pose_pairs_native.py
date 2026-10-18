"""Native Assets capability for reading sparse scene/frame pose pairs."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from app.db.models.dataset import Scene
from app.db.models.scene_pose import SceneFramePose
from tests.test_assets_catalog_native import _call, _seed_actor, _seed_dataset

pytestmark = pytest.mark.asyncio

_READ_POSE_PAIRS = text(
    "SELECT aap_assets_v1.read_spatial_pose_pairs(CAST(:pairs AS jsonb))"
)
_SCENE_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"


def _payload(value: object) -> dict:
    parsed = json.loads(value) if isinstance(value, str) else value
    assert isinstance(parsed, dict)
    return parsed


def _error(value: object, code: str) -> None:
    assert _payload(value) == {
        "schema": "aap-assets-spatial/v1",
        "ok": False,
        "error": {"code": code},
    }


async def test_pose_pair_read_returns_only_existing_requested_rows_in_order(db_session):
    actor_id = await _seed_actor(db_session, role="super_admin")
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        name=f"spatial-pose-pairs-{uuid.uuid4().hex[:8]}",
        data_type="point_cloud",
        file_count=0,
    )
    scene_a_id, scene_b_id = uuid.uuid4(), uuid.uuid4()
    missing_scene_id = uuid.uuid4()
    db_session.add_all(
        [
            Scene(
                id=scene_a_id,
                display_id=f"SPPA-{scene_a_id.hex[:10]}",
                dataset_id=dataset_id,
                name="pose-pairs-a",
                source_format="native-test",
                source_metadata={},
                created_by=actor_id,
            ),
            Scene(
                id=scene_b_id,
                display_id=f"SPPB-{scene_b_id.hex[:10]}",
                dataset_id=dataset_id,
                name="pose-pairs-b",
                source_format="native-test",
                source_metadata={},
                created_by=actor_id,
            ),
        ]
    )
    poses = [
        SceneFramePose(
            scene_id=scene_a_id,
            frame_index=2,
            timestamp_us=200,
            ego_translation=[1, 2, 3],
            ego_rotation=[1, 0, 0, 0],
            source_metadata={"selected": "a2"},
        ),
        SceneFramePose(
            scene_id=scene_a_id,
            frame_index=8,
            timestamp_us=800,
            ego_translation=[8, 0, 0],
            ego_rotation=[1, 0, 0, 0],
            source_metadata={"unrequested": True},
        ),
        SceneFramePose(
            scene_id=scene_b_id,
            frame_index=7,
            timestamp_us=700,
            ego_translation=[7, 0, 0],
            ego_rotation=[1, 0, 0, 0],
            source_metadata={"selected": "b7"},
        ),
    ]
    db_session.add_all(poses)
    await db_session.flush()

    pairs = [
        {"scene_id": str(scene_b_id), "frame_index": 7},
        {"scene_id": str(scene_a_id), "frame_index": 3},
        {"scene_id": str(scene_a_id), "frame_index": 2},
        {"scene_id": str(scene_b_id), "frame_index": 99},
        {"scene_id": str(missing_scene_id), "frame_index": 0},
    ]
    arguments = {"pairs": json.dumps(pairs, separators=(",", ":"))}
    first = _payload(await _call(db_session, _READ_POSE_PAIRS, arguments))
    second = _payload(await _call(db_session, _READ_POSE_PAIRS, arguments))
    uppercase_pairs = [dict(pair) for pair in pairs]
    uppercase_pairs[0]["scene_id"] = uppercase_pairs[0]["scene_id"].upper()
    uppercase = _payload(
        await _call(
            db_session,
            _READ_POSE_PAIRS,
            {"pairs": json.dumps(uppercase_pairs, separators=(",", ":"))},
        )
    )

    assert first["schema"] == "aap-assets-spatial/v1"
    assert first["ok"] is True
    assert first["data"]["pairs"] == pairs
    assert first["data"]["poses"] == [
        {
            "scene_id": str(scene_id),
            "frame_index": frame_index,
            "timestamp_us": timestamp_us,
            "ego_translation": translation,
            "ego_rotation": [1, 0, 0, 0],
            "source_metadata": metadata,
        }
        for scene_id, frame_index, timestamp_us, translation, metadata in sorted(
            [
                (scene_a_id, 2, 200, [1, 2, 3], {"selected": "a2"}),
                (scene_b_id, 7, 700, [7, 0, 0], {"selected": "b7"}),
            ],
            key=lambda entry: (str(entry[0]), entry[1]),
        )
    ]
    digest = first["data"]["snapshot_digest"]
    assert isinstance(digest, str) and len(digest) == 64
    assert second["data"]["snapshot_digest"] == digest
    assert uppercase["data"]["pairs"] == pairs
    assert uppercase["data"]["poses"] == first["data"]["poses"]
    assert uppercase["data"]["snapshot_digest"] == digest


@pytest.mark.parametrize(
    "pairs",
    [
        None,  # SQL NULL
        "null",  # JSON null
        [],
        [None],
        [{"scene_id": _SCENE_ID, "frame_index": None}],
        [{"scene_id": _SCENE_ID, "frame_index": True}],
        [{"scene_id": _SCENE_ID, "frame_index": 1, "extra": "no"}],
        [{"frame_index": 1}],
        [{"scene_id": _SCENE_ID, "frame_index": -1}],
        [{"scene_id": _SCENE_ID, "frame_index": 2147483648}],
        [{"scene_id": _SCENE_ID, "frame_index": 1.5}],
        [
            {"scene_id": _SCENE_ID, "frame_index": 1},
            {"scene_id": _SCENE_ID, "frame_index": 1},
        ],
        [
            {"scene_id": _SCENE_ID, "frame_index": 1},
            {"scene_id": _SCENE_ID.upper(), "frame_index": 1.0},
        ],
    ],
)
async def test_pose_pair_read_rejects_malformed_or_duplicate_pairs(db_session, pairs):
    encoded = (
        pairs
        if pairs is None or isinstance(pairs, str)
        else json.dumps(pairs, separators=(",", ":"))
    )
    _error(
        await _call(db_session, _READ_POSE_PAIRS, {"pairs": encoded}),
        "invalid_request",
    )


async def test_pose_pair_read_caps_input_at_five_thousand_pairs(db_session):
    pairs = [
        {"scene_id": str(uuid.uuid4()), "frame_index": index} for index in range(5001)
    ]
    _error(
        await _call(
            db_session,
            _READ_POSE_PAIRS,
            {"pairs": json.dumps(pairs, separators=(",", ":"))},
        ),
        "invalid_request",
    )
