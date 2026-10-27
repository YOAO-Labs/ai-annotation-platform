"""Language-neutral upload and source-deletion composition for Assets."""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError

from aap_assets_source.contract import (
    SourceDeletionRequest,
    SourceFailure,
    UploadCompleteRequest,
    UploadInitRequest,
    UploadInitResult,
    UploadOutcome,
)
from aap_assets_source.deletion_contract import (
    DatasetSourceDeletionCommand,
    DatasetSourceDeletionFailure,
    DatasetSourceDeletionReceipt,
)
from aap_assets_source.legacy_upload_contract import (
    LegacyUploadCompletion,
    LegacyUploadDataPort,
    LegacyUploadLockRequest,
    LegacyUploadState,
)
from aap_assets_source.managed_original_contract import (
    ManagedOriginalFailure,
    ManagedOriginalFailureEvidence,
    observed_current_head_match,
)
from aap_assets_source.upload_contract import (
    DatasetUploadAcceptance,
    DatasetUploadCompletedResult,
    DatasetUploadCompletion,
    DatasetUploadDataPort,
    DatasetUploadDiscard,
    DatasetUploadDiscardedResult,
    DatasetUploadFailure,
    DatasetUploadIntent,
)
from aap_assets_images.contract import ImageFailure, ObjectIdentity

from .image_admission import ImageAdmissionPort, object_identity_from_head

logger = logging.getLogger("aap_assets_source_runtime.workflow")


class SourceDeletionDataPort(Protocol):
    """Caller-transaction binding for the native Assets delete capability."""

    async def delete(
        self, command: DatasetSourceDeletionCommand
    ) -> DatasetSourceDeletionReceipt: ...


class SourceStoragePort(Protocol):
    """Fixed-bucket object operations required by the source workflows."""

    async def generate_upload_url(self, key: str, content_type: str) -> str: ...

    async def verify_staging_upload(self, key: str) -> dict[str, Any] | None: ...

    async def verify_legacy_source(self, key: str) -> dict[str, Any] | None: ...

    async def read_staging_image_dimensions(
        self, key: str, *, if_match: str
    ) -> tuple[int, int] | None: ...

    async def read_legacy_image_dimensions(
        self, key: str, *, if_match: str
    ) -> tuple[int, int] | None: ...

    async def publish_managed_upload(
        self,
        target: Any,
        *,
        staging_key: str,
        staging_etag: str | None,
        staging_version_id: str | None,
        max_bytes: int,
        image: bool,
        content_type: str,
        on_prepared: Any = None,
        on_writing: Any = None,
        on_written: Any = None,
    ) -> Any: ...

    async def verify_managed_original(
        self,
        target: Any,
        *,
        sha256: str,
        size_bytes: int,
        image: bool = False,
    ) -> Any: ...


def _original_failure(code: str) -> str:
    return {
        "creation_scope_mismatch": "source_changed",
        "creation_configuration_changed": "storage_unavailable",
        "creation_target_unavailable": "storage_unavailable",
        "creation_key_conflict": "operation_conflict",
        "creation_missing": "operation_conflict",
        "creation_state_conflict": "operation_conflict",
        "creation_evidence_mismatch": "source_changed",
        "creation_terminal": "operation_conflict",
        "creation_budget_exceeded": "invalid_request",
        "creation_byte_budget_exceeded": "invalid_request",
        "operation_unsettled": "storage_unavailable",
        "resource_budget_exceeded": "invalid_request",
        "storage_unavailable": "storage_unavailable",
    }.get(code, "invalid_response")


