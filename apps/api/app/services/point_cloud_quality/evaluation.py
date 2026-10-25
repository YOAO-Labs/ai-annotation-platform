from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.point_cloud_quality import (
    PointCloudQualityEvaluation,
    PointCloudQualityIssue,
)
from app.db.models.project import Project
from aap_workspace.point_quality_configuration_values import PointCloudQualityConfig
from app.services.point_cloud_quality.config import (
    effective_thresholds,
    load_point_cloud_quality_config,
    point_cloud_quality_config_digest,
)
from app.services.point_cloud_quality.service import (
    PointCloudQualityError,
    refresh_issue_staleness_bulk,
)
from app.services.labeling_spatial_quality import (
    quality_row,
    quality_rows,
    spatial_quality_data,
)


MAX_EVALUATION_SAMPLES = 20_000
UNKNOWN_CLASS = "__unknown__"

RULE_THRESHOLD_FIELDS: dict[str, tuple[str, ...]] = {
    "low_point_count": ("minimum_points",),
    "size_outlier": ("size_mad_z",),
    "ground_clearance": ("ground_penetration_m", "ground_float_m"),
    "temporal_jump": (
        "temporal_center_jump_m",
        "temporal_size_change_ratio",
        "temporal_yaw_jump_rad",
    ),
    "projection_residual": (
        "projection_min_iou",
        "projection_max_edge_residual_ratio",
    ),
}
NON_REPLAYABLE_FIELDS = {"ground_sample_min", "ground_margin_m", "size_min_samples"}


def _triggered(sample: dict[str, Any], config: PointCloudQualityConfig) -> bool:
    code = str(sample["code"])
    if code not in RULE_THRESHOLD_FIELDS:
        return True
    threshold = effective_thresholds(config, sample.get("class_name"))
    metric = sample.get("metric") or {}
    if code == "low_point_count":
        return float(metric.get("point_count", float("inf"))) < threshold.minimum_points
    if code == "size_outlier":
        robust_z = metric.get("robust_z") or []
        return (
            bool(robust_z)
            and max(float(value) for value in robust_z) > threshold.size_mad_z
        )
    if code == "ground_clearance":
        clearance = float(metric.get("clearance_m", 0))
        return (
            clearance < -threshold.ground_penetration_m
            or clearance > threshold.ground_float_m
        )
    if code == "temporal_jump":
        return (
            float(metric.get("center_delta_m_per_frame", 0))
            > threshold.temporal_center_jump_m
            or float(metric.get("size_change_ratio", 0))
            > threshold.temporal_size_change_ratio
            or float(metric.get("yaw_delta_rad_per_frame", 0))
            > threshold.temporal_yaw_jump_rad
        )
    if code == "projection_residual":
        return (
            float(metric.get("iou", 1)) < threshold.projection_min_iou
            or float(metric.get("max_edge_residual_ratio", 0))
            > threshold.projection_max_edge_residual_ratio
        )
    return True


def _metric_summary(
    samples: Iterable[dict[str, Any]], *, candidate: PointCloudQualityConfig | None
) -> dict[str, Any]:
    rows = list(samples)
    triggered = (
        rows
        if candidate is None
        else [row for row in rows if _triggered(row, candidate)]
    )
    confirmed = sum(row["review_verdict"] == "confirmed" for row in triggered)
    false_positive = sum(row["review_verdict"] == "false_positive" for row in triggered)
    accepted_exception = sum(
        row["review_verdict"] == "accepted_exception" for row in triggered
    )
    uncertain = sum(row["review_verdict"] == "uncertain" for row in triggered)
    denominator = confirmed + false_positive
    baseline_confirmed = sum(row["review_verdict"] == "confirmed" for row in rows)
    return {
        "sample_count": len(rows),
        "triggered_count": len(triggered),
        "confirmed": confirmed,
        "false_positive": false_positive,
        "accepted_exception": accepted_exception,
        "uncertain": uncertain,
        "decidable_count": denominator,
        "observed_precision": confirmed / denominator if denominator else None,
        "observed_false_positive_rate": (
            false_positive / denominator if denominator else None
        ),
        "confirmed_retention": (
            confirmed / baseline_confirmed
            if candidate is not None and baseline_confirmed
            else (1.0 if candidate is not None else None)
        ),
    }


