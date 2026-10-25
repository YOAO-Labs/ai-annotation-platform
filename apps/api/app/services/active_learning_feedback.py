"""Append-only observed human outcomes; absent correction/effort data stays unknown."""

from collections import defaultdict
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models.active_learning import (
    ActiveLearningDecision as Decision,
    ActiveLearningFeedback as Feedback,
)
from app.db.models.audit_log import AuditLog
from app.db.models.sample_evaluation import (
    SampleEvaluationAttempt as EvaluationAttempt,
    SampleEvaluationRun as Evaluation,
)
from app.services import active_learning as rounds, active_learning_pool as pool
from app.services import sample_evaluations
from app.services import active_learning_references as source_references
from app.services.project_performance import _union_minutes
from app.services.task_event_ingestion import validate_interval
from app.services.training_dataset_bridge import content_digest
from app.services.training_authority_contract import TrainingAuthorityPort
from app.services.labeling_core_read_relations import (
    lock_active_annotation_rows_for_share,
    lock_feedback_task_rows_for_share,
)
from app.services.labeling_discussion_relations import task_event_row

MAX_FACT_ROWS = 4096


def recorded_effort(events, start, end):
    """Union across tasks for each person/kind, with observed coverage only."""
    intervals = defaultdict(list)
    retained, ignored = [], 0
    for event in events:
        try:
            left, right = validate_interval(
                started_at=event.started_at,
                ended_at=event.ended_at,
                duration_ms=event.duration_ms,
                now=end,
            )
        except HTTPException:
            ignored += 1
            continue
        left, right = max(start, left), min(end, right)
        if (
            event.kind not in {"annotate", "review"}
            or event.collection_source != "session"
            or event.collection_coverage != "qualified"
            or right <= left
        ):
            ignored += 1
            continue
        intervals[(str(event.user_id), event.kind)].append((left, right))
        retained.append(
            {
                "id": str(event.id),
                "task_id": str(event.task_id),
                "user_id": str(event.user_id),
                "kind": event.kind,
                "started_at": left.isoformat(),
                "ended_at": right.isoformat(),
                "collector_version": event.collector_version,
            }
        )
    result = {}
    for kind in ("annotate", "review"):
        groups = [
            values
            for (_user, category), values in intervals.items()
            if category == kind
        ]
        result[kind] = {
            "recorded_ms": round(
                sum(_union_minutes(values) for values in groups) * 60_000
            )
            if groups
            else None,
            "coverage": "partial" if groups else "unknown",
        }
    # Qualified rows establish recorded sessions, never complete lifecycle coverage.
    return {
        **result,
        "qualified_event_count": len(retained),
        "ignored_event_count": ignored,
        "events_digest": content_digest(sorted(retained, key=lambda row: row["id"])),
        "method": "per_person_kind_interval_union",
        "complete_effort": False,
    }


def output(row):
    if content_digest(row.snapshot) != row.snapshot_digest:
        raise rounds.reject("feedback_corrupt", "历史反馈未通过完整性检查")
    return {
        "id": row.id,
        "request_id": row.request_id,
        "snapshot": row.snapshot,
        "snapshot_digest": row.snapshot_digest,
        "created_at": row.created_at,
    }


def _bounded_rows(rows):
    if len(rows) > MAX_FACT_ROWS:
        raise rounds.reject(
            "feedback_limit", "保留的反馈事实超过本轮快照上限，不能静默截断"
        )
    return list(rows)


async def bounded(db, query):
    rows = (await db.scalars(query.limit(MAX_FACT_ROWS + 1))).all()
    return _bounded_rows(rows)


async def evaluation_facts(db, project, round, references):
    facts = []
    for ref in references:
        run = await db.scalar(
            select(Evaluation)
            .where(
                Evaluation.id == ref.evaluation_id, Evaluation.project_id == project.id
            )
            .with_for_update(read=True)
        )
        if not run or run.deletion_requested_at:
            raise rounds.reject("evaluation_missing", "后续评估不存在或正在删除", 404)
        attempt = await db.get(EvaluationAttempt, ref.attempt_id)
        if not attempt or attempt.run_id != run.id:
            raise rounds.reject("attempt_missing", "后续评估执行尝试不存在", 404)
        report = await sample_evaluations.read_report(db, run, attempt)
        facts.append(
            {
                "evaluation_id": str(run.id),
                "attempt_id": str(attempt.id),
                "name": run.name,
                "spec_digest": run.spec_digest,
                "report_sha256": attempt.report["sha256"],
                "comparison_digest": run.spec["comparison_digest"],
                "model_ref": run.spec["model_ref"],
                "role": run.spec["role"],
                "status": report["status"],
                "complete": report["complete"],
                "aggregate": report["aggregate"],
            }
        )
        await source_references.retain_references(
            db,
            round,
            [{"kind": "evaluation", "id": str(run.id), "digest": run.spec_digest}],
        )
    reasons = []
    if len(facts) != 2:
        reasons.append("须恰好引用两个已核验的评估才能计算后续变化")
    elif any(not fact["complete"] or fact["aggregate"] is None for fact in facts):
        reasons.append("部分、失败或取消的评估不能用于计算后续变化")
    elif facts[0]["comparison_digest"] != facts[1]["comparison_digest"]:
        reasons.append("冻结基准、类别、用途、阈值或推理环境不同")
    delta = None
    if not reasons:
        delta = {
            key: None
            if facts[0]["aggregate"][key] is None or facts[1]["aggregate"][key] is None
            else facts[1]["aggregate"][key] - facts[0]["aggregate"][key]
            for key in ("tp", "fp", "fn", "precision", "recall", "f1")
        }
    return {
        "references": facts,
        "comparable": not reasons,
        "reasons": reasons,
        "delta_second_minus_first": delta,
        "causal_gain_established": False,
    }


