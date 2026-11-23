"""Retire owned export media independently of Jobs, caches and ZIP success.

Publication is a permanent delivery latch. StorageOperation has no age-based
escape hatch: an unresolved physical operation always retains responsibility.
"""

import logging
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.db.models.async_job import AsyncJob
from app.db.models.export_media_bundle import ExportMediaBundle as Bundle
from app.services import export_delivery_data as data
from app.services import export_media_bundles as bundles
from app.services import export_media_removal_accounting as removal
from app.services import export_media_storage as physical
from app.services import export_media_transfer as transfer
from app.services import export_storage, storage_access
from app.services import key_removal_accounting as key_removal
from app.services import object_removal_accounting as version_removal
from app.services.content_snapshot import ContentError

log = logging.getLogger(__name__)
_TERMINAL = {"completed", "failed", "cancelled"}
_FIELDS = (
    "id",
    "project_id",
    "job_id",
    "zip_output_id",
    "fixture_run_id",
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


async def fixture_owner_session(db):
    """Check the authenticated owner, not a caller-controlled current role."""
    return bool(
        await db.scalar(
            text(
                "SELECT pg_catalog.pg_has_role(SESSION_USER, "
                "pg_catalog.pg_get_userbyid(c.relowner), 'MEMBER') "
                "FROM pg_catalog.pg_class AS c "
                "WHERE c.oid = CAST("
                "'public.fixture_export_zip_retirements' AS pg_catalog.regclass)"
            )
        )
    )


def reject():
    raise ContentError("export_media_retirement_changed")


def purposes(owner):
    return (
        "export_media_" + owner.id.hex,
        "export_write_" + owner.zip_output_id.hex,
        "export_read_" + owner.zip_output_id.hex,
    )


async def has_operations(db, owner, *, ignore=None):
    return bool(await data.media_has_operations(db, owner.id, ignore))


async def attempt_ended(db, owner, *, current=None):
    current = current or now()
    if owner.published_at is not None:
        return owner.expires_at <= current
    if owner.expires_at <= current or (
        owner.lease_expires_at is not None and owner.lease_expires_at <= current
    ):
        return True
    return bool(await data.media_attempt_ended(db, owner.id, current))


async def lock_owner(db, identifier, *, inventory=False):
    return await data.lock_media_retirement(db, identifier, inventory=inventory)


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


def snapshot(row, *, retiring_at=None):
    facts = {
        name: physical.input_schema(row)
        if name == "input_schema"
        else deepcopy(getattr(row, name))
        for name in _VALUES
    }
    if retiring_at is not None:
        facts.update(state="retiring", retired_at=retiring_at)
    result = Receipt(
        tuple(facts.values()),
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
            from app.services import export_outputs

            output = await data.zip_receipt(owned, zip_output.id, mode="retirement")
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


async def _physical_delete(db, receipt, *, zip_output=None, zip_proof=None):
    if receipt.fixture_run_id is not None and not await fixture_owner_session(db):
        raise ContentError("fixture_export_owner_required")
    if (
        receipt.fixture_run_id is not None
        and zip_output is not None
        and zip_proof is None
    ):
        raise ContentError("fixture_export_zip_proof_required")
    ref = zip_output.storage_ref if zip_output is not None else receipt.storage_ref
    permit = await transfer._acquire(db, receipt, uuid.UUID(ref["location_id"]))
    failure, accounting = None, None
    try:
        await transfer._async(_guard(db, receipt, permit, zip_output=zip_output))
        if zip_output is None:
            target = await transfer._async(transfer._io_target(db, receipt))
            accounting = removal.recorder(db, receipt)
            versions = await bundles.member_versions(
                db, receipt.id, physical.resolve(receipt)
            )

            def remove():
                with physical.open_storage(
                    receipt,
                    owned=True,
                    **({"target": target} if target is not None else {}),
                ) as client:
                    return physical.delete_inventory(
                        client,
                        receipt,
                        accounting=accounting,
                        **({"member_versions": versions} if versions else {}),
                    )

            await transfer.physical_work(remove)
        else:
            async with transfer._session(db) as owned:
                target = await export_storage.io_target(owned, zip_output)
            recorder = (
                version_removal
                if zip_proof is not None and zip_proof["version_id"] is not None
                else key_removal
            )
            accounting = recorder.recorder(
                db, zip_output, producer="export_zip_removals", purpose="temporary"
            )
            await transfer.physical_work(
                export_storage.delete,
                zip_output,
                accounting=accounting,
                **({"proof": zip_proof} if zip_proof is not None else {}),
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
                elif not isinstance(closing, Exception):
                    transfer._uncertainty(closing, failure)
                    failure = closing
                else:
                    transfer._uncertainty(failure, closing)
        if accounting is not None:
            failure = await removal.finish(
                accounting, failure, settled=getattr(permit, "_settled", False) is True
            )
    if failure is not None:
        raise failure


async def delete_zip(db, owner, output, *, proof=None):
    """Drain linked ZIP deletion without depending on media deletion success."""
    await _physical_delete(db, owner, zip_output=output, zip_proof=proof)


async def _cleanup_one(db, identifier):
    row = await lock_owner(db, identifier, inventory=True)
    if row is None or row.state == "deleted":
        await db.commit()
        return False
    if row.fixture_run_id is not None and not await fixture_owner_session(db):
        await db.commit()
        return False
    retired_at = await data.start_media_retirement(db, identifier, now())
    if retired_at is None:
        await db.commit()
        return False
    await db.commit()
    receipt = snapshot(row, retiring_at=retired_at)
    await _physical_delete(db, receipt)
    current = await lock_owner(db, identifier, inventory=True)
    if current is None or current.state != "retiring" or snapshot(current) != receipt:
        await db.commit()
        return False
    if not await data.finish_media_retirement(db, identifier, now()):
        await db.commit()
        return False
    await db.commit()
    return True


async def retire_fixture_export(db, *, run_id, project_id, job_id):
    """Retire one sealed fixture Job's exact independently owned delivery."""
    if not await fixture_owner_session(db):
        raise ContentError("fixture_export_owner_required")
    from app.db.models.export_output import ExportOutput
    from app.db.models.fixture_run import FixtureRun, FixtureRunResource
    from app.services import export_outputs

    scope = await db.scalar(
        select(FixtureRun.id)
        .join(FixtureRunResource, FixtureRunResource.run_id == FixtureRun.id)
        .where(
            FixtureRun.id == run_id,
            FixtureRun.state == "sealing",
            FixtureRunResource.kind == "project",
            FixtureRunResource.resource_id == project_id,
            FixtureRunResource.state == "published",
        )
    )
    job = await db.get(AsyncJob, job_id, populate_existing=True)
    if (
        scope is None
        or job is None
        or job.kind != "export"
        or job.project_id != project_id
    ):
        raise ContentError("fixture_export_owner_changed")
    if job.status not in _TERMINAL:
        await db.commit()
        return False
    owners = (
        await db.execute(
            select(Bundle.id, Bundle.fixture_run_id, Bundle.project_id)
            .where(Bundle.job_id == job_id)
            .order_by(Bundle.id)
        )
    ).all()
    native = await export_outputs.fixture_native_lifetime(db, job_id, project_id)
    if (not owners and (native is None or native.run_id != run_id)) or (
        owners
        and (
            native is not None
            or any(
                owner.fixture_run_id != run_id or owner.project_id != project_id
                for owner in owners
            )
        )
    ):
        raise ContentError("fixture_export_owner_changed")
    identifiers = [owner.id for owner in owners]
    fenced = await db.scalar(
        text(
            "SELECT public.retire_fixture_export_execution(:run_id, :project_id, :job_id)"
        ),
        {"run_id": run_id, "project_id": project_id, "job_id": job_id},
    )
    if not fenced:
        await db.commit()
        return False
    await db.commit()
    if native is not None:
        if native.expires_at > now():
            return False
        outputs = list(
            await db.scalars(
                select(ExportOutput.id)
                .where(ExportOutput.job_id == job_id)
                .order_by(ExportOutput.id)
            )
        )
        await db.commit()
        for output_id in outputs:
            if not await export_outputs._cleanup_one(db, output_id, {"removed": 0}):
                return False
        proof = await db.scalar(
            text("SELECT public.fixture_export_retired(:run_id, :project_id, :job_id)"),
            {"run_id": run_id, "project_id": project_id, "job_id": job_id},
        )
        await db.commit()
        return proof is True
    for identifier in identifiers:
        owner = await db.get(Bundle, identifier, populate_existing=True)
        if owner is None or owner.expires_at > now():
            await db.commit()
            return False
        output_id = owner.zip_output_id
        output = await db.get(ExportOutput, output_id, populate_existing=True)
        if output is not None and output.media_bundle_id != identifier:
            raise ContentError("fixture_export_owner_changed")
        await db.commit()
        if output is not None:
            if not await export_outputs._cleanup_one(db, output_id, {"removed": 0}):
                return False
        owner = await db.get(Bundle, identifier, populate_existing=True)
        if owner is None:
            raise ContentError("fixture_export_owner_changed")
        if owner.state != "deleted":
            await db.commit()
            if not await _cleanup_one(db, identifier):
                return False
        owner = await db.get(Bundle, identifier, populate_existing=True)
        output = await db.get(ExportOutput, output_id, populate_existing=True)
        if output is not None:
            await db.commit()
            if not await export_outputs._cleanup_one(db, output_id, {"removed": 0}):
                return False
            owner = await db.get(Bundle, identifier, populate_existing=True)
        if owner is None or owner.state != "deleted" or await has_operations(db, owner):
            await db.commit()
            return False
        if owner.fixture_zip_created:
            recorded = await db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM public.fixture_export_zip_retirements "
                    "WHERE bundle_id=:bundle_id AND output_id=:output_id "
                    "AND fixture_run_id=:run_id AND project_id=:project_id AND job_id=:job_id)"
                ),
                {
                    "bundle_id": identifier,
                    "output_id": output_id,
                    "run_id": run_id,
                    "project_id": project_id,
                    "job_id": job_id,
                },
            )
            if not recorded:
                await db.commit()
                return False
        await db.commit()
    retired = await db.scalar(
        text("SELECT public.fixture_export_retired(:run_id, :project_id, :job_id)"),
        {"run_id": run_id, "project_id": project_id, "job_id": job_id},
    )
    execution_retired = await db.scalar(
        text(
            "SELECT public.fixture_export_execution_retired(:run_id, :project_id, :job_id)"
        ),
        {"run_id": run_id, "project_id": project_id, "job_id": job_id},
    )
    await db.commit()
    return retired is True and execution_retired is True


async def cleanup(db, *, limit=100):
    limit = max(1, min(limit, 100))
    identifiers = await data.media_retirement_queue(db, now(), limit)
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
