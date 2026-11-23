"""Fixed directory-registration and sync capabilities on the caller's session."""

import json

from sqlalchemy import select, text

from app.db.models.dataset_source import (
    DatasetRegistrationRun,
    DatasetSourceSyncSchedule,
)


def _statement(name, arguments):
    binds = ", ".join(
        f"CAST(:{parameter} AS pg_catalog.{kind})"
        for parameter, kind in (argument.split(":") for argument in arguments.split())
    )
    return text(f"SELECT * FROM yoao_assets_v1.{name}({binds})")


_READ_RUN = _statement("read_directory_registration", "run_id:uuid lock:bool")
_BINDING = _statement("directory_registration_binding", "run_id:uuid")
_FOR_SCAN = _statement("directory_registration_for_scan", "scan_id:uuid")
_FOR_JOB = _statement("directory_registration_for_job", "job_id:uuid")
_ACTIVE = _statement("active_directory_registration", "source_id:uuid except_id:uuid")
_PROBE_PATHS = _statement("directory_registration_probe_paths", "scan_id:uuid")
_CREATE = _statement(
    "create_directory_preflight",
    "run_id:uuid scan_id:uuid actor_id:uuid cache_policy:text",
)
_UNSEEN = _statement("directory_preflight_unseen", "run_id:uuid")
_EXCLUDE = _statement(
    "directory_preflight_exclude_unseen", "run_id:uuid digests:text[]"
)
_OPEN = _statement(
    "open_directory_registration_attempt",
    "run_id:uuid previous_job:uuid job_id:uuid actor_id:uuid key_id:uuid key_proof:text",
)
_JOB = "run_id:uuid job_id:uuid task_id:text"
_FENCE = f"{_JOB} generation:int4 token:uuid"
_CLAIM = _statement("claim_directory_registration", f"{_JOB} node:text token:uuid")
_NEXT = _statement("next_directory_registration_entry", _FENCE)
_RECORD = _statement(
    "record_directory_registration_entry",
    f"{_FENCE} digest:text state:text error:text item_id:uuid preview_estimate:int8 "
    "preview_error:text original_size:int8 copy_bytes:int8 preview_bytes:int8 preview_failed:bool",
)
_FINISH = _statement("finish_directory_registration", f"{_JOB} status:text code:text")
_SAMPLE = _statement("directory_confirmation_sample", "run_id:uuid scope:text")
_CONFIRM_FENCE = "run_id:uuid job_id:uuid generation:int4"
_FAIL_SAMPLE = _statement(
    "fail_directory_confirmation_sample", f"{_CONFIRM_FENCE} digest:text error:text"
)
_CONFIRM = _statement("confirm_directory_registration", f"{_CONFIRM_FENCE} scope:text")
_RETRY = _statement(
    "retry_directory_registration_entry", "run_id:uuid generation:int4 digest:text"
)
_ARM = _statement("arm_directory_registration_dispatch", _JOB)
_FAIL_DISPATCH = _statement("note_directory_registration_dispatch_failure", _JOB)
_DUE_RUNS = _statement("due_directory_registrations", "stamp:timestamptz")
_REQUEUE = _statement(
    "requeue_directory_registration",
    f"{_FENCE} next_task_id:text delay:int4 error:text recover:bool",
)
_RELOCATE = _statement(
    "consume_directory_relocation",
    "run_id:uuid digest:text item_id:uuid revision_id:uuid",
)
_ENTRIES = _statement(
    "directory_registration_entries", "run_id:uuid state:text limit:int4 offset:int4"
)
_CHANGES = _statement(
    "directory_registration_changes", "run_id:uuid kind:text limit:int4 offset:int4"
)
_KINDS = _statement("directory_registration_change_kinds", "run_id:uuid")
_HEAD = _statement("read_directory_source_head", "source_id:uuid digest:text")
_REVISION = _statement(
    "read_directory_source_revision", "source_id:uuid revision_id:uuid"
)
_REVISION_FOR_OBSERVATION = _statement(
    "directory_revision_for_observation", "source_id:uuid scan_id:uuid digest:text"
)
_CHECK_REVISION = _statement(
    "check_directory_revision_selection",
    "run_id:uuid digest:text generation:int4 previous:uuid sha:text mode:text",
)
_APPEND_REVISION = _statement(
    "append_directory_source_revision",
    "revision_id:uuid source_id:uuid scan_id:uuid digest:text item_id:uuid asset_id:uuid sha:text "
    "owns_item:bool actor_id:uuid registration_id:uuid previous:uuid generation:int4 mode:text",
)
_HISTORY = _statement(
    "directory_source_revision_history",
    "source_id:uuid digest:text limit:int4 offset:int4",
)
_MOVE_CANDIDATES = _statement(
    "directory_relocation_candidates", "run_id:uuid digest:text limit:int4 offset:int4"
)
_READ_SCHEDULE = _statement("read_directory_sync_schedule", "source_id:uuid lock:bool")
_CONFIGURE_SYNC = _statement(
    "configure_directory_sync",
    "source_id:uuid revision:int4 actor_id:uuid key_id:uuid key_proof:text enabled:bool interval:int4 "
    "policy:text timezone:text maintenance_revision:int4 lifecycle_revision:int4 anchor:jsonb stamp:timestamptz",
)
_CONTINUE_SYNC = _statement(
    "continue_directory_sync",
    "source_id:uuid revision:int4 scan_id:uuid stamp:timestamptz",
)
_DUE_SOURCES = _statement("due_directory_sync_sources", "stamp:timestamptz")
_CLAIM_SYNC = _statement(
    "claim_directory_sync_wakeup", "source_id:uuid token:uuid stamp:timestamptz"
)
_SYNC_FENCE = "source_id:uuid revision:int4 token:uuid"
_FAIL_SYNC = _statement(
    "fail_directory_sync_wakeup",
    f"{_SYNC_FENCE} code:text block:bool stamp:timestamptz",
)
_SETTLE_SYNC = _statement(
    "settle_directory_sync_wakeup",
    f"{_SYNC_FENCE} outcome:text run_id:uuid error:text stamp:timestamptz",
)


