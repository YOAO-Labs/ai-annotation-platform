"""Commit one S3 page and its continuation under the existing scan lease."""

from app.services import async_job as jobs
from app.services import dataset_scan_data as data
from app.services import dataset_scans as scans
from app.services import (
    reference_storage,
    source_selection as selection,
    storage_job_progress as progress,
)
from app.services.filesystem_storage import FilesystemStorageError as StorageError


async def next_page(db, location, source, run, job):
    root = await data.read_directory(db, run, job, scans.path_digest(""))
    if root is None:
        raise scans.LostScanLease()
    if root.state == "captured":
        await scans.finish(db, run, job, "completed")
        return None
    access = await reference_storage.configuration(db, location)
    if access.anchor != run.snapshot["anchor"]:
        raise StorageError("configuration_changed")
    checkpoint = root.observed.get("checkpoint", {})
    if checkpoint.get("pages", 0) >= 1000:
        raise StorageError("scan_budget_exceeded")
    if not await data.open_s3_page(db, run, job):
        raise scans.LostScanLease()
    await data.execution_run(db, run.id)
    await jobs.update_payload(db, job.id, {"stage": "enumerating"})
    return {
        "kind": "s3",
        "location_id": location.id,
        "maintenance_revision": run.snapshot.get("maintenance_revision", 1),
        "access": access,
        "path": source.config["relative_path"],
        "cursor": checkpoint.get("cursor"),
        "pages": checkpoint.get("pages", 0),
    }


async def apply_page(db, run, job, work, page):
    config = run.snapshot["config"]
    rows = []
    for item in page["entries"]:
        path = item["name"]
        selected = selection.selected_file(config, path, "s3")
        rows.append(
            {
                "path_digest": scans.path_digest(path),
                "relative_path": path,
                "state": "observed" if selected else "skipped",
                "observed": {k: v for k, v in item.items() if k != "name"},
            }
        )
    result = await data.capture_s3_page(db, run, job, work, page, rows)
    scans.require_capture(result.outcome)
    await data.execution_run(db, run.id)
    await jobs.replace_result(db, job.id, result={**result.counts, "complete": False})
    await db.flush()
    await progress.record(
        db,
        job,
        task_id=job.celery_task_id,
        stage="scanning",
        unit="pages",
        completed=work["pages"] + 1,
        marker=f"{run.id}:{run.generation}:{work['pages'] + 1}",
    )
