"""CLI policy, saved binding coverage and closed read-only DB observations."""

from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import inventory_storage_migration as cli
from storage_migration_inventory import InventoryError, PURPOSES
from test_storage_migration_inventory import S3


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aap-e07-cli-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "handling.json"
        self.purposes = dict.fromkeys(PURPOSES, "shared")

    def test_handling_is_explicit_and_rejects_duplicates_or_unknown_values(self):
        self.assertEqual(cli.handling_file(None), {})
        self.path.write_text('{"datasets":"migrate","media-cache":"retain"}')
        self.assertEqual(cli.handling_file(self.path)["media-cache"], "retain")
        for raw in (
            '{"datasets":"discard"}',
            '{"new":"migrate"}',
            '{"datasets":"migrate","datasets":"rebuild"}',
            "[]",
            "{",
        ):
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                with self.assertRaises(InventoryError):
                    cli.handling_file(self.path)

    def test_saved_binding_coverage_never_assumes_other_endpoint_or_bucket(self):
        base = dict(
            purpose="avatars",
            location_id="legacy",
            bucket="shared",
            generation=1,
            location_kind="s3",
            config_revision=1,
            location_config={"endpoint": "localhost:9000", "use_ssl": False},
        )
        observed = cli.binding_observations(
            [base], self.purposes, "http://localhost:9000", "legacy"
        )
        self.assertNotIn("avatars", observed["uncovered_binding_purposes"])
        other = {
            **base,
            "location_id": "other",
            "connection_id": "connector",
            "connection_kind": "s3",
            "connection_scope": "global",
            "connection_config": {"endpoint": "http://remote:9000", "bucket": "shared"},
        }
        observed = cli.binding_observations(
            [other], self.purposes, "http://localhost:9000", "legacy"
        )
        self.assertIn("avatars", observed["uncovered_binding_purposes"])
        other["connection_config"]["endpoint"] = "http://localhost:9000"
        other["bucket"] = "other-bucket"
        self.assertIn(
            "avatars",
            cli.binding_observations(
                [other], self.purposes, "http://localhost:9000", "legacy"
            )["uncovered_binding_purposes"],
        )

    def test_secret_endpoint_is_not_coverage(self):
        for endpoint in (
            "http://user:secret@host",
            "http://host?token=secret",
            "file:///etc",
            None,
        ):
            with self.subTest(endpoint=endpoint):
                self.assertIsNone(cli.endpoint_identity(endpoint))

    def test_live_comparison_ignores_only_observation_time_not_source_digest(self):
        from verify_storage_migration_inventory import source_facts

        one = Path(self.directory.name) / "one.jsonl"
        two = Path(self.directory.name) / "two.jsonl"
        record = {
            "type": "object",
            "key": "key",
            "verification": {"verified_at": "first", "sha256": "a"},
        }
        one.write_text(json.dumps(record) + "\n")
        record["verification"]["verified_at"] = "second"
        two.write_text(json.dumps(record) + "\n")
        self.assertEqual(source_facts(one), source_facts(two))
        record["verification"]["sha256"] = "b"
        two.write_text(json.dumps(record) + "\n")
        self.assertNotEqual(source_facts(one), source_facts(two))

    def test_qualification_refuses_existing_business_rows_or_bucket_content(self):
        from verify_storage_migration_inventory import require_empty_fixture

        source = S3()
        facts = {"alembic_version": {"count": 1}, "users": {"count": 0}}
        require_empty_fixture(facts, source, ["shared"])
        with self.assertRaisesRegex(ValueError, "empty disposable test database"):
            require_empty_fixture({**facts, "users": {"count": 1}}, source, ["shared"])
        source.add("existing")
        with self.assertRaisesRegex(ValueError, "empty disposable test buckets"):
            require_empty_fixture(facts, source, ["shared"])

    def test_cli_verify_preserves_incomplete_exit_and_exclusive_creation(self):
        source = S3()
        source.lifecycle_error = "AccessDenied"
        output = Path(self.directory.name) / "inventory.jsonl"
        cli.write_inventory(output, source, self.purposes)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(cli.main(["--verify", str(output)]), 1)
        self.assertFalse(json.loads(stdout.getvalue())["complete"])
        original = output.read_bytes()
        with (
            patch.object(
                cli, "configured_inventory", side_effect=FileExistsError("secret")
            ),
            redirect_stderr(io.StringIO()) as error,
        ):
            self.assertEqual(cli.main(["--output", str(output)]), 2)
        self.assertNotIn("secret", error.getvalue())
        self.assertEqual(output.read_bytes(), original)


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_is_readonly_bounded_and_closed(self):
        state = {"open": False, "transaction": False, "queries": []}

        class Result:
            def __init__(self, rows):
                self.rows = rows

            def mappings(self):
                return self

            def all(self):
                return self.rows

        class Connection:
            @asynccontextmanager
            async def begin(self):
                state["transaction"] = True
                try:
                    yield
                finally:
                    state["transaction"] = False

            async def execute(self, statement):
                self.assert_open()
                query = str(statement)
                state["queries"].append(query)
                if "FROM async_jobs" in query:
                    return Result([{"kind": "export", "status": "running", "count": 2}])
                if "GROUP BY kind ORDER" in query:
                    return Result([{"kind": "s3", "count": i} for i in range(101)])
                return Result([])

            def assert_open(self):
                if not state["open"] or not state["transaction"]:
                    raise AssertionError("closed")

        class Engine:
            @asynccontextmanager
            async def connect(self):
                state["open"] = True
                try:
                    yield Connection()
                finally:
                    state["open"] = False

        observed = await cli.collect_observations(
            Engine(), dict.fromkeys(PURPOSES, "shared"), "http://localhost", "legacy"
        )
        self.assertFalse(state["open"] or state["transaction"])
        self.assertIn("REPEATABLE READ READ ONLY", state["queries"][0])
        self.assertTrue(all("LIMIT 101" in query for query in state["queries"][2:]))
        self.assertFalse(observed["complete"])
        self.assertEqual(len(observed["saved_locations_by_kind"]), 100)
        self.assertEqual(observed["potential_writers"][0]["count"], 2)
        self.assertEqual(observed["external_writers_and_presigned_uploads"], "unknown")


if __name__ == "__main__":
    unittest.main()
