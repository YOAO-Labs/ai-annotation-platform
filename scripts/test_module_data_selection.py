"""Selected acceptance targets must remain inside the API test tree."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from scripts.verify_module_data_boundary import (
    _append_profile_junit,
    _arguments,
    _passed_test_count,
    _run_strict_default,
    main,
)


class ModuleDataSelectionTests(unittest.TestCase):
    def test_all_selected_targets_must_have_passing_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "junit.xml"
            path.write_text(
                '<testsuites><testsuite tests="3">'
                '<testcase classname="tests.test_first" name="test_one"/>'
                '<testcase classname="tests.test_second.TestGroup" name="test_two[a]"/>'
                '<testcase classname="tests.test_second.TestGroup" name="test_two[b::c]"/>'
                "</testsuite></testsuites>"
            )
            collected = [
                "tests/test_first.py::test_one",
                "tests/test_second.py::TestGroup::test_two[a]",
                "tests/test_second.py::TestGroup::test_two[b::c]",
            ]
            for targets in (
                ("tests/test_first.py", "tests/test_second.py"),
                ("tests/test_second.py::TestGroup",),
                ("tests/test_second.py::TestGroup::test_two",),
                ("tests/test_second.py::TestGroup::test_two[b::c]",),
            ):
                with self.subTest(targets=targets):
                    self.assertEqual(_passed_test_count(path, targets, collected), 3)
            for target in (
                "tests/test_missing.py",
                "tests/test_first.py::test_missing",
                "tests/test_second.py::TestGroup::test_two[c]",
                "tests/test_second.py::Test",
            ):
                with self.subTest(target=target), self.assertRaises(RuntimeError):
                    _passed_test_count(path, ("tests/test_first.py", target), collected)
            with self.assertRaisesRegex(RuntimeError, "differs from collection"):
                _passed_test_count(
                    path,
                    ("tests/test_first.py",),
                    collected + ["tests/test_second.py::TestGroup::test_two[missing]"],
                )
            with self.assertRaisesRegex(RuntimeError, "differs from collection"):
                _passed_test_count(path, ("tests/test_first.py",), collected[:-1])
            with self.assertRaisesRegex(RuntimeError, "collection manifest"):
                _passed_test_count(path, ("tests/test_first.py",), [])

    def test_profile_report_reconciliation_rejects_missing_duplicate_and_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collection = root / "profile.collection.json"
            junit = root / "profile.junit.xml"
            expected = [
                "tests/test_first.py::test_one",
                "tests/test_first.py::test_two",
            ]
            collection.write_text(json.dumps(expected))
            junit.write_text(
                '<testsuites><testsuite tests="2">'
                '<testcase classname="tests.test_first" name="test_one"/>'
                '<testcase classname="tests.test_first" name="test_two"/>'
                "</testsuite></testsuites>"
            )
            suites = ET.Element("testsuites")
            _append_profile_junit(
                suites, collection, junit, ("tests/test_first.py",), expected
            )
            combined = root / "combined.xml"
            ET.ElementTree(suites).write(combined)
            self.assertEqual(
                _passed_test_count(combined, ("tests/test_first.py",), expected), 2
            )
            for actual in (expected[:1], expected + expected[:1]):
                collection.write_text(json.dumps(actual))
                with (
                    self.subTest(actual=actual),
                    self.assertRaisesRegex(RuntimeError, "profile collection differs"),
                ):
                    _append_profile_junit(
                        ET.Element("testsuites"),
                        collection,
                        junit,
                        ("tests/test_first.py",),
                        expected,
                    )
            collection.write_text(json.dumps(expected))
            junit.write_text(
                '<testsuites><testsuite tests="2" skipped="1">'
                '<testcase classname="tests.test_first" name="test_one"/>'
                '<testcase classname="tests.test_first" name="test_two"><skipped/></testcase>'
                "</testsuite></testsuites>"
            )
            with self.assertRaisesRegex(RuntimeError, "skipped"):
                _append_profile_junit(
                    ET.Element("testsuites"),
                    collection,
                    junit,
                    ("tests/test_first.py",),
                    expected,
                )

    def test_default_gate_never_publishes_junit_after_a_profile_failure(self):
        selected = (
            "tests/test_video_frame_service.py",
            "tests/test_identity_recording_live.py",
        )
        nodes = [
            "tests/test_video_frame_service.py::test_frame",
            "tests/test_identity_recording_live.py::test_recording",
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "acceptance.json"
            args = _arguments(["--output", str(output)])

            def profile_run(argv, *, collect_only=False, partition_default=True):
                receipt = Path(argv[1])
                if collect_only:
                    receipt.with_name("collection.collection.json").write_text(
                        json.dumps(nodes)
                    )
                    return 0
                name = receipt.stem
                failed = name == "recording"
                receipt.write_text(
                    json.dumps(
                        {
                            "status": "failed" if failed else "passed",
                            "test_exit_code": 1 if failed else 0,
                            "cleanup": {"errors": []},
                        }
                    )
                )
                if not failed:
                    receipt.with_name(name + ".collection.json").write_text(
                        json.dumps(nodes[:1])
                    )
                    receipt.with_name(name + ".junit.xml").write_text(
                        '<testsuites><testsuite tests="1">'
                        '<testcase classname="tests.test_video_frame_service" '
                        'name="test_frame"/></testsuite></testsuites>'
                    )
                return int(failed)

            with (
                patch(
                    "scripts.verify_module_data_boundary.main", side_effect=profile_run
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(_run_strict_default(args, selected), 1)
            evidence = json.loads(output.read_text())
            self.assertEqual(evidence["status"], "failed")
            self.assertEqual(evidence["profiles"]["ordinary"]["status"], "passed")
            self.assertEqual(evidence["profiles"]["recording"]["status"], "failed")
            self.assertFalse(output.with_name("acceptance.junit.xml").exists())

    def test_default_interruption_cannot_leave_a_prior_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "acceptance.json"
            junit = output.with_name("acceptance.junit.xml")
            output.write_text('{"status": "passed"}')
            junit.write_text("<testsuites/>")
            args = _arguments(["--output", str(output)])
            with (
                patch(
                    "scripts.verify_module_data_boundary.main",
                    side_effect=KeyboardInterrupt,
                ),
                redirect_stdout(io.StringIO()),
                self.assertRaises(KeyboardInterrupt),
            ):
                _run_strict_default(args, ("tests/test_video_frame_service.py",))
            evidence = json.loads(output.read_text())
            self.assertEqual(evidence["status"], "failed")
            self.assertEqual(evidence["failure"]["error_type"], "KeyboardInterrupt")
            self.assertFalse(junit.exists())

    def test_empty_skipped_failed_error_and_invalid_reports_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "junit.xml"
            good = '<testcase classname="tests.test_first" name="test_one"/>'
            collected = ["tests/test_first.py::test_one"]
            for body in (
                "",
                good
                + '<testcase classname="tests.test_first" name="test_two"><skipped/></testcase>',
                good
                + '<testcase classname="tests.test_first" name="test_two"><failure/></testcase>',
                good
                + '<testcase classname="tests.test_first" name="test_two"><error/></testcase>',
                good + "<error/>",
                "<testcase/>",
            ):
                path.write_text(
                    f"<testsuites><testsuite>{body}</testsuite></testsuites>"
                )
                with self.subTest(body=body), self.assertRaises(RuntimeError):
                    _passed_test_count(path, ("tests/test_first.py",), collected)
            path.write_text(
                f'<testsuites><testsuite errors="1">{good}</testsuite></testsuites>'
            )
            with self.assertRaises(RuntimeError):
                _passed_test_count(path, ("tests/test_first.py",), collected)
            path.write_text(
                f'<testsuites><testsuite tests="2">{good}</testsuite></testsuites>'
            )
            with self.assertRaises(RuntimeError):
                _passed_test_count(path, ("tests/test_first.py",), collected)
            path.write_text("<testsuites>")
            with self.assertRaises(ET.ParseError):
                _passed_test_count(path, ("tests/test_first.py",), collected)

    def test_interruption_writes_failed_evidence_and_discards_stale_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            junit = Path(directory) / "evidence.junit.xml"
            collection = Path(directory) / "evidence.collection.json"
            junit.write_text("<testsuites><testsuite/></testsuites>")
            collection.write_text('["stale"]')
            with (
                patch(
                    "scripts.verify_module_data_boundary.shutil.which",
                    side_effect=KeyboardInterrupt,
                ),
                redirect_stdout(io.StringIO()),
                self.assertRaises(KeyboardInterrupt),
            ):
                main(
                    [
                        "--output",
                        str(output),
                        "--test-file",
                        "tests/test_identity_recovery.py",
                    ]
                )
            evidence = json.loads(output.read_text())
            self.assertEqual(evidence["status"], "failed")
            self.assertEqual(evidence["failure"]["error_type"], "KeyboardInterrupt")
            self.assertFalse(junit.exists())
            self.assertFalse(collection.exists())
            self.assertEqual(evidence["collection_manifest"], str(collection))

    def test_collection_manifest_cannot_alias_junit(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            collection = Path(directory) / "evidence.collection.json"
            collection.write_text("preserve")
            with self.assertRaises(SystemExit):
                main(["--output", str(output), "--junit-xml", str(collection)])
            self.assertEqual(collection.read_text(), "preserve")

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
