"""Video reference workflows shared by local composition and HTTP control."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from aap_assets_media import contract as c
from aap_assets_media.transport import (
    MediaReferences,
    SourceReference,
    read_all_samples,
    read_all_timetable,
    read_samples_document,
)

log = logging.getLogger(__name__)
_NEW_CACHE = re.compile(
    r"^media-assets/[0-9a-f-]{36}/g[1-9][0-9]*/a[0-9a-f-]{36}/"
    r"(?:poster\.webp|playback\.mp4|frame\.(?:webp|jpeg)|chunk\.mp4)$"
)


def _require_observed(expected: c.MediaSource, current: c.MediaSource) -> None:
    if current.source_drifted or (
        current.source_snapshot != expected.source_snapshot
        or current.source_generation != expected.source_generation
    ):
        raise c.MediaFailure("source_changed")


def cache_key(source: c.MediaSource, key: str) -> str:
    """Only sign/delete derived namespaces; raw sources never become cache keys."""
    legacy = f"videos/{source.dataset_item_id}/"
    if not _NEW_CACHE.fullmatch(key) and not (
        key.startswith(legacy)
        and not any(part in {"", ".", ".."} for part in key.split("/"))
    ):
        raise c.MediaFailure("invalid_response")
    return key


class MediaControl:
    def __init__(
        self,
        database,
        storage,
        *,
        wakeup: Callable[[object, int], Awaitable[None]] | None = None,
        expires_in: int = 3600,
    ):
        self.database = database
        self.storage = storage
        self.wakeup = wakeup
        self.expires_in = expires_in

    async def _wake(self, job_id, generation):
        if self.wakeup is not None:
            try:
                await self.wakeup(job_id, generation)
            except Exception:
                # Acceptance is already durable. A broker failure must not turn
                # it into a rejected request or a processing failure.
                log.warning("Media work accepted; execution wakeup unavailable")

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        definition = c.OPERATIONS.get(operation)
        if definition is None or definition.role == "worker":
            raise c.MediaFailure("permission_denied")
        result = await self.database.call(operation, request)
        if isinstance(result, c.AcceptedWork) and result.work.status == "pending":
            await self._wake(result.work.job_id, result.work.generation)
        if isinstance(result, c.MediaAssets) and any(
            asset.job_id is not None and asset.status == "pending"
            for asset in result.assets
        ):
            await self._wake(None, 0)
        return result

    async def _metadata(self, source: c.MediaSource) -> c.AcceptedWork:
        return await self.call(
            "schedule_video_media",
            c.ScheduleWorkRequest(
                operation_id=None,
                dataset_item_id=source.dataset_item_id,
                spec=c.MetadataSpec(expected_source_snapshot=source.source_snapshot),
            ),
        )

    async def _observe(self, request: c.ReadSourceRequest) -> c.MediaSource:
        source = await self.call("read_media_source", request)
        expected = (source.source_snapshot, source.source_generation)
        identity = await self.storage.head_source(source.file_path)
        source = await self.call(
            "observe_media_source",
            c.ObserveSourceRequest(
                **request.model_dump(),
                source_snapshot=source.source_snapshot,
                object_identity=identity,
            ),
        )
        if (
            source.source_drifted
            or (source.source_snapshot, source.source_generation) != expected
        ):
            raise c.MediaFailure("source_changed")
        return source

    async def _invalidate(
        self,
        request: c.ReadSourceRequest,
        source: c.MediaSource,
        key: str,
        kind: str,
        **locator,
    ) -> c.MediaSource:
        result = await self.call(
            "invalidate_video_media",
            c.InvalidateRequest(
                **request.model_dump(),
                invalidation=c.Invalidation(
                    source_snapshot=source.source_snapshot,
                    expected_key=key,
                    kind=kind,
                    **locator,
                ),
            ),
        )
        if (
            result.source.source_snapshot != source.source_snapshot
            or result.source.source_generation != source.source_generation
        ):
            raise c.MediaFailure("source_changed")
        if result.source.timetable_revision != source.timetable_revision:
            raise c.MediaFailure("dependency_changed")
        if not result.changed:
            raise c.MediaFailure("dependency_changed")
        return result.source

    async def _source_links(
        self, request: c.ReadSourceRequest, source: c.MediaSource
    ) -> SourceReference:
        original = await self.storage.sign_source(source.file_path, self.expires_in)
        video = original
        poster = None
        metadata = source.video_metadata
        missing = False
        if source.file_type == "video" and (
            source.materialized_generation == source.source_generation
        ):
            for kind, field in (
                ("playback", "playback_path"),
                ("poster", "poster_frame_path"),
            ):
                key = metadata.get(field)
                if not key:
                    continue
                if not isinstance(key, str):
                    raise c.MediaFailure("invalid_response")
                cache_key(source, key)
                if await self.storage.head_cache(key) is None:
                    source = await self._invalidate(request, source, key, kind)
                    missing = True
                elif kind == "playback":
                    video = await self.storage.sign_cache(key, self.expires_in)
                else:
                    poster = await self.storage.sign_cache(key, self.expires_in)
        if source.file_type == "video" and (
            missing or source.materialized_generation != source.source_generation
        ):
            await self._metadata(source)
        return SourceReference(
            source=source,
            original_url=original,
            video_url=video,
            poster_url=poster,
            expires_in=self.expires_in,
        )

    async def source_reference(self, request: c.ReadSourceRequest) -> SourceReference:
        return await self._source_links(request, await self._observe(request))

    async def raw_source_reference(
        self, request: c.ReadSourceRequest
    ) -> SourceReference:
        source = await self.call("read_media_source", request)
        original = await self.storage.sign_source(source.file_path, self.expires_in)
        poster = None
        if source.file_type == "video" and not source.source_drifted:
            if source.materialized_generation != source.source_generation:
                await self._metadata(source)
            else:
                key = source.video_metadata.get("poster_frame_path")
                if isinstance(key, str):
                    try:
                        cache_key(source, key)
                        if await self.storage.head_cache(key) is not None:
                            poster = await self.storage.sign_cache(key, self.expires_in)
                    except c.MediaFailure:
                        # Task metadata remains readable during object outages.
                        poster = None
        return SourceReference(
            source=source,
            original_url=original,
            video_url=original,
            poster_url=poster,
            expires_in=self.expires_in,
        )

    async def internal_source_reference(
        self, request: c.InternalSourceRequest, *, expires_in: int = 3600
    ) -> SourceReference:
        if request.source_snapshot is not None or not 1 <= expires_in <= 604800:
            raise c.MediaFailure("invalid_request")
        source = await self.call("resolve_internal_media_source", request)
        expected = (source.source_snapshot, source.source_generation)
        identity = await self.storage.head_source(source.file_path)
        source = await self.call(
            "resolve_internal_media_source",
            c.InternalSourceRequest(
                dataset_item_id=request.dataset_item_id,
                source_snapshot=source.source_snapshot,
                object_identity=identity,
            ),
        )
        if (
            source.source_drifted
            or (source.source_snapshot, source.source_generation) != expected
        ):
            raise c.MediaFailure("source_changed")
        original = await self.storage.sign_source(source.file_path, expires_in)
        return SourceReference(
            source=source,
            original_url=original,
            video_url=original,
            poster_url=None,
            expires_in=expires_in,
        )

    async def source_references(
        self, requests: list[c.ReadSourceRequest], *, raw_only: bool = False
    ) -> list[SourceReference]:
        references = []
        encoded_size = 1024
        for start in range(0, len(requests), 4):
            batch = await asyncio.gather(
                *(
                    (self.raw_source_reference if raw_only else self.source_reference)(
                        request
                    )
                    for request in requests[start : start + 4]
                )
            )
            for reference in batch:
                encoded_size += len(c.json_bytes(reference)) + 2
                if encoded_size > c.PAGE_BYTES:
                    raise c.MediaFailure("resource_budget_exceeded")
                references.append(reference)
        return references

    async def assets_reference(self, request: c.AssetsRequest) -> MediaReferences:
        source_request = c.ReadSourceRequest(
            actor_id=request.actor_id,
            dataset_item_id=request.dataset_item_id,
            task_id=request.task_id,
        )
        source = await self._observe(source_request)
        expected = request.selection.expected_source_snapshot
        if expected is not None and expected != source.source_snapshot:
            raise c.MediaFailure("source_changed")
        if source.file_type != "video":
            raise c.MediaFailure("source_missing")
        if source.materialized_generation != source.source_generation:
            await self._metadata(source)
            raise c.MediaFailure("metadata_not_ready")
        selection = request.selection.model_copy(
            update={"expected_source_snapshot": source.source_snapshot}
        )
        request = request.model_copy(update={"selection": selection})
        state = await self.call("ensure_video_media", request)
        _require_observed(source, state.source)
        missing = []
        for asset in state.assets:
            if asset.status == "ready":
                cache_key(state.source, asset.storage_key)
                if await self.storage.head_cache(asset.storage_key) is None:
                    missing.append(asset)
        for asset in missing:
            await self._invalidate(
                source_request,
                state.source,
                asset.storage_key,
                asset.kind,
                **self._locator(asset),
            )
        if missing:
            # A forced selection is a one-time command, not a repeatable read.
            selection = request.selection.model_copy(update={"force": False})
            state = await self.call(
                "ensure_video_media",
                request.model_copy(update={"selection": selection}),
            )
        _require_observed(source, state.source)
        state = await self._sign_assets(state)
        links = await self._source_links(source_request, state.source)
        if links.source.source_snapshot != state.source.source_snapshot:
            raise c.MediaFailure("source_changed")
        return MediaReferences(
            state=state.model_copy(update={"source": links.source}),
            video_url=links.video_url,
            expires_in=self.expires_in,
        )

    @staticmethod
    def _locator(asset: c.MediaAsset) -> dict:
        if asset.kind == "frame":
            return dict(
                frame_index=asset.frame_index, width=asset.width, format=asset.format
            )
        return dict(chunk_id=asset.chunk_id)

    async def _sign_assets(self, state: c.MediaAssets) -> c.MediaAssets:
        assets = []
        for asset in state.assets:
            if asset.status == "ready":
                cache_key(state.source, asset.storage_key)
                if await self.storage.head_cache(asset.storage_key) is None:
                    # A second disappearance races the previous invalidation;
                    # return a retryable failure, never a URL known to be dead.
                    raise c.MediaFailure("object_missing")
                asset = asset.model_copy(
                    update={
                        "url": await self.storage.sign_cache(
                            asset.storage_key, self.expires_in
                        ),
                        "expires_in": self.expires_in,
                    }
                )
            assets.append(asset)
        return state.model_copy(update={"assets": assets})

    async def internal_frame_reference(
        self, request: c.InternalFrameRequest
    ) -> MediaReferences:
        observed = await self.internal_source_reference(
            c.InternalSourceRequest(dataset_item_id=request.dataset_item_id)
        )
        state = await self.call("read_internal_video_frame", request)
        source = state.source
        _require_observed(observed.source, source)
        if source.materialized_generation != source.source_generation:
            await self._metadata(source)
            state = state.model_copy(update={"assets": []})
        else:
            ready = next((a for a in state.assets if a.status == "ready"), None)
            force = False
            if ready is not None:
                cache_key(source, ready.storage_key)
                force = await self.storage.head_cache(ready.storage_key) is None
            scheduled = any(a.job_id is not None for a in state.assets)
            if (ready is None and not scheduled) or force:
                await self.call(
                    "schedule_video_media",
                    c.ScheduleWorkRequest(
                        operation_id=None,
                        dataset_item_id=source.dataset_item_id,
                        spec=c.FrameSpec(
                            frame_index=request.frame_index,
                            width=request.width,
                            format=request.format,
                            expected_source_snapshot=source.source_snapshot,
                        ),
                        force=force,
                    ),
                )
                state = await self.call("read_internal_video_frame", request)
            _require_observed(observed.source, state.source)
            state = await self._sign_assets(state)
        _require_observed(observed.source, state.source)
        return MediaReferences(
            state=state,
            video_url=observed.original_url,
            expires_in=self.expires_in,
        )

    async def read_all_timetable(self, request: c.TimetableRequest):
        return await read_all_timetable(self, request)

    async def read_all_samples(self, request: c.SamplesRequest):
        return await read_all_samples(self, request)

    async def read_samples_document(self, request: c.SamplesRequest):
        return await read_samples_document(self, request)
