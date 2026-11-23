"""Closed SQL capabilities, readonly snapshots and safe CLI results."""

import ast
import asyncio
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from sqlalchemy.exc import DBAPIError

import inventory_storage_references as cli
import storage_migration_references as core

ROOT = Path(__file__).resolve().parents[1]


def saved_reference(purpose, *, schema=2, key=None, prefix=None, root="公司/%2F/"):
    value = {
        "schema": schema,
        "purpose": purpose,
        "location_id": str(uuid.UUID(int=17)),
        "config_revision": 1,
        "binding_generation": 2,
        "endpoint_url": "http://saved-b:9000",
        "bucket": "saved-b",
        "storage_identity": core.identity("http://saved-b:9000", "saved-b"),
        "key_prefix": root,
    }
    if key is not None:
        value["object_key"] = key
    if prefix is not None:
        value["prefix"] = prefix
    return value


def family_for(table):
    return next(family for family in cli.FAMILIES if family.table == table)


def function_source(name):
    resource = ROOT / "apps/api/alembic/sql/0497_storage_inventory_capabilities.sql"
    marker = f".{name}("
    return next(
        statement
        for statement in resource.read_text().split("-- aap-statement-break")
        if statement.lstrip().startswith("CREATE FUNCTION ")
        and marker in statement.lstrip().splitlines()[0]
    )


