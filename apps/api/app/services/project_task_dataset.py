"""Recognize frozen task identities and allocate durable management metadata.

SQL recognition deliberately mirrors training_contract.dataset.data_contract.
The legacy model's forgiving display projection must never drive association.
"""

from __future__ import annotations

from aap_task_contract import VISUAL_TASK_VALUES, normalize_task
from sqlalchemy import and_, case, func, literal, not_, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import defer
from training_contract.dataset import (
    DETECTION_CONTRACT,
    LEGACY_DETECTION,
    LEGACY_SEGMENTATION_CONTRACT,
    SEGMENTATION_CONTRACT,
    INSTANCE_RLE_CONTRACT,
    DatasetContractError,
    data_contract,
)

from app.db.models.project_task_dataset import (
    ProjectTaskDataset,
    TaskDatasetVersionMembership,
)
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services.training_dataset_bridge import TrainingDatasetError

STATUSES = ("pending", "running", "ready", "failed", "cancelled")
TASK_TO_TOOL_UNITS = {
    "detect": ("bbox",),
    "segment": ("region",),
    "pose": ("keypoint",),
    "obb": ("rotated_bbox",),
}


def _json_value(record, *path):
    # Function syntax also works with bound JSONB fixtures on PostgreSQL: an
    # unparenthesized "$1::JSONB[...]" subscript is invalid SQL.
    return func.jsonb_extract_path(record, *path, type_=JSONB)


def _json_text(record, *path):
    return func.jsonb_extract_path_text(record, *path)


def _media_field(record):
    """Legacy omission is image; an explicit null/invalid value is unknown."""
    return case(
        (not_(record.has_key("media_type")), "image"),
        (
            func.jsonb_typeof(_json_value(record, "media_type")) == "string",
            _json_text(record, "media_type"),
        ),
        else_=None,
    )


def output_media_expression(snapshot=None):
    snapshot = TrainingDatasetVersion.snapshot if snapshot is None else snapshot
    return _media_field(snapshot)


def source_media_expression(snapshot=None):
    snapshot = TrainingDatasetVersion.snapshot if snapshot is None else snapshot
    source = _json_value(snapshot, "source")
    # The existing adapter treats an absent/null source as the historical
    # omitted image source. Explicit source.media_type=null stays unknown.
    return case(
        (or_(source.is_(None), func.jsonb_typeof(source) == "null"), "image"),
        (func.jsonb_typeof(source) == "object", _media_field(source)),
        else_=None,
    )


def recognized_task_expression(snapshot=None):
    """Return NULL for any contract the strict Python resolver rejects."""
    snapshot = TrainingDatasetVersion.snapshot if snapshot is None else snapshot

    def schema_is(number):
        # JSON string/bool/float schemas are not Python int schemas.
        return and_(
            func.jsonb_typeof(_json_value(snapshot, "schema")) == "number",
            _json_text(snapshot, "schema") == str(number),
        )

    def absent_or_equal(key, value):
        return or_(not_(snapshot.has_key(key)), _json_text(snapshot, key) == value)

    legacy_detection = and_(
        schema_is(1),
        absent_or_equal("task", LEGACY_DETECTION),
        not_(snapshot.has_key("geometry")),
        not_(snapshot.has_key("bridge")),
        absent_or_equal("format", "ultralytics-detection"),
    )
    legacy_segmentation = and_(
        schema_is(2),
        *(
            _json_text(snapshot, key) == value
            for key, value in LEGACY_SEGMENTATION_CONTRACT.items()
            if key != "schema"
        ),
        absent_or_equal("format", "ultralytics-segmentation"),
    )

    def modern(contract):
        return and_(
            schema_is(contract["schema"]),
            *(
                _json_text(snapshot, key) == value
                for key, value in contract.items()
                if key != "schema"
            ),
        )

    # SQL NULL cannot leak into diagnostic NOT predicates.
    return case(
        (func.coalesce(legacy_detection, False), "detect"),
        (func.coalesce(legacy_segmentation, False), "segment"),
        (func.coalesce(modern(DETECTION_CONTRACT), False), "detect"),
        (func.coalesce(modern(SEGMENTATION_CONTRACT), False), "segment"),
        (func.coalesce(modern(INSTANCE_RLE_CONTRACT), False), "segment"),
        else_=None,
    )


def contract_profile_expression(snapshot=None):
    """Small projection only; list reads never deserialize full frozen bodies."""
    snapshot = TrainingDatasetVersion.snapshot if snapshot is None else snapshot
    task = recognized_task_expression(snapshot)
    return case(
        (
            task.is_not(None),
            func.jsonb_build_object(
                "schema",
                _json_value(snapshot, "schema"),
                "task",
                task,
                "media_type",
                output_media_expression(snapshot),
                "source_media_type",
                source_media_expression(snapshot),
                "format",
                case(
                    (
                        _json_text(snapshot, "bridge")
                        == INSTANCE_RLE_CONTRACT["bridge"],
                        INSTANCE_RLE_CONTRACT["format"],
                    ),
                    else_="ultralytics-yolo",
                ),
                "geometry",
                case(
                    (
                        _json_text(snapshot, "bridge")
                        == INSTANCE_RLE_CONTRACT["bridge"],
                        INSTANCE_RLE_CONTRACT["geometry"],
                    ),
                    (task == "segment", SEGMENTATION_CONTRACT["geometry"]),
                    else_=DETECTION_CONTRACT["geometry"],
                ),
                "bridge",
                case(
                    (
                        _json_text(snapshot, "bridge")
                        == INSTANCE_RLE_CONTRACT["bridge"],
                        INSTANCE_RLE_CONTRACT["bridge"],
                    ),
                    (task == "segment", SEGMENTATION_CONTRACT["bridge"]),
                    else_=DETECTION_CONTRACT["bridge"],
                ),
            ),
        ),
        else_=literal(None, type_=JSONB),
    ).label("contract_profile")


