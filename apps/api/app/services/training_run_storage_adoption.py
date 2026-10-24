"""Verify stopped legacy experiment outputs without reading sources or repairing.

Expected content comes from durable upload/publication receipts. The verifier
never deserializes model weights or changes semantic execution records.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import re
import tempfile
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from training_contract.observability import LOG_BYTES, REDACTION_REVISION, RedactedLines

from app.config import settings
from app.schemas.training_run import TrainingArtifactSpec
from app.services.training_dataset_bridge import TrainingDatasetError, content_digest
from app.services.training_storage_adoption import (
    CAPACITY,
    MAX_TRANSFER_BYTES,
    TIMEOUT_SECONDS,
    Reader,
    _observation,
    reject,
)

TERMINAL = {"succeeded", "failed", "cancelled"}
MAX_ATTEMPTS = 10_000
_DERIVED = re.compile(r"([^/]+)\.([0-9a-f]{64})\.log")


def _spec(name, value):
    if not isinstance(value, dict) or type(value.get("size_bytes")) is not int:
        reject()
    if value.get("name", name) != name:
        reject()
    return TrainingArtifactSpec.model_validate({"name": name, **value}).model_dump()


def _expected(reader, state):
    if state["status"] not in TERMINAL:
        reject("legacy_storage_version_active")
    attempts = state["attempts"]
    if not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS:
        reject("legacy_storage_inventory_limit")
    expected, required, seen = {}, set(), set()
    for attempt in attempts:
        identifier = str(uuid.UUID(attempt["id"]))
        if identifier != attempt["id"] or identifier in seen:
            reject()
        seen.add(identifier)
        if attempt["status"] not in TERMINAL:
            reject("legacy_storage_version_active")
        inventory, artifacts = attempt["upload_inventory"], attempt["artifacts"]
        if not isinstance(inventory, dict) or not isinstance(artifacts, dict):
            reject()
        if len(inventory) > 12 or len(artifacts) > 12:
            reject("legacy_storage_inventory_limit")
        for name in set(inventory) | set(artifacts):
            key = reader.owner.key(f"attempts/{identifier}/{name}")
            published = artifacts.get(name)
            spec = _spec(name, inventory[name]) if name in inventory else None
            if published is not None:
                if not isinstance(published, dict) or published.get("key") != key:
                    reject()
                accepted = _spec(
                    name, {k: v for k, v in published.items() if k != "key"}
                )
                if spec is not None and spec != accepted:
                    reject()
                spec = accepted
                required.add(key)
            if attempt["status"] == "succeeded":
                required.add(key)
            expected[key] = (identifier, name, spec)
    return expected, required


def _redact(data):
    redactor = RedactedLines((settings.training_executor_token,))
    output = bytearray()
    for offset in range(0, len(data), 64 * 1024):
        for line in redactor.feed(data[offset : offset + 64 * 1024]):
            output.extend(line.encode())
            if len(output) > LOG_BYTES:
                reject("legacy_storage_object_limit")
    for line in redactor.feed(b"", final=True):
        output.extend(line.encode())
    if len(output) > LOG_BYTES:
        reject("legacy_storage_object_limit")
    return bytes(output)


def _verify(reader, state, maximum):
    expected, required = _expected(reader, state)
    before = reader.inventory()
    if not required <= set(before):
        reject("legacy_storage_required_copy_missing")
    # Derived logs must be explained by a published, independently verified log.
    derived = {}
    for key in sorted(set(before) - set(expected)):
        relative = key[len(reader.owner.prefix) :].split("/")
        if (
            len(relative) != 4
            or relative[0] != "attempts"
            or relative[2] != "redacted-logs"
        ):
            reject("legacy_storage_unknown_object")
        match = _DERIVED.fullmatch(relative[3])
        if not match:
            reject("legacy_storage_unknown_object")
        name, digest = match.groups()
        original = reader.owner.key(f"attempts/{relative[1]}/{name}")
        if original not in required or expected[original][2]["role"] != "log":
            reject("legacy_storage_unknown_object")
        derived.setdefault(original, []).append((key, digest))
    for key in sorted(set(before) & set(expected)):
        identifier, _, spec = expected[key]
        reader.check()
        limit = (
            min(spec["size_bytes"], LOG_BYTES) if key in derived else spec["size_bytes"]
        )
        with tempfile.TemporaryFile() as temporary:
            _, evidence, head = reader.read(key, maximum=limit, output=temporary)
            if (
                evidence["size_bytes"] != spec["size_bytes"]
                or evidence["sha256"] != spec["sha256"]
                or head.get("Metadata", {}).get("attempt-id") != identifier
                or head.get("Metadata", {}).get("sha256") != spec["sha256"]
            ):
                reject()
            if key not in derived:
                continue
            temporary.seek(0)
            redacted = _redact(temporary.read(LOG_BYTES + 1))
            digest = hashlib.sha256(redacted).hexdigest()
            for derived_key, named_digest in derived[key]:
                temporary.seek(0)
                temporary.truncate(0)
                _, actual, observed = reader.read(
                    derived_key, maximum=LOG_BYTES, output=temporary
                )
                metadata = observed.get("Metadata", {})
                if (
                    actual["sha256"] != digest
                    or named_digest != digest
                    or actual["size_bytes"] != len(redacted)
                    or metadata.get("attempt-id") != identifier
                    or metadata.get("sha256") != digest
                    or metadata.get("source-sha256") != spec["sha256"]
                    or metadata.get("redaction-revision") != str(REDACTION_REVISION)
                ):
                    reject()
    if reader.inventory() != before:
        reject("legacy_storage_changed")
    for key, observation in reader.heads.items():
        reader.check()
        if (
            _observation(reader.client.head_object(Bucket=reader.owner.bucket, Key=key))
            != observation
        ):
            reject("legacy_storage_changed")
    return {
        "schema": 1,
        "scope": "current-retained-copy",
        "purpose": "training-run",
        "context_digest": content_digest(state),
        "attempt_count": len(state["attempts"]),
        "object_count": len(before),
        "read_bytes": maximum - reader.remaining,
        "inventory_digest": content_digest(
            sorted(reader.observed, key=lambda row: row["key"])
        ),
    }


def verify(
    client,
    owner,
    state,
    *,
    max_bytes=MAX_TRANSFER_BYTES,
    timeout_seconds=TIMEOUT_SECONDS,
):
    """Prove every present output and all published artifacts in one stopped run."""
    if (
        type(max_bytes) is not int
        or max_bytes <= 0
        or not 0 < timeout_seconds <= TIMEOUT_SECONDS
    ):
        reject("legacy_storage_invalid_budget")
    reader = Reader(client, owner, max_bytes=max_bytes, timeout_seconds=timeout_seconds)
    try:
        return _verify(reader, state, max_bytes)
    except TrainingDatasetError:
        raise
    except (BotoCoreError, ClientError):
        reject("legacy_storage_unavailable", 503)
    except (KeyError, ValueError, TypeError, OSError, AttributeError):
        reject()


def verify_operation(owner, state):
    from app.services.managed_original_creation import create_client

    client = create_client()
    try:
        return verify(client, owner, state)
    finally:
        try:
            client.close()
        except BaseException:
            reject("legacy_storage_close_unsettled", 503)


async def pin(db, run_id, actor, project, key_id):
    """Hold only this stopped run against writers while verifying retained bytes."""
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.db.models.api_key import ApiKey
    from app.db.models.dataset_asset import StorageBinding, StorageLocation
    from app.db.models.training_run import (
        TrainingRun,
        TrainingAttempt,
        TrainingResourceLease,
    )
    from app.services import storage_access, training_run_storage as owners
    from app.services.content_snapshot import ContentError
    from app.services.dataset_content import key_authority
    from app.services.project_write_guard import lock_actor_project_authority
    from app.services.project_access import resolve_project_access_by_id

    async def authority():
        await lock_actor_project_authority(db, actor.id, project.id)
        _, access = await resolve_project_access_by_id(
            db, user=actor, project_id=project.id
        )
        if not access.is_manager:
            raise HTTPException(403, detail="项目管理权限已变化")
        if key_id is not None:
            await db.execute(
                select(ApiKey.id).where(ApiKey.id == key_id).with_for_update(read=True)
            )
        return [
            await key_authority(db, actor, key_id, scope)
            for scope in ["annotations:read", "datasets:read", "datasets:write"]
        ]

    async def context():
        run = await db.scalar(
            select(TrainingRun)
            .where(TrainingRun.id == run_id, TrainingRun.project_id == project.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise HTTPException(404, detail="训练实验不存在")
        if run.storage_ref is not None:
            return run, None
        attempts = list(
            await db.scalars(
                select(TrainingAttempt)
                .where(TrainingAttempt.run_id == run.id)
                .order_by(TrainingAttempt.id)
                .limit(MAX_ATTEMPTS + 1)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        )
        if len(attempts) > MAX_ATTEMPTS:
            reject("legacy_storage_inventory_limit")
        if await db.scalar(
            select(TrainingResourceLease.resource_id)
            .where(
                TrainingResourceLease.attempt_id.in_(
                    select(TrainingAttempt.id).where(TrainingAttempt.run_id == run.id)
                )
            )
            .limit(1)
        ):
            reject("legacy_storage_version_active")
        state = {
            "id": str(run.id),
            "project_id": str(run.project_id),
            **{
                name: deepcopy(getattr(run, name))
                for name in ["status", "config", "config_digest", "authorization"]
            },
            **{
                name: str(getattr(run, name))
                for name in [
                    "creator_id",
                    "dataset_id",
                    "initialization_model_id",
                    "current_attempt_id",
                ]
            },
            "attempts": [
                {
                    "id": str(attempt.id),
                    "resume_from_id": str(attempt.resume_from_id),
                    **{
                        name: deepcopy(getattr(attempt, name))
                        for name in [
                            "status",
                            "ordinal",
                            "upload_inventory",
                            "artifacts",
                            "finish_request_digest",
                            "runtime",
                            "native_accounting",
                        ]
                    },
                }
                for attempt in attempts
            ],
        }
        return run, state

    try:
        # No API Key row lock is held through physical IO; final authority uses
        # a share lock so a completed revocation cannot race pin publication.
        await lock_actor_project_authority(db, actor.id, project.id)
        _, access = await resolve_project_access_by_id(
            db, user=actor, project_id=project.id
        )
        if not access.is_manager:
            raise HTTPException(403, detail="项目管理权限已变化")
        initial_authority = [
            await key_authority(db, actor, key_id, scope)
            for scope in ["annotations:read", "datasets:read", "datasets:write"]
        ]
        run, state = await context()
        if run.storage_ref is not None:
            owners.resolve(run)
            return run
        if state["status"] not in TERMINAL or any(
            attempt["status"] not in TERMINAL for attempt in state["attempts"]
        ):
            reject("legacy_storage_version_active")
        candidate = await owners.adoption_candidate(db, run)
        owner = owners.resolve_ref(candidate)
        if not CAPACITY.acquire(blocking=False):
            reject("legacy_storage_verification_busy", 429)
        revision = None

        async def acquired(permit):
            nonlocal revision
            revision = permit.revision

        unsettled = False
        try:
            evidence = await storage_access.run(
                db,
                uuid.UUID(candidate["location_id"]),
                "training_run_storage_adoption",
                verify_operation,
                owner,
                state,
                on_acquired=acquired,
            )
        except BaseException as exc:
            unsettled = getattr(exc, "storage_operation_unsettled", False)
            raise
        finally:
            if not unsettled:
                CAPACITY.release()
        if await authority() != initial_authority:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        await db.execute(
            select(StorageLocation.id)
            .where(StorageLocation.id == uuid.UUID(candidate["location_id"]))
            .with_for_update(read=True)
        )
        await db.execute(
            select(StorageBinding.purpose)
            .where(StorageBinding.purpose == "training")
            .with_for_update(read=True)
        )
        current, current_state = await context()
        if (
            current.storage_ref is not None
            or content_digest(state) != content_digest(current_state)
            or candidate != await owners.adoption_candidate(db, current)
        ):
            reject("legacy_storage_context_changed")
        await storage_access.check(
            db,
            uuid.UUID(candidate["location_id"]),
            expected_revision=revision,
            lock=True,
        )
        current.storage_ref = {
            **candidate,
            "evidence": {
                **evidence,
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "actor_id": str(actor.id),
                "api_key_id": str(key_id) if key_id else None,
            },
        }
        await db.commit()
        return current
    except TrainingDatasetError as exc:
        raise HTTPException(
            exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from None
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