def _changed_targets(
    baseline: PointCloudQualityConfig,
    candidate: PointCloudQualityConfig,
    sample_classes: set[str],
) -> list[tuple[str, str | None]]:
    if (
        baseline.enabled != candidate.enabled
        or baseline.enabled_rules != candidate.enabled_rules
        or baseline.severity_overrides != candidate.severity_overrides
    ):
        raise PointCloudQualityError(
            422, "point_cloud_quality_evaluation_non_replayable_config_change"
        )

    all_classes = (
        sample_classes
        | set(baseline.class_thresholds)
        | set(candidate.class_thresholds)
    )
    if not all_classes:
        all_classes.add(UNKNOWN_CLASS)
    baseline_global = baseline.thresholds.model_dump()
    candidate_global = candidate.thresholds.model_dump()
    if any(
        baseline_global[name] != candidate_global[name]
        for name in NON_REPLAYABLE_FIELDS
    ):
        raise PointCloudQualityError(
            422,
            "point_cloud_quality_evaluation_non_replayable_threshold_change",
            fields=sorted(
                name
                for name in NON_REPLAYABLE_FIELDS
                if baseline_global[name] != candidate_global[name]
            ),
        )

    targets: list[tuple[str, str | None]] = []
    for code, fields in RULE_THRESHOLD_FIELDS.items():
        for class_name in sorted(all_classes):
            effective_class = None if class_name == UNKNOWN_CLASS else class_name
            before = effective_thresholds(baseline, effective_class).model_dump()
            after = effective_thresholds(candidate, effective_class).model_dump()
            if any(before[field] != after[field] for field in NON_REPLAYABLE_FIELDS):
                raise PointCloudQualityError(
                    422,
                    "point_cloud_quality_evaluation_non_replayable_threshold_change",
                    class_name=effective_class,
                )
            if any(before[field] != after[field] for field in fields):
                _assert_replay_direction(
                    code, before, after, class_name=effective_class
                )
                targets.append((code, class_name))
    return targets


def _assert_replay_direction(
    code: str,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    class_name: str | None,
) -> None:
    fields = RULE_THRESHOLD_FIELDS[code]
    if code == "low_point_count":
        valid = candidate["minimum_points"] <= baseline["minimum_points"]
    elif code == "projection_residual":
        valid = (
            candidate["projection_min_iou"] <= baseline["projection_min_iou"]
            and candidate["projection_max_edge_residual_ratio"]
            >= baseline["projection_max_edge_residual_ratio"]
        )
    else:
        valid = all(candidate[field] >= baseline[field] for field in fields)
    if not valid:
        raise PointCloudQualityError(
            422,
            "point_cloud_quality_evaluation_requires_fresh_scan",
            code=code,
            class_name=class_name,
        )


