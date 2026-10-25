"""Retire owned export media independently of Jobs, caches and ZIP success.

Publication is a permanent delivery latch. StorageOperation has no age-based
escape hatch: an unresolved physical operation always retains responsibility.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
import uuid

from sqlalchemy import Text, and_, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased, load_only

from app.db.models.async_job import AsyncJob
from app.db.models.export_media_bundle import ExportMediaBundle as Bundle
from app.db.models.storage_maintenance import StorageOperation
from app.services import export_media_storage as physical
from app.services import export_media_transfer as transfer
from app.services import export_storage, storage_access
from app.services.content_snapshot import ContentError

log = logging.getLogger(__name__)
_TERMINAL = {"completed", "failed", "cancelled"}
_SHA = re.compile(r"[0-9a-f]{64}")
_FIELDS = (
    "id",
    "project_id",
    "job_id",
    "zip_output_id",
    "input_digest",
    "input_schema",
    "object_count",
    "size_bytes",
    "storage_ref",
    "state",
    "lease_token",
    "lease_expires_at",
    "created_at",
    "expires_at",
    "published_at",
    "retired_at",
)
_VALUES = tuple(name for name in _FIELDS if name != "storage_ref")


def now():
    return datetime.now(timezone.utc)


def reject():
    raise ContentError("export_media_retirement_changed")


def purposes(owner):
    return (
        "export_media_" + owner.id.hex,
        "export_write_" + owner.zip_output_id.hex,
        "export_read_" + owner.zip_output_id.hex,
    )


def _operations(owner, *, ignore=None):
    if owner is Bundle:
        media = "export_media_" + func.replace(cast(Bundle.id, Text), "-", "")
        zip_id = func.replace(cast(Bundle.zip_output_id, Text), "-", "")
        names = (media, "export_write_" + zip_id, "export_read_" + zip_id)
    else:
        names = purposes(owner)
    query = select(StorageOperation.id).where(StorageOperation.purpose.in_(names))
    if ignore is not None:
        query = query.where(StorageOperation.id != ignore)
    return query.exists()


async def has_operations(db, owner, *, ignore=None):
    return bool(await db.scalar(select(_operations(owner, ignore=ignore))))


def _replacement_query():
    replacement = aliased(Bundle)
    proofs = []
    keys = {"lidar_media", "image_media", "video_media"}
    for key, schema in (
        ("lidar_media", 1),
        ("lidar_media", 2),
        ("image_media", 3),
        ("video_media", 4),
    ):
        proof = func.jsonb_build_object(
            "schema",
            schema,
            "bundle_id",
            cast(replacement.id, Text),
            "input_digest",
            replacement.input_digest,
        )
        proofs.append(
            and_(
                replacement.input_snapshot["schema"] == literal(schema, type_=JSONB),
                AsyncJob.payload[key] == proof,
                *(~AsyncJob.payload.has_key(other) for other in sorted(keys - {key})),
            )
        )
    return (
        select(replacement.id)
        .join(AsyncJob, AsyncJob.id == replacement.job_id)
        .where(
            AsyncJob.id == Bundle.job_id,
            AsyncJob.kind == "export",
            AsyncJob.project_id == Bundle.project_id,
            replacement.id != Bundle.id,
            replacement.project_id == Bundle.project_id,
            or_(*proofs),
        )
        .correlate(Bundle)
        .exists()
    )


def _ended_predicate(current):
    job = select(AsyncJob.id).where(AsyncJob.id == Bundle.job_id)
    ended = or_(
        Bundle.expires_at <= current,
        Bundle.lease_expires_at <= current,
        ~job.exists(),
        job.where(AsyncJob.status.in_(_TERMINAL)).exists(),
        _replacement_query(),
    )
    return or_(
        and_(Bundle.published_at.is_not(None), Bundle.expires_at <= current),
        and_(Bundle.published_at.is_(None), ended),
    )


def candidate_predicate(current):
    return and_(
        Bundle.state != "deleted", ~_operations(Bundle), _ended_predicate(current)
    )


def pending_zip_predicate(current):
    from app.db.models.export_output import ExportOutput

    return (
        select(Bundle.id)
        .where(
            Bundle.id == ExportOutput.media_bundle_id,
            Bundle.published_at.is_(None),
            _ended_predicate(current),
            ~_operations(Bundle),
        )
        .exists()
    )


def zip_operations_predicate():
    from app.db.models.export_output import ExportOutput

    return (
        select(Bundle.id)
        .where(
            Bundle.id == ExportOutput.media_bundle_id,
            _operations(Bundle),
        )
        .exists()
    )


async def attempt_ended(db, owner, *, current=None):
    current = current or now()
    if owner.published_at is not None:
        return owner.expires_at <= current
    if owner.expires_at <= current or (
        owner.lease_expires_at is not None and owner.lease_expires_at <= current
    ):
        return True
    job = await db.get(AsyncJob, owner.job_id, populate_existing=True)
    if job is None or job.status in _TERMINAL:
        return True
    payload = job.payload if type(job.payload) is dict else {}
    protocols = {"lidar_media": {1, 2}, "image_media": {3}, "video_media": {4}}
    keys = set(payload).intersection(protocols)
    if len(keys) != 1:
        return False
    key = keys.pop()
    proof, schemas = payload[key], protocols[key]
    if (
        job.kind != "export"
        or job.project_id != owner.project_id
        or type(proof) is not dict
        or set(proof) != {"schema", "bundle_id", "input_digest"}
        or type(proof["schema"]) is not int
        or proof["schema"] not in schemas
        or type(proof["bundle_id"]) is not str
        or type(proof["input_digest"]) is not str
        or _SHA.fullmatch(proof["input_digest"]) is None
    ):
        return False
    try:
        identifier = uuid.UUID(proof["bundle_id"])
    except ValueError:
        return False
    if str(identifier) != proof["bundle_id"] or identifier == owner.id:
        return False
    replacement = (
        await db.execute(
            select(
                Bundle.id,
                Bundle.project_id,
                Bundle.job_id,
                Bundle.input_digest,
                Bundle.input_snapshot["schema"].label("input_schema"),
            ).where(Bundle.id == identifier)
        )
    ).one_or_none()
    return replacement is not None and (
        replacement.project_id == owner.project_id
        and replacement.job_id == owner.job_id
        and replacement.input_digest == proof["input_digest"]
        and type(replacement.input_schema) is int
        and replacement.input_schema == proof["schema"]
    )


async def lock_owner(db, identifier, *, inventory=False):
    fields = (*_FIELDS, "inventory") if inventory else _FIELDS
    return await db.scalar(
        select(Bundle)
        .options(load_only(*(getattr(Bundle, name) for name in fields)))
        .where(Bundle.id == identifier)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )


async def zip_eligible(db, owner, output, *, ignore=None):
    """The caller locks Bundle before Output; no live operation may be guessed away."""
    if owner is None or (
        output.media_bundle_id != owner.id
        or output.id != owner.zip_output_id
        or output.project_id != owner.project_id
        or output.job_id != owner.job_id
        or output.expires_at != owner.expires_at
        or output.media != ("video" if physical.input_schema(owner) == 4 else "image")
    ):
        return False
    if await has_operations(db, owner, ignore=ignore):
        return False
    return await attempt_ended(db, owner)


@dataclass(frozen=True)
class Receipt:
    facts: tuple
    _storage_ref: dict
    _inventory: dict

    def __getattr__(self, name):
        if name in _VALUES:
            return self.facts[_VALUES.index(name)]
        raise AttributeError(name)

    @property
    def storage_ref(self):
        return deepcopy(self._storage_ref)

    @property
    def inventory(self):
        return deepcopy(self._inventory)


def snapshot(row):
    result = Receipt(
        tuple(
            physical.input_schema(row)
            if name == "input_schema"
            else deepcopy(getattr(row, name))
            for name in _VALUES
        ),
        deepcopy(row.storage_ref),
        deepcopy(row.inventory),
    )
    objects = physical.validate_inventory(result)
    if result.object_count != len(objects) or result.size_bytes != sum(
        entry["size_bytes"] for entry in objects.values()
    ):
        reject()
    return result


async def _guard(db, receipt, permit, *, zip_output=None):
    async with transfer._session(db) as owned:
        row = await lock_owner(owned, receipt.id, inventory=zip_output is None)
        if row is None or await has_operations(owned, row, ignore=permit.id):
            reject()
        if zip_output is None:
            if row.state != "retiring" or snapshot(row) != receipt:
                reject()
            if row.published_at is not None and row.expires_at > now():
                reject()
        else:
            from app.db.models.export_output import ExportOutput
            from app.services import export_outputs

            output = await owned.scalar(
                select(ExportOutput)
                .where(ExportOutput.id == zip_output.id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if (
                output is None
                or output.state != "retired"
                or not export_outputs.same(output, zip_output)
            ):
                reject()
            if not await zip_eligible(owned, row, output, ignore=permit.id):
                reject()
        await storage_access.check(
            owned, permit.location_id, expected_revision=permit.revision
        )


async def _physical_delete(db, receipt, *, zip_output=None):
    ref = zip_output.storage_ref if zip_output is not None else receipt.storage_ref
    permit = await transfer._acquire(db, receipt, uuid.UUID(ref["location_id"]))
    failure = None
    try:
        await transfer._async(_guard(db, receipt, permit, zip_output=zip_output))
        if zip_output is None:
            target = await transfer._async(transfer._io_target(db, receipt))

            def remove():
                with physical.open_storage(
                    receipt, **({"target": target} if target is not None else {})
                ) as client:
                    return physical.delete_inventory(client, receipt)

            await transfer.physical_work(remove)
        else:
            async with transfer._session(db) as owned:
                target = await export_storage.io_target(owned, zip_output)
            await transfer.physical_work(
                export_storage.delete,
                zip_output,
                **({"target": target} if target is not None else {}),
            )
        await transfer._async(_guard(db, receipt, permit, zip_output=zip_output))
    except BaseException as exc:
        failure = exc
    finally:
        if not getattr(failure, "storage_operation_unsettled", False):
            try:
                await transfer._settle(permit)
            except BaseException as closing:
                if failure is None:
                    failure = closing
                else:
                    transfer._uncertainty(failure, closing)
    if failure is not None:
        raise failure


async def delete_zip(db, owner, output):
    """Drain linked ZIP deletion without depending on media deletion success."""
    await _physical_delete(db, owner, zip_output=output)


async def _cleanup_one(db, identifier):
    row = await lock_owner(db, identifier, inventory=True)
    if row is None or row.state == "deleted":
        await db.commit()
        return False
    row.last_attempted_at = now()
    if await has_operations(db, row) or not await attempt_ended(db, row):
        await db.commit()
        return False
    if row.state != "retiring":
        row.state, row.retired_at = "retiring", now()
    # Retiring is durable before metadata validation, client construction or I/O.
    await db.commit()
    receipt = snapshot(row)
    await _physical_delete(db, receipt)
    current = await lock_owner(db, identifier, inventory=True)
    if current is None or current.state != "retiring" or snapshot(current) != receipt:
        await db.commit()
        return False
    if await has_operations(db, current) or (
        current.published_at is not None and current.expires_at > now()
    ):
        await db.commit()
        return False
    current.state = "deleted"
    await db.commit()
    return True


async def cleanup(db, *, limit=100):
    limit = max(1, min(limit, 100))
    identifiers = list(
        await db.scalars(
            select(Bundle.id)
            .where(candidate_predicate(now()))
            .order_by(
                Bundle.last_attempted_at.asc().nulls_first(),
                Bundle.created_at,
                Bundle.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    await db.commit()
    counts = {"deleted": 0, "retained": 0, "failed": 0}
    for identifier in identifiers:
        try:
            deleted = await _cleanup_one(db, identifier)
            counts["deleted" if deleted else "retained"] += 1
        except Exception:
            await db.rollback()
            counts["failed"] += 1
            log.warning("Export media retirement retained: %s", identifier)
    return counts
