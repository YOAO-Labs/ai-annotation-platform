from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.services.ai_mask_prompt import resolve_authorized_mask_prompt
from app.services.raster_mask_storage import build_rle_reference


RLE = {
    "encoding": "coco_rle",
    "size": [2, 3],
    "counts": [1, 2, 2, 1],
}


def _task() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())


def _db(annotation) -> SimpleNamespace:
    return SimpleNamespace(
        identity_map={},
        autoflush=False,
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: annotation)
        ),
    )


def _annotation(task: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=task.project_id,
        is_active=True,
        was_cancelled=False,
        is_locked=False,
        version=3,
        geometry={
            "type": "raster_mask",
            "mask": build_rle_reference(RLE),
        },
    )


async def test_resolver_replaces_authorized_locator_without_exposing_object_key(
    monkeypatch,
) -> None:
    task = _task()
    annotation = _annotation(task)
    db = _db(annotation)
    validate = AsyncMock()
    load = AsyncMock(return_value=RLE)
    monkeypatch.setattr(
        "app.services.ai_mask_prompt.validate_mask_geometry_for_task",
        validate,
    )
    monkeypatch.setattr("app.services.ai_mask_prompt.load_coco_rle", load)

    prepared = await resolve_authorized_mask_prompt(
        db,
        task=task,
        context={
            "type": "scribble",
            "mask_prompt_source": {
                "annotation_id": str(annotation.id),
                "source_version": 3,
            },
        },
        frame_index=None,
    )

    assert "mask_prompt_source" not in prepared
    assert prepared["mask_prompt"]["rle"] == RLE
    assert prepared["mask_prompt"]["source_annotation_id"] == str(annotation.id)
    assert "object_key" not in prepared["mask_prompt"]
    assert "aap_labeling_v1.annotation_rows" in str(db.execute.await_args.args[0])
    validate.assert_awaited_once()
    load.assert_awaited_once()


async def test_resolver_rejects_browser_inline_mask_content() -> None:
    with pytest.raises(HTTPException) as caught:
        await resolve_authorized_mask_prompt(
            SimpleNamespace(get=AsyncMock()),
            task=_task(),
            context={"mask_prompt": {"rle": RLE}},
            frame_index=None,
        )
    assert caught.value.status_code == 422
    assert caught.value.detail["reason"] == "client_mask_prompt_forbidden"


async def test_resolver_rejects_cross_task_and_stale_sources() -> None:
    task = _task()
    annotation = _annotation(task)
    annotation.task_id = uuid.uuid4()
    db = _db(annotation)
    locator = {
        "mask_prompt_source": {
            "annotation_id": str(annotation.id),
            "source_version": 3,
        }
    }
    with pytest.raises(HTTPException) as crossed:
        await resolve_authorized_mask_prompt(
            db,
            task=task,
            context=locator,
            frame_index=None,
        )
    assert crossed.value.status_code == 404
    assert crossed.value.detail["reason"] == "mask_prompt_source_not_found"

    annotation.task_id = task.id
    locator["mask_prompt_source"]["source_version"] = 2
    with pytest.raises(HTTPException) as stale:
        await resolve_authorized_mask_prompt(
            db,
            task=task,
            context=locator,
            frame_index=None,
        )
    assert stale.value.status_code == 409
    assert stale.value.detail["reason"] == "mask_prompt_source_version_mismatch"


async def test_resolver_maps_storage_failure_without_leaking_content(
    monkeypatch,
) -> None:
    task = _task()
    annotation = _annotation(task)
    db = _db(annotation)
    monkeypatch.setattr(
        "app.services.ai_mask_prompt.validate_mask_geometry_for_task",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.ai_mask_prompt.load_coco_rle",
        AsyncMock(side_effect=RuntimeError("secret storage details")),
    )

    with pytest.raises(HTTPException) as caught:
        await resolve_authorized_mask_prompt(
            db,
            task=task,
            context={
                "mask_prompt_source": {
                    "annotation_id": str(annotation.id),
                    "source_version": 3,
                }
            },
            frame_index=None,
        )

    assert caught.value.status_code == 503
    assert caught.value.detail == {
        "reason": "mask_storage_unavailable",
        "message": "Mask object storage is unavailable",
        "retryable": True,
    }
    assert "secret" not in str(caught.value.detail)
