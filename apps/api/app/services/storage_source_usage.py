"""Saved source-index scale, deduplicated within one visible location."""

from sqlalchemy import text


async def read_usage(db, location, actor):
    """The summary owner has already checked current manager/location access."""
    return (await read_usage_page(db, [location], actor))[location.id]


async def read_usage_page(db, locations, actor):
    """Aggregate one already-authorized page without crossing location domains."""
    if not locations:
        return {}
    rows = (
        (
            await db.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.directory_source_usage("
                    "CAST(:actor_id AS uuid), CAST(:location_ids AS uuid[]))"
                ),
                {"actor_id": actor.id, "location_ids": [row.id for row in locations]},
            )
        )
        .mappings()
        .all()
    )
    by_id = {row.id: row for row in locations}
    result = {}
    for row in rows:
        data = dict(row)
        location = by_id[data.pop("location_id")]
        for name in data:
            if name.endswith("_count") or name == "known_size_bytes":
                data[name] = int(data[name])
        supported = location.kind in {"filesystem", "s3", "sftp"}
        complete = supported and not data["unknown_reference_count"]
        result[location.id] = {
            **data,
            "total_size_bytes": data["known_size_bytes"] if complete else None,
            "coverage": "complete"
            if complete
            else "partial"
            if data["file_count"]
            else "none",
            "scope": "visible_current_accepted_source_heads",
            "source": "accepted_source_index",
            "unit": "bytes",
            "is_estimate": not complete,
            "deduplication_basis": "location_frozen_address_and_object_version",
            "includes_versions": location.kind == "s3",
        }
    return result
