import json
from importlib.resources import files
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aap_assets_catalog.spatial_contract import (
    SPATIAL_NATIVE_SIGNATURES,
    SPATIAL_SCHEMA,
    CalibrationRecord,
    GuardSpatialActorRequest,
    ReadItemsRequest,
    PosePage,
    ScenePatch,
    SpatialActorResult,
    SpatialFailure,
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
    assert len(SPATIAL_NATIVE_SIGNATURES) == 19
    assert set(expected["x-native-capabilities"]) == set(SPATIAL_NATIVE_SIGNATURES)
    assert expected["$defs"]["ScenePatch"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "indices,next_index", [([2, 1], 1), ([1, 1], 1), ([1, 2], 1), ([0], 0)]
)
def test_page_rejects_reordered_repeated_or_nonadvancing_trajectory(
    indices, next_index
):
    with pytest.raises(ValidationError):
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
                    "translation": [0, 0, 0],
                    "rotation": [1, 0, 0, 0],
                    "source_metadata": {},
                }
                for frame in indices
            ],
        )
