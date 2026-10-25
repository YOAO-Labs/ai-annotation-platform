"""Transaction fences shared by reference publication and original reclamation.

Callers acquire their normal business locks first. Reclamation only takes the
exclusive group fence and observes MVCC roots; it must not acquire those business
locks in reverse order. Neither helper commits or holds a lock over object I/O.
"""

from fastapi import HTTPException
from sqlalchemy import func, select


def identity(bucket, object_key):
    from app.services.managed_original_retention import group_lock_identity

    return group_lock_identity(bucket, object_key)


async def _lock(db, bucket, object_key, *, exclusive):
    lock = (
        func.pg_try_advisory_xact_lock
        if exclusive
        else func.pg_try_advisory_xact_lock_shared
    )
    if not await db.scalar(
        select(lock(func.hashtextextended(identity(bucket, object_key), 0)))
    ):
        raise HTTPException(409, detail={"code": "managed_original_reclaim_busy"})


async def exclusive(db, bucket, object_key):
    """Serialize the GC MVCC recheck against all defined publication seams."""
    await _lock(db, bucket, object_key, exclusive=True)


async def shared(db, bucket, object_key):
    """Protect a publication until commit, rejecting durable deletion claims."""
    from app.db.models.managed_original_reclaim import ManagedOriginalReclaimClaim

    await _lock(db, bucket, object_key, exclusive=False)
    claim = await db.scalar(
        select(ManagedOriginalReclaimClaim.id)
        .where(
            ManagedOriginalReclaimClaim.bucket == bucket,
            ManagedOriginalReclaimClaim.object_key == object_key,
            ManagedOriginalReclaimClaim.state != "released",
        )
        .limit(1)
    )
    if claim is not None:
        raise HTTPException(409, detail={"code": "managed_original_reclaim_claimed"})


async def protect_source(db, source):
    """Use the authoritative resolved locator, including legitimate legacy refs."""
    if source.bucket is not None:
        await shared(db, source.bucket, source.object_key)


async def protect_reference(db, run, entry):
    """An S3 reference publishes the scan's saved physical observation."""
    observed = entry.observed
    if isinstance(observed, dict) and observed.get("kind") == "s3":
        bucket, key = observed.get("bucket"), observed.get("key")
        if (
            not isinstance(bucket, str)
            or not bucket
            or not isinstance(key, str)
            or not key
        ):
            raise HTTPException(
                409, detail={"code": "managed_original_reference_unknown"}
            )
        await shared(db, bucket, key)
    elif (
        isinstance(run.snapshot.get("anchor"), dict)
        and run.snapshot["anchor"].get("kind") == "s3"
    ):
        raise HTTPException(409, detail={"code": "managed_original_reference_unknown"})


async def protect_observed_read(db, observed, anchor):
    """Check the saved S3 locator after its real Permit has been registered."""
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.services import storage_access

    if not isinstance(anchor, dict) or anchor.get("kind") != "s3":
        return
    if (
        not isinstance(observed, dict)
        or observed.get("kind") != "s3"
        or not isinstance(observed.get("bucket"), str)
        or not observed["bucket"]
        or not isinstance(observed.get("key"), str)
        or not observed["key"]
        or observed["bucket"] != anchor.get("bucket")
    ):
        raise HTTPException(409, detail={"code": "managed_original_reference_unknown"})
    async with AsyncSession(
        bind=storage_access.engine(db), expire_on_commit=False, autoflush=False
    ) as owned:
        await shared(owned, observed["bucket"], observed["key"])


async def require_resumable(db, location_id):
    """Call only while holding this location's Gate UPDATE lock.

    Acquisition of a reclaim claim and its dedicated permit uses the same Gate
    lock. Job status, elapsed time and an apparently empty operation table cannot
    release an active claim or an unsettled physical responsibility.
    """
    from app.db.models.managed_original_reclaim import (
        ManagedOriginalReclaimAttempt as Attempt,
        ManagedOriginalReclaimClaim as Claim,
    )
    from app.db.models.storage_maintenance import StorageOperation

    active = await db.scalar(
        select(Claim.id)
        .join(Attempt, Attempt.id == Claim.current_attempt_id)
        .where(
            Attempt.target["location_id"].astext == str(location_id),
            (Claim.state == "claimed")
            | Attempt.status.in_(("pending", "running"))
            | (~Attempt.physical_settled),
        )
        .limit(1)
    )
    unsettled = await db.scalar(
        select(Attempt.id)
        .where(
            Attempt.target["location_id"].astext == str(location_id),
            Attempt.status.in_(("pending", "running")) | (~Attempt.physical_settled),
        )
        .limit(1)
    )
    operation = await db.scalar(
        select(StorageOperation.id)
        .where(
            StorageOperation.location_id == location_id,
            StorageOperation.purpose == "managed_original_reclaim",
        )
        .limit(1)
    )
    if active is not None or unsettled is not None or operation is not None:
        raise HTTPException(409, detail={"code": "managed_original_reclaim_unsettled"})
