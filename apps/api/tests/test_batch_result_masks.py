from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.labeling_prediction_contract import ApplyPredictionResultRequest
from app.services.labeling_write_scope import LabelingWriteScope


RLE = {"encoding": "coco_rle", "size": [2, 2], "counts": [0, 1, 3]}
REFERENCE = {
    "encoding": "coco_rle_ref",
    "size": [2, 2],
    "object_key": (
        "raster-masks/sha256/00/00/"
        "0000000000000000000000000000000000000000000000000000000000000000.json"
    ),
    "sha256": "0" * 64,
    "runs": 3,
    "bytes": 20,
}


def _scope(task_id: uuid.UUID, project_id: uuid.UUID) -> LabelingWriteScope:
    return LabelingWriteScope(
        task_id=task_id,
        project_id=project_id,
        actor_id=uuid.uuid4(),
        actor_known=True,
        operation="preannotation",
        execution_scope="bulk",
        execution_scope_known=True,
        task_version=1,
        task_status="pending",
        assignee_id=None,
        review_round_id=None,
        source_annotation_id=None,
        source_annotation_version=None,
        expected_batch_id=None,
        expected_batch_id_known=True,
        job_id=uuid.uuid4(),
        job_id_known=True,
    )


def _request(result: list[dict]) -> ApplyPredictionResultRequest:
    task_id, project_id = uuid.uuid4(), uuid.uuid4()
    return ApplyPredictionResultRequest(
        task_id=task_id,
        project_id=project_id,
        ml_backend_id=None,
        result=result,
        write_scope=_scope(task_id, project_id),
    )


@pytest.mark.asyncio
async def test_normalize_externalizes_inline_and_native_masks_once(monkeypatch):
    from app.services import batch_result_masks
    from app.services.raster_mask_storage import build_rle_reference

    candidate_id = "sha256:" + "a" * 64
    candidate = {
        "type": "mask",
        "value": {"rle": RLE, "masklabels": ["car"]},
        "score": 0.875,
        "candidate_id": candidate_id,
    }
    inline = {
        "id": "shape-1",
        "type": "raster_mask",
        "class_name": "road",
        "geometry": {"type": "raster_mask", "mask": dict(RLE)},
        "confidence": 0.7,
        "track_id": "trk_keep",
        "attributes": {"source": "backend"},
    }
    reference_shape = {
        "id": "shape-ref",
        "type": "raster_mask",
        "class_name": "road",
        "geometry": {"type": "raster_mask", "mask": REFERENCE},
        "confidence": 0.6,
        "track_id": "trk_ref",
        "attributes": {"source": "existing-reference"},
    }
    request = _request([candidate, inline, reference_shape])
    task = SimpleNamespace(id=request.task_id, project_id=request.project_id)
    project = SimpleNamespace(id=request.project_id)
    lookups = []
    stores = []
    geometry_checks = []

    class Session:
        async def get(self, model, identity, **_kwargs):
            lookups.append((model.__name__, identity))
            return task if model.__name__ == "Task" else project

    async def read_project(_db, *, project_id, populate_existing):
        assert project_id == request.project_id and populate_existing
        lookups.append(("Project", project_id))
        return project

    monkeypatch.setattr(batch_result_masks, "get_workspace_project", read_project)

    async def validate_geometry(_db, checked_task, geometry):
        geometry_checks.append((checked_task, geometry))

    def allow_masks(checked_project):
        assert checked_project is project

    async def store(_db, rle):
        stores.append(rle)
        return build_rle_reference(rle)

    monkeypatch.setattr(
        batch_result_masks, "validate_mask_geometry_for_task", validate_geometry
    )
    monkeypatch.setattr(
        batch_result_masks, "assert_raster_mask_write_enabled", allow_masks
    )
    monkeypatch.setattr(batch_result_masks, "store_generated_mask_content", store)

    (prepared,) = await batch_result_masks.normalize_batch_prediction_masks(
        Session(), (request,)
    )

    candidate_result, inline_result, unchanged_reference_shape = prepared.result
    expected_reference = build_rle_reference(RLE)
    assert candidate_result == {
        "type": "raster_mask",
        "class_name": "car",
        "geometry": {"type": "raster_mask", "mask": expected_reference},
        "confidence": 0.875,
        "tool_unit_id": "region",
        "attributes": {},
        "candidate_id": candidate_id,
    }
    assert inline_result["id"] == "shape-1"
    assert inline_result["class_name"] == "road"
    assert inline_result["confidence"] == 0.7
    assert inline_result["track_id"] == "trk_keep"
    assert inline_result["attributes"] == {"source": "backend"}
    assert inline_result["geometry"]["mask"] == expected_reference
    assert unchanged_reference_shape is reference_shape
    assert unchanged_reference_shape["geometry"]["mask"] is REFERENCE
    assert len(stores) == 1
    assert len(geometry_checks) == 2
    assert {name for name, _identity in lookups} == {"Task", "Project"}
    assert request.result == [candidate, inline, reference_shape]
    assert prepared.result is not request.result


