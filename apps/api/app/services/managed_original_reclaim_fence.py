"""Transaction fences shared by reference publication and original reclamation.

Callers acquire their normal business locks first. Reclamation only takes the
exclusive group fence and observes MVCC roots; it must not acquire those business
locks in reverse order. Neither helper commits or holds a lock over object I/O.
"""

from fastapi import HTTPException
from sqlalchemy import func, select, text


def identity(bucket, object_key):
    from app.services.storage_identity import group_lock_identity

    return group_lock_identity(bucket, object_key)


async def exclusive(db, bucket, object_key):
    """Serialize the GC MVCC recheck against all defined publication seams."""
    if not await db.scalar(
        select(
            func.pg_try_advisory_xact_lock(
                func.hashtextextended(identity(bucket, object_key), 0)
            )
        )
    ):
        raise HTTPException(409, detail={"code": "managed_original_reclaim_busy"})


def check_shared_result(result):
    if result == "claimed":
        raise HTTPException(409, detail={"code": "managed_original_reclaim_claimed"})
    if result == "busy":
        raise HTTPException(409, detail={"code": "managed_original_reclaim_busy"})
    if result != "ok":
        raise RuntimeError("invalid managed original fence result")


async def shared(db, bucket, object_key):
    """Protect a publication until commit, rejecting durable deletion claims."""
    check_shared_result(
        await db.scalar(
            text("SELECT yoao_assets_v1.protect_original_group(:bucket, :key)"),
            {"bucket": bucket, "key": object_key},
        )
    )


async def protect_source(db, source):
    """Use the authoritative resolved locator, including legitimate legacy refs."""
    if source.bucket is not None:
        await shared(db, source.bucket, source.object_key)


async def protect_snapshot_references(db, snapshot):
    """Hold saved accepted-reference physical groups through version publication."""
    import uuid

    from app.services.dataset_reference_data import directory_reference_reclaim_facts

    groups = set()
    for sample in snapshot.get("samples", []):
        identity = sample.get("asset_reference")
        if identity is None:
            continue
        try:
            lineage = sample.get("source_lineage")
            run_id = uuid.UUID(lineage["scan_run_id"]) if lineage is not None else None
            path = lineage["path_digest"] if lineage is not None else None
            facts = await directory_reference_reclaim_facts(
                db,
                uuid.UUID(identity["dataset_item_id"]),
                run_id=run_id,
                path_digest=path,
            )
            if facts is None:
                raise ValueError
            kind = facts.kind
            receipt, observed, anchor = (
                facts.verified_content,
                facts.observed,
                facts.snapshot["anchor"],
            )
            if (
                not isinstance(receipt, dict)
                or receipt.get("sha256") != identity["sha256"]
                or receipt.get("size_bytes") != identity["size_bytes"]
                or (
                    lineage is not None
                    and (
                        str(facts.source_id) != lineage["source_id"]
                        or str(facts.location_id) != lineage["location_id"]
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
                        anchor["prefix"], facts.snapshot["config"]["relative_path"]
                    )
                    + facts.relative_path
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
    if await db.scalar(
        text("SELECT yoao_assets_v1.original_reclaim_gate_blocked(:location)"),
        {"location": location_id},
    ):
        raise HTTPException(409, detail={"code": "managed_original_reclaim_unsettled"})


async def require_source_readable(db, source):
    """Post-admission check for B6's saved, read-only migration source scope.

    The source Operation must already be committed. If its check precedes a GC
    claim, GC's fresh pre-I/O/delete recheck sees that operation. If the claim
    precedes this check, no SDK read may start. Settled tombstones do not prohibit
    a future inventory of the remaining versions.
    """
    if await db.scalar(
        text(
            "SELECT yoao_assets_v1.original_reclaim_source_blocked(CAST(:buckets AS text[]))"
        ),
        {"buckets": sorted(set(source["purpose_buckets"].values()))},
    ):
        raise HTTPException(409, detail={"code": "managed_original_reclaim_unsettled"})
