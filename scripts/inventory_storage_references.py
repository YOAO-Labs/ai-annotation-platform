"""Enumerate saved S3 claims using readonly SQL, then join one sealed source report."""

import argparse
import asyncio
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
import tempfile
import time

from storage_migration_references import (
    Budget,
    Limits,
    REF_FIELDS,
    ReferenceError,
    canonical,
    now,
    private_open,
    verify,
    write_catalog,
)


@dataclass(frozen=True)
class Family:
    table: str
    keys: tuple[str, ...] = ("id",)
    form: str = "ref"
    inventory: bool = False


# A closed locator registry, not the live classifier or a list of cleanup permits.
FAMILIES = (
    Family("prediction_input_leases", inventory=True),
    Family("image_pyramid_outputs", inventory=True),
    Family("video_derivative_outputs", inventory=True),
    Family("export_media_bundles", inventory=True),
    Family("training_dataset_versions"),
    Family("training_runs"),
    Family("training_previews"),
    Family("sample_evaluation_runs"),
    Family("active_learning_rounds"),
    Family("avatar_uploads", ("token",)),
    Family("bug_attachment_uploads"),
    Family("comment_attachment_uploads"),
    Family("guide_asset_uploads"),
    Family("raster_mask_objects"),
    Family("export_outputs"),
    Family("audit_archive_outputs"),
    Family("managed_original_creations", form="creation"),
    Family("managed_original_retirements", form="retirement"),
    Family("dataset_asset_locations", ("asset_id", "location_id", "bucket"), "current"),
    Family("task_originals", form="current"),
    Family("dataset_auxiliary_originals", form="current"),
    Family("dataset_source_previews", ("source_id", "path_digest"), "preview"),
    Family("original_cache_entries", form="cache"),
    Family("training_storage_deletions", form="deletion"),
)
SCALARS = (
    "id",
    "asset_id",
    "location_id",
    "bucket",
    "object_key",
    "prefix",
    "storage_identity",
    "state",
    "status",
    "reference_state",
    "identity_state",
    "object_version_id",
    "version_id",
    "sha256",
    "size_bytes",
    "stored_sha256",
    "stored_size_bytes",
    "canonical_sha256",
    "etag",
    "staging_bucket",
    "staging_key",
    "package_key",
    "package_sha256",
)
DOCUMENTS = (
    "input_snapshot",
    "snapshot",
    "source_ref",
    "original_ref",
    "source",
    "upload_inventory",
    "manifest",
)
CONTEXT = ("storage_locations", "storage_bindings")


def quoted(name):
    # Only static registry identifiers reach SQL, never operator input.
    if not name.replace("_", "").isalnum():
        raise ReferenceError("invalid_registry")
    return '"' + name + '"'


def ref_projection(alias="r"):
    return (
        "jsonb_build_object("
        + ",".join(
            "'%s',%s.storage_ref->'%s'" % (field, alias, field) for field in REF_FIELDS
        )
        + ") AS storage_ref"
    )


def projection(family, columns):
    required = set(family.keys) | (
        {"storage_ref"} if family.form in {"ref", "deletion"} else {"bucket"}
    )
    if not required <= columns:
        raise ReferenceError("family_columns_missing")
    fields = list(
        dict.fromkeys((*family.keys, *[name for name in SCALARS if name in columns]))
    )
    result = [f"r.{quoted(name)}" for name in fields]
    if "storage_ref" in columns:
        result.append(ref_projection())
        result.append("r.storage_ref IS NOT NULL AS ref_present")
    if family.form == "creation":
        result.append("r.configuration->>'endpoint' AS saved_endpoint")
    for name in DOCUMENTS:
        if name in columns:
            result.append(
                f"r.{quoted(name)} IS NOT NULL AS {quoted(name + '_unexpanded')}"
            )
    if family.inventory:
        result.extend(
            [
                "r.inventory IS NOT NULL AS inventory_present",
                "r.inventory->'schema' AS inventory_schema",
                "jsonb_typeof(r.inventory->'objects') AS inventory_objects_type",
            ]
        )
    return ",".join(result)


def allowed_columns(family, columns):
    names = set(family.keys) | (set(SCALARS) & columns) | (set(DOCUMENTS) & columns)
    if "storage_ref" in columns:
        names.add("storage_ref")
    if family.form == "creation":
        names.add("configuration")
    if family.inventory:
        names.add("inventory")
    return names