async def _execute(db, statement, **params):
    if db.autoflush:
        await db.flush()
    return await db.execute(statement, params)


async def _scalar(db, statement, **params):
    return (await _execute(db, statement, **params)).scalar_one()


async def _id(db, statement, **params):
    return (await _execute(db, statement, **params)).scalar_one_or_none()


async def _load(db, model, statement, **params):
    query = (
        select(model)
        .from_statement(statement)
        .execution_options(populate_existing=True)
    )
    return (await _execute(db, query, **params)).scalar_one_or_none()


async def read_run(db, run_id, *, lock=False):
    return await _load(db, DatasetRegistrationRun, _READ_RUN, run_id=run_id, lock=lock)


async def binding(db, run_id):
    return (await _execute(db, _BINDING, run_id=run_id)).one_or_none()


async def for_scan(db, scan_id):
    run_id = await _id(db, _FOR_SCAN, scan_id=scan_id)
    return await read_run(db, run_id) if run_id is not None else None


async def run_for_job(db, job_id):
    return await _id(db, _FOR_JOB, job_id=job_id)


async def active_run_id(db, source_id, *, except_id=None):
    return await _id(db, _ACTIVE, source_id=source_id, except_id=except_id)


async def active_run(db, source_id):
    run_id = await active_run_id(db, source_id)
    run = await read_run(db, run_id) if run_id is not None else None
    return run if run is not None and run.status in {"pending", "running"} else None


async def probe_paths(db, scan_id):
    return (await _execute(db, _PROBE_PATHS, scan_id=scan_id)).scalars().all()


async def create_preflight(db, run_id, scan_id, actor_id, cache_policy):
    return await _scalar(
        db,
        _CREATE,
        run_id=run_id,
        scan_id=scan_id,
        actor_id=actor_id,
        cache_policy=cache_policy,
    )


