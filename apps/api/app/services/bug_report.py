from __future__ import annotations

from dataclasses import dataclass
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.bug_report import (
    BugCommentOut,
    BugReportCreate,
    BugReportDetail,
    BugReportList,
    BugReportOut,
    BugReportUpdate,
)


TERMINAL_STATUSES = {"fixed", "wont_fix", "duplicate"}


_CREATE = text(
    "SELECT yoao_diagnostics_v1.create_bug_report(CAST(:actor AS uuid), CAST(:report AS jsonb))"
)
_CONFIRM = text(
    "SELECT yoao_diagnostics_v1.confirm_bug_attachment_binding(CAST(:actor AS uuid), CAST(:report AS uuid))"
)
_PATCH = text(
    "SELECT yoao_diagnostics_v1.patch_bug_report(CAST(:actor AS uuid), CAST(:report AS uuid), CAST(:patch AS jsonb))"
)
_LIST = text(
    "SELECT yoao_diagnostics_v1.list_bug_reports(CAST(:actor AS uuid), CAST(:mine AS boolean), "
    "CAST(:status AS text), CAST(:severity AS text), CAST(:route AS text), "
    "CAST(:limit AS integer), CAST(:offset AS integer))"
)
_DETAIL = text(
    "SELECT yoao_diagnostics_v1.read_bug_report(CAST(:actor AS uuid), CAST(:report AS uuid))"
)
_DELETE = text(
    "SELECT yoao_diagnostics_v1.delete_bug_report(CAST(:actor AS uuid), CAST(:report AS uuid))"
)
_PREPARE_COMMENT = text(
    "SELECT yoao_diagnostics_v1.prepare_bug_comment(CAST(:actor AS uuid), CAST(:report AS uuid))"
)
_COMMENT = text(
    "SELECT yoao_diagnostics_v1.add_bug_comment(CAST(:actor AS uuid), CAST(:report AS uuid), CAST(:body AS text))"
)
_CLUSTER = text(
    "SELECT id, title FROM yoao_diagnostics_v1.bug_cluster_candidates(CAST(:actor AS uuid), CAST(:report AS uuid))"
)
_MARKDOWN_COMMENTS = text(
    "SELECT yoao_diagnostics_v1.bug_report_markdown_comments(CAST(:actor AS uuid), CAST(:reports AS uuid[]))"
)
_DETACH = text(
    "SELECT yoao_diagnostics_v1.detach_orphan_bug_tasks(CAST(:project AS uuid), CAST(:tasks AS uuid[]))"
)


def attachment_error(code: str) -> HTTPException:
    return HTTPException(
        {
            "bug_report_missing": 404,
            "bug_attachment_access_denied": 403,
            "bug_attachment_foreign_owner": 403,
            "bug_attachment_unregistered_key": 400,
            "bug_attachment_expired": 410,
        }.get(code, 409),
        {"code": code},
    )


@dataclass(frozen=True)
class BugCommentContext:
    id: uuid.UUID
    reporter_id: uuid.UUID
    display_id: str
    assigned_to_id: uuid.UUID | None
    title: str
    reopen_count: int
    status: str

    @classmethod
    def from_reply(cls, row):
        return cls(
            uuid.UUID(row["id"]),
            uuid.UUID(row["reporter_id"]),
            row["display_id"],
            uuid.UUID(row["assigned_to_id"]) if row["assigned_to_id"] else None,
            row["title"],
            row["reopen_count"],
            row["status"],
        )