def _text(value):
    return str(value) if value is not None else None


def owner_record(family, row):
    return {
        "family": family.table,
        "owner_id": [_text(row[k]) for k in family.keys],
        "owner_state": row.get("state", row.get("status", row.get("reference_state"))),
    }


def references(family, row):
    base = {"type": "reference", **owner_record(family, row)}
    if family.form in {"ref", "deletion"}:
        saved = row.get("storage_ref") or {}
        ref = {k: v for k, v in saved.items() if v is not None}
        result = {
            **base,
            "role": "deletion_intent" if family.form == "deletion" else "saved_owner",
            **{
                k: ref[k]
                for k in REF_FIELDS
                if k in ref and k not in {"object_key", "key_prefix"}
            },
            "key": ref.get("object_key"),
            "prefix": ref.get("prefix", ref.get("key_prefix")),
            "version_id": row.get("object_version_id"),
        }
        if not row.get("ref_present"):
            result["unknown"] = "missing_saved_owner"
        elif type(ref.get("schema")) is not int or ref["schema"] not in (
            {1, 2}
            if family.table in {"avatar_uploads", "bug_attachment_uploads"}
            else {1}
        ):
            result["unknown"] = "unknown_saved_schema"
        if family.form == "deletion" and not row.get("ref_present"):
            result.update(bucket=row.get("bucket"), prefix=row.get("prefix"))
        if result["key"] is not None:
            result.update(
                sha256=row.get("stored_sha256", row.get("sha256")),
                size_bytes=row.get("stored_size_bytes", row.get("size_bytes")),
                etag=row.get("etag"),
            )
            if row.get("canonical_sha256"):
                result["canonical_representation_sha256"] = row["canonical_sha256"]
        yield result
        if row.get("package_key") is not None:
            yield {
                **result,
                "role": "package_object",
                "key": row["package_key"],
                "prefix": None,
                "sha256": row.get("package_sha256"),
                "size_bytes": row.get("size_bytes"),
                "version_id": None,
            }
    else:
        role = {
            "creation": "creation_history",
            "retirement": "retirement_history",
            "current": "current_locator",
            "preview": "preview_locator",
            "cache": "cache_owner",
        }[family.form]
        key = row.get("object_key")
        if family.form == "cache":
            key = f"original-cache/v1/{row['asset_id']}/{row['sha256']}/{row['id']}"
        result = {
            **base,
            "role": role,
            "location_id": _text(row.get("location_id")),
            "endpoint_url": row.get("saved_endpoint"),
            "bucket": row.get("bucket"),
            "key": key,
            "storage_identity": row.get("storage_identity"),
            "version_id": row.get("version_id"),
            "sha256": row.get("sha256"),
            "size_bytes": row.get("size_bytes"),
            "etag": row.get("etag"),
        }
        yield result
        if family.form == "creation" and row.get("staging_key") is not None:
            yield {
                **base,
                "role": "staging_declaration",
                "endpoint_url": row.get("saved_endpoint"),
                "bucket": row.get("staging_bucket"),
                "key": row["staging_key"],
                "version_id": None,
            }
    for name in DOCUMENTS:
        if row.get(name + "_unexpanded"):
            yield {
                "type": "unexpanded",
                **owner_record(family, row),
                "document": name,
                "code": "private_frozen_document_not_expanded",
            }
    if (
        family.inventory
        and row.get("inventory_present")
        and (
            row.get("inventory_schema") != 1
            or row.get("inventory_objects_type") != "object"
        )
    ):
        yield {
            "type": "unexpanded",
            **owner_record(family, row),
            "document": "inventory",
            "code": "unknown_inventory_schema",
        }


class Spool:
    def __init__(self, path, budget):
        self.stream = private_open(path)
        self.budget = budget
        self.size = 0

    def emit(self, row):
        data = canonical(row)
        self.budget.check()
        if self.size + len(data) > self.budget.limits.max_output_bytes - 32768:
            raise ReferenceError("spool_byte_budget")
        self.stream.write(data)
        self.size += len(data)


