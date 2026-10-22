"""Cold-API Workspace proof for point-cloud quality promotion."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import uuid
from collections.abc import Mapping

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from aap_workspace.point_quality_configuration_values import (
    PointCloudQualityConfig,
    PointCloudQualityGovernanceConfig,
)
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset, Scene
from app.db.models.point_cloud_quality import (
    PointCloudQualityEvaluation,
    PointCloudQualityIssue,
)
from app.db.models.project import Project
from app.db.models.user import User
from tests.factory import create_project
from tests.test_asset_storage_cleanup_runtime import _verify_postgres


def _config(*, minimum_points: int) -> PointCloudQualityConfig:
    config = PointCloudQualityConfig(
        governance=PointCloudQualityGovernanceConfig(
            minimum_reviewed_per_rule=1,
            maximum_false_positive_rate=0.1,
            minimum_confirmed_retention=0.9,
        )
    )
    config.thresholds.minimum_points = minimum_points
    return config


def _promotion_audits(evaluation_id: uuid.UUID):
    return select(AuditLog).where(
        AuditLog.action == "point_cloud_quality.evaluation_promote",
        AuditLog.target_type == "point_cloud_quality_evaluation",
        AuditLog.target_id == str(evaluation_id),
    )


async def qualify_point_quality_promotion(
    test_engine: AsyncEngine,
    maker: async_sessionmaker,
    client: httpx.AsyncClient,
    *,
    actor: User,
    headers: Mapping[str, str],
) -> dict[str, object]:
    """Prove the ordinary API login commits config, evaluation, and Audit together."""
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        raise RuntimeError("point-quality assembly requires the owned verifier fixture")
    _verify_postgres(test_engine.url)
    if not test_engine.url.database or not test_engine.url.database.endswith("_test"):
        raise RuntimeError("point-quality assembly requires an owned test database")
    if getattr(actor.role, "value", actor.role) != "project_admin":
        raise AssertionError(
            "point-quality actor must own the project as project_admin"
        )

    baseline = _config(minimum_points=5).model_dump(mode="json")
    candidate = _config(minimum_points=1).model_dump(mode="json")
    suffix = uuid.uuid4().hex
    async with maker.begin() as db:
        project = await create_project(db, owner_id=actor.id, type_key="lidar")
        project.data_type = "lidar"
        project.point_cloud_quality_config = baseline
        dataset = Dataset(
            display_id=f"DS-QP-{suffix[:10]}",
            name="Workspace quality promotion fixture",
            data_type="point_cloud",
            created_by=actor.id,
        )
        db.add(dataset)
        await db.flush()
        scene = Scene(
            display_id=f"SCN-QP-{suffix[:10]}",
            dataset_id=dataset.id,
            name="Workspace quality promotion fixture",
        )
        db.add(scene)
        await db.flush()
        reviewed_at = datetime.now(timezone.utc)
        for index, (point_count, verdict, status) in enumerate(
            ((0, "confirmed", "resolved"), (4, "false_positive", "wont_fix"))
        ):
            db.add(
                PointCloudQualityIssue(
                    project_id=project.id,
                    scene_id=scene.id,
                    related_annotation_ids=[],
                    source_versions={},
                    class_name="car",
                    code="low_point_count",
                    rule_version=1,
                    severity="warning",
                    severity_rank=1,
                    status=status,
                    frame_start=index,
                    frame_end=index,
                    metric={"point_count": point_count},
                    threshold={"minimum_points": 5},
                    evidence={},
                    locator={"scene_id": str(scene.id), "frame_index": index},
                    dedupe_key=f"{index + 1:064x}",
                    review_verdict=verdict,
                    reviewed_by_id=actor.id,
                    reviewed_at=reviewed_at,
                )
            )
        project_id = project.id

    path = f"/api/v1/projects/{project_id}/point-cloud-quality/evaluations"
    created = await client.post(
        path,
        headers=headers,
        json={"candidate_config": candidate},
    )
    assert created.status_code == 201, created.text
    created_body = created.json()
    assert created_body["sample_count"] == 2
    assert created_body["gate_status"] == "promote"
    evaluation_id = uuid.UUID(created_body["id"])

    stale_created = await client.post(
        path,
        headers=headers,
        json={"candidate_config": candidate},
    )
    assert stale_created.status_code == 201, stale_created.text
    stale_evaluation_id = uuid.UUID(stale_created.json()["id"])

    trigger_suffix = uuid.uuid4().hex[:12]
    trigger_name = f"test_qp_abort_{trigger_suffix}"
    function_name = f"test_qp_abort_{trigger_suffix}"
    try:
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    f"""CREATE FUNCTION public.{function_name}() RETURNS trigger
                    LANGUAGE plpgsql AS $function$
                    BEGIN
                        IF NEW.action = 'point_cloud_quality.evaluation_promote'
                           AND NEW.target_type = 'point_cloud_quality_evaluation'
                           AND NEW.target_id = '{evaluation_id}' THEN
                            RAISE EXCEPTION 'test-only Audit rejection';
                        END IF;
                        RETURN NEW;
                    END;
                    $function$"""
                )
            )
            await db.execute(
                text(
                    f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON public.audit_logs "
                    f"FOR EACH ROW EXECUTE FUNCTION public.{function_name}()"
                )
            )
        rejected = await client.post(
            f"{path}/{evaluation_id}/promote",
            headers=headers,
        )
        assert rejected.status_code == 500, rejected.text
        async with maker() as db:
            project_after_rejection = await db.get(Project, project_id)
            evaluation_after_rejection = await db.get(
                PointCloudQualityEvaluation, evaluation_id
            )
            assert project_after_rejection is not None
            assert project_after_rejection.point_cloud_quality_config == baseline
            assert evaluation_after_rejection is not None
            assert evaluation_after_rejection.promoted_by_id is None
            assert evaluation_after_rejection.promoted_at is None
            assert evaluation_after_rejection.promoted_config_revision is None
            assert (await db.scalars(_promotion_audits(evaluation_id))).all() == []
    finally:
        async with test_engine.begin() as db:
            await db.execute(
                text(f"DROP TRIGGER IF EXISTS {trigger_name} ON public.audit_logs")
            )
            await db.execute(text(f"DROP FUNCTION IF EXISTS public.{function_name}()"))

    promoted = await client.post(
        f"{path}/{evaluation_id}/promote",
        headers=headers,
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["promoted_config_revision"] == 2

    async with maker() as db:
        promoted_project = await db.get(Project, project_id)
        evaluation = await db.get(PointCloudQualityEvaluation, evaluation_id)
        stale_evaluation = await db.get(
            PointCloudQualityEvaluation, stale_evaluation_id
        )
        audits = list((await db.scalars(_promotion_audits(evaluation_id))).all())
        assert promoted_project is not None
        assert promoted_project.point_cloud_quality_config["config_revision"] == 2
        assert (
            promoted_project.point_cloud_quality_config["thresholds"]["minimum_points"]
            == 1
        )
        assert evaluation is not None
        assert evaluation.promoted_by_id == actor.id
        assert evaluation.promoted_at is not None
        assert evaluation.promoted_config_revision == 2
        assert stale_evaluation is not None
        assert stale_evaluation.promoted_by_id is None
        assert stale_evaluation.promoted_at is None
        assert stale_evaluation.promoted_config_revision is None
        assert len(audits) == 1
        audit = audits[0]
        assert audit.actor_id == actor.id
        assert audit.status_code == 200
        assert audit.detail_json == {
            "project_id": str(project_id),
            "from_revision": 1,
            "to_revision": 2,
            "candidate_config_digest": evaluation.candidate_config_digest,
        }

    stale = await client.post(
        f"{path}/{stale_evaluation_id}/promote",
        headers=headers,
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["reason"] == (
        "point_cloud_quality_evaluation_baseline_changed"
    )
    async with maker() as db:
        promoted_project = await db.get(Project, project_id)
        evaluation = await db.get(PointCloudQualityEvaluation, evaluation_id)
        stale_evaluation = await db.get(
            PointCloudQualityEvaluation, stale_evaluation_id
        )
        audits = list((await db.scalars(_promotion_audits(evaluation_id))).all())
        stale_audits = list(
            (await db.scalars(_promotion_audits(stale_evaluation_id))).all()
        )
        assert promoted_project is not None
        assert promoted_project.point_cloud_quality_config["config_revision"] == 2
        assert evaluation is not None and evaluation.promoted_config_revision == 2
        assert stale_evaluation is not None
        assert stale_evaluation.promoted_by_id is None
        assert stale_evaluation.promoted_at is None
        assert stale_evaluation.promoted_config_revision is None
        assert len(audits) == 1
        assert stale_audits == []

    return {
        "project_id": str(project_id),
        "evaluation_id": str(evaluation_id),
        "stale_evaluation_id": str(stale_evaluation_id),
        "audit_id": audit.id,
        "actor_role": getattr(actor.role, "value", actor.role),
        "api_database_role": "identity_api_consumer",
        "audit_failure_status": rejected.status_code,
        "rollback_verified": True,
        "promotion_status": promoted.status_code,
        "stale_promotion_status": stale.status_code,
    }
