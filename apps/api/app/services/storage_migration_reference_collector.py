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

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.services.storage_migration_references import (
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
    schemas: tuple[int, ...] = (1, 2)
    purposes: tuple[str, ...] = ()
    version_column: str = "version_id"


FAMILIES = (
    Family("prediction_input_leases", inventory=True, purposes=("import",)),
    Family("image_pyramid_outputs", inventory=True, purposes=("media-cache",)),
    Family("video_derivative_outputs", inventory=True, purposes=("media-cache",)),
    Family("export_media_bundles", inventory=True, purposes=("export",)),
    Family("training_dataset_versions", purposes=("training-dataset",)),
    Family("training_runs", purposes=("training-run",)),
    Family("training_previews", purposes=("training-preview",)),
    Family("sample_evaluation_runs", purposes=("sample-evaluation",)),
    Family("active_learning_rounds", purposes=("active-learning",)),
    Family(
        "avatar_uploads",
        ("token",),
        purposes=("avatars",),
        version_column="object_version_id",
    ),
    Family(
        "bug_attachment_uploads",
        purposes=("bug-reports",),
        version_column="object_version_id",
    ),
    Family("comment_attachment_uploads", purposes=("annotations",)),
    Family("guide_asset_uploads", purposes=("annotations",)),
    Family("raster_mask_objects", purposes=("annotations",)),
    Family("export_outputs", purposes=("export",)),
    Family("audit_archive_outputs", purposes=("audit-archive",)),
    Family("managed_original_creations", form="creation"),
    Family("managed_original_retirements", form="retirement"),
    Family(
        "dataset_asset_locations",
        ("asset_id", "location_id", "bucket"),
        "current",
        schemas=(1,),
        purposes=("datasets",),
    ),
    Family("task_originals", form="current", schemas=(2,), purposes=("annotations",)),
    Family(
        "dataset_auxiliary_originals",
        form="current",
        schemas=(1,),
        purposes=("datasets",),
    ),
    Family("dataset_source_previews", ("source_id", "path_digest"), "preview"),
    Family(
        "original_cache_entries", form="cache", schemas=(2,), purposes=("media-cache",)
    ),
    Family(
        "training_storage_deletions",
        form="deletion",
        purposes=("training-dataset", "training-run"),
    ),
    Family("mask_format_uploads", form="mask-upload", purposes=("import",)),
    Family("mask_format_imports", form="mask-import", purposes=("import",)),
    Family(
        "thumbnail_outputs", form="thumbnail", schemas=(2,), purposes=("media-cache",)
    ),
)
_OWNER_SCHEMAS = {
    "training_dataset_versions": "yoao_learning_v1",
    "training_runs": "yoao_learning_v1",
    "training_previews": "yoao_learning_v1",
    "sample_evaluation_runs": "yoao_learning_v1",
    "active_learning_rounds": "yoao_learning_v1",
    "training_storage_deletions": "yoao_learning_v1",
    "avatar_uploads": "yoao_identity_v1",
    "bug_attachment_uploads": "yoao_diagnostics_v1",
    "comment_attachment_uploads": "yoao_labeling_v1",
    "guide_asset_uploads": "yoao_workspace_v1",
    "raster_mask_objects": "yoao_labeling_v1",
    "audit_archive_outputs": "yoao_audit_v1",
    "mask_format_uploads": "yoao_labeling_v1",
    "mask_format_imports": "yoao_labeling_v1",
}
_EXPORT_PROJECTIONS = {
    "export_outputs": "yoao_export_v1.export_zip_inventory",
    "export_media_bundles": "yoao_export_v1.export_media_inventory",
}
_TEXT_CURSORS = {"bucket", "inventory_key", "path_digest", "purpose", "token"}

DOCUMENTS = (
    "input_snapshot",
    "snapshot",
    "source_ref",
    "original_ref",
    "source",
    "upload_inventory",
    "manifest",
)


def _text(value):
    return str(value) if value is not None else None


def owner_record(family, row):
    return {
        "family": family.table,
        "owner_id": [_text(row[k]) for k in family.keys],
        "owner_state": row.get("state", row.get("status", row.get("reference_state"))),
    }


def reference_present(row, column="storage_ref"):
    flag = "ref_present" if column == "storage_ref" else "staging_ref_present"
    return row.get(flag, row.get(column) is not None)


def saved_locator(family, row, *, column="storage_ref", purposes=None, schemas=None):
    raw = row.get(column)
    ref = {
        key: value
        for key, value in (raw.items() if isinstance(raw, dict) else ())
        if key in REF_FIELDS and value is not None
    }
    result = {
        **{
            key: value
            for key, value in ref.items()
            if key not in {"object_key", "prefix"}
        },
        "key": ref.get("object_key"),
        "prefix": ref.get("prefix")
        if ref.get("object_key") is not None
        else ref.get("prefix", ref.get("key_prefix")),
        "version_id": row.get(family.version_column),
    }
    if not reference_present(row, column):
        result["unknown"] = "missing_saved_owner"
    elif type(ref.get("schema")) is not int or ref["schema"] not in (
        family.schemas if schemas is None else schemas
    ):
        result["unknown"] = "unknown_saved_schema"
    elif ref.get("purpose") not in (family.purposes if purposes is None else purposes):
        result["unknown"] = "unknown_saved_purpose"
    elif not all(
        isinstance(ref.get(key), str) and ref[key]
        for key in ("endpoint_url", "bucket", "storage_identity")
    ):
        result["unknown"] = "incomplete_saved_reference"
    return result


def object_receipt(result, row):
    result.update(
        sha256=row.get("stored_sha256", row.get("sha256")),
        size_bytes=row.get("stored_size_bytes", row.get("size_bytes")),
        etag=row.get("etag"),
    )
    if row.get("canonical_sha256"):
        result["canonical_representation_sha256"] = row["canonical_sha256"]
    return result


def compare_locator(result, row, fields):
    for key, column in fields:
        if row.get(column) is not None and _text(row[column]) != result.get(key):
            result.setdefault("unknown", "saved_locator_conflict")
    return result


def derived_object(result, relative):
    prefix = result.get("prefix")
    if isinstance(prefix, str) and prefix and isinstance(relative, str) and relative:
        result.update(key=prefix + relative, prefix=None)
    else:
        result.setdefault("unknown", "object_key_unknown")
        result.update(key=None, prefix=None)
    return result


def references(family, row):
    base = {"type": "reference", **owner_record(family, row)}
    if family.form in {"ref", "deletion", "thumbnail", "mask-upload", "mask-import"}:
        column = "staging_ref" if family.form == "mask-import" else "storage_ref"
        result = {
            **base,
            "role": "deletion_intent" if family.form == "deletion" else "saved_owner",
            **saved_locator(family, row, column=column),
        }
        if family.form == "deletion":
            if not reference_present(row):
                result.update(bucket=row.get("bucket"), prefix=row.get("prefix"))
            else:
                compare_locator(
                    result, row, (("bucket", "bucket"), ("prefix", "prefix"))
                )
        elif family.form == "thumbnail":
            derived_object(result, "thumbnail.webp")
        elif family.form in {"mask-upload", "mask-import"}:
            logical = row.get(
                "logical_object_key"
                if family.form == "mask-upload"
                else "staged_object_key"
            )
            prefix, separator, relative = (
                logical.rpartition("/") if isinstance(logical, str) else ("", "", "")
            )
            physical = result.get("prefix")
            if (
                not separator
                or not relative
                or not isinstance(physical, str)
                or not physical.endswith(prefix + "/")
            ):
                result.setdefault("unknown", "staged_key_conflict")
            derived_object(result, relative)
        if result["key"] is not None:
            object_receipt(result, row)
            if family.form == "mask-import":
                result["sha256"] = row.get("staged_sha256")
        yield result
        if row.get("package_key") is not None:
            package = {
                **result,
                "role": "package_object",
                "key": row["package_key"],
                "prefix": None,
                "sha256": row.get("package_sha256"),
                "size_bytes": row.get("size_bytes"),
                "version_id": None,
            }
            prefix = result.get("prefix")
            if (
                not isinstance(prefix, str)
                or not isinstance(package["key"], str)
                or not package["key"].startswith(prefix)
                or len(package["key"]) <= len(prefix)
            ):
                package.setdefault("unknown", "package_outside_saved_prefix")
            yield package
    else:
        role = {
            "creation": "creation_history",
            "retirement": "retirement_history",
            "current": "current_locator",
            "preview": "preview_locator",
            "cache": "cache_owner",
        }[family.form]
        if family.form in {"current", "cache"} and reference_present(row):
            result = {**base, "role": role, **saved_locator(family, row)}
            if family.form == "cache":
                derived_object(result, "payload")
            compare_locator(
                result,
                row,
                (
                    ("location_id", "location_id"),
                    ("bucket", "bucket"),
                    ("key", "object_key"),
                    ("storage_identity", "storage_identity"),
                ),
            )
            object_receipt(result, row)
        else:
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
                "version_id": row.get(family.version_column),
                "sha256": row.get("sha256"),
                "size_bytes": row.get("size_bytes"),
                "etag": row.get("etag"),
            }
        yield result
        if family.form == "creation" and row.get("staging_key") is not None:
            staging = {**base, "role": "staging_declaration"}
            if reference_present(row, "staging_ref"):
                staging.update(
                    saved_locator(
                        family,
                        row,
                        column="staging_ref",
                        purposes=("import",),
                        schemas=(1, 2),
                    )
                )
                derived_object(staging, "payload")
                compare_locator(
                    staging, row, (("bucket", "staging_bucket"), ("key", "staging_key"))
                )
                staging["version_id"] = None
            else:
                staging.update(
                    endpoint_url=row.get("saved_endpoint"),
                    bucket=row.get("staging_bucket"),
                    key=row["staging_key"],
                    version_id=None,
                )
                if row.get("legacy_staging_bucket") is not None and row[
                    "legacy_staging_bucket"
                ] != row.get("staging_bucket"):
                    staging["unknown"] = "saved_locator_conflict"
            yield staging
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


