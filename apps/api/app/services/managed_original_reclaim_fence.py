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


async def protect_snapshot_references(db, snapshot):
    """Hold saved accepted-reference physical groups through version publication."""
    import uuid

    from app.db.models.dataset_asset import StorageLocation
    from app.db.models.dataset_source import (
        DatasetReference,
        DatasetScanEntry,
        DatasetScanRun,
        DatasetSource,
    )

    groups = set()
    for sample in snapshot.get("samples", []):
        identity = sample.get("asset_reference")
        if identity is None:
            continue
        try:
            lineage = sample.get("source_lineage")
            if lineage is None:
                reference = await db.get(
                    DatasetReference, uuid.UUID(identity["dataset_item_id"])
                )
                if reference is None:
                    raise ValueError
                run_id, path = reference.run_id, reference.path_digest
            else:
                run_id, path = uuid.UUID(lineage["scan_run_id"]), lineage["path_digest"]
            facts = (
                await db.execute(
                    select(
                        DatasetScanRun,
                        DatasetScanEntry,
                        DatasetSource,
                        StorageLocation.kind,
                    )
                    .join(
                        DatasetScanEntry, DatasetScanEntry.run_id == DatasetScanRun.id
                    )
                    .join(DatasetSource, DatasetSource.id == DatasetScanRun.source_id)
                    .join(
                        StorageLocation, StorageLocation.id == DatasetSource.location_id
                    )
                    .where(
                        DatasetScanRun.id == run_id,
                        DatasetScanEntry.path_digest == path,
                    )
                )
            ).one_or_none()
            if facts is None:
                raise ValueError
            run, entry, source, kind = facts
            receipt, observed, anchor = (
                entry.verified_content,
                entry.observed,
                run.snapshot["anchor"],
            )
            if (
                not isinstance(receipt, dict)
                or receipt.get("sha256") != identity["sha256"]
                or receipt.get("size_bytes") != identity["size_bytes"]
                or (
                    lineage is not None
                    and (
                        str(source.id) != lineage["source_id"]
                        or str(source.location_id) != lineage["location_id"]
                    )
                )
            ):
                raise ValueError
            if kind == "s3":
                if (
                    not isinstance(anchor, dict)
                    or anchor.get("kind") != "s3"
                    or not isinstance(observed, dict)
                    or observed.get("kind") != "s3"
                    or not isinstance(observed.get("bucket"), str)
                    or not observed["bucket"]
                    or observed["bucket"] != anchor.get("bucket")
                    or not isinstance(observed.get("key"), str)
                    or not observed["key"]
                ):
                    raise ValueError
                from app.services.source_readers.s3 import directory_prefix

                if (
                    observed["key"]
                    != directory_prefix(
                        anchor["prefix"], run.snapshot["config"]["relative_path"]
                    )
                    + entry.relative_path
                ):
                    raise ValueError
                groups.add((observed["bucket"], observed["key"]))
            elif kind not in {"filesystem", "sftp"} or (
                isinstance(observed, dict) and observed.get("kind") == "s3"
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError):
            raise HTTPException(
                409, detail={"code": "managed_original_reference_unknown"}
            ) from None
    for bucket, key in sorted(groups):
        await shared(db, bucket, key)


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


async def require_source_readable(db, source):
    """Post-admission check for B6's saved, read-only migration source scope.

    The source Operation must already be committed. If its check precedes a GC
    claim, GC's fresh pre-I/O/delete recheck sees that operation. If the claim
    precedes this check, no SDK read may start. Settled tombstones do not prohibit
    a future inventory of the remaining versions.
    """
    from app.db.models.managed_original_reclaim import (
        ManagedOriginalReclaimAttempt as Attempt,
        ManagedOriginalReclaimClaim as Claim,
    )
    from app.db.models.storage_maintenance import StorageOperation

    buckets = tuple(sorted(set(source["purpose_buckets"].values())))
    operation_exists = (
        select(StorageOperation.id)
        .where(StorageOperation.id == Attempt.operation_id)
        .exists()
    )
    active = await db.scalar(
        select(Claim.id)
        .join(Attempt, Attempt.id == Claim.current_attempt_id)
        .where(
            Claim.bucket.in_(buckets),
            (Claim.state == "claimed")
            | Attempt.status.in_(("pending", "running"))
            | (~Attempt.physical_settled)
            | operation_exists,
        )
        .limit(1)
    )
    # Keep any older attempt's unresolved physical receipt, even if a later
    # attempt became current. A receipt without a surviving target is unknown.
    unsettled = await db.scalar(
        select(Attempt.id)
        .where(
            Attempt.target["bucket"].astext.in_(buckets),
            (~Attempt.physical_settled) | operation_exists,
        )
        .limit(1)
    )
    orphan = await db.scalar(
        select(StorageOperation.id)
        .where(
            StorageOperation.purpose == "managed_original_reclaim",
            ~select(Attempt.id)
            .where(Attempt.operation_id == StorageOperation.id)
            .exists(),
        )
        .limit(1)
    )
    if active is not None or unsettled is not None or orphan is not None:
        raise HTTPException(409, detail={"code": "managed_original_reclaim_unsettled"})
