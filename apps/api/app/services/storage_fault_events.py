"""Safe A3 history and ordered sparse source-fault transitions."""

from contextlib import asynccontextmanager
import asyncio
import logging
import re
import uuid

from sqlalchemy import insert, select, text

from app.db.models.audit_log import AuditLog
from app.db.models.user import User
from app.middleware.request_id import request_id_var
from app.services import storage_access

log = logging.getLogger(__name__)
ACTION = "storage.fault"
IDENTIFIERS = (
    "location_id",
    "source_id",
    "dataset_id",
    "item_id",
    "asset_id",
    "task_id",
    "run_id",
    "job_id",
    "registration_id",
)
OPERATIONS = {"scan", "scan_content", "reference_read", "preview_generate"}
CATEGORIES = {"root_unavailable", "access_denied", "source_changed", "preview_failed"}
FAULT_CODES = {
    "root_not_configured": "root_unavailable",
    "root_identity_changed": "root_unavailable",
    "permission_denied": "access_denied",
    "source_changed": "source_changed",
    "content_mismatch": "source_changed",
    "reference_content_changed": "source_changed",
    "preview_generation_failed": "preview_failed",
}


def identifier(value):
    if value is None:
        return None
    return str(uuid.UUID(str(value)))


def classify(exc, *, preview=False):
    if preview:
        return "preview_failed", "preview_generation_failed"
    category = getattr(exc, "storage_fault_category", None)
    if category in CATEGORIES:
        # Provider details remain private, including rejected access keys.
        return category, category
    code = getattr(exc, "code", None)
    category = FAULT_CODES.get(code)
    return (category, code) if category else None


async def _persist(db, detail, actor_id, request_id):
    # Use this caller's engine: API and each Worker may serve different modes.
    # An observation must neither commit nor roll back the business transaction.
    async with storage_access.engine(db).begin() as connection:
        await connection.execute(text("SET LOCAL lock_timeout = '250ms'"))
        await connection.execute(text("SET LOCAL statement_timeout = '1s'"))
        await connection.execute(
            insert(AuditLog).values(
                actor_id=select(User.id).where(User.id == actor_id).scalar_subquery()
                if actor_id
                else None,
                action=ACTION,
                target_type="storage_location",
                target_id=detail["location_id"],
                request_id=request_id,
                detail_json=detail,
            )
        )


async def report(
    db, exc, *, operation, actor_id=None, preview=False, observation=None, **identifiers
):
    """Call only with identities already resolved under the caller's authority."""
    try:
        fault = classify(exc, preview=preview)
        if fault is None or operation not in OPERATIONS:
            return
        detail = {
            key: identifier(identifiers[key])
            for key in IDENTIFIERS
            if identifiers.get(key) is not None
        }
        if "location_id" not in detail:
            return
        category, code = fault
        actor_id = identifier(actor_id)
        detail.update(category=category, code=code, operation=operation)
        if actor_id:
            detail["actor_id"] = actor_id
        request_id = request_id_var.get() or None
        if request_id and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,36}", request_id):
            request_id = None
        if observation is not None:
            from app.services import storage_fault_state

            if await storage_fault_state.record(db, observation, fault):
                return
        async with asyncio.timeout(2):
            await _persist(db, detail, actor_id, request_id)
    except Exception:
        # Do not log exception text, paths or provider responses on this path.
        log.warning("Storage fault observation could not be persisted")


@asynccontextmanager
async def observe(
    db, *, operation, actor_id=None, observation=None, validate=None, **identifiers
):
    try:
        yield
    except Exception as exc:
        permitted = True
        if validate is not None:
            try:
                await validate()
            except Exception:
                permitted = False
        if permitted:
            await report(
                db,
                exc,
                operation=operation,
                actor_id=actor_id,
                observation=observation,
                **identifiers,
            )
        raise


async def _prepare(
    db, *, location=None, source=None, operation, actor_id=None, **values
):
    from app.services import storage_fault_state

    location_id, source_id = (
        values.pop("location_id", None),
        values.pop("source_id", None),
    )
    asset_id = values.get("asset_id")
    evidence = {}
    for key in IDENTIFIERS:
        value = values.pop(key, None)
        if value is not None:
            evidence[key] = identifier(value)
    if source is not None:
        evidence.setdefault("dataset_id", identifier(source.dataset_id))
    request_id = request_id_var.get() or None
    if request_id and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,36}", request_id):
        request_id = None
    if request_id is not None:
        evidence["request_id"] = request_id
    return await storage_fault_state.prepare(
        db,
        location=location,
        source=source,
        location_id=location_id,
        source_id=source_id,
        operation=operation,
        asset_id=asset_id,
        actor_id=identifier(actor_id),
        request_id=request_id,
        evidence=evidence,
        **values,
    )


async def prepare(db, **values):
    try:
        return await _prepare(db, **values)
    except Exception:
        log.warning("Storage fault observation could not be prepared")
        return None


async def recover(db, observation):
    from app.services import storage_fault_state

    await storage_fault_state.record(db, observation)
