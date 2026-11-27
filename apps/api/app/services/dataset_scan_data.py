"""Borrowed-session calls to the directory inventory and receipt capabilities."""

from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm.attributes import set_committed_value

from app.db.models.dataset_source import DatasetScanEntry, DatasetScanRun, DatasetSource


def external_protection(source):
    if source.external_protection is None:
        return None
    return {
        **source.external_protection,
        "revision": source.external_protection_revision,
    }


def _json(sql, *keys):
    return text(sql).bindparams(*(bindparam(key, type_=JSONB) for key in keys))


_LOCK_SOURCE = (
    select(DatasetSource)
    .from_statement(
        text(
            "SELECT * FROM yoao_assets_v1.lock_dataset_directory_source("
            "CAST(:source_id AS uuid))"
        )
    )
    .execution_options(populate_existing=True)
)
_SCAN_EXECUTION = (
    select(DatasetScanRun)
    .from_statement(
        text(
            "SELECT * FROM yoao_assets_v1.read_dataset_scan_execution("
            "CAST(:run_id AS uuid), :lock)"
        )
    )
    .execution_options(populate_existing=True)
)
_CREATE_SOURCE = _json(
    "SELECT yoao_assets_v1.create_dataset_directory_source("
    "CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid), CAST(:location_id AS uuid), "
    ":name, :ownership_mode, :cache_policy, :config)",
    "config",
)
_EDIT_SOURCE = text(
    "SELECT yoao_assets_v1.edit_dataset_directory_source("
    "CAST(:actor_id AS uuid), CAST(:source_id AS uuid), "
    ":revision, :name, :enabled, :cache_policy)"
)
_START = _json(
    "SELECT yoao_assets_v1.start_dataset_scan("
    "CAST(:actor_id AS uuid), CAST(:source_id AS uuid), :snapshot)",
    "snapshot",
)
_ATTEMPT = (
    "CAST(:run_id AS uuid), CAST(:job_id AS uuid), :task_id, :generation, "
    "CAST(:lease_token AS uuid)"
)

_OBSERVED_FILE = (
    select(DatasetScanEntry)
    .from_statement(
        text(
            "SELECT * FROM yoao_assets_v1.read_observed_dataset_scan_file("
            "CAST(:actor_id AS uuid), CAST(:run_id AS uuid), :path_digest, :lock)"
        )
    )
    .execution_options(populate_existing=True)
)


async def lock_source(db, source_id):
    return await db.scalar(_LOCK_SOURCE, {"source_id": source_id})


async def create_source(
    db, actor_id, dataset_id, location_id, name, ownership_mode, cache_policy, config
):
    return await db.scalar(
        _CREATE_SOURCE,
        {
            "actor_id": actor_id,
            "dataset_id": dataset_id,
            "location_id": location_id,
            "name": name,
            "ownership_mode": ownership_mode,
            "cache_policy": cache_policy,
            "config": config,
        },
    )


async def edit_source(db, actor_id, source_id, payload):
    return await db.scalar(
        _EDIT_SOURCE,
        {
            "actor_id": actor_id,
            "source_id": source_id,
            "revision": payload.expected_revision,
            "name": payload.name,
            "enabled": payload.enabled,
            "cache_policy": payload.cache_policy,
        },
    )


async def read_scan(db, run_id):
    return (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.read_dataset_scan(CAST(:run_id AS uuid))"
            ),
            {"run_id": run_id},
        )
    ).one_or_none()


async def read_scan_snapshot(db, run_id):
    return (
        await db.execute(
            text(
                "SELECT scan.*, yoao_assets_v1.dataset_scan_snapshot(scan.id) "
                "AS snapshot "
                "FROM yoao_assets_v1.read_dataset_scan(CAST(:run_id AS uuid)) AS scan"
            ),
            {"run_id": run_id},
        )
    ).one_or_none()


async def list_scans(db, source_id, *, actor_id, limit, offset, complete, active_only):
    row = (
        await db.execute(
            text(
                "SELECT items, total FROM yoao_assets_v1.list_dataset_scans("
                "CAST(:actor_id AS uuid), CAST(:source_id AS uuid), "
                ":complete, :active_only, :limit, :offset)"
            ),
            {
                "actor_id": actor_id,
                "source_id": source_id,
                "complete": complete,
                "active_only": active_only,
                "limit": limit,
                "offset": offset,
            },
        )
    ).one()
    return {"items": row.items, "total": row.total}


async def execution_run(db, run_id, *, lock=False):
    return await db.scalar(_SCAN_EXECUTION, {"run_id": run_id, "lock": lock})


async def active_scan_id(db, source_id, *, except_run_id=None):
    return await db.scalar(
        text(
            "SELECT yoao_assets_v1.active_dataset_scan("
            "CAST(:source_id AS uuid), CAST(:except_run_id AS uuid))"
        ),
        {"source_id": source_id, "except_run_id": except_run_id},
    )


async def run_id_for_job(db, job_id):
    return await db.scalar(
        text("SELECT yoao_assets_v1.dataset_scan_for_job(CAST(:job_id AS uuid))"),
        {"job_id": job_id},
    )


