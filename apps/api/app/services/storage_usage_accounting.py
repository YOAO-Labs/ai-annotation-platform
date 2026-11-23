"""Current-visible-object facts; never an ownership or lifecycle authority."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from urllib.parse import urlsplit
import uuid

from sqlalchemy import text

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
    if kind == "delete" and not new_request and version in {None, "null"}:
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
    evidence = {
        **(evidence or {}),
        "logical_intent_id": str(identifier),
        "logical_group": identity,
        "attempt_nonce": str(identifier),
    }
    row = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.begin_usage_mutation("
                ":id, :identity, :new_identity, :scope, :key, :producer, :owner,"
                ":purpose, :kind, :version, CAST(:evidence AS jsonb))"
            ),
            {
                "id": identifier,
                "identity": identity,
                "new_identity": digest(f"{identity}:{identifier}")
                if new_request
                else None,
                "scope": scope,
                "key": key,
                "producer": producer,
                "owner": uuid.UUID(str(owner_id)),
                "purpose": purpose,
                "kind": kind,
                "version": version,
                "evidence": json.dumps(evidence),
            },
        )
    ).one()
    if row.id is None:
        raise ValueError("accounting_identity_collision")
    return MutationHandle(row.id, row.fresh)


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
            fact = dict(evidence)
            delta = fact.pop("current_delta_bytes", None)
            physical = fact.pop("physical_outcome", "unknown")
            size = fact.pop("physical_size_bytes", None)
            changed = await db.scalar(
                text(
                    "SELECT yoao_assets_v1.publish_usage_mutation("
                    ":id, :fresh, :settled, :delta, :physical, :size, CAST(:fact AS jsonb))"
                ),
                {
                    "id": handle.id,
                    "fresh": handle.fresh,
                    "settled": settled,
                    "delta": delta,
                    "physical": physical,
                    "size": size,
                    "fact": json.dumps(fact),
                },
            )
            if changed:
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
            values.append({"object_key": key, "size_bytes": size})
        await db.execute(
            text(
                "SELECT yoao_assets_v1.stage_usage_inventory_objects("
                ":generation, :scope, CAST(:objects AS jsonb), :started, :completed)"
            ),
            {
                "generation": generation,
                "scope": scope,
                "objects": json.dumps(values),
                "started": started,
                "completed": completed,
            },
        )


async def complete_bucket(db, scope, generation, started, completed):
    await db.execute(
        text(
            "SELECT yoao_assets_v1.reconcile_usage_bucket(:scope, :generation, :started, :completed)"
        ),
        {
            "scope": scope,
            "generation": generation,
            "started": started,
            "completed": completed,
        },
    )


async def discard_staging(db, generation, scope=None):
    await db.execute(
        text("SELECT yoao_assets_v1.discard_usage_staging(:generation, :scope)"),
        {"generation": generation, "scope": scope},
    )


async def summaries(db, scopes):
    keys = [item["key"] for item in scopes]
    row = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.usage_mutation_summary(CAST(:keys AS text[]))"
            ),
            {"keys": keys},
        )
    ).one()
    accounting = StorageUsageAccountingSummary(
        **{key: int(value) for key, value in row._mapping.items()}
    )
    records = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.usage_reconciliation_facts(CAST(:keys AS text[]))"
            ),
            {"keys": keys},
        )
    ).all()
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
    schedule = (
        await db.execute(text("SELECT * FROM yoao_assets_v1.usage_schedule_summary()"))
    ).one_or_none()
    return accounting, reconciliation, schedule_summary(schedule)


def schedule_summary(row):
    return (
        UsageScheduleSummary()
        if row is None
        else UsageScheduleSummary(
            **{key: getattr(row, key) for key in UsageScheduleSummary.model_fields}
        )
    )