async def snapshot(
    db,
    project,
    user,
    round_id,
    body,
    api_key_id=None,
    *,
    training_authority: TrainingAuthorityPort,
):
    digest = content_digest(body.model_dump(mode="json"))
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project.id), 0)))
    )
    await training_authority.assert_authority(
        project_id=project.id,
        actor_id=user.id,
        api_key_id=api_key_id,
        required_scopes=pool.SCOPES,
    )
    round = await rounds.load_round(db, project.id, round_id, lock=True)
    existing = await db.scalar(
        select(Feedback).where(
            Feedback.round_id == round.id, Feedback.request_id == body.request_id
        )
    )
    if existing:
        if existing.request_digest != digest:
            raise rounds.reject("request_conflict", "同一请求标识不能用于不同反馈快照")
        return existing
    decisions = (
        await db.scalars(
            select(Decision)
            .where(Decision.round_id == round.id, Decision.action == "accept")
            .order_by(Decision.created_at, Decision.id)
        )
    ).all()
    accepted = {}
    for decision in decisions:
        rounds.decision_output(decision)
        for task in decision.snapshot["tasks"]:
            accepted[uuid.UUID(task["task_id"])] = {
                **task,
                "accepted_at": decision.created_at,
            }
    if not accepted:
        raise rounds.reject("feedback_unavailable", "须先接纳至少一个普通人工标注任务")
    tasks = await lock_feedback_task_rows_for_share(db, project.id, list(accepted))
    if len(tasks) != len(accepted):
        raise rounds.reject("feedback_source_missing", "接纳任务来源已缺失")
    cutoff = rounds.now()
    annotations = _bounded_rows(
        await lock_active_annotation_rows_for_share(db, list(accepted))
    )
    events = await bounded(
        db,
        select(task_event_row)
        .where(
            task_event_row.task_id.in_(accepted),
            task_event_row.ended_at <= cutoff,
        )
        .order_by(task_event_row.id),
    )
    audits = await bounded(
        db,
        select(AuditLog)
        .where(
            AuditLog.target_type == "task",
            AuditLog.target_id.in_([str(identity) for identity in accepted]),
            AuditLog.action == "task.reject",
            AuditLog.status_code == 200,
            AuditLog.created_at <= cutoff,
        )
        .order_by(AuditLog.id),
    )
    arms = []
    for arm in ("active", "random_control"):
        members = [task for task in tasks if accepted[task.id]["arm"] == arm]
        ids = {task.id for task in members}
        facts = [
            {
                "task_id": str(task.id),
                "status": task.status,
                "reviewed": task.status == "completed" and task.reviewed_at is not None,
                "reviewed_at": task.reviewed_at.isoformat()
                if task.reviewed_at
                else None,
                "annotation_count": sum(
                    annotation.task_id == task.id for annotation in annotations
                ),
                "annotations": [
                    {
                        "id": str(annotation.id),
                        "source": annotation.source,
                        "class_name": annotation.class_name,
                        "updated_at": annotation.updated_at.isoformat(),
                        "ground_truth": annotation.ground_truth,
                    }
                    for annotation in annotations
                    if annotation.task_id == task.id
                ],
            }
            for task in members
        ]
        rejections = [
            {
                "audit_id": audit.id,
                "task_id": audit.target_id,
                "at": audit.created_at.isoformat(),
                "reason_type": (audit.detail_json or {}).get("reason_type"),
            }
            for audit in audits
            if uuid.UUID(audit.target_id) in ids
            and audit.created_at >= accepted[uuid.UUID(audit.target_id)]["accepted_at"]
        ]
        # Clip each task's events to its own acceptance before unioning across tasks.
        arm_events = [
            event
            for event in events
            if event.task_id in ids
            and event.started_at >= accepted[event.task_id]["accepted_at"]
        ]
        arms.append(
            {
                "arm": arm,
                "accepted_tasks": len(members),
                "planned_tasks": round.spec["batch_size"],
                "reviewed_tasks": sum(fact["reviewed"] for fact in facts),
                "tasks": facts,
                "reviewer_rejection_count": len(rejections),
                "reviewer_rejections": rejections,
                "correction_count": None,
                "correction_coverage": "unknown",
                "effort": recorded_effort(
                    arm_events,
                    min(
                        (accepted[task.id]["accepted_at"] for task in members),
                        default=cutoff,
                    ),
                    cutoff,
                ),
            }
        )
    value = {
        "schema": 1,
        "round_id": str(round.id),
        "spec_digest": round.spec_digest,
        "observed_at": cutoff.isoformat(),
        "arms": arms,
        "evaluations": await evaluation_facts(db, project, round, body.evaluations),
        "efficiency_claim": False,
        "limitations": [
            "仅记录已保留的人工作业事实",
            "纠正数量未完整采集，保持未知",
            "已记录会话不证明完整工时",
            "部分接纳或不同审核量不能证明效率收益",
        ],
    }
    row = Feedback(
        round_id=round.id,
        actor_id=user.id,
        request_id=body.request_id,
        request_digest=digest,
        snapshot=value,
        snapshot_digest=content_digest(value),
    )
    db.add(row)
    await db.flush()
    return row
