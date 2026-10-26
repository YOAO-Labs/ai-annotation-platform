"""Ownership and isolation guards for the live Agent fixture; no Docker needed."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from verify_site_agent import Owner, replaced_host, verify


class FixtureGuards(unittest.TestCase):
    def test_development_mode_is_rejected_before_provisioning(self):
        with patch.dict("os.environ", {"AAP_WORKTREE_MODE": "dev"}, clear=True):
            with self.assertRaisesRegex(ValueError, "launcher_test_mode_required"):
                verify(None, "unused")

    def test_cleanup_refuses_a_resource_with_changed_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(Path(directory) / "state")
            owner.value["resources"] = [
                {"kind": "network", "id": "owned-id", "name": "fixture", "image": None}
            ]
            observed = [
                {
                    "Id": "owned-id",
                    "Labels": {
                        "aap.e06.owner": "someone-else",
                        "aap.e06.checkout": owner.value["root"],
                    },
                }
            ]
            result = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(observed).encode(), stderr=b""
            )
            with patch("verify_site_agent.run", return_value=result) as invocation:
                with self.assertRaisesRegex(ValueError, "resource_owner_changed"):
                    owner.cleanup()
            self.assertEqual(invocation.call_count, 1)
            self.assertEqual(
                invocation.call_args.args[0][:3], ["docker", "network", "inspect"]
            )

    def test_intent_is_durable_before_resource_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(Path(directory) / "state")

            def stopped(arguments):
                value = json.loads((owner.state / "owner.json").read_bytes())
                self.assertEqual(len(value["resources"]), 1)
                self.assertIsNone(value["resources"][0]["id"])
                self.assertEqual(arguments[-1], value["resources"][0]["name"])
                raise RuntimeError("interrupted")

            with patch("verify_site_agent.run", side_effect=stopped):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    owner.create("network", "test-net")

    def test_network_translation_preserves_test_identity_and_credentials(self):
        original = "postgresql+asyncpg://user:p%40ss@127.0.0.1:5432/aap_wt_example_test?ssl=false"
        self.assertEqual(
            replaced_host(original, "172.18.0.1"),
            "postgresql+asyncpg://user:p%40ss@172.18.0.1:5432/aap_wt_example_test?ssl=false",
        )
        self.assertEqual(
            replaced_host("redis://127.0.0.1:33000/0", "172.18.0.1", 41000),
            "redis://172.18.0.1:41000/0",
        )


if __name__ == "__main__":
    unittest.main()
