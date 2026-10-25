"""Closed SQL registry, optional columns, readonly snapshots and safe CLI results."""

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

import inventory_storage_references as cli
import storage_migration_references as core

ROOT = Path(__file__).resolve().parents[1]


def schema_columns(version="0245"):
    columns = {
        family.table: set(family.keys)
        | {"bucket", "storage_ref", "configuration", "inventory"}
        for family in cli.FAMILIES
    }
    columns["bug_attachment_uploads"].update({"state", "sha256", "size_bytes"})
    if version == "0245":
        columns["bug_attachment_uploads"].add("object_version_id")
    return columns


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def __iter__(self):
        return iter(self.rows)

    def mappings(self):
        return self

    def all(self):
        return self.rows


class Engine:
    def __init__(self, *, version="0245", rows=()):
        self.version, self.rows = version, rows
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

    async def scalars(self, statement):
        self.calls.append(str(statement))
        return [self.version]

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append(sql)
        if "information_schema.columns" in sql:
            return Result(
                [
                    (table, column)
                    for table, names in schema_columns(self.version).items()
                    for column in names
                ]
            )
        if 'FROM "bug_attachment_uploads"' in sql and "jsonb_each" not in sql:
            rows = [
                row
                for row in self.rows
                if not params.get("last0") or row["id"] > params["last0"]
            ]
            return Result(rows[: params["limit"]])
        return Result()


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aap-ref-cli-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

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
            await cli.expand_inventory(engine, family, spool, spool.budget, {"state"})
        finally:
            spool.stream.close()
        value = json.loads((self.root / "spool").read_bytes())
        self.assertEqual(value["key"], "saved/%2F/../raw")
        self.assertEqual(value["owner_state"], "retired")
        self.assertEqual(value["version_id"], "null")
        sql = str(engine.execute.call_args_list[-1].args[0])
        self.assertIn('r."state"', sql)
        self.assertNotIn("o.value AS", sql)

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
            module, name = ast.literal_eval(call.args[0]).split(".")
            model = ast.parse(
                (ROOT / f"apps/api/app/db/models/{module}.py").read_text()
            )
            cls = next(
                node
                for node in model.body
                if isinstance(node, ast.ClassDef) and node.name == name
            )
            value = next(
                node.value
                for node in cls.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "__tablename__"
                    for target in node.targets
                )
            )
            tables.add(ast.literal_eval(value))
        self.assertEqual(len(tables), 16)
        self.assertTrue(tables <= {family.table for family in cli.FAMILIES})
        self.assertEqual(len(cli.FAMILIES), 24)

    def test_projection_never_selects_sensitive_or_whole_documents(self):
        for family in cli.FAMILIES:
            columns = schema_columns()[family.table] | {
                "authority",
                "secret_enc",
                "payload",
                "snapshot",
                "source_ref",
            }
            sql = cli.projection(family, columns)
            self.assertNotIn("authority", sql)
            self.assertNotIn("secret_enc", sql)
            self.assertNotIn("payload", sql)
            self.assertIn('r."snapshot" IS NOT NULL', sql)
            self.assertNotIn("r.*", sql)
            self.assertNotIn("r.storage_ref AS", sql)
            self.assertNotIn("evidence", sql)

    def test_0244_optional_version_is_not_named_as_a_missing_column(self):
        family = next(f for f in cli.FAMILIES if f.table == "bug_attachment_uploads")
        self.assertNotIn(
            'r."object_version_id"',
            cli.projection(family, schema_columns("0244")[family.table]),
        )
        self.assertIn(
            'r."object_version_id"',
            cli.projection(family, schema_columns()[family.table]),
        )

    def test_missing_required_table_shape_is_explicit(self):
        with self.assertRaisesRegex(core.ReferenceError, "family_columns_missing"):
            cli.projection(cli.FAMILIES[0], set())

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

    async def test_snapshot_is_readonly_paginated_and_closed_for_both_revisions(self):
        for version in ("0244", "0245"):
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
            self.assertEqual(len(metadata["families"]), 24)
            self.assertEqual(
                engine.calls[0],
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
            )
            self.assertTrue(any(":last0" in sql for sql in engine.calls))
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
