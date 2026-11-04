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
    def test_missing_workspace_driver_never_provisions_services(self):
        for node, result, phase in (
            (None, None, "preflight"),
            (
                "/test/node",
                subprocess.CompletedProcess([], 1, "", ""),
                "preflight-node-workspace-pg",
            ),
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / "result.json"
                with (
                    patch.object(boundary.shutil, "which", return_value=node),
                    patch.object(boundary.subprocess, "run", return_value=result),
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
                self.assertEqual(evidence["failure"]["phase"], phase)
                self.assertEqual(
                    evidence["workspace_node"]["node_prerequisites"], "unavailable"
                )
                self.assertIsNone(evidence.get("postgres_container"))
                self.assertIsNone(evidence["database"])
                self.assertEqual(evidence["cleanup"]["errors"], [])
                self.assertIsNone(evidence["cleanup"]["resource_root_retained"])

    def test_missing_notification_peer_never_provisions_services(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "result.json"
            results = [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess(
                    [],
                    1,
                    "",
                    "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'pg' from private path",
                ),
            ]
            with (
                patch.object(boundary.shutil, "which", return_value="/test/node"),
                patch.object(boundary.subprocess, "run", side_effect=results),
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
            self.assertEqual(
                evidence["failure"]["phase"], "preflight-node-notification-preferences"
            )
            self.assertEqual(
                evidence["notification_preferences"]["node_prerequisites"],
                "self_check_failed",
            )
            self.assertIsNone(evidence.get("postgres_container"))
            self.assertIsNone(evidence["database"])
            self.assertEqual(evidence["cleanup"]["errors"], [])
            self.assertIsNone(evidence["cleanup"]["resource_root_retained"])
            self.assertEqual(
                evidence["failure"]["node_diagnostic"],
                {
                    "stage": "preflight-node-notification-preferences",
                    "returncode": 1,
                    "category": "missing_node_module",
                    "module": "pg",
                },
            )


if __name__ == "__main__":
    unittest.main()
