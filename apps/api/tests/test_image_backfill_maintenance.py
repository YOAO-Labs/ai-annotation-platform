"""Operator image backfills use bounded published owner contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

from aap_assets_images.contract import ImageBackfillSelection
from scripts.backfill_image_pyramids import _eligible_owner_ids
from scripts.image_media_backfill import accept_image_backfill
from scripts.seed_large_images import _pyramid_statuses


def _source(owner_id, *, width, height, ready=True, drifted=False, item_id=None):
    return SimpleNamespace(
        owner=SimpleNamespace(id=owner_id),
        item_id=item_id,
        width=width,
        height=height,
        file_type="image",
        upload_ready=ready,
        source_drifted=drifted,
    )


@pytest.mark.asyncio
async def test_pyramid_backfill_scans_owner_facts_and_applies_existing_eligibility(
    monkeypatch,
):
    dataset_id = uuid.uuid4()
    eligible = uuid.uuid4()
    too_small = uuid.uuid4()
    drifted = uuid.uuid4()
    not_ready = uuid.uuid4()
    calls = []

    class Gateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def call(self, operation, request):
            calls.append((operation, request))
            assert operation == "scan_image_backfill"
            return SimpleNamespace(
                items=[
                    _source(eligible, width=5000, height=5000),
                    _source(too_small, width=1000, height=1000),
                    _source(drifted, width=5000, height=5000, drifted=True),
                    _source(not_ready, width=5000, height=5000, ready=False),
                ],
                next_owner_id=None,
                complete=True,
            )

    monkeypatch.setattr("scripts.backfill_image_pyramids.image_gateway", Gateway)

    owner_ids, accepted_before = await _eligible_owner_ids(
        owner_kind="dataset_item",
        dataset_id=dataset_id,
        cursor=None,
        limit=10,
    )

    assert owner_ids == [eligible]
    assert accepted_before.tzinfo is not None
    assert calls[0][1].selection.dataset_id == dataset_id
    assert calls[0][1].selection.accepted_before == accepted_before


@pytest.mark.asyncio
async def test_operator_acceptance_requires_frozen_cutoff_and_explicit_operation(
    monkeypatch,
):
    dataset_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    accepted_before = datetime.now(timezone.utc)
    calls = []

    class Gateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def call(self, operation, request):
            calls.append((operation, request))
            return SimpleNamespace(work=SimpleNamespace(job_id=uuid.uuid4()))

    monkeypatch.setattr("app.services.assets_images.image_gateway", Gateway)
    selection = ImageBackfillSelection(
        owner_kind="dataset_item",
        dataset_id=dataset_id,
        purpose="thumbnail",
        only_missing=True,
        accepted_before=accepted_before,
    )

    accepted = await accept_image_backfill(selection, operation_id=operation_id)

    assert accepted.work.job_id is not None
    assert len(calls) == 1
    operation, request = calls[0]
    assert operation == "schedule_image_media"
    assert request.operation_id == operation_id
    assert request.spec.selection == selection


@pytest.mark.asyncio
async def test_seed_status_uses_owner_summary_and_generation_projection(monkeypatch):
    item_id = uuid.uuid4()
    calls = []

    class Gateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def call(self, operation, request):
            calls.append((operation, request))
            if operation == "read_image_summaries":
                return SimpleNamespace(
                    items=[
                        SimpleNamespace(
                            selector=SimpleNamespace(dataset_item_id=item_id),
                            pyramid=SimpleNamespace(
                                generation=3,
                                status="ready",
                                error_code=None,
                                width=5000,
                                height=4000,
                            ),
                        )
                    ]
                )
            assert operation == "read_image_generation"
            assert request.dataset_item_id == item_id
            return SimpleNamespace(
                pyramid=SimpleNamespace(tile_count=80, retained_bytes=8192)
            )

    monkeypatch.setattr("scripts.seed_large_images.image_gateway", Gateway)

    statuses = await _pyramid_statuses([item_id], uuid.uuid4(), include_details=True)

    assert statuses[str(item_id)] == {
        "generation": 3,
        "status": "ready",
        "error_code": None,
        "width": 5000,
        "height": 4000,
        "tile_count": 80,
        "retained_bytes": 8192,
    }
    assert [operation for operation, _ in calls] == [
        "read_image_summaries",
        "read_image_generation",
    ]
