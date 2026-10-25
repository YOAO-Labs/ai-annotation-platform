"""Selected acceptance targets must remain inside the API test tree."""

from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest

from scripts.verify_module_data_boundary import _arguments, _passed_test_count


class ModuleDataSelectionTests(unittest.TestCase):
    def test_skipped_failed_and_empty_suites_do_not_supply_a_passing_case(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "junit.xml"
            for body, expected in (
                ("", 0),
                ("<testcase><skipped/></testcase>", 0),
                ("<testcase><failure/></testcase><testcase><error/></testcase>", 0),
                ("<testcase/><testcase><skipped/></testcase>", 1),
            ):
                path.write_text(
                    f"<testsuites><testsuite>{body}</testsuite></testsuites>"
                )
                self.assertEqual(_passed_test_count(path), expected)

    def test_target_selection_keeps_node_ids_and_rejects_external_paths(self):
        node = "tests/test_identity_gateway.py::test_profile_role_changed_after_admission_is_not_restored"
        parsed = _arguments(
            [
                "--output",
                "/tmp/unused.json",
                "--test-file",
                node,
                "--test-file",
                "tests/test_identity_recovery.py",
            ]
        )
        self.assertEqual(parsed.test_files, [node, "tests/test_identity_recovery.py"])
        for target in ("../identity/tests/test_contract.py", "app/main.py", __file__):
            with self.subTest(target=target), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    _arguments(["--output", "/tmp/unused.json", "--test-file", target])

    def test_default_workspace_profile_rejects_unrelated_or_combined_targets(self):
        for targets in (
            [],
            ["tests/test_identity_recovery.py"],
            [
                "tests/test_workspace_module_assembly_live.py",
                "tests/test_workspace_worker_assembly_live.py",
            ],
        ):
            args = [
                "--output",
                "/tmp/unused.json",
                "--workspace-consumer-profile",
                "default",
            ]
            for target in targets:
                args.extend(("--test-file", target))
            with self.subTest(targets=targets), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as rejected:
                    _arguments(args)
                self.assertEqual(rejected.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