class BugReportService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _run(self, statement, params: dict, *, denied: str = "Access denied"):
        if self.db.autoflush:
            await self.db.flush()
        result = await self.db.scalar(statement, params)
        error = result.get("error")
        if error == "missing":
            raise HTTPException(404, "Bug report not found")
        if error == "authority":
            raise HTTPException(403, denied)
        if error == "moderation":
            raise HTTPException(403, "仅管理员可修改处理状态、分派和处理结果")
        if error is not None:
            raise attachment_error(error)
        return result

    async def detach_task_references(
        self, project_id: uuid.UUID, task_ids: list[uuid.UUID]
    ) -> None:
        await self.db.execute(_DETACH, {"project": project_id, "tasks": task_ids})

    async def create(
        self,
        reporter_id: uuid.UUID,
        data: BugReportCreate,
        *,
        route: str,
    ) -> BugReportOut:
        fields = data.model_dump(mode="json", by_alias=True, exclude_unset=True)
        fields["route"] = route
        result = await self._run(
            _CREATE,
            {"actor": reporter_id, "report": json.dumps(fields, allow_nan=False)},
        )
        report = BugReportOut.model_validate(result["report"])
        from app.services.feedback import FeedbackService

        await FeedbackService(self.db).mirror_bug_report(report)
        return report

    async def confirm_attachments(
        self, reporter_id: uuid.UUID, report_id: uuid.UUID
    ) -> None:
        if not await self.db.scalar(
            _CONFIRM, {"actor": reporter_id, "report": report_id}
        ):
            raise attachment_error("bug_attachment_expired")

    async def update(
        self,
        report_id: uuid.UUID,
        actor_id: uuid.UUID,
        data: BugReportUpdate,
    ) -> tuple[BugReportOut, str]:
        fields = data.model_dump(mode="json", exclude_unset=True)
        result = await self._run(
            _PATCH,
            {
                "actor": actor_id,
                "report": report_id,
                "patch": json.dumps(fields, allow_nan=False),
            },
            denied="只能编辑自己提交的反馈",
        )
        return BugReportOut.model_validate(result["report"]), result["old_status"]

    async def list(
        self,
        actor_id: uuid.UUID,
        *,
        mine: bool = False,
        status: str | None = None,
        severity: str | None = None,
        route: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> BugReportList:
        result = await self._run(
            _LIST,
            {
                "actor": actor_id,
                "mine": mine,
                "status": status,
                "severity": severity,
                "route": route,
                "limit": limit,
                "offset": offset,
            },
            denied="需要角色权限: super_admin或project_admin",
        )
        return BugReportList(items=result["reports"], total=result["total"])

    async def get_with_comments(
        self, report_id: uuid.UUID, actor_id: uuid.UUID
    ) -> BugReportDetail:
        result = await self._run(_DETAIL, {"actor": actor_id, "report": report_id})
        return BugReportDetail(**result["report"], comments=result["comments"])

    async def delete(
        self, report_id: uuid.UUID, actor_id: uuid.UUID
    ) -> tuple[str, list[uuid.UUID]]:
        result = await self._run(
            _DELETE,
            {"actor": actor_id, "report": report_id},
            denied="只能删除自己提交的反馈",
        )
        return result["display_id"], [uuid.UUID(value) for value in result["retired"]]

    async def lock_for_comment(
        self, report_id: uuid.UUID, actor_id: uuid.UUID
    ) -> BugCommentContext:
        result = await self._run(
            _PREPARE_COMMENT,
            {"actor": actor_id, "report": report_id},
            denied="只有反馈提交者或管理员可以评论",
        )
        return BugCommentContext.from_reply(result)

    async def add_comment(
        self,
        report_id: uuid.UUID,
        author_id: uuid.UUID,
        body: str,
    ) -> tuple[BugCommentOut, bool, BugCommentContext]:
        result = await self._run(
            _COMMENT,
            {"actor": author_id, "report": report_id, "body": body},
            denied="只有反馈提交者或管理员可以评论",
        )
        return (
            BugCommentOut.model_validate(result["comment"]),
            result["reopened"],
            BugCommentContext.from_reply(result["report"]),
        )

    async def cluster_similar(
        self, report: BugReportOut, actor_id: uuid.UUID
    ) -> list[uuid.UUID]:
        result = await self.db.execute(
            _CLUSTER, {"actor": actor_id, "report": report.id}
        )
        return [
            row.id for row in result if self._title_similarity(report.title, row.title)
        ]

    async def list_markdown(self, actor_id: uuid.UUID, *, status: str = "new") -> str:
        page = await self.list(actor_id, status=status, limit=50)
        if not page.items:
            return ""
        comments_by_report: dict[uuid.UUID, list[BugCommentOut]] = {}
        comment_result = await self.db.scalars(
            _MARKDOWN_COMMENTS,
            {"actor": actor_id, "reports": [item.id for item in page.items]},
        )
        for row in comment_result:
            comment = BugCommentOut.model_validate(row)
            comments_by_report.setdefault(comment.bug_report_id, []).append(comment)

        parts: list[str] = []
        for item in page.items:
            parts.append(
                self._format_markdown(item, comments_by_report.get(item.id, []))
            )
            parts.append("\n---\n")
        return "\n".join(parts)

    @staticmethod
    def _format_markdown(report: BugReportOut, comments: list[BugCommentOut]) -> str:
        lines = [
            f"## {report.display_id}: {report.title}",
            "",
            f"- **Severity**: {report.severity}",
            f"- **Status**: {report.status}",
            f"- **Route**: `{report.route}`",
            f"- **Role**: {report.user_role}",
            f"- **Created**: {report.created_at.isoformat() if report.created_at else 'N/A'}",
        ]
        if report.browser_ua:
            lines.append(f"- **Browser**: {report.browser_ua[:120]}")
        if report.viewport:
            lines.append(f"- **Viewport**: {report.viewport}")
        if report.project_id:
            lines.append(f"- **Project**: {report.project_id}")
        if report.task_id:
            lines.append(f"- **Task**: {report.task_id}")
        if report.screenshot_url:
            lines.append(f"- **Screenshot**: {report.screenshot_url}")
        if report.attachments:
            lines.append("")
            lines.append("### Attachments")
            for att in report.attachments[:5]:
                key, name = att.storage_key, att.file_name
                if key:
                    lines.append(f"- {name}: `{key}`")
        if report.recent_api_calls:
            lines.append("")
            lines.append("### Recent API Calls")
            for call in report.recent_api_calls[:10]:
                lines.append(
                    f"- `{call.get('method', '?')} {call.get('url', '?')}` → {call.get('status', '?')} ({call.get('ms', '?')}ms)"
                )
        if report.recent_console_errors:
            lines.append("")
            lines.append("### Console Errors")
            for err in report.recent_console_errors[:5]:
                lines.append(f"- {err.get('msg', '?')}")
        lines.append("")
        lines.append("### Description")
        lines.append(report.description)
        if comments:
            lines.append("")
            lines.append(f"### Comments ({len(comments)})")
            for c in comments:
                lines.append(f"- [{c.author_id}] {c.body}")
        return "\n".join(lines)

    @staticmethod
    def _title_similarity(a: str, b: str) -> bool:
        """Simple word overlap heuristic for dedup."""
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return False
        overlap = len(words_a & words_b)
        return overlap / min(len(words_a), len(words_b)) > 0.5
