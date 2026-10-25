"""Bind logical location edits to a short-lived reviewed dependency snapshot."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi import HTTPException
from jose import ExpiredSignatureError, JWTError, jwt

from app.config import settings
from app.db.models.storage_connection import StorageConnection
from app.services import storage_location, storage_maintenance_impact as maintenance
from app.services.dataset_content import key_authority
from app.services.storage_maintenance_targets import confirmation_counts

PREVIEW_TTL = timedelta(minutes=5)
AUDIENCE = "storage-location-change"
PUBLIC_FIELDS = (
    "name",
    "enabled",
    "scope",
    "use_user_ids",
    "cache_policy",
    "original_cache_quota_bytes",
    "original_cache_ttl_seconds",
)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def public_values(location, candidate=None):
    config = candidate["config"] if candidate else location.config
    return {
        "name": candidate["name"] if candidate else location.name,
        "enabled": candidate["enabled"] if candidate else location.enabled,
        "scope": config.get("scope") if location.kind == "filesystem" else None,
        "use_user_ids": sorted(config.get("use_user_ids", [])),
        "cache_policy": config.get("cache_policy", "preview"),
        "original_cache_quota_bytes": config.get("original_cache_quota_bytes", 0),
        "original_cache_ttl_seconds": config.get("original_cache_ttl_seconds", 86400),
    }


async def review(db, location, payload, actor, api_key_id):
    actor = await maintenance.administrator(db, actor)
    proof = await key_authority(db, actor, api_key_id, "datasets:write")
    candidate = await storage_location.update_candidate(db, location, payload)
    connection = (
        await db.get(StorageConnection, location.connection_id, populate_existing=True)
        if location.connection_id
        else None
    )
    connection_revision = connection.revision if connection else None
    binding = digest(
        {
            "actor": actor.id,
            "key": api_key_id,
            "authority": proof,
            "location": location.id,
            "kind": location.kind,
            "revision": location.config_revision,
            "name": location.name,
            "enabled": location.enabled,
            "config": location.config,
            "connection_id": location.connection_id,
            "connection_revision": connection_revision,
            "candidate": candidate,
        }
    )
    snapshot = dict(
        (await db.execute(maintenance.dependencies(location.id, with_fingerprint=True)))
        .mappings()
        .one()
    )
    measured_at = snapshot.pop("measured_at")
    fingerprint = snapshot.pop("dependency_fingerprint")
    counts = {key: int(value) for key, value in snapshot.items()}
    actor = await maintenance.administrator(db, actor)
    if proof != await key_authority(db, actor, api_key_id, "datasets:write"):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    if connection is not None:
        current_connection = await db.get(
            StorageConnection, location.connection_id, populate_existing=True
        )
        if (
            current_connection is None
            or current_connection.revision != connection_revision
        ):
            raise HTTPException(409, detail={"code": "storage_change_preview_changed"})
    before, after = public_values(location), public_values(location, candidate)
    return {
        "binding": binding,
        "dependencies": digest(
            {
                "fingerprint": fingerprint,
                "counts": confirmation_counts(counts),
            }
        ),
        "changes": [
            {"field": field, "before": before[field], "after": after[field]}
            for field in PUBLIC_FIELDS
            if before[field] != after[field]
        ],
        "impact": {
            "location_id": location.id,
            "config_revision": location.config_revision,
            "measured_at": measured_at,
            "scope": "stored_dependencies",
            "counts": counts,
            "frozen_coverage": "recorded_location_lineage_only",
            "input_coverage": "current_task_links_only",
            "execution_permission": False,
        },
    }


async def preview(db, location, payload, actor, *, api_key_id=None):
    reviewed = await review(db, location, payload, actor, api_key_id)
    measured_at = reviewed["impact"]["measured_at"]
    expires_at = measured_at + PREVIEW_TTL
    token = jwt.encode(
        {
            "aud": AUDIENCE,
            "exp": expires_at,
            "binding": reviewed["binding"],
            "dependencies": reviewed["dependencies"],
            "measured_at": measured_at.isoformat(),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {
        "location_id": location.id,
        "config_revision": location.config_revision,
        "measured_at": measured_at,
        "expires_at": expires_at,
        "impact_token": token,
        "changes": reviewed["changes"],
        "impact": reviewed["impact"],
    }


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
            409, detail={"code": "storage_change_preview_expired"}
        ) from None
    except (JWTError, ValueError, TypeError):
        raise HTTPException(
            409, detail={"code": "storage_change_preview_invalid"}
        ) from None


async def apply(db, location, payload, actor, *, api_key_id=None):
    token = decode_token(payload.impact_token)
    reviewed = await review(db, location, payload, actor, api_key_id)
    if any(token[key] != reviewed[key] for key in ("binding", "dependencies")):
        raise HTTPException(409, detail={"code": "storage_change_preview_changed"})
    # The query may finish after the confirmation expires.
    decode_token(payload.impact_token)
    revision = location.config_revision
    await storage_location.update_filesystem(db, location, payload)
    return {
        "previous_revision": revision,
        "config_revision": location.config_revision,
        "enabled": location.enabled,
        "changes": reviewed["changes"],
        "reviewed_at": token["measured_at"],
        "preview_expires_at": datetime.fromtimestamp(
            token["exp"], timezone.utc
        ).isoformat(),
        "rechecked_at": reviewed["impact"]["measured_at"].isoformat(),
        "counts": reviewed["impact"]["counts"],
        "scope": "stored_dependencies",
    }
