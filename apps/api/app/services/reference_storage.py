"""Credential-free API adapter for Source-owned registered references."""

from contextlib import contextmanager
from dataclasses import dataclass
import json
from uuid import UUID

from sqlalchemy import text

from app.config import settings
from app.services import filesystem_storage as fs, storage_access
from app.services.saved_io import saved_io_client
from app.services.storage_registry_data import get_connection
from yoao_assets_source.reference_protocol import (
    ReferenceError,
    qualified_sftp_root,
    s3_anchor,
    sftp_anchor,
)
from yoao_assets_source.saved_io import SavedIOError

_PREPARE = text(
    "SELECT yoao_assets_v1.prepare_reference_io("
    ":permit, :action, :actor, :key, :proof, CAST(:request AS jsonb))"
)


def error(exc):
    code = exc.code
    if exc.unsettled:
        code = "storage_operation_unsettled"
    elif code in {"reference_io_proof_invalid", "target_unproven"}:
        code = "configuration_changed"
    failure = fs.FilesystemStorageError(code)
    if exc.unsettled:
        failure.status = 503
        storage_access.mark_unsettled(failure)
    else:
        failure.status = 409 if code == "configuration_changed" else exc.status_code
    if exc.storage_code == "AccessDenied":
        failure.storage_fault_category = "access_denied"
    return failure


@dataclass(frozen=True, slots=True)
class RegisteredReference:
    location_id: UUID
    location_revision: int
    connection_revision: int
    anchor: dict

    async def _prepare(self, db, permit, action, actor_id, key_id, key_proof, request):
        async with storage_access.engine(db).begin() as connection:
            ok = await connection.scalar(
                _PREPARE,
                {
                    "permit": permit.id,
                    "action": action,
                    "actor": actor_id,
                    "key": UUID(str(key_id)) if key_id else None,
                    "proof": key_proof,
                    "request": json.dumps(
                        {
                            **request,
                            "location_revision": self.location_revision,
                            "connection_revision": self.connection_revision,
                        }
                    ),
                },
            )
        if not ok:
            raise fs.FilesystemStorageError("configuration_changed")
        return {"permit_id": str(permit.id)}

    async def command(
        self,
        db,
        action,
        request,
        *,
        actor_id,
        key_id,
        key_proof,
        maintenance_revision,
        node_id,
        timeout=None,
    ):
        subject = None
        purpose = "registration_directory" if action == "directory" else action
        if "\x00" in (request.get("path") or ""):
            code = (
                "invalid_relative_path"
                if self.anchor["kind"] == "sftp"
                else "invalid_s3_prefix"
            )
            raise fs.FilesystemStorageError(code)

        async def admit(permit):
            nonlocal subject
            subject = await self._prepare(
                db, permit, action, actor_id, key_id, key_proof, request
            )

        def invoke():
            try:
                return saved_io_client("read").command("reference", action, subject)
            except SavedIOError as exc:
                raise error(exc) from None

        return await storage_access.run(
            db,
            self.location_id,
            purpose,
            invoke,
            expected_revision=maintenance_revision,
            node_id=node_id,
            timeout=timeout,
            on_acquired=admit,
        )

    async def capture(
        self,
        db,
        action,
        request,
        expected_sha256,
        *,
        actor_id,
        key_id,
        key_proof,
        maintenance_revision,
        node_id,
        on_acquired,
        inspect_quality=False,
    ):
        from app.services import content_snapshot as snapshots

        purpose = (
            "scan_content"
            if action == "observation"
            else "quality_read"
            if inspect_quality
            else "reference_read"
        )
        snapshot = None
        try:
            async with await storage_access.acquire(
                db,
                self.location_id,
                purpose,
                expected_revision=maintenance_revision,
                node_id=node_id,
            ) as permit:
                await on_acquired()
                subject = await self._prepare(
                    db, permit, action, actor_id, key_id, key_proof, request
                )

                @contextmanager
                def content():
                    try:
                        with saved_io_client("read").download(
                            "reference", action, subject
                        ) as response:
                            receipt = response.metadata
                            if (
                                not isinstance(receipt.get("observation"), dict)
                                or type(receipt.get("size")) is not int
                                or receipt["observation"].get("size") != receipt["size"]
                                or receipt.get("offset") != 0
                            ):
                                raise snapshots.ContentError(
                                    "content_source_unavailable", 503
                                )
                            yield response, receipt["observation"]
                    except SavedIOError as exc:
                        if exc.code in {
                            "content_too_large",
                            "content_read_timeout",
                            "snapshot_capacity_exceeded",
                        }:
                            raise snapshots.ContentError(
                                exc.code, exc.status_code
                            ) from None
                        raise error(exc) from None

                capture = (
                    snapshots.capture_for_quality
                    if inspect_quality
                    else snapshots.capture
                )
                snapshot = await capture(content, expected_sha256)
            return snapshot
        except BaseException as exc:
            if snapshot is not None:
                snapshot.close()
            if isinstance(exc, Exception) and storage_access.failure_unsettled(exc):
                raise storage_access.mark_unsettled(
                    snapshots.ContentError("storage_operation_unsettled", 503)
                ) from None
            raise


def original_request(context, task_id, scope):
    return {
        "item_id": str(context["item_id"]),
        "asset_id": str(context["asset_id"]),
        "revision_id": str(context["accepted_revision_id"])
        if context["accepted_revision_id"]
        else None,
        "task_id": str(task_id) if task_id else None,
        "scope": scope,
        "sha256": context["sha256"],
        "source_revision": context["source_revision"],
        "run_id": str(context["run_id"]),
        "path_digest": context["path_digest"],
    }


def sftp_location_anchor(config):
    roots = {
        handle: root.model_dump()
        for handle, root in settings.storage_sftp_roots.items()
    }
    return sftp_anchor(*qualified_sftp_root(config, roots))


async def configuration(db, location):
    if location.kind == "agent":
        from app.services.agent_storage import configuration as agent_configuration

        return await agent_configuration(db, location)
    if location.kind == "filesystem":
        return dict(location.config)
    if location.kind not in {"s3", "sftp"} or not location.enabled:
        raise fs.FilesystemStorageError("reference_backend_not_supported")
    connection = await get_connection(db, location.connection_id)
    if (
        connection is None
        or connection.kind != location.kind
        or not connection.secret_set
    ):
        raise fs.FilesystemStorageError("connection_unavailable")
    try:
        anchor = (
            sftp_location_anchor(connection.config)
            if location.kind == "sftp"
            else s3_anchor(connection.config)
        )
    except ReferenceError as exc:
        raise error(exc) from None
    return RegisteredReference(
        location.id, location.config_revision, connection.revision, anchor
    )
