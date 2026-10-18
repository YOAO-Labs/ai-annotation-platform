from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aap_assets_media.contract import (
    MEDIA_SCHEMA,
    OPERATIONS,
    AcceptedWork,
    AssetsRequest,
    BackfillSelection,
    BackfillSpec,
    ClaimRequest,
    ClaimedWork,
    FrameResult,
    FrameSelection,
    FrameSpec,
    MediaAsset,
    MediaAssets,
    MediaClaim,
    MediaFailure,
    MediaSource,
    MetadataResult,
    ObjectIdentity,
    ReadSourceRequest,
    ScheduleWorkRequest,
    TimetablePage,
    TimetableRequest,
    WorkReference,
    contract_document,
    decode_reply,
    json_bytes,
    require_result_scope,
    require_scope,
)


def source(**changes):
    fields = dict(
        dataset_item_id=uuid4(),
        dataset_id=uuid4(),
        source_generation=1,
        source_snapshot="a" * 64,
        timetable_revision=0,
        materialized_generation=1,
        object_identity=None,
        source_drifted=False,
        file_name="source.mp4",
        file_path="dataset/source.mp4",
        file_type="video",
        file_size=12,
        content_hash=None,
        width=640,
        height=480,
        thumbnail_path=None,
        blurhash=None,
        video_metadata={"fps": 30.0, "frame_count": 100},
    )
    fields.update(changes)
    return MediaSource(**fields)


def claim(**changes):
    job_id, token = uuid4(), uuid4()
    fields = dict(
        job_id=job_id,
        generation=1,
        lease_token=token,
        lease_expires_at=datetime.now(timezone.utc),
        source=source(),
        spec=FrameSpec(frame_index=0),
        output_prefix=f"media-assets/{job_id}/g1/a{token}/",
        timetable_revision=0,
        timing={},
    )
    fields.update(changes)
    return MediaClaim(**fields)


def test_installed_schema_names_roles_and_types_are_frozen():
    schema_path = (
        Path(__file__).parents[1] / "src/aap_assets_media/protocol.schema.json"
    )
    assert json.loads(schema_path.read_text()) == contract_document()
    assert len(OPERATIONS) == 26
    assert {
        role: sum(op.role == role for op in OPERATIONS.values())
        for role in ("control", "system", "worker")
    } == {"control": 12, "system": 4, "worker": 10}
    for operation in OPERATIONS.values():
        assert set(operation.arguments) <= operation.request.model_fields.keys()
        assert len(operation.arguments) == len(operation.sql_types)


def test_core_imports_no_api_or_runtime_framework():
    root = Path(__file__).parents[1] / "src/aap_assets_media"
    forbidden = {"app", "sqlalchemy", "fastapi", "celery", "asyncpg", "boto3"}
    for file in root.glob("*.py"):
        if file.name not in {"__init__.py", "contract.py"}:
            continue
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node, ast.Import):
                assert not {part.name.split(".")[0] for part in node.names} & forbidden
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden


@pytest.mark.parametrize(
    "body",
    [
        {"schema": MEDIA_SCHEMA, "ok": 1, "data": {}},
        {"schema": "other/v1", "ok": True, "data": {}},
        {
            "schema": MEDIA_SCHEMA,
            "ok": False,
            "error": {"code": "source_changed", "secret": "hidden"},
        },
        {
            "schema": MEDIA_SCHEMA,
            "ok": False,
            "error": {"code": "unrecognized_failure"},
        },
        {"schema": MEDIA_SCHEMA, "ok": True, "data": {}, "unexpected": True},
    ],
)
def test_decoder_fails_closed_without_echoing_bad_input(body):
    with pytest.raises(MediaFailure) as failed:
        decode_reply(body, MediaSource)
    assert failed.value.code == "invalid_response"
    assert "hidden" not in str(failed.value)


def test_expected_failure_code_survives_decoding():
    with pytest.raises(MediaFailure) as failed:
        decode_reply(
            {"schema": MEDIA_SCHEMA, "ok": False, "error": {"code": "source_changed"}},
            MediaSource,
        )
    assert failed.value.code == "source_changed"


def test_source_scope_and_cached_frame_generation_cannot_be_substituted():
    current = source()
    request = AssetsRequest(
        actor_id=uuid4(),
        dataset_item_id=current.dataset_item_id,
        selection=FrameSelection(frame_indices=[0]),
    )
    row = MediaAsset(
        row_id=uuid4(),
        dataset_item_id=current.dataset_item_id,
        kind="frame",
        source_generation=1,
        timetable_revision=0,
        status="pending",
        frame_index=0,
        width=512,
        format="webp",
    )
    response = MediaAssets(source=current, assets=[row])
    require_scope(request, response)
    for bad_row in (
        row.model_copy(update={"source_generation": 2}),
        row.model_copy(update={"frame_index": 1}),
        row.model_copy(update={"width": 1024}),
    ):
        with pytest.raises(ValueError):
            require_scope(request, MediaAssets(source=current, assets=[bad_row]))
    with pytest.raises(ValueError):
        require_scope(request, MediaAssets(source=current, assets=[]))


