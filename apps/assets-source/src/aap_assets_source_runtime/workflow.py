"""Language-neutral upload and source-deletion composition for Assets."""

from __future__ import annotations

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

    async def publish_dataset_upload(
        self, staging_key: str, final_key: str, *, etag: str
    ) -> None: ...


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
        if duplicate.duplicate_of is None:
            if intent.file_type == "image":
                dimensions = await self._storage.read_staging_image_dimensions(
                    intent.staging_key, if_match=etag
                )
                if dimensions is not None:
                    width, height = dimensions
            # shortcut: Retain uncertain copies until durable publication evidence can fence reclamation.
            await self._storage.publish_dataset_upload(
                intent.staging_key, intent.file_path, etag=etag
            )
            if intent.file_type == "image":
                published = await self._storage.verify_legacy_source(intent.file_path)
                if published is None:
                    raise SourceFailure("source_changed")
                try:
                    object_identity = object_identity_from_head(
                        published, expected_size=file_size
                    )
                except ImageFailure as exc:
                    raise _as_source_image_failure(exc) from None

        try:
            completion = DatasetUploadCompletion(
                actor_id=request.actor_id,
                dataset_id=request.dataset_id,
                item_id=request.item_id,
                publication_id=intent.publication_id,
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
        if accepted.publication_id != intent.publication_id:
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
                operation_id=intent.publication_id,
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
