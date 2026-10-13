from __future__ import annotations

from enum import Enum
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit_log import AuditLog
from app.db.models.user import User
from app.middleware.request_id import request_id_var


class AuditAction(str, Enum):
    AUTH_LOGIN = "auth.login"
    USER_INVITE = "user.invite"
    USER_REGISTER = "user.register"
    USER_ROLE_CHANGE = "user.role_change"
    USER_DEACTIVATE = "user.deactivate"
    USER_DELETE = "user.delete"
    USER_PROFILE_UPDATE = "user.profile_update"
    USER_PASSWORD_CHANGE = "user.password_change"
    PROJECT_CREATE = "project.create"
    PROJECT_UPDATE = "project.update"
    PROJECT_TRANSFER = "project.transfer"
    PROJECT_DELETE = "project.delete"
    PROJECT_MEMBER_ADD = "project.member_add"
    PROJECT_MEMBER_REMOVE = "project.member_remove"
    PROJECT_MEMBER_ROLE_CHANGE = "project.member_role_change"
    DATASET_CREATE = "dataset.create"
    DATASET_DELETE = "dataset.delete"
    DATASET_LINK = "dataset.link"
    DATASET_UNLINK = "dataset.unlink"
    DATASET_IMPORT = "dataset.import"
    # v0.11.14 · 存储连接器（服务端拉取导入）
    STORAGE_CONNECTION_CREATE = "storage_connection.create"
    STORAGE_CONNECTION_UPDATE = "storage_connection.update"
    STORAGE_CONNECTION_DELETE = "storage_connection.delete"
    STORAGE_CONNECTION_TEST = "storage_connection.test"
    CONNECTOR_ALLOWLIST_UPDATE = "connector.allowlist_update"
    SYSTEM_BOOTSTRAP_ADMIN = "system.bootstrap_admin"
    BUG_REPORT_CREATED = "bug_report.created"
    BUG_REPORT_STATUS_CHANGED = "bug_report.status_changed"
    BUG_COMMENT_CREATED = "bug_comment.created"
    BATCH_CREATED = "batch.created"
    BATCH_STATUS_CHANGED = "batch.status_changed"
    BATCH_REJECTED = "batch.rejected"
    BATCH_RESET_TO_DRAFT = "batch.reset_to_draft"
    BATCH_DELETED = "batch.deleted"
    BATCH_DISTRIBUTE_EVEN = "batch.distribute_even"
    # v0.7.3 · 多选批量操作
    BULK_BATCH_ARCHIVE = "batch.bulk_archive"
    BULK_BATCH_DELETE = "batch.bulk_delete"
    BULK_BATCH_REASSIGN = "batch.bulk_reassign"
    BULK_BATCH_ACTIVATE = "batch.bulk_activate"
    ANNOTATION_ATTRIBUTE_CHANGE = "annotation.attribute_change"
    # v0.7.2 · annotation 编辑历史可追溯
    ANNOTATION_CREATE = "annotation.create"
    ANNOTATION_UPDATE = "annotation.update"
    ANNOTATION_DELETE = "annotation.delete"
    ANNOTATION_MASK_MUTATION = "annotation.mask_mutation"
    ANNOTATION_SLICE = "annotation.slice"
    MASK_REPAIR_DRY_RUN = "mask_repair.dry_run"
    MASK_REPAIR_EXECUTE = "mask_repair.execute"
    MASK_REPAIR_RESUME = "mask_repair.resume"
    MASK_REPAIR_ROLLBACK = "mask_repair.rollback"
    MASK_FORMAT_IMPORT_PREFLIGHT = "mask_format_import.preflight"
    MASK_FORMAT_IMPORT_EXECUTE = "mask_format_import.execute"
    MASK_FORMAT_IMPORT_RESUME = "mask_format_import.resume"
    ANNOTATION_CONVERT = "annotation.convert"
    AI_MASK_CANDIDATE_ACCEPT = "mask_ai.candidate_accept"
    ANNOTATION_COMMENT_ADD = "annotation.comment_add"
    ANNOTATION_COMMENT_DELETE = "annotation.comment_delete"
    # v0.6.5 · 任务状态机锁定
    TASK_SUBMIT = "task.submit"
    TASK_WITHDRAW = "task.withdraw"
    TASK_REVIEW_CLAIM = "task.review_claim"
    TASK_APPROVE = "task.approve"
    TASK_REJECT = "task.reject"
    TASK_REOPEN = "task.reopen"
    # v0.8.7 F7 · 任务跳过
    TASK_SKIP = "task.skip"
    # M1 · 标注员接受退回，rejected → in_progress
    TASK_ACCEPT_REJECTION = "task.accept_rejection"
    # M2 · 审核员在 review 态直接微调标注（区别于标注员的 ANNOTATION_UPDATE）
    TASK_REVIEWER_EDIT = "task.reviewer_edit"
    # v0.7.8 · 导出审计 + 会话管理
    PROJECT_EXPORT = "project.export"
    BATCH_EXPORT = "batch.export"
    AUTH_LOGOUT = "auth.logout"
    AUTH_LOGOUT_ALL = "auth.logout_all"
    # v0.8.1 · 系统设置 / 改密 / 注销 / 审计归档
    SYSTEM_SETTINGS_UPDATE = "system.settings_update"
    USER_PASSWORD_ADMIN_RESET = "user.password_admin_reset"
    USER_DEACTIVATION_REQUEST = "user.deactivation_request"
    USER_DEACTIVATION_CANCEL = "user.deactivation_cancel"
    USER_DEACTIVATION_APPROVE = "user.deactivation_approve"
    USER_OFFBOARD = "user.offboard"
    USER_REACTIVATE = "user.reactivate"
    AUDIT_ARCHIVE = "audit.archive"
    USER_EXPORT = "user.export"
    AUDIT_LOG_EXPORT = "audit.export"
    # v0.8.8 · 失败预测 soft-delete
    FAILED_PREDICTION_DISMISSED = "failed_prediction.dismissed"
    FAILED_PREDICTION_RESTORED = "failed_prediction.restored"
    # v0.9.12 BUG B-16 · /ai-pre 已就绪卡片多选批量重激活/删除 prediction
    PREANNOTATE_BULK_CLEAR = "preannotate.bulk_clear"
    # v0.8.8 · WebSocket 鉴权过期重连：grace 期内换发新 token
    AUTH_TOKEN_REFRESH = "auth.token_refresh"
    # v0.9.15 · ADR-0008 批次 admin-lock + bulk approve/reject
    BATCH_ADMIN_LOCK = "batch.admin_lock"
    BATCH_ADMIN_UNLOCK = "batch.admin_unlock"
    BULK_BATCH_APPROVE = "batch.bulk_approve"
    BULK_BATCH_REJECT = "batch.bulk_reject"
    VIDEO_SEGMENT_CLAIM = "video_segment.claim"
    VIDEO_SEGMENT_HEARTBEAT = "video_segment.heartbeat"
    VIDEO_SEGMENT_RELEASE = "video_segment.release"
    VIDEO_TRACKER_JOB_CREATE = "video_tracker_job.create"
    VIDEO_TRACKER_JOB_CANCEL = "video_tracker_job.cancel"
    VIDEO_TRACKER_JOB_ACCEPT = "video_tracker_job.accept"
    VIDEO_TRACKER_JOB_DISCARD = "video_tracker_job.discard"
    VIDEO_TRACKER_JOB_DECISION = "video_tracker_job.decision"
    VIDEO_MASK_KEYFRAME_CORRECT = "video_mask.keyframe_correct"
    VIDEO_MASK_KEYFRAME_OPERATE = "video_mask.keyframe_operate"
    VIDEO_CORRECTION_JOB_CREATE = "video_correction_job.create"
    VIDEO_CORRECTION_JOB_CANCEL = "video_correction_job.cancel"
    VIDEO_CHAPTER_CREATE = "video_chapter.create"
    VIDEO_CHAPTER_UPDATE = "video_chapter.update"
    VIDEO_CHAPTER_DELETE = "video_chapter.delete"
    # v0.10.15 · 外部预测导入 (COCO / AAP JSON)
    PREDICTIONS_IMPORT = "predictions.import"
    # v0.10.57 · 按来源清理 prediction
    PREDICTIONS_PURGE = "predictions.purge"
    # v0.10.54 · 外部标注导入 (AAP JSON annotations[])
    ANNOTATION_IMPORT = "annotation.import"
    # I12 · Object Group 分组 + 批量编辑
    ANNOTATION_GROUP = "annotation.group"
    ANNOTATION_UNGROUP = "annotation.ungroup"
    ANNOTATION_BULK_UPDATE = "annotation.bulk_update"
    # I18 · AnnotationFeedback 统一表
    FEEDBACK_CREATED = "feedback.created"
    FEEDBACK_STATUS_CHANGED = "feedback.status_changed"
    FEEDBACK_DELETED = "feedback.deleted"
    # v0.11.0 · ADR-0027 双写一致性对账 cron 检出漂移
    FEEDBACK_RECONCILE_DRIFT = "feedback.reconcile_drift"
    POINT_CLOUD_QUALITY_ISSUE_STATUS = "point_cloud_quality.issue_status"
    POINT_CLOUD_QUALITY_EVALUATION_CREATE = "point_cloud_quality.evaluation_create"
    POINT_CLOUD_QUALITY_EVALUATION_PROMOTE = "point_cloud_quality.evaluation_promote"
    SENSOR_CALIBRATION_UPDATE = "sensor_calibration.update"