def test_lease_prefix_rejects_another_attempt_and_foreign_output():
    accepted = claim()
    with pytest.raises(ValidationError):
        claim(output_prefix=f"media-assets/{uuid4()}/g1/a{uuid4()}/")
    result = FrameResult(
        source_identity=ObjectIdentity(etag="etag", byte_size=12),
        source_sha256="b" * 64,
        artifact={
            "key": accepted.output_prefix + "frame.webp",
            "byte_size": 12,
            "sha256": "c" * 64,
        },
    )
    require_result_scope(accepted, result)
    with pytest.raises(ValueError):
        require_result_scope(
            accepted,
            result.model_copy(
                update={
                    "artifact": result.artifact.model_copy(
                        update={"key": "thumbnails/other.webp"}
                    )
                }
            ),
        )


def test_timetable_page_rejects_unordered_or_nonprogressing_cursors():
    current = source()
    request = TimetableRequest(
        actor_id=uuid4(),
        dataset_item_id=current.dataset_item_id,
        after_frame=0,
        page_size=2,
    )
    page = TimetablePage(
        source=current,
        snapshot_digest="a" * 64,
        total=4,
        after_frame=0,
        next_frame=2,
        complete=False,
        frames=[
            {"frame_index": 1, "pts_ms": 47, "is_keyframe": False},
            {"frame_index": 2, "pts_ms": 101, "is_keyframe": True},
        ],
    )
    require_scope(request, page)
    for bad in (
        page.model_copy(update={"next_frame": 1}),
        page.model_copy(update={"frames": list(reversed(page.frames))}),
        page.model_copy(update={"frames": []}),
    ):
        with pytest.raises(ValueError):
            require_scope(request, bad)


def test_backfill_authority_is_distinct_from_a_source_attempt():
    spec = BackfillSpec(selection=BackfillSelection(dataset_id=uuid4()))
    request = ScheduleWorkRequest(operation_id=uuid4(), dataset_item_id=None, spec=spec)
    accepted = AcceptedWork(
        operation_id=request.operation_id,
        source=None,
        spec=spec.model_copy(
            update={
                "selection": spec.selection.model_copy(
                    update={"accepted_before": datetime.now(timezone.utc)}
                )
            }
        ),
        work=WorkReference(
            job_id=uuid4(),
            generation=1,
            dataset_item_id=None,
            source_generation=0,
            kind="video_backfill",
            status="pending",
        ),
    )
    require_scope(request, accepted)
    with pytest.raises(ValidationError):
        ScheduleWorkRequest(
            operation_id=uuid4(), dataset_item_id=None, spec=FrameSpec(frame_index=0)
        )
    with pytest.raises(ValidationError):
        claim(spec=spec)
    backfill_claim = claim(spec=spec, source=None, output_prefix=None)
    assert backfill_claim.source is None


def test_values_reject_nul_and_nonfinite_json():
    with pytest.raises(ValidationError):
        source(file_path="private\x00source")
    with pytest.raises(ValueError):
        json_bytes({"fps": float("nan")})
    with pytest.raises(MediaFailure):
        decode_reply(
            '{"schema":"aap-assets-media/v1","ok":true,"data":{"fps":NaN}}', MediaSource
        )


def test_metadata_cannot_publish_an_unproven_path_or_partial_counts():
    common = dict(
        kind="video_metadata",
        source_identity={"etag": "etag", "byte_size": 12},
        source_sha256="a" * 64,
    )
    with pytest.raises(ValidationError):
        MetadataResult(
            **common, metadata_patch={"poster_frame_path": "foreign/key.webp"}
        )
    with pytest.raises(ValidationError):
        MetadataResult(**common, metadata_patch={}, timetable_total_frames=2)


def test_specific_claim_and_source_response_keep_requested_identity():
    current = source()
    with pytest.raises(ValueError):
        require_scope(
            ReadSourceRequest(actor_id=uuid4(), dataset_item_id=uuid4()), current
        )
    requested = ClaimRequest(worker_id=uuid4(), job_id=uuid4())
    with pytest.raises(ValueError):
        require_scope(requested, ClaimedWork(claim=claim()))
    different_capability = ClaimRequest(worker_id=uuid4(), kinds=["chunk"])
    with pytest.raises(ValueError):
        require_scope(different_capability, ClaimedWork(claim=claim()))
    # A claim remains an explicit value even when the queue is empty.
    assert ClaimedWork(claim=None).claim is None
    assert requested.kinds == [
        "video_metadata",
        "video_timetable",
        "frame",
        "chunk",
        "video_backfill",
    ]
