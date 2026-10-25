"""Current-visible-object facts; never an ownership or lifecycle authority."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from urllib.parse import urlsplit
import uuid

from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.db.models.storage_usage import (
    StorageUsageInventoryObject as Observation,
    StorageUsageMutation as Mutation,
    StorageUsageReconciliation as Reconciliation,
    StorageUsageSchedule as Schedule,
    StorageUsageSnapshot as Snapshot,
)
from app.schemas.storage import (
    StorageUsageAccountingSummary,
    StorageUsageReconciliationSummary,
    UsageScheduleSummary,
)

log = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def scope_key(endpoint_url, bucket):
    endpoint = urlsplit(endpoint_url)
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.netloc
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("invalid_accounting_endpoint")
    return digest(
        json.dumps(
            [
                endpoint.netloc + endpoint.path.rstrip("/"),
                endpoint.scheme == "https",
                bucket,
            ]
        )
    )


@dataclass(frozen=True)
class MutationHandle:
    id: uuid.UUID
    fresh: bool


async def begin_in_transaction(
    db,
    *,
    endpoint_url,
    bucket,
    object_key,
    producer,
    owner_id,
    purpose,
    kind,
    nonce=None,
    version=None,
    new_request=False,
    evidence=None,
):
    """Persist an exact-address intent in the caller's Session/Connection transaction."""
    key = object_key
    if not key or len(key.encode()) > 1024 or kind not in {"create", "delete"}:
        raise ValueError("invalid_accounting_address")
    if kind == "delete" and version in {None, "null"}:
        raise ValueError("immutable_accounting_version_required")
    scope = scope_key(endpoint_url, bucket)
    identity = digest(
        json.dumps(
            ["physical-delete", scope, key, version]
            if kind == "delete"
            else [producer, scope, str(owner_id), key, kind, nonce]
        )
    )
    identifier = uuid.uuid4()
    values = dict(
        id=identifier,
        idempotency_key=identity,
        scope_key=scope,
        object_key=key,
        key_hash=digest(key),
        producer=producer,
        purpose=purpose,
        owner_id=owner_id,
        kind=kind,
        expected_version=version,
        state="pending",
        physical_outcome="unknown",
        evidence={
            **(evidence or {}),
            "logical_intent_id": str(identifier),
            "logical_group": identity,
            "attempt_nonce": str(identifier),
        },
    )
    created = await db.scalar(
        insert(Mutation)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[Mutation.idempotency_key])
        .returning(Mutation.id)
    )
    if created is None:
        row = (
            (
                await db.execute(
                    select(Mutation.__table__)
                    .where(Mutation.idempotency_key == identity)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if row.object_key != key or row.scope_key != scope or row.kind != kind:
            raise ValueError("accounting_identity_collision")
        if new_request and kind == "create":
            # Every actual conditional submission has its own durable window.
            # Earlier unresolved attempts cannot be explained by a later 412.
            values["idempotency_key"] = digest(identity + ":" + str(identifier))
            values["evidence"]["logical_intent_id"] = str(row.id)
            await db.execute(insert(Mutation).values(**values))
            created = identifier
        else:
            identifier = row.id
    return MutationHandle(identifier, created is not None)


async def begin(
    factory, record, relative, kind, *, nonce=None, version=None, new_request=False
):
    """Keep prediction-input identities and commit before their native mutation."""
    ref = record.storage_ref
    async with factory() as db:
        handle = await begin_in_transaction(
            db,
            endpoint_url=ref["endpoint_url"],
            bucket=ref["bucket"],
            object_key=ref["key_prefix"] + relative,
            producer="prediction_inputs",
            owner_id=record.id,
            purpose="temporary",
            kind=kind,
            nonce=nonce,
            version=version,
            new_request=new_request,
        )
        await db.commit()
    return handle


async def publish(factory, handle, evidence, *, settled=True):
    """Best effort after true native drain; an uncommitted fact stays pending."""
    if handle is None:
        return
    try:
        async with factory() as db:
            row = await db.scalar(
                select(Mutation).where(Mutation.id == handle.id).with_for_update()
            )
            if row is None or row.state == "confirmed":
                return
            fact = dict(evidence)
            delta = fact.pop("current_delta_bytes", None)
            physical = fact.pop("physical_outcome", "unknown")
            size = fact.pop("physical_size_bytes", None)
            previous = dict(row.evidence or {})
            if row.physical_outcome in {"written", "deleted"}:
                physical = row.physical_outcome
                if previous.get("returned_version") is not None:
                    if fact.get("returned_version") not in {
                        None,
                        previous["returned_version"],
                    }:
                        fact["later_returned_version"] = fact["returned_version"]
                    fact["returned_version"] = previous["returned_version"]
                if row.physical_size_bytes is not None:
                    size = row.physical_size_bytes
            # A repeated unresolved intent cannot infer the earlier attempt from
            # its eventual presence or a later 412 response.
            if not settled or not handle.fresh:
                delta = None
            row.physical_outcome = physical
            row.physical_size_bytes = size
            row.current_delta_bytes = delta
            row.evidence = {**previous, **fact}
            row.state = "confirmed" if delta is not None else "unknown"
            row.finished_at = await db.scalar(select(func.clock_timestamp()))
            await db.commit()
    except Exception:
        log.warning("Storage accounting publication deferred operation=%s", handle.id)


def current_matches(client, bucket, key, version, size):
    """An observational HEAD failure must not change the business result."""
    try:
        response = client.head_object(Bucket=bucket, Key=key)
        return (
            response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 200
            and response.get("VersionId") == version
            and type(response.get("ContentLength")) is int
            and response["ContentLength"] == size
            and not response.get("DeleteMarker", False)
        )
    except Exception:
        return False


def _missing(client, bucket, key, *, version=None):
    try:
        client.head_object(
            Bucket=bucket, Key=key, **({"VersionId": version} if version else {})
        )
    except Exception as exc:
        response = getattr(exc, "response", {})
        code = str(response.get("Error", {}).get("Code", ""))
        headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        return (
            response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
            and code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}
            and not response.get("DeleteMarker")
            and str(headers.get("x-amz-delete-marker", "false")).lower() != "true"
        )
    return False


def deleted_current(client, bucket, key, version):
    """Prove actual version removal and no visible fallback or delete marker."""
    if not _missing(client, bucket, key, version=version) or not _missing(
        client, bucket, key
    ):
        return False
    try:
        response = client.list_object_versions(Bucket=bucket, Prefix=key, MaxKeys=1000)
        if (
            response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
            or response.get("IsTruncated") is not False
        ):
            return False
        return not any(
            item.get("Key") == key
            for item in response.get("Versions", []) + response.get("DeleteMarkers", [])
        )
    except Exception:
        return False


async def stage_page(db, generation, scope, objects, started, completed):
    """A duplicate in any page invalidates the bucket, never silently upserts."""
    if len(objects) > 1000:
        raise ValueError("invalid_inventory_page")
    if objects:
        values = []
        for item in objects:
            key, size = item["key"], item["size"]
            if (
                not isinstance(key, str)
                or not key
                or len(key.encode()) > 1024
                or type(size) is not int
                or size < 0
            ):
                raise ValueError("invalid_inventory_page")
            values.append(
                dict(
                    generation=generation,
                    scope_key=scope,
                    key_hash=digest(key),
                    object_key=key,
                    size_bytes=size,
                    observed_from=started,
                    observed_through=completed,
                )
            )
        await db.execute(insert(Observation), values)


# The database joins bounded persisted pages. Python never loads all keys or
# events; an absent object uses the whole successful bucket observation window.
_COMPARE = text("""
WITH p AS (
 SELECT * FROM storage_usage_inventory_objects WHERE scope_key=:scope AND generation=:previous
), c AS (
 SELECT * FROM storage_usage_inventory_objects WHERE scope_key=:scope AND generation=:current
), addresses AS (
 SELECT key_hash, object_key FROM p UNION SELECT key_hash, object_key FROM c
 UNION SELECT key_hash, object_key FROM storage_usage_mutations
 WHERE scope_key=:scope AND started_at <= :current_end
 AND (finished_at IS NULL OR finished_at >= :previous_start)
), compared AS (
 SELECT a.key_hash, a.object_key,
  COALESCE(c.size_bytes,0)-COALESCE(p.size_bytes,0) AS observed_delta,
  COALESCE(p.observed_from,:previous_start) AS previous_from,
  COALESCE(p.observed_through,:previous_end) AS previous_through,
  COALESCE(c.observed_from,:current_start) AS current_from,
  COALESCE(c.observed_through,:current_end) AS current_through
 FROM addresses a LEFT JOIN p ON p.key_hash=a.key_hash AND p.object_key=a.object_key
 LEFT JOIN c ON c.key_hash=a.key_hash AND c.object_key=a.object_key
), facts AS (
 SELECT x.*, m.known_delta, m.uncertain
 FROM compared x CROSS JOIN LATERAL (
  SELECT COALESCE(SUM(current_delta_bytes) FILTER (
    WHERE state='confirmed' AND started_at > x.previous_through
    AND finished_at < x.current_from),0) AS known_delta,
   COALESCE(BOOL_OR(
     state!='confirmed' OR current_delta_bytes IS NULL OR
     (current_delta_bytes!=0 AND NOT (started_at > x.previous_through AND finished_at < x.current_from))
   ),false) AS uncertain
  FROM storage_usage_mutations m WHERE m.scope_key=:scope
   AND m.key_hash=x.key_hash AND m.object_key=x.object_key
   AND m.started_at <= x.current_through
   AND (m.finished_at IS NULL OR m.finished_at >= x.previous_from)
 ) m
)
SELECT COUNT(*) AS compared_object_count,
 COUNT(*) FILTER (WHERE uncertain) AS uncertain_object_count,
 COUNT(*) FILTER (WHERE NOT uncertain AND observed_delta!=known_delta) AS unexplained_object_count,
 COALESCE(SUM(known_delta),0) AS confirmed_change_bytes,
 COALESCE(SUM(observed_delta-known_delta) FILTER (WHERE NOT uncertain),0) AS known_unexplained_change_bytes
FROM facts
""")


async def complete_bucket(db, snapshot, generation, started, completed):
    previous = snapshot.object_generation
    summary = dict(
        status="no_baseline",
        compared_object_count=0,
        uncertain_object_count=0,
        unexplained_object_count=0,
        confirmed_change_bytes=0,
        known_unexplained_change_bytes=0,
    )
    if previous is not None:
        old = await db.get(Reconciliation, (previous, snapshot.scope_key))
        if old is not None:
            summary = dict(
                (
                    await db.execute(
                        _COMPARE,
                        dict(
                            scope=snapshot.scope_key,
                            previous=previous,
                            current=generation,
                            previous_start=old.started_at,
                            previous_end=old.measured_at,
                            current_start=started,
                            current_end=completed,
                        ),
                    )
                )
                .mappings()
                .one()
            )
            summary = {key: int(value) for key, value in summary.items()}
            summary["status"] = "partial"
    summary["unexplained_change_bytes"] = (
        summary["known_unexplained_change_bytes"]
        if summary["status"] != "no_baseline" and not summary["uncertain_object_count"]
        else None
    )
    db.add(
        Reconciliation(
            generation=generation,
            scope_key=snapshot.scope_key,
            previous_generation=previous,
            started_at=started,
            measured_at=completed,
            summary=summary,
        )
    )
    snapshot.object_generation = generation
    # Keep exactly two successful generations for this bucket. Failed staging
    # is also removed here; active inventory publication is fenced by its owner.
    keep = [generation] + ([previous] if previous else [])
    await db.execute(
        delete(Observation).where(
            Observation.scope_key == snapshot.scope_key,
            Observation.generation.not_in(keep),
        )
    )
    await db.execute(
        delete(Reconciliation).where(
            Reconciliation.scope_key == snapshot.scope_key,
            Reconciliation.generation.not_in(keep),
        )
    )


async def discard_staging(db, generation, scope=None):
    published = exists(
        select(Snapshot.scope_key).where(
            Snapshot.object_generation == Observation.generation,
            Snapshot.scope_key == Observation.scope_key,
        )
    )
    query = delete(Observation).where(Observation.generation == generation, ~published)
    if scope:
        query = query.where(Observation.scope_key == scope)
    await db.execute(query)


async def summaries(db, scopes):
    keys = [item["key"] for item in scopes]
    row = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(Mutation.current_delta_bytes).filter(
                        Mutation.current_delta_bytes > 0
                    ),
                    0,
                ),
                func.coalesce(
                    -func.sum(Mutation.current_delta_bytes).filter(
                        Mutation.current_delta_bytes < 0
                    ),
                    0,
                ),
                func.coalesce(func.sum(Mutation.current_delta_bytes), 0),
                func.count().filter(Mutation.state == "pending"),
                func.count().filter(Mutation.state == "unknown"),
            ).where(Mutation.scope_key.in_(keys))
        )
    ).one()
    accounting = StorageUsageAccountingSummary(
        **dict(
            zip(
                (
                    "confirmed_added_bytes",
                    "confirmed_removed_bytes",
                    "net_change_bytes",
                    "pending_operation_count",
                    "unknown_operation_count",
                ),
                map(int, row),
            )
        )
    )
    records = list(
        await db.scalars(
            select(Reconciliation)
            .join(
                Snapshot,
                (Snapshot.scope_key == Reconciliation.scope_key)
                & (Snapshot.object_generation == Reconciliation.generation),
            )
            .where(Reconciliation.scope_key.in_(keys))
        )
    )
    reconciliation = None
    if records:
        values = {
            key: sum(item.summary[key] for item in records)
            for key in (
                "compared_object_count",
                "uncertain_object_count",
                "unexplained_object_count",
                "confirmed_change_bytes",
                "known_unexplained_change_bytes",
            )
        }
        first = any(item.summary["status"] == "no_baseline" for item in records)
        reconciliation = StorageUsageReconciliationSummary(
            status="no_baseline" if first else "partial",
            measured_at=max(item.measured_at for item in records),
            bucket_count=len(records),
            **values,
            unexplained_change_bytes=None
            if first or values["uncertain_object_count"]
            else values["known_unexplained_change_bytes"],
        )
    schedule = await db.get(Schedule, "platform")
    return accounting, reconciliation, schedule_summary(schedule)


def schedule_summary(row):
    return (
        UsageScheduleSummary()
        if row is None
        else UsageScheduleSummary(
            **{key: getattr(row, key) for key in UsageScheduleSummary.model_fields}
        )
    )
