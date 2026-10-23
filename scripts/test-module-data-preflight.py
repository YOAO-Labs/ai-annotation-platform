"""Check missing peer prerequisites fail before provisioning test services."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import verify_module_data_boundary as boundary


class NotificationPeerPreflightTests(unittest.TestCase):
    def test_missing_peer_never_provisions_services(self):
        for node, result, expected in (
            (None, None, "unavailable"),
            (
                "/test/node",
                subprocess.CompletedProcess(
                    [],
                    1,
                    "",
                    "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'pg' from private path",
                ),
                "self_check_failed",
            ),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / "result.json"
                with (
                    patch.object(boundary.shutil, "which", return_value=node),
                    patch.object(
                        boundary.subprocess, "run", return_value=result
                    ) as run,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    status = boundary.main(
                        [
                            "--output",
                            str(output),
                            "--test-file",
                            "tests/test_notification_preference_commands.py",
                        ]
                    )
                evidence = json.loads(output.read_text())
                self.assertEqual(status, 1)
                expected_phase = (
                    "preflight"
                    if node is None
                    else "preflight-node-notification-preferences"
                )
                self.assertEqual(evidence["failure"]["phase"], expected_phase)
                self.assertEqual(
                    evidence["notification_preferences"]["node_prerequisites"], expected
                )
                self.assertIsNone(evidence.get("postgres_container"))
                self.assertIsNone(evidence["database"])
                self.assertEqual(evidence["cleanup"]["errors"], [])
                self.assertIsNone(evidence["cleanup"]["resource_root_retained"])
                if node is None:
                    run.assert_not_called()
                else:
                    run.assert_called_once()
                    self.assertEqual(run.call_args.args[0][-1], "--self-check")
                    self.assertEqual(
                        evidence["failure"]["node_diagnostic"],
                        {
                            "stage": expected_phase,
                            "returncode": 1,
                            "category": "missing_node_module",
                            "module": "pg",
                        },
                    )


if __name__ == "__main__":
    unittest.main()
