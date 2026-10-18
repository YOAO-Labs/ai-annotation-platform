import json
from importlib.resources import files
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aap_assets_catalog.spatial_contract import (
    SPATIAL_NATIVE_SIGNATURES,
    SPATIAL_NATIVE_ARGUMENTS,
    SPATIAL_SCHEMA,
    CalibrationRecord,
    GuardSpatialActorRequest,
    LayoutReceiptResult,
    PosePairsResult,
    ReadItemsRequest,
    PosePage,
    ScenePatch,
    SpatialActorResult,
    SpatialFailure,
    WireCalibration,
    WireFramePose,
    canonical_calibration_document,
    decode_spatial_reply,
    normalize_calibration_record,
    require_spatial_scope,
    spatial_json_schema,
)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": "aap-assets-spatial/v2", "ok": True, "data": {}},
        {"schema": SPATIAL_SCHEMA, "ok": 1, "data": {}},
        {"schema": SPATIAL_SCHEMA, "ok": False, "error": {"code": "future"}},
        {"schema": SPATIAL_SCHEMA, "ok": False, "error": {"code": []}},
        {
            "schema": SPATIAL_SCHEMA,
            "ok": False,
            "error": {"code": "source_busy", "extra": True},
        },
        {"schema": SPATIAL_SCHEMA, "ok": True, "data": {}, "ignored": True},
    ],
)
def test_unknown_or_malformed_envelopes_fail_closed(payload):
    with pytest.raises((ValueError, ValidationError)):
        decode_spatial_reply(payload, SpatialActorResult)


def test_business_error_is_not_treated_as_a_success_or_protocol_failure():
    with pytest.raises(SpatialFailure) as caught:
        decode_spatial_reply(
            {
                "schema": SPATIAL_SCHEMA,
                "ok": False,
                "error": {"code": "calibration_revision_conflict"},
            },
            SpatialActorResult,
        )
    assert caught.value.code == "calibration_revision_conflict"


def test_scope_validation_rejects_another_actor_even_with_valid_reply_shape():
    request = GuardSpatialActorRequest(actor_id=uuid4(), access="read")
    result = SpatialActorResult(actor_id=uuid4(), access="read")
    with pytest.raises(ValueError, match="actor_id"):
        require_spatial_scope(request, result)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"dataset_id": str(uuid4()), "item_ids": [str(uuid4())]},
        {"item_ids": []},
        {"item_ids": [str(UUID(int=1))] * 2},
    ],
)
def test_fact_selector_cannot_be_unscoped_ambiguous_or_duplicate(body):
    with pytest.raises(ValidationError):
        ReadItemsRequest.model_validate(body)


def test_scene_patch_retains_old_ignore_null_behavior():
    assert (
        ScenePatch(name=None, source_format=None, source_metadata=None).native_payload()
        == {}
    )
    assert ScenePatch(source_metadata={}).native_payload() == {"source_metadata": {}}


def test_legacy_calibration_is_normalized_and_historical_digest_stays_opaque():
    raw = {"extrinsic": [0] * 16, "intrinsic": [1] * 9}
    record = CalibrationRecord(
        dataset_item_id=uuid4(),
        revision=1,
        calibration=raw,
        digest=None,
        created_at=None,
        source_snapshot={"opaque": "source"},
        error=None,
    )
    state = normalize_calibration_record(record)
    assert state.calibration.extrinsic == [0.0] * 16
    assert canonical_calibration_document(state.calibration).startswith(
        '{"extrinsic":[0.0,'
    )
    assert state.source_snapshot == {"opaque": "source"}
    assert state.created_at is None
    historical = normalize_calibration_record(
        record.model_copy(update={"digest": "0" * 64})
    )
    assert historical.digest == "0" * 64
    with pytest.raises(SpatialFailure, match="camera_calibration_missing"):
        normalize_calibration_record(record.model_copy(update={"calibration": None}))


def test_distributable_schema_matches_wire_models_and_exact_native_surface():
    expected = spatial_json_schema()
    packaged = files("aap_assets_catalog").joinpath("spatial_contract.schema.json")
    assert json.loads(packaged.read_text()) == expected
    assert len(SPATIAL_NATIVE_SIGNATURES) == 22
    assert set(expected["x-native-capabilities"]) == set(SPATIAL_NATIVE_SIGNATURES)
    assert expected["$defs"]["ScenePatch"]["additionalProperties"] is False
    for name, signature in SPATIAL_NATIVE_SIGNATURES.items():
        assert len(signature.split(",")) == len(SPATIAL_NATIVE_ARGUMENTS[name])
        assert expected["x-native-capabilities"][name]["arguments"] == list(
            SPATIAL_NATIVE_ARGUMENTS[name]
        )