@pytest.mark.asyncio
async def test_normalize_leaves_reference_masks_and_nonmask_requests_untouched():
    from app.services.batch_result_masks import normalize_batch_prediction_masks

    request = _request(
        [
            {
                "type": "raster_mask",
                "class_name": "car",
                "geometry": {"type": "raster_mask", "mask": REFERENCE},
            }
        ]
    )

    class Session:
        async def get(self, *_args, **_kwargs):
            pytest.fail("reference-only masks need no materialization")

    (prepared,) = await normalize_batch_prediction_masks(Session(), (request,))
    assert prepared is request


@pytest.mark.parametrize("field", ["id", "track_id", "attributes"])
def test_native_mask_candidate_rejects_unmodeled_prediction_metadata(field):
    from aap_protocol_v2 import NativeMaskCandidate
    from pydantic import ValidationError

    candidate = {
        "type": "mask",
        "value": {"rle": RLE, "masklabels": ["car"]},
        "score": 0.875,
        "candidate_id": "sha256:" + "a" * 64,
        field: {"backend": "extra"} if field == "attributes" else "extra",
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NativeMaskCandidate.model_validate(candidate)


@pytest.mark.asyncio
async def test_normalize_propagates_content_fence_without_mutating_or_committing(
    monkeypatch,
):
    from app.services import batch_result_masks
    from app.services.raster_mask_storage import RasterMaskContractError

    request = _request(
        [
            {
                "type": "raster_mask",
                "class_name": "car",
                "geometry": {"type": "raster_mask", "mask": dict(RLE)},
            }
        ]
    )
    task = SimpleNamespace(id=request.task_id, project_id=request.project_id)
    project = SimpleNamespace(id=request.project_id)

    monkeypatch.setattr(
        batch_result_masks, "get_workspace_project", AsyncMock(return_value=project)
    )

    class Session:
        async def get(self, model, *_args, **_kwargs):
            return task if model.__name__ == "Task" else project

        async def commit(self):
            pytest.fail("mask normalization must not commit caller state")

    async def validate_geometry(*_args, **_kwargs):
        return None

    async def store(*_args, **_kwargs):
        raise RasterMaskContractError(
            status_code=409,
            reason="mask_content_deleting",
            message="Mask content is being collected",
        )

    monkeypatch.setattr(
        batch_result_masks, "validate_mask_geometry_for_task", validate_geometry
    )
    monkeypatch.setattr(
        batch_result_masks, "assert_raster_mask_write_enabled", lambda _project: None
    )
    monkeypatch.setattr(batch_result_masks, "store_generated_mask_content", store)

    with pytest.raises(RasterMaskContractError) as failure:
        await batch_result_masks.normalize_batch_prediction_masks(Session(), (request,))

    assert failure.value.detail["reason"] == "mask_content_deleting"
    assert request.result[0]["geometry"]["mask"] == RLE
