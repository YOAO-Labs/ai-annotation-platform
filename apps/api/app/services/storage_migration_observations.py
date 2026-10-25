"""Bounded read-only database observations; never a source coverage assertion."""

from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.services.storage_migration_inventory import PURPOSES


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
