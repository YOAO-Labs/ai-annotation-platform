"""Reversible dataset state with actor-bound review and independent write fences."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi import HTTPException
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db.enums import UserRole
from app.services.assets_dataset_data import dataset_metadata
from app.services.audit import AuditService
from app.services.identity_account_lock_data import IdentityAccountLockData

PREVIEW_TTL = timedelta(minutes=5)
AUDIENCE = "dataset-lifecycle-change"


def state(dataset):
    return "archived" if dataset.archived_at is not None else "active"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


async def _dataset(db, dataset_id, *, lock=False):
    if not lock:
        dataset = await dataset_metadata(db, dataset_id)
    else:
        try:
            async with db.begin_nested():
                dataset = await dataset_metadata(db, dataset_id, lock_nowait=True)
        except DBAPIError as exc:
            code = getattr(exc.orig, "sqlstate", None) or getattr(
                exc.orig, "pgcode", None
            )
            if code != "55P03":
                raise
            raise HTTPException(409, detail={"code": "dataset_busy"}) from None
    if dataset is None:
        raise HTTPException(404, "Dataset not found")
    return dataset


async def require_writable(db, dataset_id, *, expected_revision=None, lock=False):
    """Protect dataset writes without changing Task or frozen-content read policy."""
    dataset = await _dataset(db, dataset_id, lock=lock)
    if dataset.archived_at is not None:
        raise HTTPException(409, detail={"code": "dataset_archived"})
    if (
        expected_revision is not None
        and dataset.lifecycle_revision != expected_revision
    ):
        raise HTTPException(409, detail={"code": "dataset_lifecycle_changed"})
    return dataset


async def lock_actor(db, actor, api_key_id):
    from app.services.dataset_content import key_authority

    proof = await key_authority(db, actor, api_key_id, "datasets:write")
    accounts = await IdentityAccountLockData(db).lock_accounts(
        [actor.id], mode="share", nowait=False
    )
    actor = accounts.get(actor.id)
    if (
        actor is None
        or not actor.is_active
        or actor.role not in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    ):
        raise HTTPException(403, detail={"code": "access_revoked"})
    if await key_authority(db, actor, api_key_id, "datasets:write") != proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    return actor, proof


def dependencies(dataset_id):
    return text(
        "SELECT * FROM yoao_assets_v1.dataset_archive_impact(CAST(:id AS uuid))"
    ).bindparams(id=dataset_id)


async def review(db, dataset_id, payload, actor, *, api_key_id=None):
    from app.services.dataset_content import key_authority

    actor, proof = await lock_actor(db, actor, api_key_id)
    dataset = await _dataset(db, dataset_id, lock=True)
    if actor.role != UserRole.SUPER_ADMIN and dataset.created_by != actor.id:
        raise HTTPException(404, "Dataset not found")
    if dataset.lifecycle_revision != payload.expected_revision:
        raise HTTPException(409, detail={"code": "dataset_lifecycle_changed"})
    if (payload.operation == "archive") != (dataset.archived_at is None):
        raise HTTPException(409, detail={"code": "dataset_lifecycle_preview_changed"})
    binding = digest(
        {
            "actor": actor.id,
            "key": api_key_id,
            "authority": proof,
            "dataset": dataset.id,
            "created_by": dataset.created_by,
            "name": dataset.name,
            "description": dataset.description,
            "metadata": dataset.metadata_,
            "data_type": dataset.data_type,
            "is_temporal": dataset.is_temporal,
            "archived_at": dataset.archived_at,
            "archived_by": dataset.archived_by,
            "revision": dataset.lifecycle_revision,
            "operation": payload.operation,
        }
    )
    snapshot = dict((await db.execute(dependencies(dataset.id))).mappings().one())
    measured_at = snapshot.pop("measured_at")
    fingerprint = snapshot.pop("fingerprint")
    counts = {key: int(value) for key, value in snapshot.items()}
    current = await IdentityAccountLockData(db).read_actor_status(actor.id)
    if (
        not current
        or not current.is_active
        or current.role not in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    ):
        raise HTTPException(403, detail={"code": "access_revoked"})
    if current.role != UserRole.SUPER_ADMIN and dataset.created_by != current.id:
        raise HTTPException(404, "Dataset not found")
    if await key_authority(db, current, api_key_id, "datasets:write") != proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    return (
        dataset,
        actor,
        {
            "dataset_id": dataset.id,
            "lifecycle_revision": dataset.lifecycle_revision,
            "state": state(dataset),
            "operation": payload.operation,
            "measured_at": measured_at,
            "counts": counts,
            "frozen_coverage": "recorded_dataset_membership_only",
            "binding": binding,
            "dependencies": digest({"fingerprint": fingerprint, "counts": counts}),
        },
    )


async def preview(db, dataset_id, payload, actor, *, api_key_id=None):
    _, _, reviewed = await review(db, dataset_id, payload, actor, api_key_id=api_key_id)
    binding, dependencies_digest = reviewed.pop("binding"), reviewed.pop("dependencies")
    expires_at = reviewed["measured_at"] + PREVIEW_TTL
    token = jwt.encode(
        {
            "aud": AUDIENCE,
            "exp": expires_at,
            "binding": binding,
            "dependencies": dependencies_digest,
            "measured_at": reviewed["measured_at"].isoformat(),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {**reviewed, "expires_at": expires_at, "impact_token": token}


def decode_token(token):
    try:
        data = jwt.decode(
            token, settings.secret_key, algorithms=["HS256"], audience=AUDIENCE
        )
        if not all(
            key in data for key in ("exp", "binding", "dependencies", "measured_at")
        ):
            raise ValueError()
        if data["exp"] <= datetime.now(timezone.utc).timestamp():
            raise ExpiredSignatureError()
        return data
    except ExpiredSignatureError:
        raise HTTPException(
            409, detail={"code": "dataset_lifecycle_preview_expired"}
        ) from None
    except (JWTError, ValueError, TypeError):
        raise HTTPException(
            409, detail={"code": "dataset_lifecycle_preview_invalid"}
        ) from None


async def apply(db, dataset_id, payload, actor, *, api_key_id=None, request=None):
    token = decode_token(payload.impact_token)
    dataset, actor, reviewed = await review(
        db, dataset_id, payload, actor, api_key_id=api_key_id
    )
    if any(token[key] != reviewed[key] for key in ("binding", "dependencies")):
        raise HTTPException(409, detail={"code": "dataset_lifecycle_preview_changed"})
    decode_token(payload.impact_token)
    previous_revision = dataset.lifecycle_revision
    archive = payload.operation == "archive"
    measured_at = reviewed["measured_at"]
    changed = await db.scalar(
        text(
            "SELECT yoao_assets_v1.change_dataset_archive_state("
            "CAST(:id AS uuid), CAST(:actor AS uuid), :revision, :archive, :measured_at)"
        ),
        {
            "id": dataset.id,
            "actor": actor.id,
            "revision": previous_revision,
            "archive": archive,
            "measured_at": measured_at,
        },
    )
    if not changed:
        raise HTTPException(409, detail={"code": "dataset_lifecycle_preview_changed"})
    archived_at = measured_at if archive else None
    archived_by = actor.id if archive else None
    lifecycle_revision = previous_revision + 1
    new_state = "archived" if archive else "active"
    await AuditService.log(
        db,
        actor=actor,
        action=f"dataset.{payload.operation}",
        target_type="dataset",
        target_id=str(dataset.id),
        request=request,
        status_code=200,
        detail={
            "previous_revision": previous_revision,
            "lifecycle_revision": lifecycle_revision,
            "state": new_state,
            "reviewed_at": token["measured_at"],
            "preview_expires_at": datetime.fromtimestamp(
                token["exp"], timezone.utc
            ).isoformat(),
            "rechecked_at": reviewed["measured_at"].isoformat(),
            "counts": reviewed["counts"],
            "frozen_coverage": reviewed["frozen_coverage"],
        },
    )
    return {
        "dataset_id": dataset.id,
        "lifecycle_revision": lifecycle_revision,
        "state": new_state,
        "archived_at": archived_at,
        "archived_by": archived_by,
    }