async def paged(connection, base, keys, budget):
    from sqlalchemy import text

    last = None
    order = ",".join(expression for expression, _ in keys)
    while True:
        budget.take("pages")
        remaining = budget.limits.max_seconds - (time.monotonic() - budget.started)
        milliseconds = max(
            1, math.ceil(min(remaining, budget.limits.statement_seconds) * 1000)
        )
        await connection.execute(
            text("SET LOCAL statement_timeout = '%dms'" % milliseconds)
        )
        params = {"limit": budget.limits.page_size + 1}
        where = ""
        if last is not None:
            params.update({"last" + str(i): value for i, value in enumerate(last)})
            where = (
                " WHERE ("
                + order
                + ") > ("
                + ",".join(":last" + str(i) for i in range(len(last)))
                + ")"
            )
        rows = (
            (
                await connection.execute(
                    text(base + where + " ORDER BY " + order + " LIMIT :limit"), params
                )
            )
            .mappings()
            .all()
        )
        yield rows[: budget.limits.page_size]
        if len(rows) <= budget.limits.page_size:
            return
        last = tuple(rows[budget.limits.page_size - 1][column] for _, column in keys)


async def collect(engine, spool, budget):
    from sqlalchemy import text
    from storage_migration_references import endpoint

    result = {
        "started_at": now(),
        "enumeration_complete": True,
        "families": [],
        "transaction": "repeatable read, read only; closed before source report processing",
    }
    try:
        budget.check()
        remaining = budget.limits.max_seconds - (time.monotonic() - budget.started)
        async with (
            asyncio.timeout(remaining),
            engine.connect() as connection,
            connection.begin(),
        ):
            await connection.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            await connection.execute(
                text(
                    "SET LOCAL statement_timeout = '%dms'"
                    % max(1, math.ceil(budget.limits.statement_seconds * 1000))
                )
            )
            tables = (
                *[family.table for family in FAMILIES],
                *CONTEXT,
                "storage_connections",
                "alembic_version",
            )
            table_list = ",".join("'" + name + "'" for name in tables)
            rows = await connection.execute(
                text(
                    "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' AND table_name IN ("
                    + table_list
                    + ") ORDER BY table_name,column_name"
                )
            )
            columns = {}
            for table, column in rows:
                columns.setdefault(table, set()).add(column)
            result["revisions"] = list(
                await connection.scalars(
                    text("SELECT version_num FROM alembic_version ORDER BY version_num")
                )
            )
            context_sql = (
                "SELECT l.id,l.kind,l.config_revision,l.enabled,l.connection_id,"
                "CASE WHEN l.connection_id IS NULL THEN l.config->>'endpoint' ELSE c.config->>'endpoint' END AS endpoint,"
                "CASE WHEN l.connection_id IS NULL THEN l.config->>'use_ssl' ELSE c.config->>'use_ssl' END AS use_ssl,"
                "c.kind AS connection_kind,c.scope AS connection_scope,c.revision AS connection_revision,"
                "c.config->>'bucket' AS bucket,c.config->>'base_prefix' AS base_prefix "
                "FROM storage_locations l LEFT JOIN storage_connections c ON c.id=l.connection_id"
            )
            async for page in paged(connection, context_sql, (("l.id", "id"),), budget):
                for row in page:
                    budget.take("rows")
                    value = dict(row)
                    address = value.pop("endpoint")
                    ssl = value.pop("use_ssl")
                    if isinstance(address, str) and "://" not in address:
                        address = (
                            ("https" if ssl == "true" else "http") + "://" + address
                        )
                    if value["kind"] != "s3" or (
                        value["connection_id"] is not None
                        and value["connection_kind"] != "s3"
                    ):
                        address = None
                    value["endpoint_url"] = address if endpoint(address) else None
                    value["location_id"] = str(value.pop("id"))
                    value["connection_id"] = _text(value["connection_id"])
                    spool.emit({"type": "location", **value})
            async for page in paged(
                connection,
                "SELECT b.purpose,b.location_id,b.bucket,b.generation FROM storage_bindings b",
                (("b.purpose", "purpose"),),
                budget,
            ):
                for row in page:
                    budget.take("rows")
                    spool.emit(
                        {
                            "type": "binding",
                            **{
                                k: _text(v) if k == "location_id" else v
                                for k, v in row.items()
                            },
                        }
                    )
            for family in FAMILIES:
                fields = columns.get(family.table, set())
                projected = projection(family, fields)
                base = f"SELECT {projected} FROM {quoted(family.table)} r"
                keys = tuple(("r." + quoted(key), key) for key in family.keys)
                async for page in paged(connection, base, keys, budget):
                    for row in page:
                        budget.take("rows")
                        for record in references(family, dict(row)):
                            spool.emit(record)
                if family.inventory:
                    await expand_inventory(connection, family, spool, budget, fields)
                result["families"].append(family.table)
    except Exception as exc:
        result["enumeration_complete"] = False
        result["issue"] = (
            exc.code
            if isinstance(exc, ReferenceError)
            else "time_budget"
            if isinstance(exc, TimeoutError)
            else "database_snapshot_failed"
        )
    finally:
        result.update(
            finished_at=now(),
            rows=budget.rows,
            pages=budget.pages,
            expanded=budget.expanded,
        )
        spool.stream.flush()
    return result


