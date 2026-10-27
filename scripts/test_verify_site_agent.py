"""Ownership and isolation guards for the live Agent fixture; no Docker needed."""

import json
from email.message import Message
from io import BytesIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from site_agent_replicas import ui_handoff
from verify_site_agent import HTTP, Owner, private_json, replaced_host, verify


class FixtureGuards(unittest.TestCase):
    def test_empty_delete_response_does_not_require_json(self):
        class Response(BytesIO):
            status = 204
            headers = Message()

        http = HTTP.__new__(HTTP)
        http.origin, http.token = "https://center.invalid", None
        http.opener = Mock()
        http.opener.open.return_value = Response()
        self.assertIsNone(http.call("DELETE", "/owned", expected=204))

    def test_failure_status_set_never_accepts_partial_image_success(self):
        class Response(BytesIO):
            status = 206
            headers = Message()

        http = HTTP.__new__(HTTP)
        http.origin, http.token = "https://center.invalid", None
        http.opener = Mock()
        http.opener.open.return_value = Response(b"partial image")
        with self.assertRaisesRegex(ValueError, "unexpected_http_status:206"):
            http.call("GET", "/owned", binary=True, expected={409, 503})

    def test_ui_hold_is_bounded_before_writing_handoff(self):
        for seconds in (-1, 301, True, 0.5):
            with self.assertRaisesRegex(ValueError, "ui_hold_out_of_bounds"):
                ui_handoff(None, {}, seconds)

    def test_ui_done_requires_the_exact_owner_nonce(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(Path(directory) / "state")
            private_json(owner.state / "ui-done.json", {"owner_nonce": "wrong"})
            with self.assertRaisesRegex(ValueError, "ui_owner_changed"):
                ui_handoff(owner, {}, 10)

    def test_ui_handoff_uses_private_owner_bound_files(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(Path(directory) / "state")
            private_json(
                owner.state / "ui-done.json", {"owner_nonce": owner.value["nonce"]}
            )
            self.assertEqual(
                ui_handoff(owner, {"api_origin": "https://127.0.0.1:1"}, 10),
                "completed",
            )
            ready = owner.state / "ui-ready.json"
            self.assertFalse(ready.stat().st_mode & 0o077)
            self.assertEqual(
                json.loads(ready.read_bytes())["owner_nonce"], owner.value["nonce"]
            )

    def test_ui_hold_timeout_is_finite(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(Path(directory) / "state")
            now = [0.0]

            def sleep(seconds):
                now[0] += seconds

            self.assertEqual(
                ui_handoff(owner, {}, 1, clock=lambda: now[0], sleep=sleep), "timeout"
            )
            self.assertEqual(now[0], 1)

    def test_ui_done_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(Path(directory) / "state")
            target = owner.state / "other.json"
            private_json(target, {"owner_nonce": owner.value["nonce"]})
            (owner.state / "ui-done.json").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "ui_done_not_private"):
                ui_handoff(owner, {}, 10)

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
