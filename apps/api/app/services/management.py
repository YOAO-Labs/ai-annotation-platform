"""Shared query and impact helpers for the administrator management APIs."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.management import (
    BatchDistributionPreview,
    BatchDistributionPreviewItem,
    BatchDistributionRecipient,
)
from app.services.labeling_core_read_relations import (
    batch_row,
    lock_batch_rows_for_core_read,
    task_row,
)


async def preview_batch_distribution(
    db: AsyncSession,
    *,
    project_id: UUID,
    annotator_ids: list[UUID],
    reviewer_ids: list[UUID],
    only_unassigned: bool,
    validate_targets,
    lock_batches: bool = False,
    batch_ids: list[UUID] | None = None,
    single_batch_id: UUID | None = None,
    clear_unselected: bool = False,
) -> BatchDistributionPreview:
    """Calculate distribution changes without changing batches or tasks."""

    if (
        not annotator_ids
        and not reviewer_ids
        and not (single_batch_id and clear_unselected)
    ):
        raise HTTPException(
            status_code=400, detail="annotator_ids or reviewer_ids required"
        )
    await validate_targets(
        project_id,
        [("annotator", user_id) for user_id in annotator_ids]
        + [("reviewer", user_id) for user_id in reviewer_ids],
    )
    batch_query = (
        select(batch_row)
        .where(
            batch_row.project_id == project_id,
            batch_row.status != "archived",
            batch_row.id.in_(batch_ids) if batch_ids is not None else True,
            batch_row.id == single_batch_id if single_batch_id is not None else True,
        )
        .order_by(batch_row.priority.desc(), batch_row.created_at, batch_row.id)
        .execution_options(populate_existing=True)
    )
    if lock_batches:
        # The shared lock capability orders IDs for its own deadlock discipline.
        # Management historically locks in distribution priority order, so acquire
        # each row separately in that same sequence.
        probe_batches = (await db.execute(batch_query)).scalars().all()
        batches = []
        for probe in probe_batches:
            locked_rows = await lock_batch_rows_for_core_read(
                db,
                [probe.id],
                project_id=project_id,
                populate_existing=True,
            )
            if locked_rows and locked_rows[0].status != "archived":
                batches.append(locked_rows[0])
    else:
        batches = (await db.execute(batch_query)).scalars().all()
    if batch_ids is not None and len(batches) != len(set(batch_ids)):
        raise HTTPException(
            status_code=409, detail="选中批次已被移除或归档，请刷新后重新选择"
        )
    if not batches:
        raise HTTPException(status_code=400, detail="没有可分派的批次")
    # Aggregate persisted tasks rather than trusting cached batch counters.
    task_groups = (
        await db.execute(
            select(
                task_row.batch_id,
                task_row.assignee_id,
                task_row.reviewer_id,
                task_row.status,
                func.count(),
            )
            .where(task_row.batch_id.in_([batch.id for batch in batches]))
            .group_by(
                task_row.batch_id,
                task_row.assignee_id,
                task_row.reviewer_id,
                task_row.status,
            )
        )
    ).all()
    by_batch: dict[UUID, list] = {}
    for row in task_groups:
        by_batch.setdefault(row.batch_id, []).append(row)
    pending_statuses = {
        "annotator": {"pending", "in_progress", "rejected"},
        "reviewer": {"review"},
    }
    recipients: dict[tuple[str, UUID], BatchDistributionRecipient] = {}
    for role, user_ids in (("annotator", annotator_ids), ("reviewer", reviewer_ids)):
        if not user_ids:
            continue
        owner_column = (
            task_row.assignee_id if role == "annotator" else task_row.reviewer_id
        )
        backlog = dict(
            (
                await db.execute(
                    select(owner_column, func.count())
                    .where(
                        owner_column.in_(user_ids),
                        task_row.status.in_(pending_statuses[role]),
                    )
                    .group_by(owner_column)
                )
            ).all()
        )
        for user_id in dict.fromkeys(user_ids):
            recipients[role, user_id] = BatchDistributionRecipient(
                user_id=user_id,
                role=role,
                new_task_count=0,
                existing_backlog_count=backlog.get(user_id, 0),
            )
    items: list[BatchDistributionPreviewItem] = []
    a_idx = 0
    r_idx = 0
    candidate = 0
    changed = 0
    skipped = 0
    for batch in batches:
        before_a = batch.annotator_id
        before_r = batch.reviewer_id
        after_a = before_a
        after_r = before_r
        eligible = False
        if clear_unselected and single_batch_id:
            eligible = True
            if not annotator_ids:
                after_a = None
            if not reviewer_ids:
                after_r = None
        if annotator_ids and (not only_unassigned or before_a is None):
            eligible = True
            after_a = annotator_ids[a_idx % len(annotator_ids)]
            a_idx += 1
        if reviewer_ids and (not only_unassigned or before_r is None):
            eligible = True
            after_r = reviewer_ids[r_idx % len(reviewer_ids)]
            r_idx += 1
        if eligible:
            candidate += 1
        will_change = after_a != before_a or after_r != before_r
        rows = by_batch.get(batch.id, [])
        for role, before, after, index in (
            ("annotator", before_a, after_a, 1),
            ("reviewer", before_r, after_r, 2),
        ):
            if after is not None and after != before:
                recipients[role, after].new_task_count += sum(
                    row[4]
                    for row in rows
                    if row[index] != after and row.status in pending_statuses[role]
                )
        reason = None
        if not eligible:
            skipped += 1
            reason = (
                "已分派，only_unassigned 未覆盖"
                if only_unassigned
                else "没有可更新的职责"
            )
        elif not will_change:
            skipped += 1
            reason = "目标与当前分派相同"
        else:
            changed += 1
        items.append(
            BatchDistributionPreviewItem(
                batch_id=batch.id,
                display_id=batch.display_id,
                name=batch.name,
                status=batch.status,
                task_count=sum(row[4] for row in rows),
                before_annotator_id=before_a,
                after_annotator_id=after_a,
                before_reviewer_id=before_r,
                after_reviewer_id=after_r,
                will_change=will_change,
                skipped_reason=reason,
            )
        )
    version = hashlib.sha256(
        json.dumps(
            {
                "project_id": str(project_id),
                "annotator_ids": [str(value) for value in annotator_ids],
                "reviewer_ids": [str(value) for value in reviewer_ids],
                "only_unassigned": only_unassigned,
                "items": [item.model_dump(mode="json") for item in items],
                "recipients": [
                    item.model_dump(mode="json") for item in recipients.values()
                ],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return BatchDistributionPreview(
        project_id=project_id,
        only_unassigned=only_unassigned,
        total_batches=len(batches),
        candidate_batches=candidate,
        changed_batches=changed,
        skipped_batches=skipped,
        items=items,
        recipient_summary=list(recipients.values()),
        preview_version=version,
    )
