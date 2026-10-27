from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import FromClause

from app.db.models.bug_report import BugReport, BugComment
from app.services.display_id import next_display_id
from app.services.identity_read_relations import (
    account_profiles,
    read_account_profile,
)


TERMINAL_STATUSES = {"fixed", "wont_fix", "duplicate"}


class BugReportService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def detach_task_references(self, task_ids: FromClause) -> None:
        await self.db.execute(
            update(BugReport)
            .where(BugReport.task_id.in_(select(task_ids.c.id)))
            .values(task_id=None)
            .execution_options(synchronize_session=False)
        )

    async def create(
        self, reporter_id: uuid.UUID, user_role: str, **fields
    ) -> BugReport:
        display_id = await next_display_id(self.db, "bug_reports")
        report = BugReport(
            id=uuid.uuid4(),
            display_id=display_id,
            reporter_id=reporter_id,
            user_role=user_role,
            **fields,
        )
        self.db.add(report)
        await self.db.flush()
        # ADR-0027 第二段 · 双写到 annotation_feedbacks (同事务一致性, 失败一起回滚)
        from app.services.feedback import FeedbackService

        await FeedbackService(self.db).mirror_bug_report(report)
        return report

    async def update(self, report_id: uuid.UUID, **fields) -> BugReport | None:
        report = await self.db.get(BugReport, report_id)
        if not report:
            return None

        status = fields.pop("status", None)
        if status and status != report.status:
            report.status = status
            if status == "triaged":
                report.triaged_at = datetime.now(timezone.utc)
            elif status == "fixed":
                report.fixed_at = datetime.now(timezone.utc)

        for key, value in fields.items():
            if hasattr(report, key):
                setattr(report, key, value)

        await self.db.flush()
        return report

    async def list(
        self,
        *,
        status: str | None = None,
        severity: str | None = None,
        route: str | None = None,
        reporter_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[BugReport], int]:
        q = select(BugReport)
        count_q = select(func.count(BugReport.id))
        filters = []
        if status:
            filters.append(BugReport.status == status)
        if severity:
            filters.append(BugReport.severity == severity)
        if route:
            filters.append(BugReport.route.ilike(f"%{route}%"))
        if reporter_id:
            filters.append(BugReport.reporter_id == reporter_id)
        if filters:
            q = q.where(*filters)
            count_q = count_q.where(*filters)
        q = (
            q.order_by(BugReport.created_at.desc(), BugReport.id)
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(q)
        items = list(result.scalars().all())
        total_result = await self.db.execute(count_q)
        total = total_result.scalar() or 0
        return items, total

    async def get(self, report_id: uuid.UUID) -> BugReport | None:
        return await self.db.get(BugReport, report_id)

    async def delete(self, report_id: uuid.UUID) -> None:
        report = await self.db.get(BugReport, report_id)
        if report:
            from sqlalchemy import delete as sa_delete

            await self.db.execute(
                sa_delete(BugComment).where(BugComment.bug_report_id == report_id)
            )
            await self.db.delete(report)
            await self.db.flush()

    async def get_with_comments(
        self, report_id: uuid.UUID
    ) -> tuple[BugReport | None, list[tuple[BugComment, str, str]]]:
        """Returns report + list of (comment, author_name, author_role) tuples."""
        report = await self.db.get(BugReport, report_id)
        if not report:
            return None, []
        author = account_profiles.alias("bug_comment_author")
        result = await self.db.execute(
            select(BugComment, author.c.name, author.c.role)
            .join(author, author.c.id == BugComment.author_id)
            .where(BugComment.bug_report_id == report_id)
            .order_by(BugComment.created_at)
        )
        rows = [(row[0], row[1] or "", row[2] or "") for row in result.all()]
        return report, rows

    async def add_comment(
        self, report_id: uuid.UUID, author_id: uuid.UUID, body: str
    ) -> tuple[BugComment, bool, str, str] | None:
        """Add a comment. If author is the reporter and status is terminal,
        auto-reopen by switching status back to 'triaged' and bumping reopen_count.

        Returns (comment, was_reopened, author_name, author_role) or None.
        """
        report = await self.db.get(BugReport, report_id)
        if not report:
            return None

        was_reopened = False
        if author_id == report.reporter_id and report.status in TERMINAL_STATUSES:
            report.status = "triaged"
            report.reopen_count = (report.reopen_count or 0) + 1
            report.last_reopened_at = datetime.now(timezone.utc)
            report.triaged_at = datetime.now(timezone.utc)
            was_reopened = True

        comment = BugComment(
            id=uuid.uuid4(),
            bug_report_id=report_id,
            author_id=author_id,
            body=body,
        )
        self.db.add(comment)
        await self.db.flush()

        author = await read_account_profile(self.db, author_id)
        author_name = (author.name if author else "") or ""
        author_role = (author.role if author else "") or ""
        return comment, was_reopened, author_name, author_role

    async def cluster_similar(self, report_id: uuid.UUID) -> list[uuid.UUID]:
        """Find other open bugs on the same route with similar titles."""
        report = await self.db.get(BugReport, report_id)
        if not report:
            return []
        result = await self.db.execute(
            select(BugReport).where(
                BugReport.id != report_id,
                BugReport.route == report.route,
                BugReport.status.in_(["new", "triaged", "in_progress"]),
            )
        )
        candidates = result.scalars().all()
        similar: list[uuid.UUID] = []
        for c in candidates:
            if self._title_similarity(report.title, c.title):
                similar.append(c.id)
        return similar

    async def get_markdown(self, report_id: uuid.UUID) -> str | None:
        report, comment_rows = await self.get_with_comments(report_id)
        if not report:
            return None
        comments = [comment for comment, _, _ in comment_rows]
        return self._format_markdown(report, comments)

    async def list_markdown(self, status: str = "new") -> str:
        items, _ = await self.list(status=status, limit=50)
        if not items:
            return ""

        comment_result = await self.db.execute(
            select(BugComment)
            .where(BugComment.bug_report_id.in_([item.id for item in items]))
            .order_by(BugComment.bug_report_id, BugComment.created_at)
        )
        comments_by_report: dict[uuid.UUID, list[BugComment]] = {
            item.id: [] for item in items
        }
        for comment in comment_result.scalars().all():
            comments_by_report.setdefault(comment.bug_report_id, []).append(comment)

        parts: list[str] = []
        for item in items:
            parts.append(self._format_markdown(item, comments_by_report[item.id]))
            parts.append("\n---\n")
        return "\n".join(parts)

    @staticmethod
    def _format_markdown(report: BugReport, comments: list[BugComment]) -> str:
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
                key = att.get("storageKey") or att.get("storage_key") or ""
                name = att.get("fileName") or att.get("file_name") or key
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