def _safe_error_code(code: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", (code or "").lower()).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"upload_{cleaned or 'io_failed'}"
    return cleaned[:80]


def _managed_verification(proof) -> dict[str, Any]:
    """Verification evidence only after every physical handle has drained."""
    verification = {
        "sha256": proof.sha256,
        "md5": proof.md5,
        "size_bytes": proof.size_bytes,
        "etag": proof.etag,
        "version_id": proof.version_id,
        "observation": (proof.verification or {}).get("observation"),
        "physical_settled": True,
    }
    matched = observed_current_head_match(
        proof.verification, size_bytes=proof.size_bytes
    )
    if matched is not None:
        verification["current_head_matches"] = matched
    return verification


def _failure_evidence(
    stage: str,
    exc: BaseException,
    *,
    receipt: dict[str, Any] | None = None,
    observation: dict[str, Any] | None = None,
    sha256: str | None = None,
    md5: str | None = None,
    size_bytes: int | None = None,
    current_head_matches: bool | None = None,
) -> dict[str, Any]:
    uncertain = bool(
        getattr(exc, "storage_operation_unsettled", False)
        or getattr(exc, "outcome", None) == "unknown"
    )
    if uncertain:
        physical_outcome = "unknown"
    elif receipt is not None:
        physical_outcome = "written"
    else:
        physical_outcome = "not_written"
    return ManagedOriginalFailureEvidence.build(
        physical_settled=not uncertain,
        physical_outcome=physical_outcome,
        stage=stage,
        receipt=receipt,
        observation=observation,
        sha256=sha256,
        md5=md5,
        size_bytes=size_bytes,
        physical_size_bytes=size_bytes,
        current_head_matches=current_head_matches,
    )


def _upload_original_request(
    intent: DatasetUploadIntent,
    *,
    actor_id: UUID,
    dataset_id: UUID,
    item_id: UUID,
    staging_key: str,
    staging_etag: str | None = None,
    staging_version_id: str | None = None,
) -> dict[str, Any]:
    # The upload intent is created in the caller's own uncommitted unit of work,
    # so the caller supplies the publication identity deterministically and the
    # owner allocates independent responsibility from it without re-reading it.
    from uuid import NAMESPACE_URL, uuid5

    publication_id = intent.publication_id or uuid5(
        NAMESPACE_URL, f"aap-dataset-upload/{intent.item_id}/{staging_key}"
    )
    file_path = intent.file_path or (
        f"dataset-sources/{dataset_id}/{item_id}/{publication_id}/{intent.file_name}"
    )
    staging: dict[str, Any] = {"key": staging_key}
    if staging_etag is not None:
        staging["etag"] = staging_etag
    staging["version_id"] = staging_version_id
    return {
        "dataset_id": str(dataset_id),
        "item_id": str(item_id),
        "publication_id": str(publication_id),
        "origin": "direct",
        "mode": "write",
        "file_path": file_path,
        "logical_path": intent.file_name,
        "file_type": intent.file_type,
        "content_type": intent.content_type,
        "max_bytes": MAX_DIRECT_UPLOAD_BYTES,
        "authority": {
            "kind": "dataset_upload",
            "item_id": str(item_id),
            "publication_id": str(publication_id),
        },
        "staging": staging,
        "requested_by": str(actor_id),
    }


MAX_DIRECT_UPLOAD_BYTES = 5 * 1024**3


class UploadWorkflow:
    """Compose native data capabilities and bounded S3 I/O without owning commits."""

    def __init__(
        self,
        *,
        upload_data: DatasetUploadDataPort,
        legacy_upload_data: LegacyUploadDataPort,
        deletion_data: SourceDeletionDataPort,
        storage: SourceStoragePort,
        image_admission: ImageAdmissionPort,
        cleanup_bucket: str,
    ) -> None:
        self._upload_data = upload_data
        self._legacy_upload_data = legacy_upload_data
        self._deletion_data = deletion_data
        self._storage = storage
        self._image_admission = image_admission
        self._cleanup_bucket = cleanup_bucket

    async def init_upload(self, request: UploadInitRequest) -> UploadInitResult:
        try:
            intent = await self._upload_data.reserve(request)
        except DatasetUploadFailure as exc:
            raise SourceFailure(exc.code) from exc
        self._check_upload_scope(
            intent,
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            item_id=request.item_id,
        )
        if (intent.file_name, intent.content_type) != (
            request.file_name,
            request.content_type,
        ):
            raise SourceFailure("invalid_response")
        try:
            await self._upload_data.allocate(
                _upload_original_request(
                    intent,
                    actor_id=request.actor_id,
                    dataset_id=request.dataset_id,
                    item_id=request.item_id,
                    staging_key=intent.staging_key,
                )
            )
        except ManagedOriginalFailure as exc:
            raise SourceFailure(_original_failure(exc.code)) from None
        try:
            upload_url = await self._storage.generate_upload_url(
                intent.staging_key, intent.content_type
            )
            return UploadInitResult(
                actor_id=request.actor_id,
                dataset_id=request.dataset_id,
                item_id=request.item_id,
                upload_url=upload_url,
                expires_in=900,
            )
        except ValidationError:
            raise SourceFailure("invalid_response") from None

    async def complete_upload(self, request: UploadCompleteRequest) -> UploadOutcome:
        try:
            intent = await self._upload_data.lock(
                actor_id=request.actor_id,
                dataset_id=request.dataset_id,
                item_id=request.item_id,
            )
        except DatasetUploadFailure as exc:
            if exc.code != "upload_missing":
                raise SourceFailure(exc.code) from exc
            return await self._complete_legacy_upload(request)

        self._check_upload_scope(
            intent,
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            item_id=request.item_id,
        )
        if intent.state == "completed":
            if not isinstance(intent.result, DatasetUploadCompletedResult):
                raise SourceFailure("invalid_response")
            return self._outcome(
                request,
                intent.file_type,
                result=intent.result,
            )
        if intent.state == "discarded":
            if not isinstance(intent.result, DatasetUploadDiscardedResult):
                raise SourceFailure("invalid_response")
            if intent.result.duplicate_of is None and intent.result.reason != "missing":
                raise SourceFailure("upload_not_pending")
            return self._outcome(
                request,
                intent.file_type,
                duplicate_of=intent.result.duplicate_of,
            )
        if intent.file_path is None or intent.publication_id is None:
            raise SourceFailure("invalid_response")

        metadata = await self._storage.verify_staging_upload(intent.staging_key)
        if metadata is None:
            try:
                discarded = await self._upload_data.discard(
                    DatasetUploadDiscard(
                        actor_id=request.actor_id,
                        dataset_id=request.dataset_id,
                        item_id=request.item_id,
                        reason="missing",
                    )
                )
            except DatasetUploadFailure as exc:
                raise SourceFailure(exc.code) from exc
            self._check_upload_scope(
                discarded,
                actor_id=request.actor_id,
                dataset_id=request.dataset_id,
                item_id=request.item_id,
            )
            if (
                discarded.state != "discarded"
                or not isinstance(discarded.result, DatasetUploadDiscardedResult)
                or discarded.result.reason != "missing"
            ):
                raise SourceFailure("invalid_response")
            return self._outcome(request, intent.file_type)

        file_size, etag, content_hash = self._upload_metadata(metadata)
        try:
            duplicate = await self._upload_data.find_duplicate(
                actor_id=request.actor_id,
                dataset_id=request.dataset_id,
                item_id=request.item_id,
                content_hash=content_hash,
            )
        except DatasetUploadFailure as exc:
            raise SourceFailure(exc.code) from exc
        if duplicate.duplicate_of == request.item_id:
            raise SourceFailure("invalid_response")

        width = height = None
        object_identity: ObjectIdentity | None = None
        target = None
        operation = None
        if duplicate.duplicate_of is None:
            try:
                target = await self._upload_data.allocate(
                    _upload_original_request(
                        intent,
                        actor_id=request.actor_id,
                        dataset_id=request.dataset_id,
                        item_id=request.item_id,
                        staging_key=intent.staging_key,
                        staging_etag=etag,
                        staging_version_id=metadata.get("VersionId"),
                    )
                )
            except ManagedOriginalFailure as exc:
                raise SourceFailure(_original_failure(exc.code)) from None
            try:
                operation = await self._upload_data.open_operation(target.creation_id)
            except ManagedOriginalFailure as exc:
                raise SourceFailure(_original_failure(exc.code)) from None
            if operation.mode == "read":
                if target.sha256 is None or target.size_bytes is None:
                    raise SourceFailure("invalid_response")
                try:
                    verified = await self._storage.verify_managed_original(
                        target,
                        sha256=target.sha256,
                        size_bytes=target.size_bytes,
                    )
                except SourceFailure as exc:
                    await self._abandon_read_operation(target, operation, exc)
                    raise
                except Exception as exc:
                    await self._abandon_read_operation(target, operation, exc)
                    raise SourceFailure("storage_unavailable") from None
                if (
                    verified.sha256 != target.sha256
                    or verified.size_bytes != target.size_bytes
                ):
                    error = SourceFailure("source_changed")
                    await self._abandon_read_operation(target, operation, error)
                    raise error
                await self._close_read_operation(target, operation)
                file_size = verified.size_bytes
                content_hash = verified.md5
                if intent.file_type == "image":
                    try:
                        object_identity = ObjectIdentity(
                            etag=verified.etag.strip().strip('"'),
                            byte_size=verified.size_bytes,
                            version_id=verified.version_id,
                        )
                    except (TypeError, ValueError):
                        raise SourceFailure("source_changed") from None
                    dimensions = verified.dimensions
                    if dimensions is not None:
                        width, height = dimensions
            else:
                if intent.file_type == "image":
                    dimensions = await self._storage.read_staging_image_dimensions(
                        intent.staging_key, if_match=etag
                    )
                    if dimensions is not None:
                        width, height = dimensions
                try:
                    written = await self._storage.publish_managed_upload(
                        target,
                        staging_key=intent.staging_key,
                        staging_etag=etag,
                        staging_version_id=metadata.get("VersionId"),
                        max_bytes=max(file_size, 0),
                        image=intent.file_type == "image",
                        content_type=intent.content_type,
                        on_prepared=lambda prepared: (
                            self._upload_data.original_prepared(
                                target.creation_id, prepared.evidence()
                            )
                        ),
                        on_writing=lambda: self._upload_data.original_writing(
                            target.creation_id
                        ),
                        on_written=lambda receipt: self._upload_data.original_written(
                            target.creation_id,
                            {
                                "etag": receipt.etag,
                                "version_id": receipt.version_id,
                                "request_id": receipt.request_id,
                                "response": receipt.response,
                            },
                        ),
                    )
                except SourceFailure as exc:
                    await self._fail_original(
                        target, "write", exc.code or "storage_write_failed", exc
                    )
                    raise
                except Exception as exc:
                    await self._fail_original(
                        target, "write", "storage_unavailable", exc
                    )
                    raise SourceFailure("storage_unavailable") from None
                # Every source, body and destination handle has been drained and
                # closed before the owner may settle this creation.
                try:
                    await self._upload_data.original_verified(
                        target.creation_id,
                        _managed_verification(written.proof),
                    )
                except ManagedOriginalFailure as exc:
                    raise SourceFailure(_original_failure(exc.code)) from None
                except Exception:
                    # The write is physically settled but its ledger transition is
                    # unknown: retain responsibility instead of pretending success.
                    raise SourceFailure("storage_unavailable") from None
                file_size = written.file_size
                content_hash = written.content_hash
                if width is None and written.dimensions is not None:
                    width, height = written.dimensions
                object_identity = written.object_identity
                if intent.file_type == "image" and object_identity is None:
                    raise SourceFailure("source_changed")

        try:
            completion = DatasetUploadCompletion(
                actor_id=request.actor_id,
                dataset_id=request.dataset_id,
                item_id=request.item_id,
                publication_id=(
                    target.publication_id
                    if target is not None and target.publication_id is not None
                    else intent.publication_id
                ),
                file_size=file_size,
                content_hash=content_hash,
                width=width,
                height=height,
            )
        except ValidationError:
            raise SourceFailure("invalid_response") from None
        try:
            accepted = await self._upload_data.complete(completion)
        except DatasetUploadFailure as exc:
            raise SourceFailure(exc.code) from exc
        self._check_upload_scope(
            accepted,
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            item_id=request.item_id,
        )
        expected_publication = (
            target.publication_id
            if target is not None and target.publication_id is not None
            else intent.publication_id
        )
        if accepted.publication_id != expected_publication:
            raise SourceFailure("invalid_response")
        if accepted.disposition == "duplicate":
            if accepted.duplicate_of is None:
                raise SourceFailure("invalid_response")
            return self._outcome(
                request, intent.file_type, duplicate_of=accepted.duplicate_of
            )
        if not isinstance(accepted.result, DatasetUploadCompletedResult):
            raise SourceFailure("invalid_response")
        if intent.file_type == "image":
            if object_identity is None or intent.file_path is None:
                raise SourceFailure("invalid_response")
            await self._admit_image(
                item_id=request.item_id,
                operation_id=expected_publication,
                file_path=intent.file_path,
                file_size=file_size,
                content_hash=content_hash,
                object_identity=object_identity,
            )
        return self._outcome(request, intent.file_type, result=accepted.result)

    async def _complete_legacy_upload(
        self, request: UploadCompleteRequest
    ) -> UploadOutcome:
        lock_request = LegacyUploadLockRequest(
            operation_id=request.operation_id,
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            item_id=request.item_id,
        )
        try:
            state = await self._legacy_upload_data.read(lock_request)
        except DatasetUploadFailure as exc:
            raise SourceFailure(exc.code) from exc
        self._check_legacy_scope(state, request)
        if state.state == "completed":
            if state.result is None:
                raise SourceFailure("invalid_response")
            return self._outcome(request, state.file_type, result=state.result)
        if state.state == "duplicate":
            return self._outcome(
                request, state.file_type, duplicate_of=state.duplicate_of
            )
        expected_file_path = state.file_path
        expected_file_type = state.file_type

        object_identity = None
        image_source = None
        width = height = None
        if state.file_type == "image":
            try:
                image_source = await self._image_admission.read_dataset_item(
                    request.item_id
                )
            except ImageFailure as exc:
                raise _as_source_image_failure(exc) from None
            if (
                image_source.file_path != state.file_path
                or image_source.owner.id != request.item_id
            ):
                raise SourceFailure("source_changed")
            object_identity = image_source.object_identity
            if object_identity is None:
                metadata = await self._storage.verify_legacy_source(state.file_path)
                if metadata is None:
                    return self._outcome(request, state.file_type)
                file_size, _etag, head_content_hash = self._upload_metadata(metadata)
                try:
                    object_identity = object_identity_from_head(
                        metadata, expected_size=file_size
                    )
                except ImageFailure as exc:
                    raise _as_source_image_failure(exc) from None
                content_hash = head_content_hash or image_source.content_hash
            else:
                file_size = (
                    image_source.file_size
                    if image_source.file_size is not None
                    else object_identity.byte_size
                )
                if file_size != object_identity.byte_size:
                    raise SourceFailure("source_changed")
                content_hash = image_source.content_hash
            width, height = image_source.width, image_source.height
            if width is None or height is None:
                width = height = None
                dimensions = await self._storage.read_legacy_image_dimensions(
                    state.file_path, if_match=object_identity.etag
                )
                if dimensions is not None:
                    width, height = dimensions

        try:
            state = await self._legacy_upload_data.lock(lock_request)
        except DatasetUploadFailure as exc:
            raise SourceFailure(exc.code) from exc
        self._check_legacy_scope(state, request)
        if state.state == "completed":
            if state.result is None:
                raise SourceFailure("invalid_response")
            return self._outcome(request, state.file_type, result=state.result)
        if state.state == "duplicate":
            return self._outcome(
                request, state.file_type, duplicate_of=state.duplicate_of
            )
        if (state.file_path, state.file_type) != (
            expected_file_path,
            expected_file_type,
        ):
            raise SourceFailure("source_changed")
        if state.file_type != "image":
            metadata = await self._storage.verify_legacy_source(state.file_path)
            if metadata is None:
                return self._outcome(request, state.file_type)
            file_size, _etag, content_hash = self._upload_metadata(metadata)
        if image_source is not None:
            try:
                locked_source = await self._image_admission.read_dataset_item(
                    request.item_id
                )
            except ImageFailure as exc:
                raise _as_source_image_failure(exc) from None
            if (
                locked_source.file_path != image_source.file_path
                or locked_source.source_snapshot != image_source.source_snapshot
                or locked_source.file_size != image_source.file_size
                or locked_source.content_hash != image_source.content_hash
                or locked_source.width != image_source.width
                or locked_source.height != image_source.height
                or locked_source.source_generation != image_source.source_generation
                or (
                    locked_source.object_identity != image_source.object_identity
                    and locked_source.object_identity != object_identity
                )
            ):
                raise SourceFailure("source_changed")

        try:
            completion = LegacyUploadCompletion(
                operation_id=request.operation_id,
                actor_id=request.actor_id,
                dataset_id=request.dataset_id,
                item_id=request.item_id,
                expected_file_path=state.file_path,
                file_size=file_size,
                content_hash=content_hash,
                width=width,
                height=height,
            )
        except ValidationError:
            raise SourceFailure("invalid_response") from None
        try:
            completed = await self._legacy_upload_data.complete(completion)
        except DatasetUploadFailure as exc:
            raise SourceFailure(exc.code) from exc
        self._check_legacy_scope(completed, request)
        if completed.state == "duplicate":
            return self._outcome(
                request, completed.file_type, duplicate_of=completed.duplicate_of
            )
        if completed.state != "completed" or completed.result is None:
            raise SourceFailure("invalid_response")
        if completed.file_type == "image":
            if object_identity is None:
                raise SourceFailure("invalid_response")
            await self._admit_image(
                item_id=request.item_id,
                operation_id=request.operation_id,
                file_path=completed.file_path,
                file_size=file_size,
                content_hash=content_hash,
                object_identity=object_identity,
            )
        return self._outcome(request, completed.file_type, result=completed.result)

    async def _fail_original(
        self,
        target,
        stage: str,
        code: str,
        exc: BaseException,
    ) -> None:
        """Record durable failure evidence; never publish untrusted bytes."""
        if target is None:
            return
        evidence = _failure_evidence(stage, exc)
        try:
            await self._upload_data.fail_original(
                target.creation_id,
                "unknown" if not evidence["physical_settled"] else "failed",
                _safe_error_code("upload_" + code),
                evidence,
            )
        except Exception:
            logger.warning("Upload creation failure could not be settled code=%s", code)

    async def _close_read_operation(self, target, operation) -> None:
        """Settle the read permit only after the readback handle has drained."""
        try:
            await self._upload_data.close_operation(
                target.creation_id, operation.operation_id, True
            )
        except ManagedOriginalFailure as exc:
            raise SourceFailure(_original_failure(exc.code)) from None
        except Exception:
            raise SourceFailure("storage_unavailable") from None

    async def _abandon_read_operation(self, target, operation, exc) -> None:
        """Keep the read permit when closure is not proven."""
        if (
            getattr(exc, "storage_operation_unsettled", False)
            or getattr(exc, "outcome", None) == "unknown"
        ):
            return
        try:
            await self._upload_data.close_operation(
                target.creation_id, operation.operation_id, True
            )
        except Exception:
            logger.warning(
                "Upload read permit close failed creation=%s", target.creation_id
            )

    async def _admit_image(
        self,
        *,
        item_id: UUID,
        operation_id: UUID,
        file_path: str,
        file_size: int,
        content_hash: str | None,
        object_identity: ObjectIdentity,
    ) -> None:
        try:
            await self._image_admission.admit_dataset_item(
                item_id=item_id,
                publication_id=operation_id,
                file_path=file_path,
                file_size=file_size,
                content_hash=content_hash,
                object_identity=object_identity,
            )
        except ImageFailure as exc:
            raise _as_source_image_failure(exc) from None

    async def delete_source(
        self, request: SourceDeletionRequest
    ) -> DatasetSourceDeletionReceipt:
        try:
            command = DatasetSourceDeletionCommand(
                operation_id=request.operation_id,
                actor_id=request.actor_id,
                dataset_id=request.dataset_id,
                item_id=request.item_id,
                cleanup_bucket=self._cleanup_bucket,
                audit_context=request.audit_context,
            )
        except ValidationError:
            raise SourceFailure("invalid_request") from None
        try:
            receipt = await self._deletion_data.delete(command)
        except DatasetSourceDeletionFailure as exc:
            raise SourceFailure(exc.code) from exc
        if (
            receipt.operation_id,
            receipt.actor_id,
            receipt.dataset_id,
            receipt.item_id,
            receipt.cleanup_bucket,
        ) != (
            request.operation_id,
            request.actor_id,
            request.dataset_id,
            request.item_id,
            self._cleanup_bucket,
        ):
            raise SourceFailure("invalid_response")
        return receipt

    @staticmethod
    def _outcome(
        request: UploadCompleteRequest,
        file_type: str,
        *,
        result: DatasetUploadCompletedResult | None = None,
        duplicate_of: UUID | None = None,
    ) -> UploadOutcome:
        try:
            return UploadOutcome(
                operation_id=request.operation_id,
                actor_id=request.actor_id,
                dataset_id=request.dataset_id,
                item_id=request.item_id,
                file_type=file_type,
                result=result,
                duplicate_of=duplicate_of,
            )
        except ValidationError:
            raise SourceFailure("invalid_response") from None

    @staticmethod
    def _check_upload_scope(
        value: DatasetUploadIntent | DatasetUploadAcceptance,
        *,
        actor_id: UUID,
        dataset_id: UUID,
        item_id: UUID,
    ) -> None:
        if (value.actor_id, value.dataset_id, value.item_id) != (
            actor_id,
            dataset_id,
            item_id,
        ):
            raise SourceFailure("invalid_response")

    @staticmethod
    def _check_legacy_scope(
        value: LegacyUploadState, request: UploadCompleteRequest
    ) -> None:
        if (
            value.operation_id,
            value.actor_id,
            value.dataset_id,
            value.item_id,
        ) != (
            request.operation_id,
            request.actor_id,
            request.dataset_id,
            request.item_id,
        ):
            raise SourceFailure("invalid_response")

    @staticmethod
    def _upload_metadata(metadata: dict[str, Any]) -> tuple[int, str, str | None]:
        size, etag = metadata.get("ContentLength"), metadata.get("ETag")
        if (
            type(size) is not int
            or size < 0
            or not isinstance(etag, str)
            or not etag
            or len(etag) > 1024
            or any(char in etag for char in "\r\n")
        ):
            raise SourceFailure("invalid_response")
        candidate = etag.strip('"')
        content_hash = (
            candidate.lower()
            if len(candidate) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", candidate)
            else None
        )
        return size, etag, content_hash


def _as_source_image_failure(exc: ImageFailure) -> SourceFailure:
    if exc.code == "asset_missing":
        return SourceFailure("item_missing")
    if exc.code in {
        "source_missing",
        "source_not_ready",
        "not_image",
        "owner_changed",
        "dependency_changed",
    }:
        return SourceFailure("source_changed")
    if exc.code in {
        "account_busy",
        "dataset_busy",
        "source_busy",
        "operation_conflict",
        "source_changed",
        "storage_unavailable",
    }:
        return SourceFailure(exc.code)
    return SourceFailure("invalid_response")