async def unseen_paths(db, run_id):
    return (await _execute(db, _UNSEEN, run_id=run_id)).all()


async def exclude_unseen(db, run_id, digests):
    await _execute(db, _EXCLUDE, run_id=run_id, digests=digests)


async def open_attempt(db, run, job_id, actor_id, key_id, key_proof):
    return await _scalar(
        db,
        _OPEN,
        run_id=run.id,
        previous_job=run.async_job_id,
        job_id=job_id,
        actor_id=actor_id,
        key_id=key_id,
        key_proof=key_proof,
    )


def _job_values(run, job):
    return dict(
        run_id=run.id,
        job_id=job.id if job else None,
        task_id=job.celery_task_id if job else None,
    )


def _fence_values(run, job, fence):
    return dict(**_job_values(run, job), generation=fence[0], token=fence[1])


async def claim(db, run, job, node, token):
    return await _scalar(db, _CLAIM, **_job_values(run, job), node=node, token=token)


async def next_entry(db, run, job, fence):
    return (await _execute(db, _NEXT, **_fence_values(run, job, fence))).one_or_none()


async def record_entry(
    db,
    run,
    job,
    fence,
    digest,
    state,
    *,
    error,
    result,
    preview_estimate,
    preview_error,
    original_size,
):
    receipt = result or {}
    return (
        await _execute(
            db,
            _RECORD,
            **_fence_values(run, job, fence),
            digest=digest,
            state=state,
            error=error,
            item_id=receipt.get("item_id"),
            preview_estimate=preview_estimate,
            preview_error=preview_error,
            original_size=original_size,
            copy_bytes=receipt.get("original_copy_bytes", 0),
            preview_bytes=receipt.get("preview_bytes", 0),
            preview_failed=receipt.get("preview_failed", False),
        )
    ).one_or_none()


async def finish(db, run, job, status, code):
    return await _scalar(db, _FINISH, **_job_values(run, job), status=status, code=code)


async def confirmation_sample(db, run_id, scope):
    return (await _execute(db, _SAMPLE, run_id=run_id, scope=scope)).one_or_none()


async def fail_confirmation_sample(db, run_id, version, digest, error):
    await _execute(
        db,
        _FAIL_SAMPLE,
        run_id=run_id,
        job_id=version[0],
        generation=version[1],
        digest=digest,
        error=error,
    )


async def confirm(db, run_id, version, scope):
    return await _scalar(
        db,
        _CONFIRM,
        run_id=run_id,
        job_id=version[0],
        generation=version[1],
        scope=scope,
    )


async def retry_entry(db, run, digest):
    return await _scalar(
        db, _RETRY, run_id=run.id, generation=run.generation, digest=digest
    )


async def arm_dispatch(db, run, job):
    return await _scalar(db, _ARM, **_job_values(run, job))


async def fail_dispatch(db, run, job):
    await _execute(db, _FAIL_DISPATCH, **_job_values(run, job))


async def due_runs(db, stamp):
    return (await _execute(db, _DUE_RUNS, stamp=stamp)).scalars().all()


async def requeue(
    db, run, job, fence, next_task_id, *, delay=0, error=None, recover=False
):
    return await _scalar(
        db,
        _REQUEUE,
        **_fence_values(run, job, fence),
        next_task_id=next_task_id,
        delay=delay,
        error=error,
        recover=recover,
    )


async def consume_relocation(db, run_id, digest, item_id, revision_id):
    return await _scalar(
        db,
        _RELOCATE,
        run_id=run_id,
        digest=digest,
        item_id=item_id,
        revision_id=revision_id,
    )


async def entries_page(db, run_id, state, limit, offset):
    return await _scalar(
        db, _ENTRIES, run_id=run_id, state=state, limit=limit, offset=offset
    )


async def changes_page(db, run_id, kind, limit, offset):
    return await _scalar(
        db, _CHANGES, run_id=run_id, kind=kind, limit=limit, offset=offset
    )


async def change_kinds(db, run_id):
    return set((await _execute(db, _KINDS, run_id=run_id)).scalars())