def version_metadata_statement():
    return select(
        TrainingDatasetVersion,
        TaskDatasetVersionMembership.task_dataset_id,
        TaskDatasetVersionMembership.ordinal,
        contract_profile_expression(),
    ).outerjoin(
        TaskDatasetVersionMembership,
        TaskDatasetVersionMembership.version_id == TrainingDatasetVersion.id,
    )


def summary_metadata(row):
    version, stream_id, ordinal, profile = row
    version.task_dataset_id = stream_id
    version.version_ordinal = ordinal
    version.contract_profile = profile
    return version


async def attach_read_metadata(db, version):
    row = (
        await db.execute(
            version_metadata_statement().where(TrainingDatasetVersion.id == version.id)
        )
    ).one()
    return summary_metadata(row)


async def lock_project(db, project_id):
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project_id), 0)))
    )


async def ensure_membership(db, version, *, project_locked=False):
    """Associate once, atomically with the caller's creation/replay transaction."""
    if not project_locked:
        await lock_project(db, version.project_id)
    try:
        task = normalize_task(data_contract(version.snapshot)["task"])
    except (DatasetContractError, TypeError, AttributeError) as exc:
        raise TrainingDatasetError(
            "unknown_data_contract",
            "冻结数据契约未被识别，不能分配任务版本序号",
            status_code=422,
        ) from exc
    membership = await db.get(
        TaskDatasetVersionMembership, version.id, populate_existing=True
    )
    if membership:
        stream = await db.get(ProjectTaskDataset, membership.task_dataset_id)
        if stream.project_id != version.project_id or stream.task != task:
            raise TrainingDatasetError(
                "task_dataset_membership_mismatch",
                "任务版本关联与冻结数据身份不一致",
                status_code=409,
            )
        return membership
    stream = await db.scalar(
        select(ProjectTaskDataset)
        .where(
            ProjectTaskDataset.project_id == version.project_id,
            ProjectTaskDataset.task == task,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if stream is None:
        stream = ProjectTaskDataset(project_id=version.project_id, task=task)
        db.add(stream)
        await db.flush()
    max_ordinal = await db.scalar(
        select(func.max(TaskDatasetVersionMembership.ordinal)).where(
            TaskDatasetVersionMembership.task_dataset_id == stream.id
        )
    )
    if stream.next_ordinal <= (max_ordinal or 0):
        raise TrainingDatasetError(
            "task_dataset_high_water_invalid",
            "任务数据集序号高水位无效，请先核查管理元数据",
            status_code=409,
        )
    ordinal = stream.next_ordinal
    stream.next_ordinal += 1
    membership = TaskDatasetVersionMembership(
        version_id=version.id, task_dataset_id=stream.id, ordinal=ordinal
    )
    db.add(membership)
    await db.flush()
    return membership


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
    task = recognized_task_expression()
    analyzable = output_media_expression() == "image"
    trainable = and_(analyzable, source_media_expression() == "image")
    counts = (
        await db.execute(
            select(
                task.label("task"),
                TrainingDatasetVersion.status,
                func.count().label("count"),
                func.count()
                .filter(TaskDatasetVersionMembership.version_id.is_(None))
                .label("pending"),
                func.count().filter(analyzable).label("analyzable"),
                func.count().filter(trainable).label("trainable"),
            )
            .outerjoin(
                TaskDatasetVersionMembership,
                TaskDatasetVersionMembership.version_id == TrainingDatasetVersion.id,
            )
            .where(TrainingDatasetVersion.project_id == project.id)
            .group_by(task, TrainingDatasetVersion.status)
        )
    ).all()
    streams = list(
        await db.scalars(
            select(ProjectTaskDataset).where(
                ProjectTaskDataset.project_id == project.id
            )
        )
    )
    stream_by_task = {stream.task: stream for stream in streams}
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
        # Issued sequence is authoritative once associated. Pending old-writer
        # versions still remain visible in the separately filtered version list.
        row = (
            await db.execute(
                version_metadata_statement()
                .options(
                    defer(TrainingDatasetVersion.snapshot),
                    defer(TrainingDatasetVersion.manifest),
                    defer(TrainingDatasetVersion.preparation_report),
                )
                .where(
                    TrainingDatasetVersion.project_id == project.id,
                    task == task_name,
                    TrainingDatasetVersion.status == "ready",
                )
                .order_by(
                    TaskDatasetVersionMembership.ordinal.desc().nullslast(),
                    TrainingDatasetVersion.created_at.desc(),
                    TrainingDatasetVersion.id.desc(),
                )
                .limit(1)
            )
        ).one_or_none()
        states = summaries.get(task_name, {state: 0 for state in STATUSES})
        analyzable_ready, training_ready = ready_qualifications.get(task_name, (0, 0))
        items.append(
            {
                "id": stream_by_task[task_name].id
                if task_name in stream_by_task
                else None,
                "project_id": project.id,
                "task": task_name,
                "status_counts": states,
                "version_count": sum(states.values()),
                "pending_membership_count": pending.get(task_name, 0),
                "latest_ready_version": summary_metadata(row) if row else None,
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
