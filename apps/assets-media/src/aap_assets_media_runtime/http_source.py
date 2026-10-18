"""Read selected-owner source references for a trusted CLI probe dry-run."""

from __future__ import annotations

from pathlib import Path

import httpx

from aap_assets_media.contract import (
    InternalSourceRequest,
    MediaFailure,
    MediaSource,
    ObjectIdentity,
)


class HttpMediaSourceStorage:
    def __init__(self, client, source: MediaSource, *, transport=None):
        self.client = client
        self.source = source
        self.reference = None
        self.transport = transport

    async def head_source(self, key: str) -> ObjectIdentity:
        if key != self.source.file_path:
            raise MediaFailure("invalid_request")
        reference = await self.client.internal_source_reference(
            InternalSourceRequest(dataset_item_id=self.source.dataset_item_id)
        )
        current = reference.source
        if (
            current.dataset_item_id != self.source.dataset_item_id
            or current.source_snapshot != self.source.source_snapshot
            or current.source_generation != self.source.source_generation
            or current.file_path != self.source.file_path
            or current.source_drifted
        ):
            raise MediaFailure("source_changed")
        if current.object_identity is None:
            raise MediaFailure("invalid_response")
        self.reference = reference
        return current.object_identity

    async def download_source(self, source: MediaSource, destination: Path) -> None:
        reference = self.reference
        if (
            reference is None
            or source.source_snapshot != self.source.source_snapshot
            or source.file_path != self.source.file_path
            or source.object_identity != reference.source.object_identity
        ):
            raise MediaFailure("source_changed")
        identity = source.object_identity
        try:
            async with httpx.AsyncClient(
                timeout=120,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "GET",
                    reference.original_url,
                    headers={"If-Match": '"' + identity.etag.strip('"') + '"'},
                ) as response:
                    if response.status_code == 412:
                        raise MediaFailure("source_changed")
                    if response.status_code == 404:
                        raise MediaFailure("object_missing")
                    if response.status_code != 200:
                        raise MediaFailure("storage_unavailable")
                    if (
                        response.headers.get("etag", "").strip('"')
                        != identity.etag.strip('"')
                        or response.headers.get("x-amz-version-id")
                        != identity.version_id
                    ):
                        raise MediaFailure("source_changed")
                    written = 0
                    with destination.open("xb") as output:
                        async for chunk in response.aiter_raw(chunk_size=1024 * 1024):
                            written += len(chunk)
                            if written > identity.byte_size:
                                raise MediaFailure("source_changed")
                            output.write(chunk)
                    if written != identity.byte_size:
                        raise MediaFailure("source_changed")
        except BaseException as exc:
            destination.unlink(missing_ok=True)
            if isinstance(exc, httpx.HTTPError):
                raise MediaFailure("storage_unavailable") from None
            raise