async def source_head(db, source_id, digest):
    return (await _execute(db, _HEAD, source_id=source_id, digest=digest)).one_or_none()


async def revision_by_id(db, source_id, revision_id):
    return (
        await _execute(db, _REVISION, source_id=source_id, revision_id=revision_id)
    ).one_or_none()


async def revision_for_observation(db, source_id, scan_id, digest):
    revision_id = await _id(
        db,
        _REVISION_FOR_OBSERVATION,
        source_id=source_id,
        scan_id=scan_id,
        digest=digest,
    )
    return await revision_by_id(db, source_id, revision_id) if revision_id else None


async def check_revision(db, run_id, digest, payload, mode):
    return (
        await _execute(
            db,
            _CHECK_REVISION,
            run_id=run_id,
            digest=digest,
            generation=payload.expected_generation,
            previous=payload.before_revision_id,
            sha=payload.candidate_sha256,
            mode=mode,
        )
    ).one()


async def append_revision(
    db, revision_id, source, scan, entry, result, asset, actor, resolution
):
    return await _scalar(
        db,
        _APPEND_REVISION,
        revision_id=revision_id,
        source_id=source.id,
        scan_id=scan.id,
        digest=entry.path_digest,
        item_id=result["item_id"],
        asset_id=asset.id,
        sha=result["sha256"],
        owns_item=result.get("owns_item", result["created"]),
        actor_id=actor.id,
        registration_id=resolution.get("registration_id"),
        previous=resolution.get("before_revision_id"),
        generation=resolution.get("generation"),
        mode=resolution.get("mode"),
    )


async def revision_history(db, source_id, digest, limit, offset):
    return await _scalar(
        db, _HISTORY, source_id=source_id, digest=digest, limit=limit, offset=offset
    )


async def move_candidates(db, run_id, digest, limit, offset):
    return await _scalar(
        db, _MOVE_CANDIDATES, run_id=run_id, digest=digest, limit=limit, offset=offset
    )


async def read_schedule(db, source_id, *, lock=False):
    return await _load(
        db, DatasetSourceSyncSchedule, _READ_SCHEDULE, source_id=source_id, lock=lock
    )


async def configure_sync(
    db,
    source_id,
    payload,
    actor_id,
    key_id,
    proof,
    anchor,
    maintenance_revision,
    lifecycle_revision,
    stamp,
):
    return await _scalar(
        db,
        _CONFIGURE_SYNC,
        source_id=source_id,
        revision=payload.expected_revision,
        actor_id=actor_id,
        key_id=key_id,
        key_proof=proof,
        anchor=json.dumps(anchor) if anchor is not None else None,
        enabled=payload.enabled,
        interval=payload.interval_minutes,
        policy=payload.new_file_policy,
        timezone=payload.timezone,
        maintenance_revision=maintenance_revision,
        lifecycle_revision=lifecycle_revision,
        stamp=stamp,
    )


async def continue_sync(db, source_id, payload, stamp):
    return await _scalar(
        db,
        _CONTINUE_SYNC,
        source_id=source_id,
        revision=payload.expected_revision,
        scan_id=payload.scan_id,
        stamp=stamp,
    )


async def due_sources(db, stamp):
    return (await _execute(db, _DUE_SOURCES, stamp=stamp)).scalars().all()


async def claim_sync(db, source_id, token, stamp):
    return await _scalar(db, _CLAIM_SYNC, source_id=source_id, token=token, stamp=stamp)


async def fail_sync(db, source_id, revision, token, code, block, stamp):
    await _execute(
        db,
        _FAIL_SYNC,
        source_id=source_id,
        revision=revision,
        token=token,
        code=code,
        block=block,
        stamp=stamp,
    )


async def settle_sync(db, row, outcome, stamp, *, run_id=None, error=None):
    return await _scalar(
        db,
        _SETTLE_SYNC,
        source_id=row.source_id,
        revision=row.revision,
        token=row.dispatch_token,
        outcome=outcome,
        run_id=run_id,
        error=error,
        stamp=stamp,
    )
