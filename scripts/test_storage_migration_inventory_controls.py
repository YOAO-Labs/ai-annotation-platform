"""The optional app controls preserve CLI framing and stop native reads promptly."""

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import inventory_storage_migration as cli
import storage_migration_inventory as core
from test_storage_migration_inventory import S3


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.source = S3()
        self.purposes = {purpose: "fixture" for purpose in core.PURPOSES}

    def test_compatibility_module_is_the_real_core_including_private_patch_seams(self):
        from app.services import storage_migration_inventory

        self.assertIs(core, storage_migration_inventory)
        self.assertIs(core._Writer, storage_migration_inventory._Writer)

    def test_output_byte_budget_is_enforced_while_writing_and_sink_closes(self):
        sink = io.BytesIO()
        with self.assertRaises(core.InventoryStopped) as caught:
            core.write_inventory(
                None, self.source, self.purposes, sink=sink, max_output_bytes=30
            )
        self.assertEqual(caught.exception.code, "inventory_report_byte_budget")
        self.assertTrue(sink.closed)
        self.assertFalse(self.source.calls)

    def test_stop_after_sdk_return_closes_new_body_and_does_not_read(self):
        self.source.add("key", b"body")
        stopped = False
        original = self.source.get_object

        def get(**kwargs):
            nonlocal stopped
            response = original(**kwargs)
            stopped = True
            return response

        def checkpoint():
            if stopped:
                raise core.InventoryStopped("inventory_cancelled")

        self.source.get_object = get
        with self.assertRaises(core.InventoryStopped):
            core.write_inventory(
                None,
                self.source,
                self.purposes,
                sink=io.BytesIO(),
                checkpoint=checkpoint,
                strict_closure=True,
            )
        self.assertEqual(self.source.bodies[0].close_calls, 1)
        self.assertTrue(self.source.bodies[0].closed)

    def test_stop_inside_stream_cannot_become_issue_or_continue_next_object(self):
        self.source.add("a", b"body")
        self.source.add("b", b"next")
        stopped = False

        def callback():
            nonlocal stopped
            stopped = True

        self.source.body_hook = callback

        def checkpoint():
            if stopped:
                raise core.InventoryStopped("inventory_cancelled")

        with self.assertRaises(core.InventoryStopped):
            core.write_inventory(
                None,
                self.source,
                self.purposes,
                sink=io.BytesIO(),
                checkpoint=checkpoint,
            )
        self.assertEqual(len(self.source.bodies), 1)
        self.assertTrue(self.source.bodies[0].closed)

    def test_strict_body_close_uncertainty_is_fatal_and_keeps_permit_marker(self):
        self.source.add("a", b"body")
        self.source.close_error = True
        with self.assertRaises(core.InventoryStopped) as caught:
            core.write_inventory(
                None, self.source, self.purposes, sink=io.BytesIO(), strict_closure=True
            )
        self.assertTrue(caught.exception.storage_operation_unsettled)
        self.assertEqual(caught.exception.code, "inventory_body_close_failed")

    def test_cli_sanitizes_base_exception_stop_code(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    cli,
                    "verify_inventory",
                    side_effect=core.InventoryStopped("inventory_report_write_failed"),
                ),
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                self.assertEqual(
                    cli.main(["--verify", str(Path(directory) / "report")]), 2
                )
            self.assertIn("inventory_report_write_failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