class SqlFailure(Exception):
    def __init__(self, state):
        self.sqlstate = state


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def __iter__(self):
        return iter(self.rows)

    def mappings(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return self.rows


class Engine:
    def __init__(self, *, version="bo0497", rows=(), fault=None):
        self.version, self.rows, self.fault = version, rows, fault
        self.calls, self.open = [], False

    @asynccontextmanager
    async def connect(self):
        self.open = True
        try:
            yield self
        finally:
            self.open = False

    @asynccontextmanager
    async def begin(self):
        yield self

    def _record(self, statement):
        sql = str(statement)
        self.calls.append(sql)
        if self.fault and self.fault[0] in sql:
            raise DBAPIError(sql, None, SqlFailure(self.fault[1]))
        return sql

    async def scalar(self, statement):
        assert "migration_inventory_revisions()" in self._record(statement)
        return [self.version]

    async def execute(self, statement, params=None):
        sql = self._record(statement)
        if "inventory_bug_attachment_uploads(" in sql:
            rows = [
                row
                for row in self.rows
                if params["id"] is None or row["id"] > params["id"]
            ]
            return Result(rows[: params["limit"]])
        return Result()


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="storage-reference-cli-test-"
        )
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_registry_covers_every_persisted_saved_reference_column(self):
        tables = set()
        for path in (ROOT / "apps/api/app/db/models").glob("*.py"):
            for model in ast.parse(path.read_text()).body:
                if not isinstance(model, ast.ClassDef):
                    continue
                if not any(
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id in {"storage_ref", "staging_ref"}
                    for node in model.body
                ):
                    continue
                for node in model.body:
                    if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "__tablename__"
                        for target in node.targets
                    ):
                        tables.add(ast.literal_eval(node.value))
        self.assertEqual(len(tables), 25)
        self.assertTrue(tables <= {family.table for family in cli.FAMILIES})
        self.assertEqual(len(cli.FAMILIES), 27)

    def test_schema_two_object_root_is_metadata_not_a_prefix_claim(self):
        for table, purpose in (
            ("comment_attachment_uploads", "annotations"),
            ("guide_asset_uploads", "annotations"),
            ("raster_mask_objects", "annotations"),
            ("export_outputs", "export"),
            ("audit_archive_outputs", "audit-archive"),
        ):
            with self.subTest(table=table):
                ref = saved_reference(purpose, key="公司/%2F/raw/../payload")
                record = next(
                    cli.references(
                        family_for(table),
                        {"id": "I", "ref_present": True, "storage_ref": ref},
                    )
                )
                self.assertNotIn("unknown", record)
                self.assertEqual(record["key"], ref["object_key"])
                self.assertIsNone(record.get("prefix"))

    def test_current_forms_keep_saved_endpoint_identity_and_version(self):
        for table, purpose, schema in (
            ("dataset_asset_locations", "datasets", 1),
            ("dataset_auxiliary_originals", "datasets", 1),
            ("task_originals", "annotations", 2),
        ):
            with self.subTest(table=table):
                ref = saved_reference(purpose, schema=schema, key="公司/%2F/raw")
                row = {
                    "id": "I",
                    "asset_id": "A",
                    "ref_present": True,
                    "storage_ref": ref,
                    "bucket": ref["bucket"],
                    "location_id": ref["location_id"],
                    "object_key": ref["object_key"],
                    "storage_identity": ref["storage_identity"],
                    "version_id": "null",
                }
                record = next(cli.references(family_for(table), row))
                self.assertNotIn("unknown", record)
                self.assertEqual(record["endpoint_url"], ref["endpoint_url"])
                self.assertEqual(record["storage_identity"], ref["storage_identity"])
                self.assertEqual(record["version_id"], "null")

    def test_new_cache_uses_saved_payload_and_null_keeps_legacy_key(self):
        family = family_for("original_cache_entries")
        row = {
            "id": "I",
            "asset_id": "A",
            "sha256": "a" * 64,
            "bucket": "saved-b",
            "location_id": str(uuid.UUID(int=17)),
        }
        logical = f"original-cache/v1/A/{'a' * 64}/I/"
        row.update(
            ref_present=True,
            storage_ref=saved_reference("media-cache", prefix="公司/%2F/" + logical),
        )
        record = next(cli.references(family, row))
        self.assertEqual(record["key"], "公司/%2F/" + logical + "payload")
        self.assertIsNone(record.get("prefix"))
        self.assertEqual(record["endpoint_url"], "http://saved-b:9000")
        row.update(ref_present=False, storage_ref=None)
        self.assertEqual(next(cli.references(family, row))["key"], logical.rstrip("/"))

    def test_creation_staging_uses_its_own_saved_endpoint(self):
        ref = saved_reference("import", root="公司/%2F/staging/I/")
        row = {
            "id": "I",
            "bucket": "final-a",
            "object_key": "final-key",
            "saved_endpoint": "http://final-a:9000",
            "staging_bucket": "saved-b",
            "staging_key": ref["key_prefix"] + "payload",
            "staging_ref_present": True,
            "staging_ref": ref,
        }
        final, staging = list(
            cli.references(family_for("managed_original_creations"), row)
        )
        self.assertEqual(final["endpoint_url"], "http://final-a:9000")
        self.assertEqual(staging["endpoint_url"], ref["endpoint_url"])
        self.assertEqual(staging["key"], row["staging_key"])
        self.assertNotIn("unknown", staging)

    def test_mask_saved_keys_use_opaque_filename_and_current_transport(self):
        for table, column, logical_field in (
            ("mask_format_uploads", "storage_ref", "logical_object_key"),
            ("mask_format_imports", "staging_ref", "staged_object_key"),
        ):
            with self.subTest(table=table):
                ref = saved_reference("import", root="公司/%2F/mask-formats/P/U/I/")
                row = {
                    "id": "I",
                    column: ref,
                    "ref_present": True,
                    "staging_ref_present": True,
                    logical_field: "mask-formats/P/U/I/raw%2F.png",
                    "staged_sha256": "a" * 64,
                }
                record = next(cli.references(family_for(table), row))
                self.assertEqual(record["key"], ref["key_prefix"] + "raw%2F.png")
                self.assertIsNone(record.get("prefix"))
                self.assertEqual(record["endpoint_url"], ref["endpoint_url"])
                self.assertNotIn("unknown", record)

    def test_thumbnail_has_exact_physical_key_and_saved_version(self):
        prefix = "公司/%2F/thumbnails/objects/I/"
        row = {
            "id": "I",
            "storage_ref": saved_reference("media-cache", prefix=prefix),
            "ref_present": True,
            "version_id": "saved-version",
            "sha256": "a" * 64,
            "size_bytes": 3,
        }
        record = next(cli.references(family_for("thumbnail_outputs"), row))
        self.assertEqual(record["key"], prefix + "thumbnail.webp")
        self.assertEqual(record["version_id"], "saved-version")
        self.assertIsNone(record.get("prefix"))
        self.assertNotIn("unknown", record)

    def test_family_schemas_are_closed_and_current_bad_ref_never_falls_back(self):
        for table, purpose, forbidden in (
            ("dataset_asset_locations", "datasets", 2),
            ("dataset_auxiliary_originals", "datasets", 2),
            ("task_originals", "annotations", 1),
            ("original_cache_entries", "media-cache", 1),
            ("guide_asset_uploads", "annotations", True),
        ):
            with self.subTest(table=table):
                row = {
                    "id": "I",
                    "asset_id": "A",
                    "location_id": str(uuid.UUID(int=17)),
                    "bucket": "today",
                    "object_key": "today",
                    "sha256": "a" * 64,
                    "ref_present": True,
                    "storage_ref": saved_reference(
                        purpose, schema=forbidden, key="saved"
                    ),
                }
                record = next(cli.references(family_for(table), row))
                self.assertEqual(record["unknown"], "unknown_saved_schema")
                self.assertEqual(record["bucket"], "saved-b")

    async def test_schema_two_inventory_keeps_prediction_relative_keys_only(self):
        for table, purpose, relative in (
            ("prediction_input_leases", "import", True),
            ("image_pyramid_outputs", "media-cache", False),
            ("video_derivative_outputs", "media-cache", False),
            ("export_media_bundles", "export", False),
        ):
            with self.subTest(table=table):
                prefix = "公司/%2F/objects/I/"
                ref = saved_reference(
                    purpose,
                    root=prefix
                    if relative or table == "export_media_bundles"
                    else "公司/%2F/",
                    prefix=None
                    if relative or table == "export_media_bundles"
                    else prefix,
                )
                row = {
                    "id": uuid.uuid4(),
                    "state": "retired",
                    "storage_ref": ref,
                    "inventory_key": "%2F/../raw"
                    if relative
                    else prefix + "%2F/../raw",
                    "object_sha256": "a" * 64,
                    "object_size": 3,
                    "object_version": "null",
                    "object_state": "verified",
                }
                engine = SimpleNamespace(execute=AsyncMock(return_value=Result([row])))
                path = self.root / table
                spool = cli.Spool(path, core.Budget(core.Limits()))
                try:
                    await cli.expand_inventory(
                        engine,
                        family_for(table),
                        spool,
                        spool.budget,
                        "yoao_assets_v1.inventory_objects",
                        record=False,
                    )
                finally:
                    spool.stream.close()
                record = json.loads(path.read_text())
                self.assertEqual(record["key"], prefix + "%2F/../raw")
                self.assertEqual(record["version_id"], "null")
                self.assertNotIn("unknown", record)

    async def test_inventory_keys_must_be_nonempty_literal_owner_descendants(self):
        owner_prefix = "公司/%2F/objects/I/"
        for table, purpose, relative in (
            ("prediction_input_leases", "import", True),
            ("image_pyramid_outputs", "media-cache", False),
            ("video_derivative_outputs", "media-cache", False),
            ("export_media_bundles", "export", False),
        ):
            ref = saved_reference(
                purpose,
                root=owner_prefix
                if relative or table == "export_media_bundles"
                else "公司/%2F/",
                prefix=None
                if relative or table == "export_media_bundles"
                else owner_prefix,
            )
            keys = (
                ("", "../opaque%2F/key")
                if relative
                else (
                    owner_prefix,
                    "公司/%2F/objects/I-neighbor/payload",
                    "公司/%2F/objects/J/payload",
                    owner_prefix + "../opaque%2F/key",
                )
            )
            for index, key in enumerate(keys):
                with self.subTest(table=table, key=key):
                    row = {
                        "id": uuid.uuid4(),
                        "state": "retired",
                        "storage_ref": ref,
                        "inventory_key": key,
                        "object_sha256": "a" * 64,
                        "object_size": 3,
                        "object_version": "saved-version",
                        "object_state": "verified",
                    }
                    engine = SimpleNamespace(
                        execute=AsyncMock(return_value=Result([row]))
                    )
                    path = self.root / f"boundary-{table}-{index}"
                    spool = cli.Spool(path, core.Budget(core.Limits()))
                    try:
                        await cli.expand_inventory(
                            engine,
                            family_for(table),
                            spool,
                            spool.budget,
                            "yoao_assets_v1.inventory_objects",
                            record=False,
                        )
                    finally:
                        spool.stream.close()
                    record = json.loads(path.read_text())
                    self.assertEqual(
                        record["key"], owner_prefix + key if relative else key
                    )
                    if index == len(keys) - 1:
                        self.assertNotIn("unknown", record)
                    else:
                        self.assertEqual(
                            record["unknown"], "inventory_key_outside_saved_prefix"
                        )

    def test_staging_projection_is_separate_and_excludes_entire_config(self):
        sql = function_source("inventory_managed_original_creations")
        self.assertIn("_inventory_saved_locator(r.staging_ref, false)", sql)
        self.assertIn("r.configuration->>'endpoint'", sql)
        self.assertNotIn("'configuration', r.configuration", sql)
        self.assertNotIn("evidence", sql)

    async def test_hidden_family_is_not_reported_as_absent(self):
        engine = Engine(fault=("inventory_task_originals(", "42501"))
        spool = cli.Spool(self.root / "denied", core.Budget(core.Limits()))
        try:
            metadata = await cli.collect(engine, spool, spool.budget)
        finally:
            spool.stream.close()
        self.assertFalse(metadata["enumeration_complete"])
        self.assertEqual(metadata["issue"], "database_snapshot_denied")
        self.assertNotIn("families_not_present_in_schema", metadata)
        self.assertNotIn("task_originals", metadata["families"])

    def test_all_existing_schema_one_and_two_owner_families_are_supported(self):
        for table, purpose, kind in (
            ("prediction_input_leases", "import", "key_prefix"),
            ("image_pyramid_outputs", "media-cache", "prefix"),
            ("video_derivative_outputs", "media-cache", "prefix"),
            ("export_media_bundles", "export", "key_prefix"),
            ("training_dataset_versions", "training-dataset", "prefix"),
            ("training_runs", "training-run", "prefix"),
            ("training_previews", "training-preview", "prefix"),
            ("sample_evaluation_runs", "sample-evaluation", "prefix"),
            ("active_learning_rounds", "active-learning", "prefix"),
            ("avatar_uploads", "avatars", "object_key"),
            ("bug_attachment_uploads", "bug-reports", "object_key"),
            ("comment_attachment_uploads", "annotations", "object_key"),
            ("guide_asset_uploads", "annotations", "object_key"),
            ("raster_mask_objects", "annotations", "object_key"),
            ("export_outputs", "export", "object_key"),
            ("audit_archive_outputs", "audit-archive", "object_key"),
            ("training_storage_deletions", "training-run", "prefix"),
        ):
            for schema in (1, 2):
                with self.subTest(table=table, schema=schema):
                    ref = saved_reference(purpose, schema=schema)
                    ref[kind] = (
                        "公司/%2F/raw/object"
                        if kind == "object_key"
                        else "公司/%2F/raw/"
                    )
                    row = {
                        "id": "I",
                        "token": "T",
                        "storage_ref": ref,
                        "ref_present": True,
                    }
                    record = next(cli.references(family_for(table), row))
                    self.assertNotIn("unknown", record)
                    self.assertEqual(
                        record["key"] if kind == "object_key" else record["prefix"],
                        ref[kind],
                    )
                    row["storage_ref"] = {**ref, "schema": 99}
                    self.assertEqual(
                        next(cli.references(family_for(table), row))["unknown"],
                        "unknown_saved_schema",
                    )

    def test_actual_version_columns_keep_unknown_null_and_exact_distinct(self):
        for table, purpose, column in (
            ("avatar_uploads", "avatars", "object_version_id"),
            ("bug_attachment_uploads", "bug-reports", "object_version_id"),
            ("task_originals", "annotations", "version_id"),
            ("thumbnail_outputs", "media-cache", "version_id"),
        ):
            for version in (None, "null", "saved-exact-version"):
                with self.subTest(table=table, version=version):
                    ref = saved_reference(purpose, key="root/key")
                    if table == "thumbnail_outputs":
                        ref.pop("object_key")
                        ref["prefix"] = "root/thumbnails/objects/I/"
                    row = {
                        "id": "I",
                        "token": "T",
                        "storage_ref": ref,
                        "ref_present": True,
                        column: version,
                    }
                    self.assertEqual(
                        next(cli.references(family_for(table), row))["version_id"],
                        version,
                    )

    def test_return_to_legacy_with_empty_root_keeps_physical_key_and_prefix(self):
        for table, purpose, key, prefix in (
            ("guide_asset_uploads", "annotations", "guide/I.png", None),
            ("training_runs", "training-run", None, "projects/P/runs/R/"),
        ):
            with self.subTest(table=table):
                ref = saved_reference(purpose, key=key, prefix=prefix, root="")
                record = next(
                    cli.references(
                        family_for(table),
                        {"id": "I", "ref_present": True, "storage_ref": ref},
                    )
                )
                self.assertNotIn("unknown", record)
                self.assertEqual(record["key"], key)
                self.assertEqual(record["prefix"], prefix)

    def test_current_scalar_conflict_keeps_saved_address_and_is_unknown(self):
        ref = saved_reference("annotations", key="saved/key")
        row = {
            "id": "I",
            "storage_ref": ref,
            "ref_present": True,
            "bucket": "today",
            "object_key": "today/key",
        }
        record = next(cli.references(family_for("task_originals"), row))
        self.assertEqual(record["endpoint_url"], ref["endpoint_url"])
        self.assertEqual(record["key"], "saved/key")
        self.assertEqual(record["unknown"], "saved_locator_conflict")

    def test_wrong_purpose_and_null_old_mask_never_adopt_current_binding(self):
        ref = saved_reference("training-run", key="saved/key")
        row = {"id": "I", "storage_ref": ref, "ref_present": True}
        self.assertEqual(
            next(cli.references(family_for("guide_asset_uploads"), row))["unknown"],
            "unknown_saved_purpose",
        )
        record = next(
            cli.references(
                family_for("mask_format_imports"),
                {
                    "id": "I",
                    "staged_object_key": "mask-formats/P/U/I/x.png",
                    "staging_ref_present": False,
                    "staging_ref": None,
                    "bucket": "today",
                },
            )
        )
        self.assertEqual(record["unknown"], "missing_saved_owner")
        self.assertIsNone(record.get("endpoint_url"))
        self.assertNotEqual(record.get("bucket"), "today")

    async def test_old_or_partially_installed_capabilities_fail_closed(self):
        for capability, state in (
            ("migration_inventory_revisions()", "42883"),
            ("inventory_thumbnail_outputs(", "42883"),
            ("inventory_mask_format_uploads(", "42P01"),
            ("inventory_task_originals(", "42703"),
        ):
            with self.subTest(capability=capability, state=state):
                engine = Engine(fault=(capability, state))
                spool = cli.Spool(
                    self.root / f"missing-{capability}", core.Budget(core.Limits())
                )
                try:
                    result = await cli.collect(engine, spool, spool.budget)
                finally:
                    spool.stream.close()
                self.assertFalse(result["enumeration_complete"])
                self.assertEqual(result["issue"], "inventory_capabilities_unavailable")
                self.assertNotIn("families_not_present_in_schema", result)

    async def test_total_snapshot_deadline_cancels_and_closes_connection(self):
        engine = Engine()

        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()

        engine.execute = blocked
        budget = core.Budget(core.Limits(max_seconds=0.02))
        spool = cli.Spool(self.root / "deadline", budget)
        try:
            result = await asyncio.wait_for(cli.collect(engine, spool, budget), 1)
        finally:
            spool.stream.close()
        self.assertFalse(result["enumeration_complete"])
        self.assertEqual(result["issue"], "time_budget")
        self.assertFalse(engine.open)

    async def test_source_join_starts_after_transaction_close_and_spill_is_cleaned(
        self,
    ):
        engine = Engine()
        engine.dispose = AsyncMock()
        arguments = cli.parser().parse_args(
            [
                "--output",
                str(self.root / "output"),
                "--source-inventory",
                str(self.root / "source"),
                "--temp-dir",
                str(self.root),
            ]
        )
        directories = []

        def joining(output, spool, metadata, source, directory, budget):
            self.assertFalse(engine.open)
            self.assertTrue(metadata["enumeration_complete"])
            self.assertEqual(spool.stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(directory).stat().st_mode & 0o777, 0o700)
            directories.append(Path(directory))
            return {"complete": False}

        with (
            patch.dict(
                "sys.modules",
                {
                    "app.config": SimpleNamespace(
                        settings=SimpleNamespace(
                            database_url="postgresql+asyncpg://unused"
                        )
                    )
                },
            ),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
            patch.object(cli, "write_catalog", side_effect=joining),
        ):
            await cli.configured(arguments, core.Budget(core.Limits()))
        self.assertEqual(len(directories), 1)
        self.assertFalse(directories[0].exists())
        engine.dispose.assert_awaited_once()

    async def test_inventory_relative_raw_key_keeps_own_prefix_and_retired_state(self):
        family = cli.FAMILIES[0]
        engine = Engine()
        row = {
            "id": uuid.uuid4(),
            "state": "retired",
            "storage_ref": {"schema": 1, "key_prefix": "saved/", "bucket": "B"},
            "inventory_key": "%2F/../raw",
            "object_sha256": "a" * 64,
            "object_size": 3,
            "object_version": "null",
            "object_state": "present",
        }
        engine.execute = AsyncMock(return_value=Result([row]))
        spool = cli.Spool(self.root / "spool", core.Budget(core.Limits()))
        try:
            await cli.expand_inventory(
                engine,
                family,
                spool,
                spool.budget,
                "yoao_assets_v1.inventory_prediction_input_leases_objects",
                record=False,
            )
        finally:
            spool.stream.close()
        value = json.loads((self.root / "spool").read_bytes())
        self.assertEqual(value["key"], "saved/%2F/../raw")
        self.assertEqual(value["owner_state"], "retired")
        self.assertEqual(value["version_id"], "null")
        sql = str(engine.execute.call_args_list[-1].args[0])
        self.assertIn("inventory_prediction_input_leases_objects(", sql)
        projection = function_source("inventory_prediction_input_leases_objects")
        self.assertIn("'state', r.state", projection)
        self.assertNotIn("'inventory', r.inventory", projection)

    def test_registry_covers_all_existing_classifier_owner_families(self):
        source = ast.parse(
            (ROOT / "apps/api/app/services/storage_usage_categories.py").read_text()
        )
        assignment = next(
            node
            for node in source.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "FAMILIES"
                for target in node.targets
            )
        )
        tables = set()
        for call in assignment.value.elts:
            capability = ast.literal_eval(call.args[0]).split(".")[1]
            tables.add(
                {
                    "export_zip_usage": "export_outputs",
                    "export_media_usage": "export_media_bundles",
                }.get(capability, capability.removeprefix("usage_"))
            )
        self.assertEqual(len(tables), 16)
        self.assertTrue(tables <= {family.table for family in cli.FAMILIES})
        self.assertEqual(len(cli.FAMILIES), 27)

    def test_projection_never_selects_sensitive_or_whole_documents(self):
        for family in cli.FAMILIES:
            if family.table in {"export_outputs", "export_media_bundles"}:
                continue
            sql = function_source("inventory_" + family.table)
            for name in (
                "authority",
                "secret_enc",
                "snapshot",
                "source_ref",
                "evidence",
            ):
                self.assertNotIn(f"'{name}', r.{name}", sql)
            self.assertNotIn("r.*", sql)
            self.assertNotIn("'storage_ref', r.storage_ref", sql)
            self.assertIn("p_limit BETWEEN 1 AND 1001", sql)
        self.assertIn(
            "'snapshot_unexpanded', r.snapshot IS NOT NULL",
            function_source("inventory_training_dataset_versions"),
        )

    def test_saved_version_fact_remains_in_closed_projection(self):
        sql = function_source("inventory_bug_attachment_uploads")
        self.assertIn("'object_version_id', r.object_version_id", sql)

    def test_unknown_schema_and_missing_owner_are_reported_without_fallback(self):
        family = next(f for f in cli.FAMILIES if f.table == "bug_attachment_uploads")
        for row, code in [
            (
                {"id": "id", "ref_present": False, "bucket": "today"},
                "missing_saved_owner",
            ),
            (
                {
                    "id": "id",
                    "ref_present": True,
                    "storage_ref": {
                        "schema": 99,
                        "bucket": "history",
                        "object_key": "raw",
                    },
                },
                "unknown_saved_schema",
            ),
        ]:
            result = list(cli.references(family, row))[0]
            self.assertEqual(result["unknown"], code)
            self.assertNotEqual(result.get("bucket"), "today")

    def test_creation_final_and_staging_keep_saved_history_distinct(self):
        family = next(f for f in cli.FAMILIES if f.form == "creation")
        rows = list(
            cli.references(
                family,
                {
                    "id": "I",
                    "bucket": "final",
                    "object_key": "final-key",
                    "staging_bucket": "import",
                    "staging_key": "staging-key",
                    "saved_endpoint": "http://history:9000",
                    "storage_identity": "historical",
                    "version_id": "v1",
                },
            )
        )
        self.assertEqual(
            [row["role"] for row in rows], ["creation_history", "staging_declaration"]
        )
        self.assertEqual(rows[0]["version_id"], "v1")
        self.assertIsNone(rows[1]["version_id"])
        self.assertEqual(rows[1]["endpoint_url"], "http://history:9000")
        self.assertNotIn("storage_identity", rows[1])

    def test_prefix_digest_is_not_claimed_as_one_object_sha(self):
        family = next(f for f in cli.FAMILIES if f.table == "training_dataset_versions")
        row = {
            "id": "I",
            "ref_present": True,
            "storage_ref": {"schema": 1, "prefix": "p/", "bucket": "B"},
            "sha256": "aggregate",
            "size_bytes": 100,
            "package_key": "p/package.zip",
            "package_sha256": "package",
        }
        prefix, package = list(cli.references(family, row))
        self.assertNotIn("sha256", prefix)
        self.assertEqual(package["sha256"], "package")
        self.assertIsNone(package["prefix"])

    async def test_snapshot_is_readonly_paginated_and_closed_for_observed_revisions(
        self,
    ):
        for version in ("bo0498", "bo0500"):
            path = self.root / f"{version}.jsonl"
            budget = core.Budget(core.Limits(page_size=1))
            rows = [
                {
                    "id": uuid.UUID(int=i),
                    "state": "retired",
                    "ref_present": True,
                    "storage_ref": {
                        "schema": 1,
                        "endpoint_url": "http://history:9000",
                        "bucket": "saved",
                        "object_key": f"key{i}",
                    },
                }
                for i in (1, 2, 3)
            ]
            engine = Engine(version=version, rows=rows)
            spool = cli.Spool(path, budget)
            try:
                metadata = await cli.collect(engine, spool, budget)
            finally:
                spool.stream.close()
            self.assertTrue(metadata["enumeration_complete"], metadata)
            self.assertFalse(engine.open)
            self.assertEqual(metadata["revisions"], [version])
            self.assertEqual(
                metadata["families"], [family.table for family in cli.FAMILIES]
            )
            self.assertNotIn("families_not_present_in_schema", metadata)
            self.assertEqual(
                engine.calls[0],
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
            )
            self.assertTrue(
                any(
                    "inventory_bug_attachment_uploads(CAST(:id AS uuid)" in sql
                    for sql in engine.calls
                )
            )
            self.assertFalse(
                any(
                    "information_schema" in sql or "alembic_version" in sql
                    for sql in engine.calls
                )
            )
            records = list(map(json.loads, path.read_text().splitlines()))
            self.assertEqual(len(records), 3)
            self.assertTrue(all(row["owner_state"] == "retired" for row in records))

    async def test_row_budget_marks_incomplete_and_closes_transaction(self):
        engine = Engine(
            rows=[
                {"id": uuid.UUID(int=i), "state": "pending", "ref_present": False}
                for i in (1, 2)
            ]
        )
        spool = cli.Spool(self.root / "limited", core.Budget(core.Limits(max_rows=1)))
        try:
            metadata = await cli.collect(engine, spool, spool.budget)
        finally:
            spool.stream.close()
        self.assertFalse(metadata["enumeration_complete"])
        self.assertEqual(metadata["issue"], "rows_budget")
        self.assertFalse(engine.open)

    def test_cli_errors_never_print_exception_secrets_or_paths(self):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(
                cli,
                "configured",
                AsyncMock(side_effect=RuntimeError("secret /private/path")),
            ),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            code = cli.main(
                ["--output", str(self.root / "out"), "--source-inventory", "source"]
            )
        self.assertEqual(code, 2)
        self.assertNotIn("secret", errors.getvalue())
        self.assertNotIn("private", errors.getvalue())
        self.assertEqual(output.getvalue(), "")

    def test_verify_incomplete_has_nonzero_exit_and_only_summary(self):
        path = self.root / "out.jsonl"
        writer = core.Writer(path, 65536)
        try:
            writer.emit(
                {
                    "type": "header",
                    "schema": core.SCHEMA,
                    "secret_not_for_terminal": "private",
                }
            )
            writer.finish(
                {
                    "complete": False,
                    "migration_ready": False,
                    "counts": {"references": 1},
                }
            )
        finally:
            writer.stream.close()
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["--verify", str(path)])
        self.assertEqual(code, 1)
        self.assertNotIn("private", output.getvalue())

    def test_source_required_and_invalid_budget_return_safe_error(self):
        for args in (
            ["--output", "out"],
            ["--output", "out", "--source-inventory", "source", "--max-rows", "0"],
        ):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(args), 2)


if __name__ == "__main__":
    unittest.main()
