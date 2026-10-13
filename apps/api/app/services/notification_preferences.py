"""Known notification types and their per-type delivery defaults.

This module is the single backend source of truth for which notification
types users may configure and which of those types transiently alert (toast)
when the account has no explicit popup choice yet. The preferences REST layer
consumes it; the frontend reads effective values from that API instead of
keeping a competing permanent allowlist.
"""

from __future__ import annotations

# 当前已知的可配置 type 列表 — 设置页据此渲染开关。新增类型时同步此表
# 与前端设置页标签（见 docs-site/dev/concepts/audit-and-notifications.md）。
KNOWN_NOTIFICATION_TYPES: tuple[str, ...] = (
    "bug_report.commented",
    "bug_report.reopened",
    "bug_report.status_changed",
    "batch.rejected",
    "batch.review_reopened",
    "batch.admin_locked",
    "batch.admin_unlocked",
    "batch.unarchived",
    "task.approved",
    "task.rejected",
    "task.reopened",
    "failed_prediction.retry.started",
    "failed_prediction.retry.succeeded",
    "failed_prediction.retry.failed",
    "export.ready",
    "export.failed",
    "job.completed",
    "job.failed",
    "job.cancelled",
    "user.deactivation_requested",
    "user.deactivation_completed",
    "feedback.reply_created",
    "feedback.status_changed",
    "feedback.comment_mentioned",
    "annotation.comment_mentioned",
)

# 无显式弹出选择时默认弹出提示（重要消息）的类型。未知/未来类型默认不弹。
DEFAULT_TOAST_TYPES: frozenset[str] = frozenset(
    {
        "task.rejected",
        "batch.rejected",
        "batch.review_reopened",
        "batch.admin_locked",
        "annotation.comment_mentioned",
        "feedback.comment_mentioned",
        "feedback.reply_created",
        "job.failed",
        "export.failed",
    }
)


def default_toast_for(notification_type: str) -> bool:
    """Effective popup default for an account without a stored popup choice."""
    return notification_type in DEFAULT_TOAST_TYPES