async def expand_inventory(connection, family, spool, budget, columns=()):
    keys = ",".join("r." + quoted(k) for k in family.keys)
    states = "".join(
        ",r." + quoted(name) for name in ("state", "status") if name in columns
    )
    query = (
        f"SELECT {keys}{states},{ref_projection()},o.key AS inventory_key,"
        "o.value->>'sha256' AS object_sha256,o.value->'size_bytes' AS object_size,"
        "o.value->>'version_id' AS object_version,o.value->>'state' AS object_state "
        f"FROM {quoted(family.table)} r CROSS JOIN LATERAL jsonb_each("
        "CASE WHEN r.inventory->'schema'='1'::jsonb AND jsonb_typeof(r.inventory->'objects')='object' "
        "THEN r.inventory->'objects' ELSE '{}'::jsonb END) o"
    )
    keyset = tuple(("r." + quoted(key), key) for key in family.keys) + (
        ('o.key COLLATE "C"', "inventory_key"),
    )
    async for page in paged(connection, query, keyset, budget):
        for row in page:
            budget.take("expanded")
            ref = row["storage_ref"]
            key = row["inventory_key"]
            if family.table == "prediction_input_leases":
                prefix = ref.get("key_prefix")
                if not isinstance(prefix, str):
                    raise ReferenceError("inventory_prefix_unknown")
                key = prefix + key
            record = {
                "type": "reference",
                **owner_record(family, row),
                "role": "inventory_object",
                **{
                    k: ref[k]
                    for k in REF_FIELDS
                    if ref.get(k) is not None
                    and k not in {"prefix", "key_prefix", "object_key"}
                },
                "key": key,
                "sha256": row["object_sha256"],
                "size_bytes": row["object_size"],
                "version_id": row["object_version"],
                "receipt_state": row["object_state"],
            }
            if ref.get("schema") != 1:
                record["unknown"] = "unknown_saved_schema"
            spool.emit(record)


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    mode = command.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--verify", type=Path)
    command.add_argument("--source-inventory", type=Path)
    command.add_argument("--temp-dir", type=Path)
    for name, value in Limits.__dataclass_fields__.items():
        command.add_argument(
            "--" + name.replace("_", "-"),
            type=float if name.endswith("seconds") else int,
            default=value.default,
        )
    return command


async def configured(args, budget):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
    from app.config import settings
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        settings.database_url, hide_parameters=True, echo=False
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix="aap-reference-", dir=args.temp_dir
        ) as directory:
            spool_path = Path(directory) / "snapshot.jsonl"
            spool = Spool(spool_path, budget)
            try:
                metadata = await collect(engine, spool, budget)
            finally:
                spool.stream.close()
            # collect() has exited its connection and transaction before source open.
            return write_catalog(
                args.output,
                spool_path,
                metadata,
                args.source_inventory,
                directory,
                budget,
            )
    finally:
        await engine.dispose()


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    try:
        if args.output and args.source_inventory is None:
            raise ReferenceError("source_inventory_required")
        limits = Limits(
            **{name: getattr(args, name) for name in Limits.__dataclass_fields__}
        )
        result = (
            verify(args.verify, limits)
            if args.verify
            else asyncio.run(configured(args, Budget(limits)))
        )
        # Only bounded result counters, never owner IDs, raw paths or exceptions.
        print(
            json.dumps(
                {k: v for k, v in result.items() if k not in {"file_sha256", "type"}},
                sort_keys=True,
            )
        )
        return 0 if result["complete"] else 1
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, ReferenceError)
            else "reference_inventory_failed"
        )
        print(json.dumps({"complete": False, "code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
