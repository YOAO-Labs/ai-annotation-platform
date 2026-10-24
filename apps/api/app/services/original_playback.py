"""Prepare verified video originals without giving native media a bearer token."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import threading
from uuid import UUID

from fastapi import HTTPException

from app.services import managed_original_reader as reader
from app.services import original_binary as originals
from app.services.content_snapshot import ContentError
from app.services.original_request import connected_snapshot
from app.services.original_playback_registry import MAX_STAGE_BYTES

COPY_SECONDS = 900
_copy_slots = threading.BoundedSemaphore(2)


@dataclass(frozen=True)
class PlaybackContext:
    original: originals.AuthorizedOriginal = field(repr=False)
    session_id: UUID


async def capture_video(spec):
    return await reader.capture(
        spec,
        max_bytes=MAX_STAGE_BYTES,
        timeout_seconds=COPY_SECONDS,
        admission=_copy_slots,
        transport_timeout_seconds=30,
    )


async def prepare(db, resource, actor, payload, request, registry):
    original = await originals.authorize_original(
        db,
        resource,
        actor,
        request=request,
        api_key_id=getattr(request.state, "api_key_id", None),
        expected_content_id=payload.content_id,
        expected_sha256=payload.sha256,
    )
    if original.source.media_type != "video":
        raise ContentError("original_playback_requires_video", 415)
    context = PlaybackContext(original, payload.session_id)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    proof = original.proof
    if proof.jwt is not None:
        expires_at = min(expires_at, proof.jwt.expires_at)
    if proof.key_proof is not None and proof.key_proof[2] is not None:
        expires_at = min(expires_at, proof.key_proof[2])

    async def capture():
        stage = await connected_snapshot(
            request, originals.capture_authorized(db, original, capture_video)
        )
        if stage is None:
            raise HTTPException(499, {"code": "original_playback_disconnected"})
        return stage

    return await registry.prepare(
        key=context,
        context=context,
        size_bytes=original.source.size_bytes,
        factory=capture,
        expires_at=expires_at,
        media_type="application/octet-stream",
    )
