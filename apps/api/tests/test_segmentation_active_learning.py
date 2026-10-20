"""Mask-specific scoring and human acceptance through the retained round owner."""

import copy
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from training_contract.model_return import segmentation_identity

from app.config import settings
from app.db.models.annotation import Annotation
from app.schemas.active_learning import DecisionCreate
from app.services import active_learning as service, active_learning_engine as engine
from app.services import active_learning_pool as pool, trained_models
from app.services.training_dataset_bridge import content_digest
from tests.active_learning_fixtures import seed, create
from tests.sample_evaluation_fixtures import prediction
from tests.test_sample_evaluation_masks import predictions, rle, wire


def test_mask_ambiguity_uses_pixels_and_keeps_random_control_score_independent():
    first, second = rle(0, 1, 14, 1), rle(3, 1, 8, 1, 3)
    values = predictions((0, 0.8, first), (1, 0.9, second))
    features = engine.mask_features(values)
    assert features["ambiguity"] == 0
    assert features["priority"] == pytest.approx(0.2)
    assert features["minimum_instance_pixels"] == 2
    assert engine.mask_features([])["priority"] is None
    assert engine.strategy_for({"task": "segmentation"}) == engine.MASK_STRATEGY
    assert engine.strategy_for({}) == engine.STRATEGY
    samples = engine.partition(
        [
            {"task_id": str(uuid.UUID(int=i + 1)), "group_digest": str(i)}
            for i in range(8)
        ],
        42,
        2,
    )
    spec = {"samples": samples, "seed": 42, "batch_size": 2}
    evidence = {
        sample["ordinal"]: {
            "features": engine.mask_features([]),
            "evidence_digest": "a" * 64,
        }
        for sample in samples
    }
    initial = engine.ranking(spec, evidence)
    for row in evidence.values():
        row["features"] = features
    changed = engine.ranking(spec, evidence)
    assert [row for row in initial if row["arm"] == "random_control"] == [
        {**row, "features": engine.mask_features([])}
        for row in changed
        if row["arm"] == "random_control"
    ]


async def test_mask_round_revalidates_pixel_evidence_and_accepts_only_human_tasks(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    world.project.tool_bindings = {
        **world.project.tool_bindings,
        "region": world.project.tool_bindings["bbox"],
    }
    world.project.raster_mask_native_editing_enabled = True
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    world.model.provenance = {
        **world.model.provenance,
        "task": "segment",
        "base_model": {"id": "yolo11n-seg"},
        "model_return": segmentation_identity("yolo11n-seg"),
    }
    world.deployment.receipt = trained_models.expected_receipt(world.model)
    world.ref = trained_models.frozen_reference(world.model, world.deployment)
    world.profile = {**world.profile, "implementation": "aap-ultralytics-instance-mask"}
    world.profile["profile_digest"] = content_digest(
        {k: v for k, v in world.profile.items() if k != "profile_digest"}
    )
    monkeypatch.setattr(
        pool, "qualification", AsyncMock(return_value=(world.ref, world.profile))
    )
    await db_session.flush()
    round, attempt, _ = await create(db_session, owner, world)
    assert round.spec["schema"] == round.spec["schema_version"] == 2
    assert round.spec["strategy"] == engine.MASK_STRATEGY
    rows = await service.candidate_rows(db_session, round)
    for row in rows:
        sample = round.spec["samples"][row.ordinal]
        service.prepare_image(round, sample)
        row.media = service.media_descriptor(round, sample)
        row.request = service.managed.request_identity(
            round.spec, row, sample["preprocessing"]
        )
        result = prediction(round, row)
        raw = wire(rle(0, 192, size=(12, 16)))
        result.result, result.meta = raw["result"], {**result.meta, **raw["meta"]}
        row.evidence = service.successful_evidence(round, attempt, row, result)
        row.status, row.produced_attempt_id = "succeeded", attempt.id
    await db_session.flush()
    original = copy.deepcopy(rows[0].evidence)
    rows[0].evidence = {
        **original,
        "predictions": [{**original["predictions"][0], "area_pixels": 191}],
    }
    with pytest.raises(ValueError):
        service.verified_evidence(round, rows[0])
    rows[0].evidence = original
    round.inference_dispatches = len(rows)
    report = await service.build_report(db_session, round, attempt)
    await service.mark_terminal(db_session, round, attempt, "succeeded", report=report)
    await db_session.flush()
    arms = {
        arm: [
            row for row in report["ranking"] if row["arm"] == arm and row["recommended"]
        ]
        for arm in ("active", "random_control")
    }
    assert len(arms["active"]) == len(arms["random_control"]) == 2
    assert not {row["task_id"] for row in arms["active"]} & {
        row["task_id"] for row in arms["random_control"]
    }
    selected = [arms[arm][0]["ordinal"] for arm in arms]
    await service.decide(
        db_session,
        world.project,
        owner,
        round.id,
        DecisionCreate(request_id=uuid.uuid4(), action="accept", ordinals=selected),
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Annotation)
            .where(Annotation.task_id.in_([t.id for t in world.tasks]))
        )
        == 0
    )
    assert all(task.status == "pending" and not task.is_labeled for task in world.tasks)
