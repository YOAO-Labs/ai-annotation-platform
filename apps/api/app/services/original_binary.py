"""Authenticated complete managed-original downloads with frozen ownership.

Routes own HTTP serialization. Physical capture and response ownership stay in
original_binary_response; this module owns current authority and source checks.
"""

from dataclasses import dataclass
from typing import Literal
import uuid

from fastapi import HTTPException

from app.services import original_content_access as access
from app.services import original_sources, storage_access
from app.services.content_snapshot import ContentError
from app.services.managed_original_reader import ObjectSpec


@dataclass(frozen=True)
class Resource:
    kind: Literal["task", "item", "camera"]
    task_id: uuid.UUID | None = None
    dataset_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    link_id: uuid.UUID | None = None

    def __post_init__(self):
        required = {
            "task": ("task_id",),
            "item": ("dataset_id", "item_id"),
            "camera": ("task_id", "link_id"),
        }.get(self.kind)
        if required is None or any(
            (not isinstance(getattr(self, name), uuid.UUID))
            if name in required
            else getattr(self, name) is not None
            for name in ("task_id", "dataset_id", "item_id", "link_id")
        ):
            raise ContentError("original_resource_invalid", 422)

    @property
    def scope(self):
        return "datasets:read" if self.kind == "item" else "tasks:read"


async def _credentials(db, actor, request, api_key_id):
    if request is None:
        raise HTTPException(403, {"code": "content_authority_revoked"})
    proof = await access.capture_credentials(db, actor, request)
    if proof is None:
        raise HTTPException(403, {"code": "content_authority_revoked"})
    if api_key_id is not None:
        try:
            matches = uuid.UUID(str(api_key_id)) == proof.key_id
        except (ValueError, TypeError, AttributeError):
            matches = False
        if not matches:
            raise HTTPException(403, {"code": "api_key_authority_revoked"})
    return proof


async def _source(db, resource, actor):
    with db.no_autoflush:
        if resource.kind == "item":
            dataset_id = await access.managed_dataset(
                db, resource.item_id, actor, lock=True
            )
            if dataset_id != resource.dataset_id:
                raise HTTPException(404, "Dataset item not found")
            source = await original_sources.resolve_item(
                db, resource.item_id, lock=True
            )
            if source.item_facts.dataset_id != resource.dataset_id:
                raise ContentError("content_binding_changed")
        else:
            await access.visible_task(db, resource.task_id, actor, lock=True)
            source = await (
                original_sources.resolve_task_link(
                    db, resource.task_id, resource.link_id, lock=True
                )
                if resource.kind == "camera"
                else original_sources.resolve_task(db, resource.task_id, lock=True)
            )
    if source.bucket is None:
        raise ContentError("reference_requires_verified_content")
    return source


async def _current(db, resource, proof):
    actor, key = await access.actor_and_key(
        db, proof.actor_id, proof.key_id, resource.scope, proof
    )
    return await _source(db, resource, actor), key


async def public_identity(db, resource, actor, request, api_key_id=None):
    """Return saved identity only; neither probe nor claim physical availability."""
    proof = await _credentials(db, actor, request, api_key_id)
    source, key = await _current(db, resource, proof)
    await db.commit()
    access.check_clock(proof, key)
    return {
        "owner_kind": source.owner_kind,
        "content_id": source.content_id,
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "media_type": source.media_type,
        "verification_state": "verified",
        "original_availability": "unchecked",
    }


async def snapshot(
    db,
    resource,
    actor,
    *,
    expected_content_id,
    expected_sha256,
    request,
    api_key_id=None,
):
    """Return one complete verified stage; its caller must transfer or close it."""
    proof = await _credentials(db, actor, request, api_key_id)
    source, key = await _current(db, resource, proof)
    if source.content_id != expected_content_id or source.sha256 != expected_sha256:
        raise ContentError("content_binding_changed")
    await db.commit()
    access.check_clock(proof, key)

    async def validate():
        current, current_key = await _current(db, resource, proof)
        if current_key != key:
            raise HTTPException(403, {"code": "api_key_authority_revoked"})
        if current != source:
            raise ContentError("content_binding_changed")

    from app.services.original_binary_response import capture_download

    stage = None
    try:
        purpose = (
            "task_original_read_"
            if source.owner_kind == "task_original"
            else "dataset_original_read_"
        ) + source.content_id.hex
        async with await storage_access.acquire(
            db,
            source.location_id,
            purpose,
            expected_revision=source.maintenance_revision,
        ):
            await validate()
            await db.commit()
            access.check_clock(proof, key)
            stage = await capture_download(
                ObjectSpec(
                    source.endpoint_url,
                    source.bucket,
                    source.object_key,
                    source.sha256,
                    source.size_bytes,
                )
            )
        await validate()
        await db.commit()
        access.check_clock(proof, key)
        return stage
    except BaseException:
        if stage is not None:
            stage.close()
        raise
