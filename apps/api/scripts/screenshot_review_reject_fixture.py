#!/usr/bin/env python3
"""Prepare and precisely clean the review-reject marketing recording state.

The helper is fail-closed to the screenshot-managed image project. It restores
the declared review task after the real reject API runs, and removes only the
feedback and notification rows carrying this recording's exact reason text.
Audit rows remain immutable by design.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_API_ROOT = Path(__file__).resolve().parent.parent
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.config import settings  # noqa: E402
from app.db.models.notification import Notification  # noqa: E402
from app.services.identity_commands import IdentityCommandService  # noqa: E402
from app.services.annotation_evidence import (  # noqa: E402
    freeze_review_contributor_evidence,
    known_annotation_contributor_ids,
)
from app.services.batch import BatchService  # noqa: E402
from scripts.cleanup_screenshot_ocr_flow import assert_screenshot_scope  # noqa: E402
from aap_labeling.screenshot_operations_contract import (  # noqa: E402
    RestoreReviewReject,
    SCREENSHOT_OPERATIONS_PROTOCOL,
)
from app.services.labeling_core_read_relations import get_core_row, task_row  # noqa: E402
from app.services.labeling_screenshot_operations import (  # noqa: E402
    screenshot_operations_command,
)


REASON_TEXT = "车辆边界存在偏移，请重新贴合目标外沿。"
LEGACY_REASON_TEXT = "标注框偏移，请重新对齐目标边缘（演示）"
OWNED_REASONS = (REASON_TEXT, LEGACY_REASON_TEXT)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "cleanup"))
    parser.add_argument("--project-id", type=uuid.UUID, required=True)
    parser.add_argument("--task-id", type=uuid.UUID, required=True)
    parser.add_argument("--reviewer-email", required=True)
    return parser.parse_args()


async def restore(args: argparse.Namespace) -> dict[str, int | str]:
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with session_factory() as db:
            expected_status = await assert_screenshot_scope(
                db,
                project_key="image_demo",
                project_id=args.project_id,
                task_id=args.task_id,
            )
            if expected_status != "review":
                raise RuntimeError(
                    f"review-reject task must be declared as review, got {expected_status}"
                )

            task = await get_core_row(db, task_row, args.task_id)
            reviewer_id = await IdentityCommandService(db).lookup_seed_account_id(
                args.reviewer_email
            )
            if task is None or task.project_id != args.project_id:
                raise RuntimeError("review-reject task escaped screenshot project")
            if reviewer_id is None or task.reviewer_id != reviewer_id:
                raise RuntimeError(
                    "review-reject task is not assigned to the recording reviewer"
                )
            contributors = known_annotation_contributor_ids(task)
            if (
                contributors is None
                or task.assignee_id is None
                or str(task.assignee_id) not in contributors
                or str(reviewer_id) in contributors
            ):
                raise RuntimeError(
                    "review-reject fixture needs complete seeded author evidence; "
                    "repair the screenshot seed before recording"
                )

            review_round_id = task.review_round_id or uuid.uuid4()
            task_copy = SimpleNamespace(
                annotation_contributor_ids=task.annotation_contributor_ids,
                review_contributor_ids=task.review_contributor_ids,
                review_submitter_id=task.review_submitter_id,
            )
            freeze_review_contributor_evidence(task_copy, submitter_id=task.assignee_id)
            review_contributors = task_copy.review_contributor_ids
            submitter_id = task_copy.review_submitter_id
            result = await screenshot_operations_command(
                db,
                RestoreReviewReject(
                    schema=SCREENSHOT_OPERATIONS_PROTOCOL,
                    operation="restore_review_reject",
                    project_id=args.project_id,
                    task_id=task.id,
                    reviewer_id=reviewer_id,
                    expected_status="review",
                    review_round_id=review_round_id,
                    review_contributor_ids=(
                        tuple(review_contributors)
                        if review_contributors is not None
                        else None
                    ),
                    review_submitter_id=(
                        uuid.UUID(str(submitter_id))
                        if submitter_id is not None
                        else None
                    ),
                    reason_texts=OWNED_REASONS,
                ),
            )
            notification_result = await db.execute(
                delete(Notification).where(
                    Notification.type == "task.rejected",
                    Notification.target_type == "task",
                    Notification.target_id == task.id,
                    Notification.payload["reject_reason"].astext.in_(OWNED_REASONS),
                )
            )

            task = await get_core_row(
                db, task_row, args.task_id, populate_existing=True
            )
            if task is None:
                raise RuntimeError("review-reject task disappeared")
            if task.batch_id is not None:
                await BatchService(db).recalculate_counters(task.batch_id)
            await db.commit()
            return {
                "status": expected_status,
                "feedbacks": result.counts["feedbacks"],
                "notifications": notification_result.rowcount or 0,
            }
    finally:
        await engine.dispose()


def main() -> int:
    if settings.environment == "production":
        print("[review-reject-fixture] refusing to run in production", file=sys.stderr)
        return 2
    args = parse_args()
    result = asyncio.run(restore(args))
    print(f"[review-reject-fixture] {args.action} {json.dumps(result, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
