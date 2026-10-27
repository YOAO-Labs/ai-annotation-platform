"""Synthetic avatar ownership evidence for the isolated offline recovery drill.

Pending and retired bytes are deliberately retained to verify that relocation
does not silently publish or discard uncertain work and cleanup responsibilities.
"""

from __future__ import annotations

import hashlib
import io
import uuid

STATES = ("pending", "ready", "retired")


def token_for(state):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aap:isolated-recovery-avatar:{state}").hex


def content_for(state):
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (256, 256), (23, 61, 83 + STATES.index(state))).save(
        stream, format="WEBP"
    )
    return stream.getvalue()


async def seed_avatars(db, *, actor, storage, now):
    """Allocate three explicit owners without publishing pending/retired tokens."""
    from app.db.models.avatar_upload import AvatarUpload
    from app.services import avatar_storage

    for state in STATES:
        token, raw = token_for(state), content_for(state)
        row = AvatarUpload(
            token=token,
            owner_id=actor.id,
            storage_ref=await avatar_storage.allocate(db, token),
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            state=state,
            retired_at=now if state == "retired" else None,
        )
        db.add(row)
        await db.flush()
        owner = avatar_storage.resolve(row)
        storage.client.put_object(
            Bucket=owner.bucket,
            Key=owner.object_key,
            Body=raw,
            ContentType="image/webp",
        )
    actor.avatar_ref = "upload:" + token_for("ready")
    await db.flush()


async def verify_avatars(db, *, storage):
    """Verify full copied bytes and independent state/reference facts."""
    from sqlalchemy import select
    from app.db.models.avatar_upload import AvatarUpload
    from app.db.models.user import User
    from app.services import avatar_storage

    for state in STATES:
        token, expected = token_for(state), content_for(state)
        row = await db.get(AvatarUpload, token)
        assert row is not None and row.state == state
        assert (row.retired_at is not None) == (state == "retired")
        assert row.size_bytes == len(expected)
        assert row.sha256 == hashlib.sha256(expected).hexdigest()
        owner = avatar_storage.resolve(row)
        assert owner.bucket == storage.avatars_bucket
        response = storage.client.get_object(Bucket=owner.bucket, Key=owner.object_key)
        try:
            raw = response["Body"].read(row.size_bytes + 1)
        finally:
            response["Body"].close()
        assert raw == expected
        references = list(
            await db.scalars(select(User).where(User.avatar_ref == "upload:" + token))
        )
        assert len(references) == (1 if state == "ready" else 0)
        if references:
            assert references[0].id == row.owner_id
    return {
        "pinned_avatar_uploads": len(STATES),
        "verified_avatar_objects": len(STATES),
    }
