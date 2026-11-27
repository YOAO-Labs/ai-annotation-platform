"""Retire one pinned fixture media output and verify physical settlement."""

from __future__ import annotations

import asyncio
import builtins
import uuid
from contextvars import copy_context
from functools import partial
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import storage_access
from app.services.content_snapshot import ContentError


async def _physical(call, *args):
    try:
        pending = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, partial(call, *args)
        )
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)
    try:
        return await asyncio.shield(pending)
    except BaseException as interrupted:
        try:
            await storage_access.finish(pending)
        except BaseException as late:
            if storage_access.failure_unsettled(late) and late is not interrupted:
                if isinstance(interrupted, asyncio.CancelledError):
                    storage_access.mark_unsettled(interrupted)
                    raise interrupted from late
                raise builtins.BaseExceptionGroup(
                    "Fixture media removal and native drain failed", [interrupted, late]
                ) from None
        raise


async def _retire_output(db, control, kind, identifier, remove):
    from app.services import media_cache_storage

    manifest = await control.output(kind, identifier)
    reference = manifest.get("storage_ref")
    if manifest.get("kind") != kind or manifest.get("id") != str(identifier):
        raise ContentError("fixture_media_owner_changed", 409)
    versions = manifest.get("versions")
    if not isinstance(versions, list):
        raise ContentError("fixture_media_version_unavailable", 503)
    try:
        version_ids = [uuid.UUID(value) for value in versions]
    except (TypeError, ValueError):
        raise ContentError("fixture_media_version_unavailable", 503) from None
    state = manifest.get("state")
    if state == "deleted":
        if not await control.output_done(kind, identifier):
            raise ContentError("fixture_media_output_unsettled", 409)
        return
    if state not in {"building", "ready", "failed", "retiring"}:
        raise ContentError("fixture_media_owner_changed", 409)
    if state != "retiring":
        await control.transition(kind, identifier, state, "retiring")
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    async with factory() as owner:
        if version_ids:
            saved = media_cache_storage.resolve_ref(reference)
            await storage_access.ensure(owner, saved.location_id)
            target = await media_cache_storage.target_for_ref(
                owner, reference, require_enabled=False
            )
            from app.services.saved_media_io import scoped_fixture_target

            cleanup = control.cleanup_target
            if (
                target.endpoint_url != cleanup["endpoint"]
                or target.bucket != cleanup["bucket"]
                or reference["storage_identity"] != cleanup["storage_identity"]
                or cleanup["run_id"] != str(control.run_id)
            ):
                raise ContentError("fixture_media_owner_changed", 409)
            target = await scoped_fixture_target(owner, target, cleanup)
            await owner.commit()
            permit = await control.acquire(kind, identifier)
            async with permit:
                if permit.location_id != saved.location_id:
                    raise ContentError("fixture_media_owner_changed", 409)
                await storage_access.check(
                    owner,
                    saved.location_id,
                    expected_revision=permit.revision,
                    lock=True,
                )
                await owner.commit()
                receipts = [
                    await control.version(version_id, permit)
                    for version_id in version_ids
                ]
                if any(receipt.get("storage_ref") != reference for receipt in receipts):
                    raise ContentError("fixture_media_owner_changed", 409)
                output = SimpleNamespace(id=identifier, storage_ref=reference)
                await _physical(remove, target, output, (control, permit, receipts))
        if not await control.output_done(kind, identifier):
            raise ContentError("fixture_media_output_unsettled", 409)
        await control.transition(kind, identifier, "retiring", "deleted")
