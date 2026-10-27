"""Operator CLI argument boundaries, redaction and offline checkpoint status."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import copy_storage_migration as cli
from storage_migration_copy import Checkpoint, Limits
from test_storage_migration_copy import TARGET, inputs


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aap-copy-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "target.json"
        self.target.write_text(json.dumps(TARGET))
        self.target.chmod(0o600)
        self.checkpoint = self.root / "checkpoint.sqlite"

    def invoke(self, arguments):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(arguments)
        value = out.getvalue() + err.getvalue()
        self.assertNotIn("private-secret", value)
        self.assertNotIn("图", value)
        return code, json.loads(value)

    def plan(self):
        source, catalog = inputs(self.root)
        return self.invoke(
            [
                "plan",
                "--checkpoint",
                str(self.checkpoint),
                "--source-inventory",
                str(source),
                "--references",
                str(catalog),
                "--target-config",
                str(self.target),
            ]
        )

    def test_offline_plan_and_status_return_pending_subset_and_no_paths(self):
        code, planned = self.plan()
        self.assertEqual(code, 1)
        self.assertEqual(planned["counts"], {"pending": 1})
        with patch.object(
            cli, "client", side_effect=AssertionError("unexpected network")
        ):
            code, result = self.invoke(["status", "--checkpoint", str(self.checkpoint)])
        self.assertEqual(code, 1)
        self.assertEqual(result, planned)
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_target_secret_mode_is_enforced(self):
        self.target.chmod(0o644)
        code, result = self.invoke(
            [
                "run",
                "--checkpoint",
                str(self.checkpoint),
                "--target-config",
                str(self.target),
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "private_json_required")

    def test_target_identity_cannot_change_during_resume(self):
        self.plan()
        self.target.write_text(json.dumps({**TARGET, "bucket": "another"}))
        code, result = self.invoke(
            [
                "run",
                "--checkpoint",
                str(self.checkpoint),
                "--target-config",
                str(self.target),
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "target_identity_mismatch")

    def test_saved_limits_are_finite_and_explicit_override_is_per_run(self):
        self.plan()
        args = cli.parser().parse_args(
            [
                "run",
                "--checkpoint",
                str(self.checkpoint),
                "--target-config",
                str(self.target),
                "--max-objects",
                "2",
            ]
        )
        with Checkpoint(self.checkpoint) as state:
            limits = cli.limits_from(args, state.meta["limits"])
            self.assertEqual(limits.max_objects, 2)
            self.assertEqual(state.meta["limits"]["max_objects"], 10000)
            self.assertEqual(limits.max_object_bytes, Limits().max_object_bytes)

    def test_provider_error_is_safe_constant(self):
        self.plan()
        with patch.object(
            cli,
            "configured_source",
            side_effect=RuntimeError("private-secret http://u:pw@host/key"),
        ):
            code, result = self.invoke(
                [
                    "run",
                    "--checkpoint",
                    str(self.checkpoint),
                    "--target-config",
                    str(self.target),
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "io_failure")

    def test_invalid_arguments_do_not_echo_private_values(self):
        code, result = self.invoke(["private-secret"])
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "invalid_arguments")

    def test_close_failure_is_redacted_and_all_clients_are_closed(self):
        self.plan()
        fake = unittest.mock.Mock()
        fake.close.side_effect = RuntimeError("private-secret")
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(cli, "client", return_value=fake),
            patch.object(
                cli,
                "execute",
                return_value={"copy_subset_complete": True, "migration_ready": False},
            ),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = cli.main(
                [
                    "verify",
                    "--checkpoint",
                    str(self.checkpoint),
                    "--target-config",
                    str(self.target),
                ]
            )
        self.assertEqual(code, 2)
        self.assertNotIn("private-secret", out.getvalue() + err.getvalue())
        self.assertEqual(json.loads(err.getvalue())["error"], "client_close_failed")
        fake.close.assert_called_once()

    def test_verify_never_loads_source_credentials(self):
        self.plan()
        fake = unittest.mock.Mock()
        with (
            patch.object(
                cli, "configured_source", side_effect=AssertionError("source used")
            ),
            patch.object(cli, "client", return_value=fake),
            patch.object(
                cli,
                "execute",
                return_value={"copy_subset_complete": True, "migration_ready": False},
            ) as execute,
        ):
            code, result = self.invoke(
                [
                    "verify",
                    "--checkpoint",
                    str(self.checkpoint),
                    "--target-config",
                    str(self.target),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIsNone(execute.call_args.args[1])
        self.assertTrue(execute.call_args.kwargs["verify_only"])
        fake.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
