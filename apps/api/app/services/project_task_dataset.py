"""Allocate and summarize durable project task-version streams."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import JSONB
from yoao_task_contract import VISUAL_TASK_VALUES

from app.services import training_dataset_data as data

STATUSES = ("pending", "running", "ready", "failed", "cancelled")
TASK_TO_TOOL_UNITS = {
    "detect": ("bbox",),
    "segment": ("region",),
    "pose": ("keypoint",),
    "obb": ("rotated_bbox",),
}


def recognized_task_expression(snapshot):
    return func.yoao_learning_v1.training_dataset_task(snapshot)


def contract_profile_expression(snapshot):
    return func.yoao_learning_v1.training_dataset_profile(
        snapshot, recognized_task_expression(snapshot), type_=JSONB
    )


async def lock_project(db, project_id):
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project_id), 0)))
    )


async def ensure_membership(db, version):
    """Allocate once in the same transaction as creation or idempotent replay."""
    if db.autoflush:
        await db.flush()
    return await data.ensure_membership(db, version.project_id, version.id)


def task_capabilities(
    project, task, *, has_ready_version, has_analyzable_ready, has_training_ready
):
    bindings = project.tool_bindings or {}
    tool_enabled = any(
        isinstance(bindings.get(tool), dict) and bindings[tool].get("enabled")
        for tool in TASK_TO_TOOL_UNITS.get(task, ())
    )
    qualified = task in {"detect", "segment"}
    create_supported = qualified and project.data_type == "image" and tool_enabled
    create_reason = (
        None
        if create_supported
        else (
            "项目未启用对应图片标注工具"
            if qualified
            else "此任务的冻结、桥接和训练路径尚未通过资格验证"
        )
    )
    read_supported = qualified and has_analyzable_ready
    read_reason = (
        None
        if read_supported
        else (
            "此任务尚无合格分析适配器"
            if not qualified
            else "该输出媒体尚无经过资格验证的分析适配器"
            if has_ready_version
            else "尚无已就绪的合格冻结版本"
        )
    )
    training_supported = qualified and has_training_ready
    training_reason = (
        None
        if training_supported
        else (
            "当前仅资格验证图片来源物化；该来源媒体尚未通过训练资格验证"
            if read_supported
            else read_reason
        )
    )
    return {
        "create_version": {
            "supported": bool(create_supported),
            "reason": create_reason,
        },
        "qualified_training": {
            "supported": training_supported,
            "reason": training_reason,
        },
        **{
            name: {"supported": read_supported, "reason": read_reason}
            for name in (
                "overview_statistics",
                "task_statistics",
                "sample_preview",
                "semantic_comparison",
            )
        },
    }


async def list_task_datasets(db, project):
    version = data.dataset_lineage_row.c
    task = version.recognized_task
    analyzable = version.contract_profile["media_type"].astext == "image"
    trainable = analyzable & (
        version.contract_profile["source_media_type"].astext == "image"
    )
    counts = (
        await db.execute(
            select(
                task,
                version.status,
                func.count(),
                func.count().filter(version.task_dataset_id.is_(None)),
                func.count().filter(analyzable),
                func.count().filter(trainable),
            )
            .where(version.project_id == project.id)
            .group_by(task, version.status)
        )
    ).all()
    stream_by_task = {
        stream.task: stream.id
        for stream in await db.execute(select(data.stream_rows(project.id)))
    }
    summaries = {}
    pending = {}
    unrecognized = 0
    ready_qualifications = {}
    for (
        canonical,
        status,
        count,
        pending_count,
        analyzable_count,
        trainable_count,
    ) in counts:
        if canonical is None:
            unrecognized += count
            continue
        summaries.setdefault(canonical, {state: 0 for state in STATUSES})[status] = (
            count
        )
        pending[canonical] = pending.get(canonical, 0) + pending_count
        if status == "ready":
            ready_qualifications[canonical] = (analyzable_count, trainable_count)
    bindings = project.tool_bindings or {}
    choices = set(summaries) | set(stream_by_task)
    for task_name, tools in TASK_TO_TOOL_UNITS.items():
        if any(
            isinstance(bindings.get(tool), dict) and bindings[tool].get("enabled")
            for tool in tools
        ):
            choices.add(task_name)
    items = []
    for task_name in VISUAL_TASK_VALUES:
        if task_name not in choices:
            continue
        latest = (
            (
                await db.execute(
                    data.version_summaries()
                    .where(
                        version.project_id == project.id,
                        task == task_name,
                        version.status == "ready",
                    )
                    .order_by(
                        version.ordinal.desc().nullslast(),
                        version.created_at.desc(),
                        version.id.desc(),
                    )
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        states = summaries.get(task_name, {state: 0 for state in STATUSES})
        analyzable_ready, training_ready = ready_qualifications.get(task_name, (0, 0))
        items.append(
            {
                "id": stream_by_task.get(task_name),
                "project_id": project.id,
                "task": task_name,
                "status_counts": states,
                "version_count": sum(states.values()),
                "pending_membership_count": pending.get(task_name, 0),
                "latest_ready_version": latest,
                "capabilities": task_capabilities(
                    project,
                    task_name,
                    has_ready_version=bool(states["ready"]),
                    has_analyzable_ready=bool(analyzable_ready),
                    has_training_ready=bool(training_ready),
                ),
            }
        )
    return {
        "items": items,
        "pending_membership_count": sum(pending.values()),
        "unrecognized_version_count": unrecognized,
    }
