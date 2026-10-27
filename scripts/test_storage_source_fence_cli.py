"""Descriptor ownership and CLI error boundaries without a running provider."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import storage_source_fence as cli
from storage_source_fence_state import FenceError


class DescriptorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "descriptor.json"
        self.path.write_bytes(b'{"schema":"example"}')
        self.path.chmod(0o600)

    def test_same_open_file_supplies_value_and_digest(self):
        import hashlib

        value, digest = cli.descriptor(self.path)
        self.assertEqual(value, {"schema": "example"})
        self.assertEqual(digest, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_duplicate_keys_are_rejected(self):
        self.path.write_bytes(b'{"owner":"first","owner":"second"}')
        with self.assertRaises(FenceError):
            cli.descriptor(self.path)

    def test_shared_file_permissions_are_rejected(self):
        self.path.chmod(0o640)
        with self.assertRaises(FenceError):
            cli.descriptor(self.path)

    def test_shared_parent_permissions_are_rejected(self):
        self.path.parent.chmod(0o750)
        try:
            with self.assertRaises(FenceError):
                cli.descriptor(self.path)
        finally:
            self.path.parent.chmod(0o700)

    def test_symlink_is_rejected(self):
        link = self.path.parent / "link"
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            cli.descriptor(link)

    def test_hardlink_is_rejected(self):
        link = self.path.parent / "link"
        os.link(self.path, link)
        with self.assertRaises(FenceError):
            cli.descriptor(self.path)

    def test_budget_and_non_object_json_are_rejected(self):
        for payload in (b"x" * 65537, b"[]", b"null", b"\xff"):
            with self.subTest(size=len(payload)):
                self.path.write_bytes(payload)
                with self.assertRaises(FenceError):
                    cli.descriptor(self.path)

    def test_fifo_is_not_read_or_blocked(self):
        self.path.unlink()
        os.mkfifo(self.path, mode=0o600)
        with self.assertRaises(FenceError):
            cli.descriptor(self.path)


class CommandTests(unittest.TestCase):
    def call(self, args):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = cli.main(args)
        return code, output.getvalue(), error.getvalue()

    def test_invalid_arguments_do_not_echo_paths_or_credentials(self):
        secret = "secret-in-an-unsupported-argument"
        code, output, error = self.call([secret])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertNotIn(secret, error)
        self.assertEqual(json.loads(error)["code"], "invalid_arguments")

    def test_native_exception_details_do_not_reach_output(self):
        secret = "sensitive-object-key-or-token"
        with (
            patch.object(cli, "descriptor", return_value=({}, "0" * 64)),
            patch.object(cli, "DockerAdapter", side_effect=OSError(secret)),
        ):
            code, output, error = self.call(
                ["status", "--config", "private", "--state", "private-state"]
            )
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertNotIn(secret, error)
        self.assertEqual(json.loads(error)["code"], "source_fence_failed")

    def test_init_qualifies_native_scope_before_creating_journal(self):
        adapter = Mock()
        adapter.directory = Path("/private/gateway")
        adapter.preview.side_effect = FenceError("source_ineligible")
        with (
            patch.object(cli, "descriptor", return_value=({}, "0" * 64)),
            patch.object(cli, "DockerAdapter", return_value=adapter),
            patch.object(cli, "Journal") as journal,
        ):
            code, _, error = self.call(
                ["init", "--config", "private", "--state", "/private/fence-state.json"]
            )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(error)["code"], "source_ineligible")
        journal.assert_not_called()

    def test_one_ingress_cannot_use_two_journal_locks(self):
        cli.bound_state(Path("/private/gateway"), Path("/private/fence-state.json"))
        with self.assertRaises(FenceError):
            cli.bound_state(
                Path("/private/gateway"), Path("/private/another-state.json")
            )


if __name__ == "__main__":
    unittest.main()