def evaluate_snapshot(
    samples: list[dict[str, Any]],
    *,
    baseline: PointCloudQualityConfig,
    candidate: PointCloudQualityConfig,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    sample_classes = {str(row.get("class_name") or UNKNOWN_CLASS) for row in samples}
    targets = _changed_targets(baseline, candidate, sample_classes)
    reasons: list[dict[str, Any]] = []
    if not targets:
        reasons.append({"reason": "candidate_unchanged"})

    by_rule: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        by_rule[str(sample["code"])].append(sample)
        by_class[str(sample.get("class_name") or UNKNOWN_CLASS)].append(sample)

    rules: list[dict[str, Any]] = []
    for code in sorted(by_rule):
        rows = by_rule[code]
        rules.append(
            {
                "code": code,
                "baseline": _metric_summary(rows, candidate=None),
                "candidate": _metric_summary(rows, candidate=candidate),
            }
        )
    classes: list[dict[str, Any]] = []
    for class_name in sorted(by_class):
        rows = by_class[class_name]
        classes.append(
            {
                "class_name": class_name,
                "baseline": _metric_summary(rows, candidate=None),
                "candidate": _metric_summary(rows, candidate=candidate),
            }
        )

    governance = baseline.governance
    insufficient = False
    held = False
    target_results: list[dict[str, Any]] = []
    for code, class_name in targets:
        rows = [
            row
            for row in samples
            if row["code"] == code
            and (
                (class_name == UNKNOWN_CLASS and not row.get("class_name"))
                or row.get("class_name") == class_name
            )
        ]
        baseline_metrics = _metric_summary(rows, candidate=None)
        candidate_metrics = _metric_summary(rows, candidate=candidate)
        target_status = "promote"
        target_reasons: list[str] = []
        if baseline_metrics["decidable_count"] < governance.minimum_reviewed_per_rule:
            insufficient = True
            target_status = "insufficient_data"
            target_reasons.append("minimum_reviewed_not_met")
        else:
            false_positive_rate = candidate_metrics["observed_false_positive_rate"]
            if (
                false_positive_rate is None
                or false_positive_rate > governance.maximum_false_positive_rate
            ):
                held = True
                target_status = "hold"
                target_reasons.append("maximum_false_positive_rate_exceeded")
            if (
                candidate_metrics["confirmed_retention"]
                < governance.minimum_confirmed_retention
            ):
                held = True
                target_status = "hold"
                target_reasons.append("minimum_confirmed_retention_not_met")
        result = {
            "code": code,
            "class_name": class_name,
            "status": target_status,
            "reasons": target_reasons,
            "baseline": baseline_metrics,
            "candidate": candidate_metrics,
        }
        target_results.append(result)
        reasons.extend(
            {"reason": reason, "code": code, "class_name": class_name}
            for reason in target_reasons
        )

    if not targets or held:
        gate_status = "hold"
    elif insufficient:
        gate_status = "insufficient_data"
    else:
        gate_status = "promote"
    summary = {
        "metric_contract": {
            "precision": "observed_review_precision",
            "false_positive_rate": "observed_review_false_positive_rate",
            "retention": "confirmed_issue_retention_proxy_not_recall",
        },
        "sample_count": len(samples),
        "baseline": _metric_summary(samples, candidate=None),
        "candidate": _metric_summary(samples, candidate=candidate),
        "rules": rules,
        "classes": classes,
        "changed_targets": target_results,
    }
    return summary, gate_status, reasons


async def create_evaluation(
    db: AsyncSession,
    *,
    project: Project,
    actor_id: uuid.UUID,
    candidate: PointCloudQualityConfig,
) -> PointCloudQualityEvaluation:
    baseline = load_point_cloud_quality_config(project.point_cloud_quality_config)
    if candidate.config_revision != baseline.config_revision:
        raise PointCloudQualityError(
            409,
            "point_cloud_quality_config_revision_conflict",
            expected=baseline.config_revision,
            actual=candidate.config_revision,
        )
    cutoff = datetime.now(timezone.utc)
    rows_data = await spatial_quality_data(
        db,
        "list_point_evaluation_samples",
        project_id=project.id,
        cutoff_at=cutoff.isoformat(),
        limit=MAX_EVALUATION_SAMPLES + 1,
    )
    rows = quality_rows(PointCloudQualityIssue, rows_data.get("rows"))
    if len(rows) > MAX_EVALUATION_SAMPLES:
        raise PointCloudQualityError(
            422,
            "point_cloud_quality_evaluation_sample_budget_exceeded",
            limit=MAX_EVALUATION_SAMPLES,
        )
    await refresh_issue_staleness_bulk(db, rows)
    rows = [row for row in rows if row.status != "stale"]
    samples = [
        {
            "issue_id": str(row.id),
            "code": row.code,
            "rule_version": row.rule_version,
            "class_name": row.class_name,
            "metric": row.metric,
            "threshold": row.threshold,
            "review_verdict": row.review_verdict,
            "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        }
        for row in rows
    ]
    summary, gate_status, reasons = evaluate_snapshot(
        samples, baseline=baseline, candidate=candidate
    )
    evaluation_data = await spatial_quality_data(
        db,
        "create_point_evaluation",
        new_evaluation_id=uuid.uuid4(),
        project_id=project.id,
        actor_id=actor_id,
        baseline_config_revision=baseline.config_revision,
        baseline_config_digest=point_cloud_quality_config_digest(baseline),
        baseline_config_snapshot=baseline.model_dump(mode="json"),
        candidate_config_digest=point_cloud_quality_config_digest(candidate),
        candidate_config_snapshot=candidate.model_dump(mode="json"),
        cutoff_at=cutoff.isoformat(),
        sample_count=len(samples),
        sample_snapshot=samples,
        summary=summary,
        gate_status=gate_status,
        gate_reasons=reasons,
    )
    await db.flush()
    return quality_row(PointCloudQualityEvaluation, evaluation_data["row"])


async def list_evaluations(
    db: AsyncSession, *, project_id: uuid.UUID, limit: int, offset: int
) -> tuple[list[PointCloudQualityEvaluation], int]:
    data = await spatial_quality_data(
        db,
        "list_point_evaluations",
        project_id=project_id,
        limit=limit,
        offset=offset,
    )
    return quality_rows(PointCloudQualityEvaluation, data.get("rows")), int(
        data.get("total", 0)
    )


def prepare_evaluation_promotion(
    evaluation: PointCloudQualityEvaluation,
    *,
    current_config: dict[str, Any] | None,
) -> PointCloudQualityConfig:
    if evaluation.promoted_at is not None:
        raise PointCloudQualityError(
            409, "point_cloud_quality_evaluation_already_promoted"
        )
    if evaluation.gate_status != "promote":
        raise PointCloudQualityError(
            409,
            "point_cloud_quality_evaluation_gate_not_passed",
            gate_status=evaluation.gate_status,
        )
    current = load_point_cloud_quality_config(current_config)
    if (
        current.config_revision != evaluation.baseline_config_revision
        or point_cloud_quality_config_digest(current)
        != evaluation.baseline_config_digest
    ):
        raise PointCloudQualityError(
            409, "point_cloud_quality_evaluation_baseline_changed"
        )
    return PointCloudQualityConfig.model_validate(
        evaluation.candidate_config_snapshot
    ).model_copy(update={"config_revision": current.config_revision + 1})


def record_evaluation_promotion(
    evaluation: PointCloudQualityEvaluation,
    *,
    actor_id: uuid.UUID,
    config_revision: int,
) -> None:
    evaluation.promoted_by_id = actor_id
    evaluation.promoted_at = datetime.now(timezone.utc)
    evaluation.promoted_config_revision = config_revision
