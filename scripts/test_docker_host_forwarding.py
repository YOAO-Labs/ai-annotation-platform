from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.verify_docker_host_forwarding import (
    ID_LABEL,
    LABEL,
    LABEL_VALUE,
    VerificationError,
    labels_belong_to_task,
    resolve_output_path,
    tcpdump_argv_matches,
    validate_image_id,
)


class DockerHostForwardingInputTests(unittest.TestCase):
    def test_image_requires_sha256_id(self) -> None:
        image = "sha256:" + "a" * 64
        self.assertEqual(validate_image_id(image), image)
        with self.assertRaises(VerificationError):
            validate_image_id("python:3.12")

    def test_output_must_be_new_and_inside_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / ".worktree"
            worktree.mkdir()
            result = resolve_output_path(".worktree/probe.json", root)
            self.assertEqual(result.parent, worktree)
            with self.assertRaises(VerificationError):
                resolve_output_path("outside.json", root)
            result.touch()
            with self.assertRaises(VerificationError):
                resolve_output_path(".worktree/probe.json", root)

    def test_output_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / ".worktree"
            worktree.mkdir()
            target = worktree / "target.json"
            target.touch()
            (worktree / "receipt.json").symlink_to(target)
            with self.assertRaises(VerificationError):
                resolve_output_path(".worktree/receipt.json", root)

    def test_cleanup_requires_both_owned_labels(self) -> None:
        labels = {LABEL: LABEL_VALUE, ID_LABEL: "owned-id"}
        self.assertTrue(labels_belong_to_task(labels, "owned-id"))
        self.assertFalse(labels_belong_to_task(labels, "other-id"))

    def test_trace_command_is_port_scoped_and_has_no_payload_flags(self) -> None:
        argv = [
            "/usr/sbin/tcpdump",
            "-i",
            "lo0",
            "-nn",
            "-tt",
            "-l",
            "-s",
            "128",
            "-Z",
            "tester",
            "tcp",
            "and",
            "port",
            "43210",
        ]
        self.assertTrue(tcpdump_argv_matches(argv, 43210, "tester"))
        self.assertFalse(
            tcpdump_argv_matches([*argv, "-w", "capture.pcap"], 43210, "tester")
        )


if __name__ == "__main__":
    unittest.main()