async def dispatch_route(db, run_id):
    return (
        await db.execute(
            text(
                "SELECT task_id, node_id FROM yoao_assets_v1.dataset_scan_dispatch_route(CAST(:run_id AS uuid))"
            ),
            {"run_id": run_id},
        )
    ).one()


async def start(db, actor_id, source_id, snapshot):
    return await db.scalar(
        _START, {"actor_id": actor_id, "source_id": source_id, "snapshot": snapshot}
    )


async def bind_attempt(db, run, job, api_key_id, key_proof):
    return await db.scalar(
        text(
            "SELECT yoao_assets_v1.bind_dataset_scan_attempt("
            "CAST(:run_id AS uuid), CAST(:previous_job_id AS uuid), :generation, "
            "CAST(:job_id AS uuid), CAST(:api_key_id AS uuid), :key_proof)"
        ),
        {
            "run_id": run.id,
            "previous_job_id": run.async_job_id,
            "generation": run.generation,
            "job_id": job.id,
            "api_key_id": api_key_id,
            "key_proof": key_proof,
        },
    )


async def claim(db, run, job, node_id, host_id):
    return await db.scalar(
        text(
            "SELECT yoao_assets_v1.claim_dataset_scan("
            "CAST(:run_id AS uuid), CAST(:job_id AS uuid), :task_id, :node_id, :host_id)"
        ),
        {
            "run_id": run.id,
            "job_id": job.id,
            "task_id": job.celery_task_id,
            "node_id": node_id,
            "host_id": host_id,
        },
    )


def _attempt(run, job):
    return {
        "run_id": run.id,
        "job_id": job.id if job else None,
        "task_id": job.celery_task_id if job else None,
        "generation": run.generation,
        "lease_token": run.lease_token,
    }


async def settle(db, run, job, status, code):
    return await db.scalar(
        text(
            f"SELECT yoao_assets_v1.settle_dataset_scan("
            f"{_ATTEMPT}, :previous_status, :status, :code)"
        ),
        {
            **_attempt(run, job),
            "previous_status": run.status,
            "status": status,
            "code": code,
        },
    )


async def next_directory(db, run, job):
    return (
        await db.execute(
            text(
                f"SELECT * FROM yoao_assets_v1.next_dataset_scan_directory({_ATTEMPT})"
            ),
            _attempt(run, job),
        )
    ).one()


async def read_directory(db, run, job, path_digest):
    return (
        await db.execute(
            text(
                f"SELECT * FROM yoao_assets_v1.read_dataset_scan_directory("
                f"{_ATTEMPT}, :path_digest)"
            ),
            {**_attempt(run, job), "path_digest": path_digest},
        )
    ).one_or_none()


async def validate_directory(db, run, job, work, manifest):
    return (
        await db.execute(
            text(
                f"SELECT * FROM yoao_assets_v1.validate_dataset_scan_directory("
                f"{_ATTEMPT}, :phase, :path_digest, :directory_digest)"
            ),
            {
                **_attempt(run, job),
                "phase": work["phase"],
                "path_digest": work["path_digest"],
                "directory_digest": manifest["digest"],
            },
        )
    ).one()


async def capture_directory(db, run, job, work, manifest, entries):
    return (
        await db.execute(
            _json(
                f"SELECT * FROM yoao_assets_v1.capture_dataset_scan_directory("
                f"{_ATTEMPT}, :path_digest, :directory_digest, :identity, :entries)",
                "identity",
                "entries",
            ),
            {
                **_attempt(run, job),
                "path_digest": work["path_digest"],
                "directory_digest": manifest["digest"],
                "identity": manifest["identity"],
                "entries": entries,
            },
        )
    ).one()


async def open_s3_page(db, run, job):
    return await db.scalar(
        text(f"SELECT yoao_assets_v1.open_dataset_scan_s3_page({_ATTEMPT})"),
        _attempt(run, job),
    )


async def capture_s3_page(db, run, job, work, page, entries):
    return (
        await db.execute(
            _json(
                f"SELECT * FROM yoao_assets_v1.capture_dataset_scan_s3_page("
                f"{_ATTEMPT}, :cursor, :pages, :next_cursor, :listed_count, :entries)",
                "entries",
            ),
            {
                **_attempt(run, job),
                "cursor": work["cursor"],
                "pages": work["pages"],
                "next_cursor": page["next_cursor"],
                "listed_count": page["listed_count"],
                "entries": entries,
            },
        )
    ).one()


async def observed_file(db, run_id, path_digest, *, actor_id, lock=False):
    return await db.scalar(
        _OBSERVED_FILE,
        dict(actor_id=actor_id, run_id=run_id, path_digest=path_digest, lock=lock),
    )


async def verify_file(db, actor_id, entry, receipt):
    written = await db.scalar(
        _json(
            "SELECT yoao_assets_v1.verify_observed_dataset_scan_file("
            "CAST(:actor_id AS uuid), CAST(:run_id AS uuid), :path_digest, :receipt)",
            "receipt",
        ),
        dict(
            actor_id=actor_id,
            run_id=entry.run_id,
            path_digest=entry.path_digest,
            receipt=receipt,
        ),
    )
    if written:
        set_committed_value(entry, "verified_content", receipt)
    return written