async def paged(connection, capability, keys, budget, *, record=False):
    cursor = {key: None for key in keys}
    arguments = ", ".join(
        f"CAST(:{key} AS {'text' if key in _TEXT_CURSORS else 'uuid'})" for key in keys
    )
    source = f"{capability}({arguments}, :limit)"
    query = text(
        f"SELECT * FROM {source}" if record else f"SELECT value FROM {source} AS value"
    )
    while True:
        budget.take("pages")
        remaining = budget.limits.max_seconds - (time.monotonic() - budget.started)
        milliseconds = max(
            1, math.ceil(min(remaining, budget.limits.statement_seconds) * 1000)
        )
        await connection.execute(
            text("SELECT pg_catalog.set_config('statement_timeout', :value, true)"),
            {"value": f"{milliseconds}ms"},
        )
        result = await connection.execute(
            query, {**cursor, "limit": budget.limits.page_size + 1}
        )
        rows = result.mappings().all() if record else result.scalars().all()
        yield rows[: budget.limits.page_size]
        if len(rows) <= budget.limits.page_size:
            return
        last = rows[budget.limits.page_size - 1]
        cursor = {key: last[key] for key in keys}


def _snapshot_issue(exc):
    if isinstance(exc, ReferenceError):
        return exc.code
    if isinstance(exc, TimeoutError):
        return "time_budget"
    if isinstance(exc, DBAPIError):
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate == "42501":
            return "database_snapshot_denied"
        if sqlstate in {"42883", "42P01", "42703"}:
            return "inventory_capabilities_unavailable"
    return "database_snapshot_failed"


