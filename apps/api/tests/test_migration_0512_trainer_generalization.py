"""Verify forward-only migration 0512 trainer SQL generalization and security invariants."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.db.models.active_learning import ActiveLearningRound
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.trained_model import (
    ModelDeployment,
    ModelRelease,
    ProjectModelSelection,
    TrainedModelVersion,
)
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services import trained_models, training_adapters
from tests.conftest import create_registry_with_pool
from tests.factory import create_project
from tests.test_trained_models import seed_source


async def _seed_ready_deployment(db, project, version):
    backend, pool = await create_registry_with_pool(
        db,
        name="t1-0512-" + uuid.uuid4().hex[:8],
        state="connected",
        enabled_pool=True,
        auth_method="token",
        auth_token="a" * 40,
        last_checked_at=trained_models.now(),
        health_meta={"capabilities": {}, "compute": {"effective_device": "cpu"}},
    )
    db.add(ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True))
    deployment = ModelDeployment(
        model_version_id=version.id,
        registry_id=backend.id,
        status="ready",
        receipt={"model_version_id": str(version.id)},
        trial={"result": []},
    )
    db.add(deployment)
    await db.flush()
    return backend, deployment


REPLACED_FUNCTIONS = (
    "yoao_learning_v1.training_dataset_task(jsonb)",
    "yoao_learning_v1.training_dataset_profile(jsonb,text)",
    "yoao_learning_v1.advance_model_deployment_retirement(uuid,uuid,uuid,uuid,boolean)",
    "yoao_learning_v1.pending_dataset_mask_keys()",
    "yoao_learning_v1.pending_dataset_mask_referenced(text)",
    "yoao_learning_v1.selection_rounds(uuid,text,integer,integer)",
    "yoao_learning_v1.selection_round_count(uuid,text)",
)

SECURITY_DEFINER_FUNCTIONS = (
    "yoao_learning_v1.advance_model_deployment_retirement(uuid,uuid,uuid,uuid,boolean)",
    "yoao_learning_v1.pending_dataset_mask_keys()",
    "yoao_learning_v1.pending_dataset_mask_referenced(text)",
    "yoao_learning_v1.selection_rounds(uuid,text,integer,integer)",
    "yoao_learning_v1.selection_round_count(uuid,text)",
)


async def test_migration_0512_preserves_security_definer_search_path_and_public_revoke(
    db_session,
):
    rows = (
        (
            await db_session.execute(
                text(
                    "SELECT sig, p.prosecdef, p.proconfig, "
                    "has_function_privilege('public', p.oid, 'EXECUTE') AS public_exec "
                    "FROM unnest(CAST(:sigs AS text[])) AS sig "
                    "JOIN pg_catalog.pg_proc p ON p.oid = sig::regprocedure"
                ),
                {"sigs": list(REPLACED_FUNCTIONS)},
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == len(REPLACED_FUNCTIONS)
    for row in rows:
        assert row["public_exec"] is False
        assert "search_path=pg_catalog, pg_temp" in (row["proconfig"] or [])
        if row["sig"] in SECURITY_DEFINER_FUNCTIONS:
            assert row["prosecdef"] is True


async def test_generalized_dataset_task_and_profile_preserve_legacy_and_support_plugins(
    db_session,
):
    cases = [
        (
            {"schema": 1},
            "detect",
            {
                "format": "ultralytics-yolo",
                "geometry": "normalized-bbox",
                "bridge": "ultralytics-bbox-1",
            },
        ),
        (
            {
                "schema": 2,
                "task": "image-instance-segmentation",
                "format": "ultralytics-segmentation",
                "geometry": "normalized-simple-polygon",
                "bridge": "ultralytics-simple-polygon-1",
            },
            "segment",
            {
                "format": "ultralytics-yolo",
                "geometry": "normalized-simple-polygon",
                "bridge": "ultralytics-simple-polygon-1",
            },
        ),
        (
            {
                "schema": 4,
                "task": "segment",
                "media_type": "image",
                "format": "aap-instance-rle",
                "geometry": "instance-mask",
                "bridge": "ultralytics-instance-rle-1",
            },
            "segment",
            {
                "format": "aap-instance-rle",
                "geometry": "instance-mask",
                "bridge": "ultralytics-instance-rle-1",
            },
        ),
        (
            {
                "schema": 5,
                "task": "detect",
                "media_type": "image",
                "format": "coco-json",
                "geometry": "normalized-bbox",
                "bridge": "custom-coco-bbox-1",
            },
            "detect",
            {
                "format": "coco-json",
                "geometry": "normalized-bbox",
                "bridge": "custom-coco-bbox-1",
            },
        ),
        (
            {
                "schema": 5,
                "task": "obb",
                "media_type": "image",
                "format": "dota-obb",
                "geometry": "oriented-bbox",
                "bridge": "custom-obb-1",
            },
            "obb",
            {
                "format": "dota-obb",
                "geometry": "oriented-bbox",
                "bridge": "custom-obb-1",
            },
        ),
    ]
    for snapshot, expected_task, expected_profile in cases:
        raw = json.dumps(snapshot)
        task = await db_session.scalar(
            text("SELECT yoao_learning_v1.training_dataset_task(CAST(:s AS jsonb))"),
            {"s": raw},
        )
        assert task == expected_task
        profile = await db_session.scalar(
            text(
                "SELECT yoao_learning_v1.training_dataset_profile(CAST(:s AS jsonb), :t)"
            ),
            {"s": raw, "t": task},
        )
        for key, value in expected_profile.items():
            assert profile[key] == value

    rejected_snapshots = [
        {"schema": 4.5, "task": "detect", "media_type": "image"},
        {"schema": "bad", "task": "detect", "media_type": "image"},
        {
            "schema": 4,
            "task": "segment",
            "media_type": "image",
            "format": "ultralytics-yolo",
            "geometry": "instance-mask",
            "bridge": "ultralytics-instance-rle-1",
        },
        {
            "schema": 4,
            "task": "segment",
            "media_type": "image",
            "format": "aap-instance-rle",
            "geometry": "normalized-simple-polygon",
            "bridge": "ultralytics-instance-rle-1",
        },
        {
            "schema": 4,
            "task": "segment",
            "media_type": "image",
            "format": "aap-instance-rle",
            "geometry": "instance-mask",
            "bridge": "ultralytics-simple-polygon-1",
        },
        {
            "schema": 5,
            "task": "image-detection",
            "media_type": "image",
            "format": "coco-json",
            "geometry": "normalized-bbox",
            "bridge": "custom-coco-bbox-1",
        },
        {
            "schema": 5,
            "task": "image-instance-segmentation",
            "media_type": "image",
            "format": "coco-json",
            "geometry": "normalized-simple-polygon",
            "bridge": "custom-poly-1",
        },
    ]
    for bad in rejected_snapshots:
        raw = json.dumps(bad)
        bad_task = await db_session.scalar(
            text("SELECT yoao_learning_v1.training_dataset_task(CAST(:s AS jsonb))"),
            {"s": raw},
        )
        assert bad_task is None, bad
        profile = await db_session.scalar(
            text(
                "SELECT yoao_learning_v1.training_dataset_profile(CAST(:s AS jsonb), 'detect')"
            ),
            {"s": raw},
        )
        assert isinstance(profile, dict)

    overflow_snapshot = {
        "schema": 9999999999,
        "task": "obb",
        "media_type": "image",
        "format": "dota-obb",
        "geometry": "oriented-bbox",
        "bridge": "custom-obb-1",
    }
    raw_overflow = json.dumps(overflow_snapshot)
    assert (
        await db_session.scalar(
            text("SELECT yoao_learning_v1.training_dataset_task(CAST(:s AS jsonb))"),
            {"s": raw_overflow},
        )
        == "obb"
    )
    overflow_profile = await db_session.scalar(
        text(
            "SELECT yoao_learning_v1.training_dataset_profile(CAST(:s AS jsonb), 'obb')"
        ),
        {"s": raw_overflow},
    )
    assert overflow_profile["format"] == "dota-obb"


async def test_pending_dataset_mask_gc_guards_generalized_instance_rle_snapshots(
    db_session, super_admin
):
    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    plugin_mask_key = f"raster-masks/sha256/{'1' * 64}.rle"
    legacy_mask_key = f"raster-masks/sha256/{'2' * 64}.rle"
    for idx, (snapshot, key) in enumerate(
        (
            (
                {
                    "schema": 4,
                    "task": "segment",
                    "media_type": "image",
                    "format": "plugin-rle",
                    "geometry": "instance-mask",
                    "bridge": "custom-rle-1",
                    "mask_storage_kind": "instance_rle",
                    "samples": [{"mask": {"object_key": plugin_mask_key}}],
                },
                plugin_mask_key,
            ),
            (
                {
                    "schema": 4,
                    "task": "segment",
                    "media_type": "image",
                    "format": "aap-instance-rle",
                    "geometry": "instance-mask",
                    "bridge": "ultralytics-instance-rle-1",
                    "samples": [{"mask": {"object_key": legacy_mask_key}}],
                },
                legacy_mask_key,
            ),
        )
    ):
        db_session.add(
            TrainingDatasetVersion(
                id=uuid.uuid4(),
                project_id=project.id,
                creator_id=owner.id,
                request_id=uuid.uuid4(),
                request_digest=f"{idx}" * 64,
                attempt_id=uuid.uuid4(),
                name=f"pending-mask-{idx}",
                status="pending",
                snapshot=snapshot,
                snapshot_digest=f"{idx + 3}" * 64,
                preparation_report={},
                manifest={},
                progress_pct=0,
            )
        )
    await db_session.flush()

    keys = set(
        await db_session.scalars(
            text("SELECT object_key FROM yoao_learning_v1.pending_dataset_mask_keys()")
        )
    )
    assert plugin_mask_key in keys
    assert legacy_mask_key in keys
    assert (
        await db_session.scalar(
            text("SELECT yoao_learning_v1.pending_dataset_mask_referenced(:k)"),
            {"k": plugin_mask_key},
        )
        is True
    )
    assert (
        await db_session.scalar(
            text("SELECT yoao_learning_v1.pending_dataset_mask_referenced(:k)"),
            {"k": legacy_mask_key},
        )
        is True
    )


@pytest.mark.parametrize("framework", ["ultralytics", "custom_trainer"])
async def test_deployment_retirement_works_for_non_libreyolo_frameworks(
    db_session,
    super_admin,
    framework,
):
    owner, _ = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    version = TrainedModelVersion(
        project_id=project.id,
        run_id=run.id,
        attempt_id=attempt.id,
        creator_id=owner.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name=f"{framework}-retirable",
        framework=framework,
        framework_version="8.4.183",
        artifact={"key": "attempts/x/best.pt", "sha256": "b" * 64, "size_bytes": 100},
        classes=["car"],
        bindings=["car"],
        binding_digest="c" * 64,
        provenance={"task": "detect"},
        evaluation={},
    )
    db_session.add(version)
    await db_session.flush()
    _, deployment = await _seed_ready_deployment(db_session, project, version)

    release = ModelRelease(
        project_id=project.id,
        model_version_id=version.id,
        deployment_id=deployment.id,
        creator_id=owner.id,
        request_id=uuid.uuid4(),
        request_digest="d" * 64,
        generation=1,
        task="detect",
        snapshot={"retained": True},
    )
    db_session.add(release)
    await db_session.flush()
    selection = ProjectModelSelection(
        project_id=project.id, task="detect", generation=1, release_id=release.id
    )
    db_session.add(selection)
    await db_session.flush()

    drained = await trained_models.retire_deployment(
        db_session, project.id, version.id, deployment.id, terminal=False
    )
    assert drained.status == "draining"

    with pytest.raises(Exception) as exc_info:
        await trained_models.retire_deployment(
            db_session, project.id, version.id, deployment.id, terminal=True
        )
    assert exc_info.value.detail["active_releases"] == 1

    selection.release_id = None
    await db_session.flush()
    retired = await trained_models.retire_deployment(
        db_session, project.id, version.id, deployment.id, terminal=True
    )
    assert retired.status == "retired"


async def test_selection_rounds_supports_custom_canonical_tasks(
    db_session, super_admin
):
    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    run_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    _, run, attempt = await seed_source(db_session, owner)
    version = TrainedModelVersion(
        project_id=project.id,
        run_id=run.id,
        attempt_id=attempt.id,
        creator_id=owner.id,
        request_id=run_id,
        request_digest="e" * 64,
        name="obb-model",
        framework="custom_trainer",
        framework_version="1.0.0",
        artifact={"key": "k", "sha256": "f" * 64, "size_bytes": 10},
        classes=["ship"],
        bindings=["ship"],
        binding_digest="1" * 64,
        provenance={"task": "obb"},
        evaluation={},
    )
    db_session.add(version)
    await db_session.flush()
    _, deployment = await _seed_ready_deployment(db_session, project, version)

    obb_round = ActiveLearningRound(
        id=attempt_id,
        project_id=project.id,
        creator_id=owner.id,
        model_version_id=version.id,
        deployment_id=deployment.id,
        request_id=uuid.uuid4(),
        request_digest="2" * 64,
        name="obb-round",
        status="queued",
        spec={"model_ref": {"task": "obb"}},
        spec_digest="3" * 64,
        authorization={},
        generation=0,
        inference_dispatches=0,
        accepted_tasks=0,
    )
    detect_default_round = ActiveLearningRound(
        id=uuid.uuid4(),
        project_id=project.id,
        creator_id=owner.id,
        model_version_id=version.id,
        deployment_id=deployment.id,
        request_id=uuid.uuid4(),
        request_digest="4" * 64,
        name="detect-default-round",
        status="queued",
        spec={"model_ref": {}},
        spec_digest="5" * 64,
        authorization={},
        generation=0,
        inference_dispatches=0,
        accepted_tasks=0,
    )
    segment_round = ActiveLearningRound(
        id=uuid.uuid4(),
        project_id=project.id,
        creator_id=owner.id,
        model_version_id=version.id,
        deployment_id=deployment.id,
        request_id=uuid.uuid4(),
        request_digest="6" * 64,
        name="segment-round",
        status="queued",
        spec={"model_ref": {"task": "image-instance-segmentation"}},
        spec_digest="7" * 64,
        authorization={},
        generation=0,
        inference_dispatches=0,
        accepted_tasks=0,
    )
    db_session.add_all([obb_round, detect_default_round, segment_round])
    await db_session.flush()

    for query_task, expected_ids in (
        ("obb", [obb_round.id]),
        ("detect", [detect_default_round.id]),
        ("segment", [segment_round.id]),
        (None, [obb_round.id, detect_default_round.id, segment_round.id]),
    ):
        count = await db_session.scalar(
            text("SELECT yoao_learning_v1.selection_round_count(:p, :t)"),
            {"p": project.id, "t": query_task},
        )
        rows = (
            (
                await db_session.execute(
                    text(
                        "SELECT id FROM yoao_learning_v1.selection_rounds(:p, :t, 10, 0)"
                    ),
                    {"p": project.id, "t": query_task},
                )
            )
            .scalars()
            .all()
        )
        assert count == len(expected_ids)
        assert set(rows) == set(expected_ids)


def test_capability_driven_managed_evaluation_gate():
    from pydantic import ValidationError

    from app.schemas.training_preview import (
        TrainingPreviewCreate,
        TrainingPreviewRequestOut,
    )

    assert training_adapters.supports_managed_evaluation("ultralytics") is True
    assert training_adapters.supports_managed_evaluation("libreyolo") is False
    compat_entry = {"framework": "custom_trainer", "format": None}
    backend_capable = SimpleNamespace(
        health_meta={
            "capabilities": {
                "managed_evaluation": True,
                "managed_model_compatibility": [compat_entry],
            }
        }
    )
    backend_incapable = SimpleNamespace(
        health_meta={"capabilities": {"managed_evaluation": False}}
    )
    backend_string_false = SimpleNamespace(
        health_meta={
            "capabilities": {
                "managed_evaluation": "false",
                "managed_model_compatibility": [compat_entry],
            }
        }
    )
    backend_malformed_caps = SimpleNamespace(health_meta={"capabilities": ["detect"]})
    backend_malformed_health = SimpleNamespace(health_meta=["bad"])
    plugin_self_reported_only = SimpleNamespace(
        extra_params={"plugin": {"manifest": {"managed_model_compatibility": []}}},
        health_meta={
            "capabilities": {
                "managed_evaluation": True,
                "managed_model_compatibility": [compat_entry],
            }
        },
    )
    plugin_without_eval_capability = SimpleNamespace(
        extra_params={
            "plugin": {
                "manifest": {
                    "capabilities": ["inference", "managed_models"],
                    "managed_model_compatibility": [compat_entry],
                }
            }
        },
        health_meta={
            "capabilities": {
                "managed_evaluation": True,
                "managed_model_compatibility": [compat_entry],
            }
        },
    )
    plugin_approved_and_observed = SimpleNamespace(
        extra_params={
            "plugin": {
                "manifest": {
                    "capabilities": [
                        "inference",
                        "managed_models",
                        "managed_evaluation",
                    ],
                    "managed_model_compatibility": [compat_entry],
                }
            }
        },
        health_meta={
            "capabilities": {
                "managed_evaluation": True,
                "managed_model_compatibility": [compat_entry],
            }
        },
    )
    assert (
        training_adapters.supports_managed_evaluation(
            "custom_trainer", backend=backend_capable
        )
        is True
    )
    assert (
        training_adapters.supports_managed_evaluation(
            "custom_trainer", backend=backend_incapable
        )
        is False
    )
    assert (
        training_adapters.supports_managed_evaluation(
            "custom_trainer", backend=backend_string_false
        )
        is False
    )
    assert (
        training_adapters.supports_managed_evaluation(
            "custom_trainer", backend=backend_malformed_caps
        )
        is False
    )
    assert (
        training_adapters.supports_managed_evaluation(
            "custom_trainer", backend=backend_malformed_health
        )
        is False
    )
    assert (
        training_adapters.supports_managed_evaluation(
            "custom_trainer", backend=plugin_self_reported_only
        )
        is False
    )
    assert (
        training_adapters.supports_managed_evaluation(
            "custom_trainer", backend=plugin_without_eval_capability
        )
        is False
    )
    assert (
        training_adapters.supports_managed_evaluation(
            "custom_trainer", backend=plugin_approved_and_observed
        )
        is True
    )

    preview_payload = {
        "dataset_id": str(uuid.uuid4()),
        "framework": "custom_trainer",
        "base_model": "custom_det_n",
        "configuration": {
            "schema_id": "custom/v1",
            "schema_digest": "a" * 64,
            "values": {},
        },
        "request_id": str(uuid.uuid4()),
    }
    assert (
        TrainingPreviewRequestOut.model_validate(preview_payload).framework
        == "custom_trainer"
    )
    with pytest.raises(ValidationError):
        TrainingPreviewCreate.model_validate(preview_payload)


async def test_public_router_port_reports_registry_routability(db_session):
    from app.services.inference_topology_data import InferenceTopologyData
    from app.services.ml_routing.contracts import RouterMode
    from app.services.ml_routing.router import MLBackendRouter, router_mode

    assert router_mode() in {RouterMode.OFF, RouterMode.OBSERVE, RouterMode.ENFORCE}
    backend, pool = await create_registry_with_pool(
        db_session,
        name="t1-router-port-" + uuid.uuid4().hex[:8],
        state="connected",
        enabled_pool=True,
        auth_method="token",
        auth_token="a" * 40,
        last_checked_at=trained_models.now(),
        health_meta={"capabilities": {}, "compute": {"effective_device": "cpu"}},
    )
    pool_facts = await InferenceTopologyData(db_session).pool_for_registry(backend.id)
    assert pool_facts is not None
    router = MLBackendRouter(db_session, mode=RouterMode.ENFORCE)
    assert await router.is_registry_routable(pool_facts, backend.id) is True
    assert await router.is_registry_routable(pool_facts, uuid.uuid4()) is False
