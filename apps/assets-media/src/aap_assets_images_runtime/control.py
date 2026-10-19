"""Source-bound image control workflows for native image-media operations."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field, model_validator

from aap_assets_images import contract as c
from aap_assets_images.geometry import canonical_manifest_bytes, sha256_digest
from aap_assets_images.policy import ImagePolicy
from aap_assets_images.transport import (
    ImageAssetReferences,
    ImageAssetReferencesRequest,
    ImageAssetReference,
    ImageGenerationReference,
    ImageGenerationReferenceRequest,
    ImageSourceReference,
    ImageSourceReferences,
    ImageSourceReferencesRequest,
    InternalImageSourceReferenceRequest,
    require_asset_reference_scope,
    require_generation_reference_scope,
    require_internal_source_reference_scope,
    require_source_reference_scope,
    require_source_references_scope,
)

log = logging.getLogger(__name__)
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_IMAGE_THUMBNAIL_KEY = re.compile(
    rf"^image-assets/(?P<job>{_UUID})/g(?P<generation>[1-9][0-9]*)/"
    rf"a{_UUID}/thumbnail\.webp$"
)
_PYRAMID_MANIFEST_KEY = re.compile(
    rf"^(?P<prefix>image-pyramids/(?P<asset>{_UUID})/g(?P<generation>[1-9][0-9]*)"
    rf"(?:/a{_UUID})?)/manifest\.json$"
)
_MANIFEST_MAX_BYTES = 1024 * 1024


class _RetainedThumbnailPublication(c.Value):
    """Private durable proof for a still-current published thumbnail."""

    schema_tag: Literal["aap-image-thumbnail-publication/v1"] = Field(alias="schema")
    job_id: UUID
    generation: c.Revision
    owner: c.ImageOwner
    source_generation: c.Revision
    source_snapshot: c.Digest
    source_identity: c.ObjectIdentity
    spec: c.ThumbnailWorkSpec
    result: c.ThumbnailResult

    @model_validator(mode="after")
    def publication_matches_scope(self):
        if (
            self.spec.expected_source_snapshot != self.source_snapshot
            or self.result.source_identity != self.source_identity
        ):
            raise ValueError("retained thumbnail publication escaped its source")
        return self


class ImageDatabase(Protocol):
    async def call(self, operation: str, request: BaseModel) -> BaseModel: ...


class ImageStorage(Protocol):
    async def head_source(self, source: c.ImageSource) -> c.ObjectIdentity: ...

    async def head_cache(self, key: str) -> c.ObjectIdentity | None: ...

    async def read_cache(self, key: str, limit: int) -> bytes: ...

    async def sign_source(
        self, source: c.ImageSource, expires_in: int = 3600
    ) -> str: ...

    async def sign_cache(self, key: str, expires_in: int = 3600) -> str: ...


class ImageControl:
    def __init__(
        self,
        database: ImageDatabase,
        storage: ImageStorage,
        policy: ImagePolicy | None = None,
        *,
        wakeup: Callable[[UUID, int], Awaitable[None]] | None = None,
    ):
        self.database = database
        self.storage = storage
        self.policy = policy or ImagePolicy()
        self.wakeup = wakeup

    async def close(self) -> None:
        close = getattr(self.storage, "close", None)
        if not callable(close):
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    @property
    def source_url_expiry(self) -> int:
        return 3600

    @property
    def asset_url_expiry(self) -> int:
        return min(self.policy.url_expiry_seconds, 86400)

    async def _wake(self, accepted: c.AcceptedImageWork) -> None:
        if self.wakeup is None or accepted.work.status != "pending":
            return
        try:
            await self.wakeup(accepted.work.job_id, accepted.work.generation)
        except Exception:
            log.warning("Image work accepted; execution wakeup unavailable")

    async def _native_call(
        self,
        operation: str,
        request: BaseModel,
    ) -> BaseModel:
        definition = c.OPERATIONS.get(operation)
        if (
            definition is None
            or definition.role in {"worker", "cleanup"}
            or type(request) is not definition.request
        ):
            raise c.ImageFailure("permission_denied")
        try:
            result = await self.database.call(operation, request)
        except c.ImageFailure:
            raise
        except Exception:
            raise c.ImageFailure("storage_unavailable") from None
        if not isinstance(result, definition.result):
            raise c.ImageFailure("invalid_response")
        try:
            c.require_scope(request, result)
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        if isinstance(result, c.AcceptedImageWork):
            await self._wake(result)
        return result

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        definition = c.OPERATIONS.get(operation)
        if (
            definition is None
            or definition.role in {"worker", "cleanup"}
            or type(request) is not definition.request
        ):
            raise c.ImageFailure("permission_denied")
        if operation == "prepare_image_media":
            return await self.prepare_image_media(request)
        if operation == "retry_image_media":
            return await self.retry_image_media(request)
        if operation == "schedule_image_media":
            return await self.schedule_image_media(request)
        native_request = request
        if (
            isinstance(
                request, (c.ReadImageSummariesRequest, c.ReadImageGenerationRequest)
            )
            and request.profile_version is None
        ):
            native_request = request.model_copy(
                update={"profile_version": self.policy.profile_version}
            )
        result = await self._native_call(operation, native_request)
        if isinstance(result, c.ImageSummaries):
            return c.ImageSummaries(
                items=[
                    c.ImageSummaryItem(
                        selector=item.selector,
                        source=item.source,
                        thumbnail_current=item.thumbnail_current,
                        pyramid=item.pyramid.model_copy(
                            update={
                                "required": self.policy.pyramid_required(
                                    item.source.width, item.source.height
                                )
                            }
                        ),
                    )
                    for item in result.items
                ]
            )
        if isinstance(result, c.ImageGenerationRead):
            pyramid = result.pyramid.model_copy(
                update={
                    "required": self.policy.pyramid_required(
                        result.source.width, result.source.height
                    )
                }
            )
            return c.ImageGenerationRead(source=result.source, pyramid=pyramid)
        return result

    async def _storage_call(self, name: str, *args, missing_code: str | None = None):
        try:
            operation = getattr(self.storage, name)
            return await operation(*args)
        except c.ImageFailure:
            raise
        except FileNotFoundError:
            raise c.ImageFailure(missing_code or "object_missing") from None
        except Exception:
            raise c.ImageFailure("storage_unavailable") from None

    @staticmethod
    def _selector(request: c.ImageSourceSelector) -> c.ReadImageSourceRequest:
        return c.ReadImageSourceRequest(
            actor_id=request.actor_id,
            task_id=request.task_id,
            dataset_item_id=request.dataset_item_id,
        )

    @staticmethod
    def _same_source(expected: c.ImageSource, actual: c.ImageSource) -> bool:
        return (
            actual.owner == expected.owner
            and actual.task_id == expected.task_id
            and actual.project_id == expected.project_id
            and actual.dataset_id == expected.dataset_id
            and actual.item_id == expected.item_id
            and actual.source_generation == expected.source_generation
            and actual.source_snapshot == expected.source_snapshot
            and actual.object_identity == expected.object_identity
            and actual.file_path == expected.file_path
            and actual.bucket_kind == expected.bucket_kind
            and (expected.width is None or actual.width == expected.width)
            and (expected.height is None or actual.height == expected.height)
            and not actual.source_drifted
        )

    @classmethod
    def _same_observed_source(
        cls,
        expected: c.ImageSource,
        actual: c.ImageSource,
        identity: c.ObjectIdentity,
    ) -> bool:
        if expected.object_identity is None:
            expected = expected.model_copy(update={"object_identity": identity})
        return cls._same_source(expected, actual)

    async def _observe_actor(
        self,
        request: c.ImageSourceSelector,
        *,
        expected_source_snapshot: str | None = None,
    ) -> c.ImageSource:
        selector = self._selector(request)
        source = await self._native_call("read_image_source", selector)
        if expected_source_snapshot is not None and (
            source.source_snapshot != expected_source_snapshot
        ):
            raise c.ImageFailure("source_changed")
        if not source.upload_ready:
            raise c.ImageFailure("source_not_ready")
        identity = await self._storage_call(
            "head_source", source, missing_code="source_missing"
        )
        if not isinstance(identity, c.ObjectIdentity):
            raise c.ImageFailure("invalid_response")
        observed = await self._native_call(
            "observe_image_source",
            c.ObserveImageSourceRequest(
                actor_id=selector.actor_id,
                task_id=selector.task_id,
                dataset_item_id=selector.dataset_item_id,
                source_snapshot=source.source_snapshot,
                object_identity=identity,
            ),
        )
        if observed.source_drifted or not self._same_observed_source(
            source, observed, identity
        ):
            raise c.ImageFailure("source_changed")
        if observed.object_identity != identity:
            raise c.ImageFailure("source_changed")
        if not observed.upload_ready:
            raise c.ImageFailure("source_not_ready")
        return observed

    async def observe_source(
        self,
        request: c.ReadImageSourceRequest,
        *,
        expected_source_snapshot: str | None = None,
    ) -> c.ImageSource:
        """Return observed facts for another Assets capability without signing a URL."""
        return await self._observe_actor(
            request, expected_source_snapshot=expected_source_snapshot
        )

    async def observe_internal_source(
        self, request: c.InternalImageSourceRequest
    ) -> c.ImageSource:
        return await self._observe_internal(request)

    async def _observe_internal(
        self, request: c.InternalImageSourceRequest
    ) -> c.ImageSource:
        owner_request = c.ResolveInternalImageSourceRequest(
            owner_kind=request.owner.kind, owner_id=request.owner.id
        )
        source = await self._native_call("resolve_internal_image_source", owner_request)
        if request.expected_source_snapshot is not None and (
            request.expected_source_snapshot != source.source_snapshot
        ):
            raise c.ImageFailure("source_changed")
        if not source.upload_ready:
            raise c.ImageFailure("source_not_ready")
        identity = await self._storage_call(
            "head_source", source, missing_code="source_missing"
        )
        if not isinstance(identity, c.ObjectIdentity):
            raise c.ImageFailure("invalid_response")
        observed = await self._native_call(
            "resolve_internal_image_source",
            c.ResolveInternalImageSourceRequest(
                owner_kind=request.owner.kind,
                owner_id=request.owner.id,
                expected_source_snapshot=source.source_snapshot,
                object_identity=identity,
            ),
        )
        if observed.source_drifted or not self._same_observed_source(
            source, observed, identity
        ):
            raise c.ImageFailure("source_changed")
        if observed.object_identity != identity:
            raise c.ImageFailure("source_changed")
        if (
            request.object_identity is not None
            and observed.object_identity != request.object_identity
        ):
            raise c.ImageFailure("source_changed")
        if not observed.upload_ready:
            raise c.ImageFailure("source_not_ready")
        return observed

    async def _final_actor(
        self, source: c.ImageSource, selector: c.ImageSourceSelector
    ) -> c.ImageSource:
        current = await self._observe_actor(
            selector, expected_source_snapshot=source.source_snapshot
        )
        if not self._same_source(source, current):
            raise c.ImageFailure("source_changed")
        return current

    async def _final_internal(self, source: c.ImageSource) -> c.ImageSource:
        current = await self._observe_internal(
            c.InternalImageSourceRequest(owner=source.owner)
        )
        if not self._same_source(source, current):
            raise c.ImageFailure("source_changed")
        return current

    def _work_spec(self, purpose: c.Purpose, snapshot: str):
        if purpose == "dimensions":
            return c.DimensionsWorkSpec(
                expected_source_snapshot=snapshot, policy=self.policy
            )
        if purpose == "thumbnail":
            return c.ThumbnailWorkSpec(
                expected_source_snapshot=snapshot, policy=self.policy
            )
        return c.PyramidWorkSpec(expected_source_snapshot=snapshot, policy=self.policy)

    def _pyramid_dispatch_eligible(
        self, source: c.ImageSource, profile_version: str | None = None
    ) -> bool:
        return (
            (profile_version is None or profile_version == self.policy.profile_version)
            and self.policy.auto_generate_pyramids
            and self.policy.pyramid_eligible(source.width, source.height)
        )

    def _repair_operation_id(
        self,
        source: c.ImageSource,
        selector: c.ActorImageSourceRequest,
        purpose: str,
        target: str,
    ) -> UUID:
        target_digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
        receipt_scope = hashlib.sha256(
            c.json_bytes(
                {
                    "actor_id": selector.actor_id,
                    "task_id": selector.task_id,
                    "dataset_item_id": selector.dataset_item_id,
                    "policy": self.policy,
                }
            )
        ).hexdigest()
        return uuid5(
            NAMESPACE_URL,
            f"aap-assets-images/v1:{purpose}:{source.owner.kind}:{source.owner.id}:"
            f"{source.source_snapshot}:{target_digest}:{receipt_scope}",
        )

    async def prepare_image_media(
        self, request: c.PrepareImageMediaRequest
    ) -> c.AcceptedImageWork:
        if type(request) is not c.PrepareImageMediaRequest:
            raise c.ImageFailure("invalid_request")
        if request.policy != self.policy:
            raise c.ImageFailure("invalid_request")
        source = await self._observe_actor(
            request,
            expected_source_snapshot=request.expected_source_snapshot,
        )
        admission = request.model_copy(
            update={
                "expected_source_snapshot": source.source_snapshot,
                "policy": self.policy,
            }
        )
        return await self._native_call("prepare_image_media", admission)

    async def retry_image_media(
        self, request: c.RetryImageMediaRequest
    ) -> c.AcceptedImageWork:
        if type(request) is not c.RetryImageMediaRequest:
            raise c.ImageFailure("invalid_request")
        receipt_request = request.model_copy(
            update={"expected_source_snapshot": None, "policy": self.policy}
        )
        try:
            accepted = await self._native_call(
                "retry_image_media",
                receipt_request,
            )
        except c.ImageFailure as exc:
            if exc.code != "source_not_ready":
                raise
            source = await self._observe_actor(
                request,
                expected_source_snapshot=request.expected_source_snapshot,
            )
            admission = request.model_copy(
                update={
                    "expected_source_snapshot": source.source_snapshot,
                    "policy": self.policy,
                }
            )
            return await self._native_call(
                "retry_image_media",
                admission,
            )
        if (
            request.expected_source_snapshot is not None
            and accepted.source is not None
            and accepted.source.source_snapshot != request.expected_source_snapshot
        ):
            raise c.ImageFailure("source_changed")
        return accepted

    async def schedule_image_media(
        self, request: c.ScheduleImageMediaRequest
    ) -> c.AcceptedImageWork:
        if type(request) is not c.ScheduleImageMediaRequest:
            raise c.ImageFailure("invalid_request")
        if request.spec.policy != self.policy:
            raise c.ImageFailure("invalid_request")
        if isinstance(request.spec, c.ImageBackfillSpec):
            return await self._native_call("schedule_image_media", request)
        source = await self._observe_internal(
            c.InternalImageSourceRequest(
                owner=c.ImageOwner(kind=request.owner_kind, id=request.owner_id),
                expected_source_snapshot=request.spec.expected_source_snapshot,
            )
        )
        spec = request.spec.model_copy(
            update={
                "expected_source_snapshot": source.source_snapshot,
                "policy": self.policy,
            }
        )
        admission = request.model_copy(update={"spec": spec})
        return await self._native_call("schedule_image_media", admission)

    async def _prepare_repair(
        self,
        source: c.ImageSource,
        selector: c.ActorImageSourceRequest,
        purpose: c.Purpose,
        target: str,
    ) -> c.AcceptedImageWork:
        request = c.PrepareImageMediaRequest(
            operation_id=self._repair_operation_id(source, selector, purpose, target),
            actor_id=selector.actor_id,
            task_id=selector.task_id,
            dataset_item_id=selector.dataset_item_id,
            purpose=purpose,
            expected_source_snapshot=source.source_snapshot,
            policy=self.policy,
            force=False,
        )
        return await self.prepare_image_media(request)

    async def _best_effort_repair(self, operation: Awaitable[object]) -> bool:
        """Derived references must not deny an otherwise authorized source read."""
        try:
            await operation
            return True
        except c.ImageFailure as exc:
            if exc.code in {
                "permission_denied",
                "operation_busy",
                "operation_conflict",
            }:
                return False
            raise

    async def _invalidate(
        self,
        source: c.ImageSource,
        selector: c.ImageSourceSelector,
        invalidation: c.ImageInvalidation,
    ) -> c.ImageSource:
        result = await self._native_call(
            "invalidate_image_media",
            c.InvalidateImageMediaRequest(
                actor_id=selector.actor_id,
                task_id=selector.task_id,
                dataset_item_id=selector.dataset_item_id,
                invalidation=invalidation,
            ),
        )
        if not self._same_source(source, result.source):
            raise c.ImageFailure("source_changed")
        return result.source

    async def source_reference(
        self, request: c.ReadImageSourceRequest
    ) -> ImageSourceReference:
        source = await self._observe_actor(request)
        original = await self._storage_call(
            "sign_source", source, self.source_url_expiry
        )
        if not isinstance(original, str):
            raise c.ImageFailure("invalid_response")
        thumbnail_url = await self._thumbnail_url(source, request)
        current = await self._final_actor(source, request)
        if thumbnail_url is not None and (
            current.thumbnail_path != source.thumbnail_path
            or not current.thumbnail_current
        ):
            raise c.ImageFailure("source_changed")
        try:
            result = ImageSourceReference(
                actor_id=request.actor_id,
                source=current,
                original_url=original,
                thumbnail_url=thumbnail_url,
                expires_in=self.source_url_expiry,
            )
            require_source_reference_scope(request, result)
            return result
        except ValueError:
            raise c.ImageFailure("invalid_response") from None

    async def raw_source_reference(
        self, request: c.ReadImageSourceRequest
    ) -> ImageSourceReference:
        source = await self._observe_actor(request)
        original = await self._storage_call(
            "sign_source", source, self.source_url_expiry
        )
        if not isinstance(original, str):
            raise c.ImageFailure("invalid_response")
        current = await self._final_actor(source, request)
        try:
            result = ImageSourceReference(
                actor_id=request.actor_id,
                source=current,
                original_url=original,
                thumbnail_url=None,
                expires_in=self.source_url_expiry,
            )
            require_source_reference_scope(request, result, raw_only=True)
            return result
        except ValueError:
            raise c.ImageFailure("invalid_response") from None

    async def source_references(
        self, request: ImageSourceReferencesRequest
    ) -> ImageSourceReferences:
        references: list[ImageSourceReference] = []
        encoded_size = 0
        for start in range(0, len(request.requests), 4):
            batch = await asyncio.gather(
                *(
                    (
                        self.raw_source_reference
                        if request.raw_only
                        else self.source_reference
                    )(item)
                    for item in request.requests[start : start + 4]
                ),
                return_exceptions=True,
            )
            failure = next(
                (row for row in batch if isinstance(row, BaseException)), None
            )
            if failure is not None:
                raise failure
            for reference in batch:
                encoded_size += len(c.json_bytes(reference)) + 2
                if encoded_size > c.PAGE_BYTES:
                    raise c.ImageFailure("resource_budget_exceeded")
                references.append(reference)
        try:
            result = ImageSourceReferences(references=references)
            require_source_references_scope(request, result)
            return result
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None

    async def internal_source_reference(
        self, request: InternalImageSourceReferenceRequest
    ) -> ImageSourceReference:
        source = await self._observe_internal(request.source)
        original = await self._storage_call("sign_source", source, request.expires_in)
        if not isinstance(original, str):
            raise c.ImageFailure("invalid_response")
        current = await self._final_internal(source)
        try:
            result = ImageSourceReference(
                actor_id=None,
                source=current,
                original_url=original,
                thumbnail_url=None,
                expires_in=request.expires_in,
            )
            require_internal_source_reference_scope(request, result)
            return result
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None

    async def generation_reference(
        self, request: ImageGenerationReferenceRequest
    ) -> ImageGenerationReference:
        source = await self._observe_actor(
            request, expected_source_snapshot=request.expected_source_snapshot
        )
        state = await self._read_generation(request, source)
        source = state.source
        if source.width is None or source.height is None:
            await self._best_effort_repair(
                self._prepare_repair(source, request, "dimensions", "dimensions")
            )
            state = await self._read_generation(request, source)
            source = state.source
        if self._pyramid_dispatch_eligible(source, request.profile_version) and (
            state.pyramid.status in {"missing", "not_available", "stale"}
        ):
            await self._prepare_repair(source, request, "pyramid", "pyramid")
            state = await self._read_generation(request, source)
            source = state.source
        required = self.policy.pyramid_required(source.width, source.height)

        overview_url = None
        manifest = None
        if state.pyramid.status == "ready":
            repair_kind = None
            repair_target = None
            try:
                manifest, prefix = await self._read_ready_manifest(state.pyramid)
            except c.ImageFailure as exc:
                if exc.code not in {"object_missing", "invalid_response"}:
                    raise
                repair_kind = "manifest"
                repair_target = state.pyramid.manifest_key or "manifest"
            if repair_kind is None:
                overview_key = prefix + "/overview.webp"
                if state.pyramid.overview_key != overview_key:
                    repair_kind = "manifest"
                    repair_target = state.pyramid.manifest_key or "manifest"
                elif await self._head_cache(overview_key) is None:
                    repair_kind = "overview"
                    repair_target = overview_key
                else:
                    overview_url = await self._sign_cache(
                        overview_key, request.expires_in
                    )
            if repair_kind is not None:
                await self._invalidate_generation(
                    source, request, state.pyramid, repair_kind
                )
                await self._repair_pyramid(
                    source, request, repair_target or repair_kind
                )
                state = await self._read_generation(request, source)
                manifest = None
                if state.pyramid.status == "ready":
                    raise c.ImageFailure("object_missing")
        if overview_url is not None:
            manifest, _ = await self._read_ready_manifest(state.pyramid)
            current = await self._final_actor(source, request)
            final_state = await self._read_generation(request, current)
            if (
                final_state.pyramid.status != "ready"
                or final_state.pyramid.generation != state.pyramid.generation
                or final_state.pyramid.manifest_key != state.pyramid.manifest_key
                or final_state.pyramid.source_snapshot != current.source_snapshot
            ):
                raise c.ImageFailure("source_changed")
            source = current
            state = final_state
            source = state.source
            required = self.policy.pyramid_required(source.width, source.height)
        summary = state.pyramid.summary().model_copy(update={"required": required})
        try:
            result = ImageGenerationReference(
                source=source,
                pyramid=summary,
                pyramid_source_generation=state.pyramid.source_generation,
                pyramid_source_snapshot=state.pyramid.source_snapshot,
                manifest=manifest,
                overview_url=overview_url,
                expires_in=request.expires_in,
            )
            require_generation_reference_scope(request, result)
            return result
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None

    async def asset_references(
        self, request: ImageAssetReferencesRequest
    ) -> ImageAssetReferences:
        source = await self._observe_actor(
            request, expected_source_snapshot=request.expected_source_snapshot
        )
        generation = request.items[0].generation
        state = await self._read_generation(request, source, generation=generation)
        try:
            manifest, prefix = await self._read_ready_manifest(state.pyramid)
        except c.ImageFailure as exc:
            if exc.code not in {"object_missing", "invalid_response"}:
                raise
            await self._invalidate_generation(
                source, request, state.pyramid, "manifest"
            )
            await self._repair_pyramid(
                source, request, state.pyramid.manifest_key or "manifest"
            )
            raise c.ImageFailure("object_missing") from None
        if (
            state.pyramid.source_snapshot != source.source_snapshot
            or state.pyramid.source_generation != source.source_generation
        ):
            raise c.ImageFailure("source_changed")
        entries: list[tuple[str, int, int | None, int | None, int | None, str]] = []
        for item in request.items:
            if item.kind == "overview":
                key = prefix + "/overview.webp"
                entries.append(("overview", item.generation, None, None, None, key))
            else:
                if item.level >= len(manifest.levels):
                    raise c.ImageFailure("invalid_coordinate")
                level = manifest.levels[item.level]
                if item.x >= level.columns or item.y >= level.rows:
                    raise c.ImageFailure("invalid_coordinate")
                key = f"{prefix}/tiles/{item.level}/{item.x}/{item.y}.webp"
                entries.append(
                    (
                        "tile",
                        item.generation,
                        item.level,
                        item.x,
                        item.y,
                        key,
                    )
                )
        missing: list[tuple[str, int, int | None, int | None, int | None, str]] = []
        for entry in entries:
            kind, _generation, _level, _x, _y, key = entry
            if await self._head_cache(key) is None:
                missing.append(entry)
        if missing:
            for kind, _generation, level, x, y, _key in missing:
                await self._invalidate_generation(
                    source,
                    request,
                    state.pyramid,
                    kind,
                    level=level,
                    x=x,
                    y=y,
                )
            await self._repair_pyramid(
                source, request, "|".join(sorted(entry[-1] for entry in missing))
            )
            raise c.ImageFailure("object_missing")
        result_items = []
        for kind, item_generation, level, x, y, key in entries:
            url = await self._sign_cache(key, self.asset_url_expiry)
            result_items.append(
                ImageAssetReference(
                    kind=kind,
                    generation=item_generation,
                    level=level,
                    x=x,
                    y=y,
                    url=url,
                )
            )
        current = await self._final_actor(source, request)
        final_state = await self._read_generation(
            request, current, generation=generation
        )
        if (
            final_state.pyramid.status != "ready"
            or final_state.pyramid.manifest_key != state.pyramid.manifest_key
            or final_state.pyramid.source_snapshot != current.source_snapshot
        ):
            raise c.ImageFailure("source_changed")
        try:
            result = ImageAssetReferences(
                source=current,
                generation=generation,
                profile_version=final_state.pyramid.profile_version,
                expires_in=self.asset_url_expiry,
                items=result_items,
            )
            require_asset_reference_scope(request, result)
            return result
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None

    async def _thumbnail_url(
        self, source: c.ImageSource, request: c.ImageSourceSelector
    ) -> str | None:
        key = source.thumbnail_path
        if key is None or not source.thumbnail_current:
            await self._prepare_small_thumbnail(source, request, key or "initial")
            return None
        if (
            key == f"thumbnails/{source.owner.id}.webp"
            or _IMAGE_THUMBNAIL_KEY.fullmatch(key)
        ):
            match = _IMAGE_THUMBNAIL_KEY.fullmatch(key)
            if match is not None:
                await self._verify_thumbnail_attempt(source, match)
            if await self._head_cache(key) is None:
                if not await self._best_effort_repair(
                    self._invalidate(
                        source,
                        request,
                        c.ImageInvalidation(
                            source_snapshot=source.source_snapshot, kind="thumbnail"
                        ),
                    ),
                ):
                    return None
                await self._prepare_small_thumbnail(source, request, key)
                return None
            return await self._sign_cache(key, self.source_url_expiry)
        if key.startswith("image-pyramids/"):
            state = await self._read_generation(request, source)
            if (
                state.pyramid.status != "ready"
                or state.pyramid.overview_key != key
                or state.pyramid.source_generation != source.source_generation
                or state.pyramid.source_snapshot != source.source_snapshot
            ):
                if source.width is None or source.height is None:
                    await self._best_effort_repair(
                        self._prepare_repair(source, request, "dimensions", key)
                    )
                elif self._pyramid_dispatch_eligible(source):
                    await self._best_effort_repair(
                        self._repair_pyramid(source, request, key)
                    )
                else:
                    await self._prepare_small_thumbnail(source, request, key)
                return None
            _manifest, prefix = await self._read_ready_manifest(state.pyramid)
            if key != prefix + "/overview.webp" or await self._head_cache(key) is None:
                if not await self._best_effort_repair(
                    self._invalidate_generation(
                        source, request, state.pyramid, "overview"
                    )
                ):
                    return None
                await self._best_effort_repair(
                    self._repair_pyramid(source, request, key)
                )
                return None
            return await self._sign_cache(key, self.source_url_expiry)
        raise c.ImageFailure("invalid_response")

    async def _verify_thumbnail_attempt(self, source, match) -> None:
        job_id = UUID(match.group("job"))
        generation = int(match.group("generation"))
        status = await self._native_call(
            "read_internal_image_work",
            c.ReadInternalImageWorkRequest(job_id=job_id),
        )
        if (
            status.work.job_id != job_id
            or status.work.owner != source.owner
            or status.work.kind != "thumbnail"
        ):
            raise c.ImageFailure("invalid_response")
        if status.work.generation == generation and status.work.status == "ready":
            if status.work.source_generation != source.source_generation:
                raise c.ImageFailure("source_changed")
            if (
                not isinstance(status.spec, c.ThumbnailWorkSpec)
                or status.spec.expected_source_snapshot != source.source_snapshot
            ):
                raise c.ImageFailure("source_changed")
            return

        if (
            status.work.generation <= generation
            or status.work.status not in {"pending", "running", "failed", "canceled"}
            or status.work.source_generation != source.source_generation
            or not isinstance(status.spec, c.ThumbnailWorkSpec)
        ):
            raise c.ImageFailure("invalid_response")
        if status.spec.expected_source_snapshot != source.source_snapshot:
            raise c.ImageFailure("source_changed")
        try:
            publication = _RetainedThumbnailPublication.model_validate(
                status.progress.get("retained_publication")
            )
        except (TypeError, ValueError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        if (
            publication.job_id != job_id
            or publication.generation != generation
            or publication.owner != source.owner
        ):
            raise c.ImageFailure("invalid_response")
        if (
            publication.source_generation != source.source_generation
            or publication.source_snapshot != source.source_snapshot
            or publication.source_identity != source.object_identity
        ):
            raise c.ImageFailure("source_changed")
        if publication.result.artifact.key != match.string:
            raise c.ImageFailure("invalid_response")

    async def _prepare_small_thumbnail(
        self, source: c.ImageSource, selector: c.ImageSourceSelector, target: str
    ) -> None:
        if source.width is None or source.height is None:
            await self._best_effort_repair(
                self._prepare_repair(source, selector, "dimensions", "dimensions")
            )
            return
        if self._pyramid_dispatch_eligible(source):
            await self._best_effort_repair(
                self._repair_pyramid(source, selector, target)
            )
            return
        if self.policy.pyramid_eligible(source.width, source.height):
            return
        size = (
            source.object_identity.byte_size
            if source.object_identity is not None
            else source.file_size
        )
        if size is None or size > self.policy.thumbnail_max_source_bytes:
            return
        await self._best_effort_repair(
            self._prepare_repair(source, selector, "thumbnail", target)
        )

    async def _read_generation(
        self,
        request: c.ImageSourceSelector,
        source: c.ImageSource,
        *,
        generation: int | None = None,
    ) -> c.ImageGenerationRead:
        selector = self._selector(request)
        native_request = c.ReadImageGenerationRequest(
            actor_id=selector.actor_id,
            task_id=selector.task_id,
            dataset_item_id=selector.dataset_item_id,
            generation=generation,
            profile_version=(
                getattr(request, "profile_version", None) or self.policy.profile_version
            ),
        )
        result = await self._native_call("read_image_generation", native_request)
        if not self._same_source(source, result.source):
            raise c.ImageFailure("source_changed")
        return result

    async def _read_ready_manifest(
        self, pyramid: c.ImagePyramidGeneration
    ) -> tuple[c.ImagePyramidManifest, str]:
        if (
            pyramid.status != "ready"
            or pyramid.asset_id is None
            or pyramid.generation is None
            or pyramid.source_generation is None
            or pyramid.source_snapshot is None
            or pyramid.manifest is None
            or pyramid.manifest_key is None
            or pyramid.manifest_digest is None
            or pyramid.overview_key is None
        ):
            raise c.ImageFailure("asset_missing")
        match = _PYRAMID_MANIFEST_KEY.fullmatch(pyramid.manifest_key)
        if (
            match is None
            or match.group("asset") != str(pyramid.asset_id)
            or int(match.group("generation")) != pyramid.generation
        ):
            raise c.ImageFailure("invalid_response")
        prefix = match.group("prefix")
        if pyramid.overview_key != prefix + "/overview.webp":
            raise c.ImageFailure("invalid_response")
        try:
            raw = await self._storage_call(
                "read_cache", pyramid.manifest_key, _MANIFEST_MAX_BYTES
            )
        except c.ImageFailure as exc:
            if exc.code == "object_missing":
                raise
            raise
        if not isinstance(raw, bytes) or len(raw) > _MANIFEST_MAX_BYTES:
            raise c.ImageFailure("invalid_response")
        try:

            def reject_constant(_value):
                raise ValueError("nonfinite manifest JSON")

            data = json.loads(raw, parse_constant=reject_constant)
            manifest = c.ImagePyramidManifest.model_validate(data)
            if (
                canonical_manifest_bytes(data) != raw
                or sha256_digest(raw) != pyramid.manifest_digest
                or manifest != pyramid.manifest
                or manifest.generation != pyramid.generation
                or sum(level.rows * level.columns for level in manifest.levels)
                != pyramid.tile_count
            ):
                raise ValueError("image manifest disagrees with its projection")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise c.ImageFailure("invalid_response") from None
        return manifest, prefix

    async def _head_cache(self, key: str) -> c.ObjectIdentity | None:
        result = await self._storage_call("head_cache", key)
        if result is not None and not isinstance(result, c.ObjectIdentity):
            raise c.ImageFailure("invalid_response")
        return result

    async def _sign_cache(self, key: str, expires_in: int) -> str:
        result = await self._storage_call("sign_cache", key, expires_in)
        if not isinstance(result, str):
            raise c.ImageFailure("invalid_response")
        return result

    async def _invalidate_generation(
        self,
        source: c.ImageSource,
        request: c.ImageSourceSelector,
        pyramid: c.ImagePyramidGeneration,
        kind: str,
        **coordinates,
    ) -> None:
        if pyramid.generation is None:
            raise c.ImageFailure("invalid_response")
        await self._invalidate(
            source,
            request,
            c.ImageInvalidation(
                source_snapshot=source.source_snapshot,
                kind=kind,
                generation=pyramid.generation,
                profile_version=pyramid.profile_version,
                **coordinates,
            ),
        )

    async def _repair_pyramid(
        self, source: c.ImageSource, selector: c.ImageSourceSelector, target: str
    ) -> None:
        selected_profile = getattr(selector, "profile_version", None)
        if (
            selected_profile is not None
            and selected_profile != self.policy.profile_version
        ):
            return
        await self._prepare_repair(source, selector, "pyramid", target)
