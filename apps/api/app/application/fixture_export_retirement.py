"""Retire ZIP and media deliveries owned by a sealed fixture Job."""

from sqlalchemy import select, text

from app.db.models.async_job import AsyncJob
from app.db.models.export_media_bundle import ExportMediaBundle as Bundle
from app.db.models.export_output import ExportOutput
from app.db.models.fixture_run import FixtureRun, FixtureRunResource
from app.services import export_media_retirement, export_outputs
from app.services.content_snapshot import ContentError

_TERMINAL = {"completed", "failed", "cancelled"}


async def retire_fixture_export(db, *, run_id, project_id, job_id):
    """Retire one sealed fixture Job's exact independently owned delivery."""
    if not await export_media_retirement.fixture_owner_session(db):
        raise ContentError("fixture_export_owner_required")
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
        if native.expires_at > export_media_retirement.now():
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
        if owner is None or owner.expires_at > export_media_retirement.now():
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
            if not await export_media_retirement._cleanup_one(db, identifier):
                return False
        owner = await db.get(Bundle, identifier, populate_existing=True)
        output = await db.get(ExportOutput, output_id, populate_existing=True)
        if output is not None:
            await db.commit()
            if not await export_outputs._cleanup_one(db, output_id, {"removed": 0}):
                return False
            owner = await db.get(Bundle, identifier, populate_existing=True)
        if (
            owner is None
            or owner.state != "deleted"
            or await export_media_retirement.has_operations(db, owner)
        ):
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