async def collect(engine, spool, budget):
    from app.services.storage_migration_references import endpoint

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
            revisions = await connection.scalar(
                text("SELECT yoao_assets_v1.migration_inventory_revisions()")
            )
            if not revisions:
                raise ReferenceError("database_revision_unavailable")
            result["revisions"] = revisions
            async for page in paged(
                connection,
                "yoao_assets_v1.migration_inventory_locations",
                ("id",),
                budget,
            ):
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
                "yoao_assets_v1.migration_inventory_bindings",
                ("purpose",),
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
                export_projection = _EXPORT_PROJECTIONS.get(family.table)
                owner = _OWNER_SCHEMAS.get(family.table, "yoao_assets_v1")
                capability = export_projection or f"{owner}.inventory_{family.table}"
                async for page in paged(
                    connection,
                    capability,
                    family.keys,
                    budget,
                    record=export_projection is not None,
                ):
                    for row in page:
                        budget.take("rows")
                        for record in references(family, dict(row)):
                            spool.emit(record)
                if family.inventory:
                    await expand_inventory(
                        connection,
                        family,
                        spool,
                        budget,
                        capability + "_objects",
                        record=export_projection is not None,
                    )
                result["families"].append(family.table)
    except Exception as exc:
        result["enumeration_complete"] = False
        result["issue"] = _snapshot_issue(exc)
    finally:
        result.update(
            finished_at=now(),
            rows=budget.rows,
            pages=budget.pages,
            expanded=budget.expanded,
        )
        spool.stream.flush()
    return result


async def expand_inventory(connection, family, spool, budget, capability, *, record):
    async for page in paged(
        connection, capability, (*family.keys, "inventory_key"), budget, record=record
    ):
        for row in page:
            budget.take("expanded")
            ref = row["storage_ref"] or {}
            key = row["inventory_key"]
            prefix = ref.get(
                "key_prefix"
                if family.table in {"prediction_input_leases", "export_media_bundles"}
                else "prefix"
            )
            if family.table == "prediction_input_leases":
                key = (
                    prefix + key
                    if isinstance(prefix, str) and isinstance(key, str)
                    else None
                )
            entry = {
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
            saved = saved_locator(family, row)
            if saved.get("unknown"):
                entry["unknown"] = saved["unknown"]
            if not isinstance(prefix, str) or not prefix or not prefix.endswith("/"):
                entry.setdefault("unknown", "inventory_prefix_unknown")
            elif (
                not isinstance(key, str)
                or not key.startswith(prefix)
                or len(key) <= len(prefix)
            ):
                entry.setdefault("unknown", "inventory_key_outside_saved_prefix")
            spool.emit(entry)


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
