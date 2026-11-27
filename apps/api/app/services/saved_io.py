"""Ordinary consumer selectors and reviewed managed-target probe admission."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from yoao_assets_source.saved_io import (
    READ_WRITE_SCOPES,
    SavedIOClient,
    SavedIOError,
)

from app.config import assets_source_scoped_token, settings
from app.services import storage_access

_BEGIN_PROBE = text("""
    SELECT yoao_assets_v1.begin_saved_binding_probe(
        CAST(:operation AS uuid), CAST(:actor AS uuid), CAST(:key AS uuid),
        CAST(:authority AS text), CAST(:purpose AS text), CAST(:location AS uuid),
        CAST(:config AS integer), CAST(:connection AS integer), CAST(:generation AS integer)
    )
""")


def saved_io_client(access, *, timeout=None) -> SavedIOClient:
    if access not in READ_WRITE_SCOPES:
        raise ValueError("Invalid ordinary Source access scope")
    return SavedIOClient(
        settings.assets_source_url,
        assets_source_scoped_token(access),
        timeout=settings.assets_source_timeout_seconds if timeout is None else timeout,
    )


async def probe_binding(db, target, initial, legacy_probe, *, permit_purpose):
    """Preserve legacy probes and the caller's preview/re-lock transaction order."""
    if not target.is_registered:
        return await storage_access.run(
            db,
            target.location_id,
            permit_purpose,
            legacy_probe,
            target,
            expected_revision=initial["maintenance_revision"],
        )

    from app.services import managed_s3_target

    operation = None
    client = saved_io_client("write")

    async def admit(permit):
        nonlocal operation
        try:
            async with storage_access.engine(db).begin() as connection:
                accepted = await connection.scalar(
                    _BEGIN_PROBE,
                    {
                        "operation": permit.id,
                        "actor": UUID(initial["actor"]),
                        "key": UUID(initial["key"])
                        if initial["key"] is not None
                        else None,
                        "authority": initial["authority"],
                        "purpose": target.purpose,
                        "location": target.location_id,
                        "config": target.config_revision,
                        "connection": target.connection_revision,
                        "generation": initial["current"]["generation"],
                    },
                )
        except DBAPIError as exc:
            if storage_access.unavailable(exc):
                raise managed_s3_target.error(
                    target.purpose, "binding_unproven"
                ) from None
            raise
        if accepted is not True:
            raise managed_s3_target.error(target.purpose, "binding_unproven")
        operation = permit.id

    def invoke():
        try:
            return client.command("binding", "probe", {"operation_id": str(operation)})
        except SavedIOError as exc:
            suffix = {
                "target_unproven": "binding_unproven",
                "probe_scope_unproven": "binding_unproven",
                "storage_invalid_receipt": "invalid_receipt",
                "conditions_unsupported": "conditions_unsupported",
                "probe_cleanup_unproven": "probe_cleanup_unproven",
                "versioning_required": "versioning_required",
            }.get(exc.code, "unavailable")
            failure = managed_s3_target.error(target.purpose, suffix)
            if exc.unsettled:
                storage_access.mark_unsettled(failure)
            raise failure from None

    return await storage_access.run(
        db,
        target.location_id,
        permit_purpose,
        invoke,
        expected_revision=initial["maintenance_revision"],
        on_acquired=admit,
    )
