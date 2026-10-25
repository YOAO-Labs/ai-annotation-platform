"""Read the configured platform S3 source into a private migration inventory.

This operator command never selects a target, copies objects or authorizes cutover.
Run with the intended API virtualenv and explicit deployment environment.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

from storage_migration_inventory import (
    InventoryError,
    InventoryLimits,
    PURPOSES,
    verify_inventory,
    write_inventory,
)


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    mode = command.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--verify", type=Path)
    command.add_argument("--handling", type=Path)
    command.add_argument("--label", default="Configured platform storage")
    for name in ("max-pages", "max-entries", "max-object-bytes", "max-total-bytes"):
        command.add_argument("--" + name, type=int)
    command.add_argument("--read-timeout-seconds", type=float, default=300)
    command.add_argument("--page-size", type=int, default=1000)
    return command


def handling_file(path):
    if path is None:
        return {}
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise InventoryError("invalid_handling")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InventoryError("invalid_handling")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise InventoryError("invalid_handling") from None
    if (
        not isinstance(value, dict)
        or not set(value) <= PURPOSES
        or any(item not in ("migrate", "rebuild", "retain") for item in value.values())
    ):
        raise InventoryError("invalid_handling")
    return value


def endpoint_identity(value):
    """Compare location configuration without emitting credentials or query data."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, parsed.hostname.lower(), port, parsed.path.rstrip("/")
    except (TypeError, ValueError):
        return None


def binding_observations(rows, purpose_buckets, endpoint, legacy_id):
    covered, result = set(), []
    source_identity = endpoint_identity(endpoint)
    for row in rows:
        purpose = row["purpose"]
        config = row.get("location_config") or {}
        same = row.get("location_kind") == "s3"
        if str(row.get("location_id")) == legacy_id and not row.get("connection_id"):
            candidate = config.get("endpoint")
        elif (
            row.get("connection_kind") == "s3"
            and row.get("connection_scope") == "global"
        ):
            config = row.get("connection_config") or {}
            candidate = config.get("endpoint")
            same = same and config.get("bucket") == row["bucket"]
        else:
            candidate, same = None, False
        if isinstance(candidate, str) and "://" not in candidate:
            candidate = f"{'https' if config.get('use_ssl') else 'http'}://{candidate}"
        same = bool(
            same
            and source_identity is not None
            and endpoint_identity(candidate) == source_identity
            and purpose_buckets.get(purpose) == row["bucket"]
        )
        if same:
            covered.add(purpose)
        result.append(
            {
                "purpose": purpose,
                "location_id": str(row["location_id"]),
                "generation": row["generation"],
                "location_revision": row.get("config_revision"),
                "connection_revision": row.get("connection_revision"),
                "configured_source_covered": same,
            }
        )
    return {
        "bindings": result,
        "uncovered_binding_purposes": sorted(PURPOSES - covered),
        "coverage_basis": "saved configuration only; no remote probes",
    }


async def collect_observations(engine, purpose_buckets, endpoint, legacy_id):
    """Close one bounded, read-only DB snapshot before any S3 I/O."""
    from sqlalchemy import text

    started = datetime.now(timezone.utc).isoformat()
    async with engine.connect() as connection, connection.begin():
        await connection.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        )
        await connection.execute(text("SET LOCAL statement_timeout = '15s'"))
        bindings = (
            (
                await connection.execute(
                    text(
                        "SELECT b.purpose,b.location_id,b.bucket,b.generation,"
                        "l.kind AS location_kind,l.config AS location_config,"
                        "l.config_revision,l.connection_id,c.kind AS connection_kind,"
                        "c.scope AS connection_scope,c.config AS connection_config,"
                        "c.revision AS connection_revision "
                        "FROM storage_bindings b LEFT JOIN storage_locations l "
                        "ON l.id=b.location_id LEFT JOIN storage_connections c "
                        "ON c.id=l.connection_id ORDER BY b.purpose LIMIT 101"
                    )
                )
            )
            .mappings()
            .all()
        )
        jobs = (
            (
                await connection.execute(
                    text(
                        "SELECT kind,status,count(*) AS count FROM async_jobs "
                        "WHERE status IN ('pending','running') "
                        "GROUP BY kind,status ORDER BY kind,status LIMIT 101"
                    )
                )
            )
            .mappings()
            .all()
        )
        locations = (
            (
                await connection.execute(
                    text(
                        "SELECT kind,count(*) AS count FROM storage_locations "
                        "GROUP BY kind ORDER BY kind LIMIT 101"
                    )
                )
            )
            .mappings()
            .all()
        )
    return {
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "transaction": "repeatable read, read only; closed before object reads",
        "complete": all(len(rows) <= 100 for rows in (bindings, jobs, locations)),
        **binding_observations(bindings[:100], purpose_buckets, endpoint, legacy_id),
        "saved_locations_by_kind": [dict(row) for row in locations[:100]],
        "potential_writers": [dict(row) for row in jobs[:100]],
        "writer_coverage": "pending/running AsyncJob groups only; not a writer barrier",
        "historical_locations": "unknown",
        "business_references": "unknown",
        "external_writers_and_presigned_uploads": "unknown",
    }


async def configured_inventory(args, handling, limits):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
    from botocore.config import Config
    from app.config import settings
    from app.db.base import engine
    from app.services.dataset_assets import (
        LEGACY_LOCATION_ID,
        PURPOSE_BUCKET_ATTRIBUTES,
    )
    from app.services.storage import StorageService

    storage = None
    try:
        # Constructing StorageService is read-free; never call ensure_all_buckets.
        storage = StorageService(
            client_config=Config(
                connect_timeout=min(10, limits.read_timeout_seconds),
                read_timeout=limits.read_timeout_seconds,
                retries={"total_max_attempts": 1},
            )
        )
        purposes = {
            purpose: getattr(storage, attribute)
            for purpose, attribute in PURPOSE_BUCKET_ATTRIBUTES.items()
        }
        endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
        observations = await collect_observations(
            engine, purposes, endpoint, str(LEGACY_LOCATION_ID)
        )
        return write_inventory(
            args.output,
            storage.client,
            purposes,
            source={"label": args.label, "endpoint": endpoint},
            observations=observations,
            handling=handling,
            limits=limits,
        )
    finally:
        if storage is not None:
            storage.client.close()
        await engine.dispose()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        handling = handling_file(args.handling)
        values = {
            key: getattr(args, key)
            for key in (
                "max_pages",
                "max_entries",
                "max_object_bytes",
                "max_total_bytes",
            )
            if getattr(args, key) is not None
        }
        limits = InventoryLimits(
            **values,
            page_size=args.page_size,
            read_timeout_seconds=args.read_timeout_seconds,
        )
        report = (
            verify_inventory(args.verify)
            if args.verify
            else asyncio.run(configured_inventory(args, handling, limits))
        )
        print(json.dumps(report, sort_keys=True))
        return 0 if report["complete"] else 1
    except Exception as exc:
        # Raw SDK, SQL and OS messages can contain object keys or credentials.
        code = exc.code if isinstance(exc, InventoryError) else "inventory_failed"
        print(json.dumps({"complete": False, "code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
