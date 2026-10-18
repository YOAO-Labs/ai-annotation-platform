"""Transaction-bound source identity binding and durable image admission."""

from __future__ import annotations

from typing import Any, Literal, Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel

from aap_assets_images import contract as c
from aap_assets_images.policy import ImagePolicy


class ImageAdmissionPort(Protocol):
    async def read_dataset_item(self, item_id: UUID) -> c.ImageSource: ...

    async def admit_dataset_item(
        self,
        *,
        item_id: UUID,
        publication_id: UUID,
        file_path: str,
        file_size: int,
        content_hash: str | None,
        object_identity: c.ObjectIdentity,
        missing_is_skipped: bool = False,
    ) -> c.AcceptedImageWork | None: ...

    async def admit_task(
        self,
        *,
        task_id: UUID,
        source_generation: int,
        file_path: str,
        object_identity: c.ObjectIdentity,
    ) -> c.AcceptedImageWork | None: ...


class ImageNativePort(Protocol):
    async def call(self, operation: str, request: BaseModel) -> BaseModel: ...


def object_identity_from_head(
    metadata: dict[str, Any], *, expected_size: int | None = None
) -> c.ObjectIdentity:
    """Retain only the bounded identity needed for conditional image reads."""
    byte_size = metadata.get("ContentLength")
    etag = metadata.get("ETag")
    version_id = metadata.get("VersionId")
    if type(byte_size) is not int or byte_size < 0:
        raise c.ImageFailure("invalid_response")
    if expected_size is not None and byte_size != expected_size:
        raise c.ImageFailure("source_changed")
    if not isinstance(etag, str):
        raise c.ImageFailure("invalid_response")
    etag = etag.strip().strip('"')
    if (
        not etag
        or len(etag) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in etag)
    ):
        raise c.ImageFailure("invalid_response")
    if version_id is not None and (
        not isinstance(version_id, str)
        or not version_id
        or len(version_id) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in version_id)
    ):
        raise c.ImageFailure("invalid_response")
    try:
        return c.ObjectIdentity(
            etag=etag,
            byte_size=byte_size,
            version_id=version_id,
        )
    except ValueError:
        raise c.ImageFailure("invalid_response") from None


