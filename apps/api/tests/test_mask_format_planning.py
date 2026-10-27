from __future__ import annotations

import json
import uuid

import pytest

from app.schemas.mask_format import MaskFormatPlan
from app.services.mask_formats.contracts import canonical_digest
from app.services.mask_formats.planning import _plan


@pytest.mark.parametrize("direction", ["import", "export"])
@pytest.mark.parametrize("complete", [False, True])
def test_plan_digest_matches_normalized_persisted_json(direction, complete):
    payload = {
        "format_id": "aap_json",
        "direction": direction,
        "adapter_version": "1.0.0",
        "manifest_version": "1.3",
        "media_type": "image",
        "loss_class": "lossless",
        "mapping_digest": canonical_digest({}),
        "options_digest": canonical_digest({}),
        "estimated_objects": 1,
        "estimated_files": 1,
        "estimated_bytes": 10,
        "items": [
            {
                "item_id": "item",
                "task_id": uuid.UUID(int=1),
                "loss_class": "lossless",
                "estimated_objects": 1,
                "estimated_files": 1,
                "estimated_bytes": 10,
            }
        ],
    }
    if complete:
        payload = MaskFormatPlan.model_validate(
            {**payload, "plan_digest": ""}
        ).model_dump(mode="json", exclude={"plan_digest"})

    plan = _plan(payload)
    persisted = json.loads(plan.model_dump_json())
    assert persisted["items"][0]["task_id"] == str(uuid.UUID(int=1))
    assert persisted["warnings"] == []
    assert persisted["items"][0]["warnings"] == []
    assert persisted.pop("plan_digest") == canonical_digest(persisted)
    assert _plan(persisted).plan_digest == plan.plan_digest