@pytest.mark.parametrize("field", ["operation_id", "actor_id", "dataset_id", "dry_run"])
def test_layout_receipt_rejects_a_result_from_another_scope(field):
    scope = {
        "operation_id": uuid4(),
        "actor_id": uuid4(),
        "dataset_id": uuid4(),
        "mode": "auto",
        "dry_run": False,
    }
    result = {key: value for key, value in scope.items() if key != "mode"}
    result.update(created_scenes=0, assigned_items=0, skipped_items=1, notes=[])
    result[field] = True if field == "dry_run" else uuid4()
    with pytest.raises(ValidationError, match="Layout receipt escaped"):
        LayoutReceiptResult(**scope, result=result)


@pytest.mark.parametrize("escaped", ["scene", "frame"])
def test_pose_pairs_reject_records_outside_the_requested_subset(escaped):
    scene_id = uuid4()
    with pytest.raises(ValidationError, match="requested pairs"):
        PosePairsResult(
            pairs=[{"scene_id": scene_id, "frame_index": 0}],
            poses=[
                {
                    "scene_id": uuid4() if escaped == "scene" else scene_id,
                    "frame_index": 1 if escaped == "frame" else 0,
                    "timestamp_us": None,
                    "ego_translation": [0.0] * 3,
                    "ego_rotation": [1.0, 0.0, 0.0, 0.0],
                    "source_metadata": {},
                }
            ],
            snapshot_digest="a" * 64,
        )


def test_pose_pair_reply_parses_uuid_strings_from_native_json():
    scene_id = uuid4()
    reply = PosePairsResult.model_validate(
        {
            "pairs": [{"scene_id": str(scene_id), "frame_index": 0}],
            "poses": [
                {
                    "scene_id": str(scene_id),
                    "frame_index": 0,
                    "timestamp_us": None,
                    "ego_translation": [0.0] * 3,
                    "ego_rotation": [1.0, 0.0, 0.0, 0.0],
                    "source_metadata": {},
                }
            ],
            "snapshot_digest": "a" * 64,
        }
    )
    assert reply.poses[0].scene_id == scene_id


@pytest.mark.parametrize(
    "indices,next_index", [([2, 1], 1), ([1, 1], 1), ([1, 2], 1), ([0], 0)]
)
def test_page_rejects_reordered_repeated_or_nonadvancing_trajectory(
    indices, next_index
):
    with pytest.raises(ValidationError, match="page|cursor|order"):
        PosePage(
            actor_id=None,
            scene_id=uuid4(),
            after_frame_index=0,
            next_frame_index=next_index,
            snapshot_digest="0" * 64,
            total=20,
            complete=False,
            poses=[
                {
                    "frame_index": frame,
                    "timestamp_us": -10,
                    "ego_translation": [0, 0, 0],
                    "ego_rotation": [1, 0, 0, 0],
                    "source_metadata": {},
                }
                for frame in indices
            ],
        )


def test_wire_pose_accepts_legacy_field_names_but_rejects_extra_or_coerced_facts():
    pose = {
        "frame_index": 0,
        "timestamp_us": -10,
        "ego_translation": [0, 0, 0],
        "ego_rotation": [1, 0, 0, 0],
        "source_metadata": {"vendor": "kept"},
    }
    page = PosePage(
        actor_id=None,
        scene_id=uuid4(),
        after_frame_index=None,
        next_frame_index=None,
        snapshot_digest="0" * 64,
        total=1,
        complete=True,
        poses=[pose],
    )
    assert page.poses[0].timestamp_us == -10
    assert page.poses[0].source_metadata == {"vendor": "kept"}
    for altered in (
        {**pose, "unknown": True},
        {**pose, "frame_index": "0"},
        {**pose, "timestamp_us": True},
    ):
        with pytest.raises(ValidationError):
            WireFramePose.model_validate(altered)
    with pytest.raises(ValidationError):
        WireCalibration(extrinsic=["0"] * 16, intrinsic=[1] * 9)