def extract_client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def export_detail(
    *,
    actor: User | None,
    request: Request | None,
    base: dict[str, Any] | None = None,
    filter_criteria: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """v0.8.1 · 数据导出审计 detail 标准化扩展。

    在已有 {format, count/rows} 之上叠加 actor_email / ip / request_id / filter_criteria。
    actor_email / ip 与 audit_logs 的列冗余，但 detail_json 写入便于后续 JSONB 查询 +
    归档 jsonl 离线检索（不依赖外键 join）。
    """
    out: dict[str, Any] = dict(base or {})
    out.setdefault("actor_email", getattr(actor, "email", None))
    out.setdefault("ip", extract_client_ip(request))
    out.setdefault("request_id", request_id_var.get() or None)
    if filter_criteria is not None:
        out["filter_criteria"] = filter_criteria
    return out


def export_metadata_header(
    *,
    actor: User | None,
    fmt: str,
    request: Request | None = None,
) -> str:
    """v0.8.1 · CSV / JSON 导出文件首部注释行（不引入新依赖）。

    用于在 CSV 第一行 / JSON 顶部插入审计可追溯信息：导出人 / 时间戳 / Request ID。
    JSON 走 `_export_meta` 顶层字段；CSV 走 `# ...` 注释行（标准 CSV 解析器会忽略，
    Excel / pandas read_csv(comment='#') 也支持）。
    """
    from datetime import datetime, timezone

    actor_email = getattr(actor, "email", None) or "anonymous"
    rid = request_id_var.get() or ""
    ts = datetime.now(timezone.utc).isoformat()
    if fmt == "json":
        # 调用方拿到字符串后自行包装；这里返回 dict 形式更通用
        return ""
    return f"# Exported by: {actor_email}\n# Exported at: {ts}\n# Request ID: {rid}\n"


class AuditService:
    """业务层显式打点。同事务 flush，由调用方 commit。"""

    @staticmethod
    async def log(
        db: AsyncSession,
        *,
        actor: User | None,
        action: str | AuditAction,
        target_type: str | None = None,
        target_id: str | int | None = None,
        request: Request | None = None,
        status_code: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditLog:
        action_str = action.value if isinstance(action, AuditAction) else str(action)
        rid = request_id_var.get()
        entry = AuditLog(
            actor_id=getattr(actor, "id", None),
            actor_email=getattr(actor, "email", None),
            actor_role=getattr(actor, "role", None),
            action=action_str,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            method=getattr(request, "method", None),
            path=str(request.url.path) if request is not None else None,
            status_code=status_code,
            ip=extract_client_ip(request),
            detail_json=detail,
            request_id=rid or None,
        )
        db.add(entry)
        await db.flush()
        return entry

    @staticmethod
    async def log_many(
        db: AsyncSession,
        *,
        actor: User | None,
        action: str | AuditAction,
        target_type: str | None = None,
        request: Request | None = None,
        status_code: int | None = None,
        items: list[dict[str, Any]],
    ) -> list[AuditLog]:
        """v0.6.3 Q-2：一次 PATCH 写 N 条同 action 的审计行（如 attribute_change），
        共享 actor/request/status_code，仅 target_id + detail 逐条不同；一次 add_all + 一次 flush。"""
        if not items:
            return []
        action_str = action.value if isinstance(action, AuditAction) else str(action)
        rid = request_id_var.get()
        method = getattr(request, "method", None)
        path = str(request.url.path) if request is not None else None
        ip = extract_client_ip(request)
        actor_id = getattr(actor, "id", None)
        actor_email = getattr(actor, "email", None)
        actor_role = getattr(actor, "role", None)
        entries: list[AuditLog] = []
        for it in items:
            entries.append(
                AuditLog(
                    actor_id=actor_id,
                    actor_email=actor_email,
                    actor_role=actor_role,
                    action=action_str,
                    target_type=target_type,
                    target_id=str(it["target_id"])
                    if it.get("target_id") is not None
                    else None,
                    method=method,
                    path=path,
                    status_code=status_code,
                    ip=ip,
                    detail_json=it.get("detail"),
                    request_id=rid or None,
                )
            )
        db.add_all(entries)
        await db.flush()
        return entries