class ImageAdmission:
    """Bind an observed immutable object and enqueue work through its native owner."""

    def __init__(self, native: ImageNativePort, policy: ImagePolicy) -> None:
        self._native = native
        self._policy = policy

    async def read_dataset_item(self, item_id: UUID) -> c.ImageSource:
        source = await self._native.call(
            "resolve_internal_image_source",
            c.ResolveInternalImageSourceRequest(
                owner_kind="dataset_item",
                owner_id=item_id,
                expected_source_snapshot=None,
                object_identity=None,
            ),
        )
        if type(source) is not c.ImageSource:
            raise c.ImageFailure("invalid_response")
        if (
            source.owner.kind != "dataset_item"
            or source.owner.id != item_id
            or source.file_type != "image"
            or not source.upload_ready
            or source.source_drifted
        ):
            raise c.ImageFailure("source_changed")
        return source

    async def admit_dataset_item(
        self,
        *,
        item_id: UUID,
        publication_id: UUID,
        file_path: str,
        file_size: int,
        content_hash: str | None,
        object_identity: c.ObjectIdentity,
        missing_is_skipped: bool = False,
    ) -> c.AcceptedImageWork | None:
        return await self._admit(
            owner_kind="dataset_item",
            owner_id=item_id,
            operation_namespace=publication_id,
            file_path=file_path,
            file_size=file_size,
            content_hash=content_hash,
            object_identity=object_identity,
            missing_is_skipped=missing_is_skipped,
            operation_suffix="admission",
        )

    async def admit_task(
        self,
        *,
        task_id: UUID,
        source_generation: int,
        file_path: str,
        object_identity: c.ObjectIdentity,
    ) -> c.AcceptedImageWork | None:
        return await self._admit(
            owner_kind="task",
            owner_id=task_id,
            operation_namespace=task_id,
            operation_suffix=f"admission:g{source_generation}",
            expected_source_generation=source_generation,
            file_path=file_path,
            file_size=None,
            content_hash=None,
            object_identity=object_identity,
        )

    async def _admit(
        self,
        *,
        owner_kind: Literal["dataset_item", "task"],
        owner_id: UUID,
        operation_namespace: UUID,
        file_path: str,
        file_size: int | None,
        content_hash: str | None,
        object_identity: c.ObjectIdentity,
        operation_suffix: str = "admission",
        missing_is_skipped: bool = False,
        expected_source_generation: int | None = None,
    ) -> c.AcceptedImageWork | None:
        if object_identity.byte_size != file_size and file_size is not None:
            raise c.ImageFailure("source_changed")

        try:
            current = await self._native.call(
                "resolve_internal_image_source",
                c.ResolveInternalImageSourceRequest(
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    expected_source_snapshot=None,
                    object_identity=None,
                ),
            )
        except c.ImageFailure as exc:
            if missing_is_skipped and exc.code in {"asset_missing", "source_missing"}:
                return None
            raise
        if type(current) is not c.ImageSource:
            raise c.ImageFailure("invalid_response")
        self._require_descriptor(
            current,
            owner_kind=owner_kind,
            owner_id=owner_id,
            file_path=file_path,
            file_size=file_size,
            content_hash=content_hash,
            expected_source_generation=expected_source_generation,
        )
        bound = await self._native.call(
            "resolve_internal_image_source",
            c.ResolveInternalImageSourceRequest(
                owner_kind=owner_kind,
                owner_id=owner_id,
                expected_source_snapshot=current.source_snapshot,
                object_identity=object_identity,
            ),
        )
        if type(bound) is not c.ImageSource:
            raise c.ImageFailure("invalid_response")
        self._require_descriptor(
            bound,
            owner_kind=owner_kind,
            owner_id=owner_id,
            file_path=file_path,
            file_size=file_size,
            content_hash=content_hash,
            expected_source_generation=expected_source_generation,
        )
        if bound.source_snapshot != current.source_snapshot:
            raise c.ImageFailure("source_changed")
        if bound.object_identity != object_identity:
            raise c.ImageFailure("source_changed")

        spec = self._work_spec(bound)
        if spec is None:
            return None
        operation_id = uuid5(operation_namespace, f"aap-image:{operation_suffix}")
        spec = spec.model_copy(
            update={"expected_source_snapshot": bound.source_snapshot}
        )
        request = c.ScheduleImageMediaRequest(
            operation_id=operation_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            spec=spec,
            force=False,
        )
        accepted = await self._native.call("schedule_image_media", request)
        if type(accepted) is not c.AcceptedImageWork:
            raise c.ImageFailure("invalid_response")
        if (
            accepted.operation_id != operation_id
            or accepted.source is None
            or accepted.source.owner != bound.owner
            or accepted.source.source_snapshot != bound.source_snapshot
            or accepted.source.object_identity != object_identity
            or accepted.work.owner != bound.owner
            or accepted.work.kind != spec.kind
            or accepted.spec != spec
        ):
            raise c.ImageFailure("invalid_response")
        return accepted

    @staticmethod
    def _require_descriptor(
        source: c.ImageSource,
        *,
        owner_kind: Literal["dataset_item", "task"],
        owner_id: UUID,
        file_path: str,
        file_size: int | None,
        content_hash: str | None,
        expected_source_generation: int | None = None,
    ) -> None:
        if (
            source.owner.kind != owner_kind
            or source.owner.id != owner_id
            or source.file_type != "image"
            or source.file_path != file_path
            or (file_size is not None and source.file_size != file_size)
            or source.content_hash != content_hash
            or (
                expected_source_generation is not None
                and source.source_generation != expected_source_generation
            )
            or not source.upload_ready
            or source.source_drifted
        ):
            raise c.ImageFailure("source_changed")

    def _work_spec(self, source: c.ImageSource) -> c.ImageWorkSpec | None:
        if source.owner.kind == "task" or source.width is None or source.height is None:
            return c.DimensionsWorkSpec(
                expected_source_snapshot=source.source_snapshot,
                policy=self._policy,
                generate_preview=True,
            )
        if self._policy.pyramid_eligible(source.width, source.height):
            if self._policy.auto_generate_pyramids:
                return c.PyramidWorkSpec(
                    expected_source_snapshot=source.source_snapshot,
                    policy=self._policy,
                )
            return None
        return c.ThumbnailWorkSpec(
            expected_source_snapshot=source.source_snapshot,
            policy=self._policy,
        )


class AsyncpgImageAdmissionData:
    """Expose only the two Image system operations on a caller-owned connection."""

    _OPERATIONS = frozenset({"resolve_internal_image_source", "schedule_image_media"})

    def __init__(self, connection: Any, max_reply_bytes: int) -> None:
        self._connection = connection
        self._max_reply_bytes = max_reply_bytes

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        definition = c.OPERATIONS.get(operation)
        if operation not in self._OPERATIONS or definition is None:
            raise c.ImageFailure("invalid_request")
        if type(request) is not definition.request:
            raise c.ImageFailure("invalid_request")
        if len(c.json_bytes(request)) > c.REQUEST_BYTES:
            raise c.ImageFailure("resource_budget_exceeded")

        arguments: list[Any] = []
        for name, sql_type in zip(
            definition.arguments, definition.sql_types, strict=True
        ):
            value = getattr(request, name)
            if sql_type == "jsonb" and isinstance(value, BaseModel):
                value = value.model_dump(mode="json")
            arguments.append(value)
        placeholders = ", ".join(
            f"${index}::{sql_type}"
            for index, sql_type in enumerate(definition.sql_types, start=1)
        )
        try:
            raw = await self._connection.fetchval(
                f"SELECT aap_assets_v1.{operation}({placeholders})", *arguments
            )
        except Exception:
            raise c.ImageFailure("storage_unavailable") from None
        try:
            if isinstance(raw, (str, bytes)):
                payload_size = (
                    len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
                )
            else:
                payload_size = len(c.json_bytes(raw))
        except (TypeError, ValueError, RecursionError):
            raise c.ImageFailure("invalid_response") from None
        if payload_size > self._max_reply_bytes:
            raise c.ImageFailure("invalid_response")
        result = c.decode_reply(raw, definition.result)
        try:
            c.require_scope(request, result)
        except (TypeError, ValueError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        return result
