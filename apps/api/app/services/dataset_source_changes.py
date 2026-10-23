"""Frozen, preflight-owned differences; not-seen never means deleted."""

from fastapi import HTTPException
from sqlalchemy import case, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from app.db.models.dataset_source import (
    DatasetSourceChange as Change,
    DatasetSourceItem,
    DatasetSourceRevision as Revision,
    DatasetScanEntry,
    DatasetScanRun,
    DatasetRegistrationEntry as Entry,
)

KINDS = ("added", "unchanged", "changed", "not_seen", "excluded", "unreadable")


async def initialize(db, run):
    old, current = aliased(DatasetScanEntry), aliased(DatasetScanEntry)
    baseline_count = await db.scalar(
        select(func.count())
        .select_from(DatasetSourceItem)
        .where(DatasetSourceItem.source_id == run.source_id)
    )
    if baseline_count > 50000:
        raise HTTPException(409, detail={"code": "comparison_budget_exceeded"})
    eligible = (current.kind == "file") & (current.state == "observed")
    await db.execute(
        insert(Change).from_select(
            [
                "run_id",
                "path_digest",
                "relative_path",
                "baseline_run_id",
                "before_item_id",
                "before_revision_id",
                "before_sha256",
                "before_observation",
                "before_anchor",
                "kind",
                "reason",
            ],
            select(
                literal(run.id),
                DatasetSourceItem.path_digest,
                old.relative_path,
                DatasetSourceItem.run_id,
                DatasetSourceItem.dataset_item_id,
                DatasetSourceItem.current_revision_id,
                DatasetSourceItem.content_sha256,
                old.verified_content["observation"],
                DatasetScanRun.snapshot["anchor"],
                case(
                    (eligible, "pending"),
                    (current.path_digest.is_not(None), "excluded"),
                    else_="not_seen",
                ),
                case(
                    (eligible, None),
                    (current.path_digest.is_not(None), "not_a_candidate"),
                    else_="not_enumerated",
                ),
            )
            .join(
                old,
                (old.run_id == DatasetSourceItem.run_id)
                & (old.path_digest == DatasetSourceItem.path_digest),
            )
            .join(DatasetScanRun, DatasetScanRun.id == DatasetSourceItem.run_id)
            .outerjoin(
                current,
                (current.run_id == run.scan_run_id)
                & (current.path_digest == DatasetSourceItem.path_digest),
            )
            .where(DatasetSourceItem.source_id == run.source_id),
        )
    )
    await db.execute(
        pg_insert(Change)
        .from_select(
            ["run_id", "path_digest", "relative_path", "kind"],
            select(
                literal(run.id),
                DatasetScanEntry.path_digest,
                DatasetScanEntry.relative_path,
                literal("pending"),
            ).where(
                DatasetScanEntry.run_id == run.scan_run_id,
                DatasetScanEntry.kind == "file",
                DatasetScanEntry.state == "observed",
            ),
        )
        .on_conflict_do_nothing(index_elements=["run_id", "path_digest"])
    )


async def record(db, run, digest, state, error_code):
    if not run.comparison_available:
        return False
    change = await db.get(Change, (run.id, digest), populate_existing=True)
    if change is None:
        raise HTTPException(409, detail={"code": "comparison_entry_missing"})
    if state != "ready":
        change.kind, change.reason = "unreadable", error_code
        return False
    entry = await db.get(
        DatasetScanEntry, (run.scan_run_id, digest), populate_existing=True
    )
    receipt = entry.verified_content
    if not receipt:
        raise HTTPException(409, detail={"code": "content_unverified"})
    change.candidate_sha256 = receipt["sha256"]
    scan = await db.get(DatasetScanRun, run.scan_run_id)
    change.reason = None
    if change.before_sha256 is None:
        change.kind = "added"
    elif change.before_sha256 != receipt["sha256"]:
        change.kind, change.reason = "changed", "content_changed"
    elif change.before_anchor != scan.snapshot["anchor"]:
        change.kind, change.reason = "changed", "qualification_changed"
    elif change.before_observation != receipt["observation"]:
        change.kind, change.reason = "changed", "observation_changed"
    else:
        change.kind = "unchanged"
    return change.kind == "changed"


async def page(db, run, kind, limit, offset):
    if not run.comparison_complete:
        return {
            "complete": False,
            "counts": {},
            "total": 0,
            "items": [],
            "observed_at": run.created_at,
        }
    rows = (
        await db.execute(
            select(Change.kind, func.count())
            .where(Change.run_id == run.id)
            .group_by(Change.kind)
        )
    ).all()
    counts = dict.fromkeys(KINDS, 0)
    counts.update(dict(rows))
    stmt = (
        select(Change, Entry, Revision)
        .outerjoin(
            Entry,
            (Entry.run_id == Change.run_id) & (Entry.path_digest == Change.path_digest),
        )
        .outerjoin(
            Revision,
            (Revision.registration_id == Change.run_id)
            & (Revision.path_digest == Change.path_digest),
        )
        .where(Change.run_id == run.id)
    )
    if kind:
        stmt = stmt.where(Change.kind == kind)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    entries = (
        await db.execute(stmt.order_by(Change.path_digest).offset(offset).limit(limit))
    ).all()
    return {
        "complete": True,
        "counts": counts,
        "total": total,
        "observed_at": run.created_at,
        "items": [
            dict(
                path_digest=c.path_digest,
                relative_path=c.relative_path,
                kind=c.kind,
                reason=c.reason,
                before_item_id=c.before_item_id,
                before_revision_id=c.before_revision_id,
                accepted_revision_id=r.id if r else None,
                accepted_item_id=r.item_id_at_accept if r else None,
                can_accept_revision=(
                    r is None
                    and run.phase == "preflight"
                    and run.status == "completed"
                    and c.kind == "changed"
                    and c.reason == "content_changed"
                    and c.before_revision_id is not None
                ),
                can_revalidate=(
                    r is None
                    and run.phase == "preflight"
                    and run.status == "completed"
                    and c.kind == "changed"
                    and c.reason == "observation_changed"
                    and c.before_revision_id is not None
                    and c.before_item_id is not None
                    and c.before_sha256 == c.candidate_sha256
                ),
                before_sha256=c.before_sha256,
                candidate_sha256=c.candidate_sha256,
                registration_state=e.state if e else None,
                error_code=e.error_code if e else None,
            )
            for c, e, r in entries
        ],
    }


async def skip_unchanged(db, run):
    digests = select(Change.path_digest).where(
        Change.run_id == run.id, Change.kind == "unchanged"
    )
    result = await db.execute(
        update(Entry)
        .where(
            Entry.run_id == run.id,
            Entry.state == "ready",
            Entry.path_digest.in_(digests),
        )
        .values(state="unchanged")
    )
    run.counts = {
        **run.counts,
        "ready_count": run.counts["ready_count"] - result.rowcount,
        "unchanged_count": run.counts["unchanged_count"] + result.rowcount,
    }
