"""Commit one S3 page and its continuation under the existing scan lease."""

import hashlib

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models.dataset_source import DatasetScanEntry as Entry
from app.services import dataset_scans as scans
from app.services import (
    reference_storage,
    source_selection as selection,
    storage_job_progress as progress,
)
from app.services.filesystem_storage import FilesystemStorageError as StorageError


async def next_page(db, location, source, run, job):
    root = await db.get(Entry, (run.id, scans.path_digest("")))
    if root.state == "captured":
        await scans.finish(db, run, job, "completed")
        return None
    access = await reference_storage.configuration(db, location)
    if access.anchor != run.snapshot["anchor"]:
        raise StorageError("configuration_changed")
    checkpoint = root.observed.get("checkpoint", {})
    if checkpoint.get("pages", 0) >= 1000:
        raise StorageError("scan_budget_exceeded")
    from datetime import timedelta

    run.phase = "enumerating"
    run.lease_expires_at = await scans.now(db) + timedelta(seconds=scans.LEASE_SECONDS)
    job.payload = {**job.payload, "stage": "enumerating"}
    return {
        "kind": "s3",
        "location_id": location.id,
        "maintenance_revision": run.snapshot.get("maintenance_revision", 1),
        "access": access,
        "path": source.config["relative_path"],
        "cursor": checkpoint.get("cursor"),
        "pages": checkpoint.get("pages", 0),
    }


async def apply_page(db, source, run, job, work, page):
    root = await db.get(Entry, (run.id, scans.path_digest("")), populate_existing=True)
    checkpoint = root.observed.get("checkpoint", {})
    if (
        root.state != "pending"
        or checkpoint.get("cursor") != work["cursor"]
        or checkpoint.get("pages", 0) != work["pages"]
    ):
        raise scans.LostScanLease()
    token = page["next_cursor"]
    tokens = checkpoint.get("tokens", [])
    digest = hashlib.sha256(token.encode()).hexdigest() if token else None
    if digest in tokens:
        raise StorageError("s3_invalid_cursor")
    listed = checkpoint.get("listed", 0) + page["listed_count"]
    if listed > run.snapshot["budget"]["max_entries"]:
        raise StorageError("scan_budget_exceeded")
    config = run.snapshot["config"]
    rows = []
    for item in page["entries"]:
        path = item["name"]
        selected = selection.selected_file(config, path, "s3")
        rows.append(
            {
                "run_id": run.id,
                "path_digest": scans.path_digest(path),
                "relative_path": path,
                "kind": "file",
                "state": "observed" if selected else "skipped",
                "observed": {k: v for k, v in item.items() if k != "name"},
            }
        )
    if rows:
        await db.execute(insert(Entry).values(rows).on_conflict_do_nothing())
    root.observed = {
        "checkpoint": {
            "cursor": token,
            "pages": work["pages"] + 1,
            "tokens": [*tokens, digest] if digest else tokens,
            "listed": listed,
        }
    }
    root.state = "pending" if token else "captured"
    await db.flush()
    counts = (
        await db.execute(
            select(Entry.state, func.count())
            .where(Entry.run_id == run.id, Entry.kind == "file")
            .group_by(Entry.state)
        )
    ).all()
    run.counts = {
        "processed_count": sum(n for _, n in counts),
        "file_count": sum(n for state, n in counts if state == "observed"),
        "skipped_count": sum(n for state, n in counts if state == "skipped"),
        "directory_count": 0,
    }
    job.result = {**run.counts, "complete": False}
    await progress.record(
        db,
        job,
        task_id=job.celery_task_id,
        stage="scanning",
        unit="pages",
        completed=work["pages"] + 1,
        marker=f"{run.id}:{run.generation}:{work['pages'] + 1}",
    )
