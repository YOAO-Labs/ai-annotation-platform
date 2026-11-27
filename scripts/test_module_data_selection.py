"""Selected acceptance targets must remain inside the API test tree."""

from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from scripts.verify_module_data_boundary import (
    ROOT,
    TEST_FILES,
    _RECLAMATION_PROFILES,
    _STRICT_PROFILE_TARGETS,
    _append_profile_junit,
    _arguments,
    _passed_test_count,
    _run_strict_default,
    main,
)

RETIRED_MODULE_DATA_SELECTORS = frozenset(
    {
        "tests/test_screenshot_seed_catalog.py",
        "tests/test_identity_recording_live.py",
        "tests/test_identity_recording_native.py",
        "tests/test_inference_registry_ingress_binding.py",
        "tests/test_inference_registry_ingress_uow.py",
        "tests/test_workspace_capacity_live.py",
        "tests/test_workspace_module_healthcheck.py",
    }
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
            "tests/test_identity_e2e_live.py",
        )
        nodes = [
            "tests/test_video_frame_service.py::test_frame",
            "tests/test_identity_e2e_live.py::test_installed_api_consumes_owner_prepared_project_roles",
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
                failed = name == "identity-e2e"
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
            self.assertEqual(evidence["profiles"]["identity-e2e"]["status"], "failed")
            self.assertFalse(output.with_name("acceptance.junit.xml").exists())

    def test_default_reclaim_cases_use_separate_native_profiles(self):
        ordinary = "tests/test_other.py::Group::test_parametrized"
        selected = (ordinary, *_RECLAMATION_PROFILES)
        collected = {target: target for target in selected}
        collected[ordinary] += "[a::b]"
        invocations = {}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "acceptance.json"
            args = _arguments(["--output", str(output)])

            def profile_run(argv, *, collect_only=False, partition_default=True):
                receipt = Path(argv[1])
                manifest = receipt.with_suffix(".collection.json")
                if collect_only:
                    manifest.write_text(json.dumps(list(collected.values())))
                    return 0
                selection = argv[3::2]
                invocations[receipt.stem] = tuple(selection)
                actual = [collected[target] for target in selection]
                manifest.write_text(json.dumps(actual))
                receipt.write_text(
                    json.dumps(
                        {
                            "status": "passed",
                            "test_exit_code": 0,
                            "cleanup": {"errors": []},
                        }
                    )
                )
                suite = ET.Element("testsuite", tests=str(len(actual)))
                for node in actual:
                    target, bracket, parameter = node.partition("[")
                    path, *parts = target.split("::")
                    ET.SubElement(
                        suite,
                        "testcase",
                        classname=".".join(
                            [path.removesuffix(".py").replace("/", "."), *parts[:-1]]
                        ),
                        name=parts[-1] + bracket + parameter,
                    )
                ET.ElementTree(suite).write(receipt.with_suffix(".junit.xml"))
                return 0

            with (
                patch(
                    "scripts.verify_module_data_boundary.main", side_effect=profile_run
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(_run_strict_default(args, selected), 0)

            self.assertTrue(_RECLAMATION_PROFILES.keys() <= set(TEST_FILES))
            self.assertEqual(
                invocations,
                {
                    "ordinary": (ordinary,),
                    **{
                        name: (target,)
                        for target, name in _RECLAMATION_PROFILES.items()
                    },
                },
            )

    def test_fast_native_fixture_guards_and_audit_migration_profile_routing(self):
        ordinary_guards = (
            "tests/test_jobs_fixture_cleanup_native.py",
            "tests/test_jobs_fixture_media_publication_native.py",
            "tests/test_fixture_export_owner_native.py",
        )
        migration_guard = "tests/test_fixture_audit_migration.py"
        selected = (*ordinary_guards, migration_guard)
        self.assertTrue(set(selected) <= set(TEST_FILES))
        self.assertEqual(
            _STRICT_PROFILE_TARGETS.get(migration_guard), "fixture-audit-migration"
        )
        for target in ordinary_guards:
            self.assertNotIn(target, _STRICT_PROFILE_TARGETS)

        collected = {
            "tests/test_jobs_fixture_cleanup_native.py": [
                "tests/test_jobs_fixture_cleanup_native.py::test_cleanup"
            ],
            "tests/test_jobs_fixture_media_publication_native.py": [
                "tests/test_jobs_fixture_media_publication_native.py::test_publication"
            ],
            "tests/test_fixture_export_owner_native.py": [
                "tests/test_fixture_export_owner_native.py::test_export_owner"
            ],
            "tests/test_fixture_audit_migration.py": [
                "tests/test_fixture_audit_migration.py::test_audit_receipt_forward_migration[legacy-audit]",
                "tests/test_fixture_audit_migration.py::test_audit_receipt_forward_migration[empty-audit]",
            ],
        }
        invocations = {}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "acceptance.json"
            args = _arguments(["--output", str(output)])

            def profile_run(argv, *, collect_only=False, partition_default=True):
                receipt = Path(argv[1])
                manifest = receipt.with_suffix(".collection.json")
                if collect_only:
                    manifest.write_text(
                        json.dumps(
                            [node for nodes in collected.values() for node in nodes]
                        )
                    )
                    return 0
                selection = tuple(argv[3::2])
                invocations[receipt.stem] = selection
                actual = [node for target in selection for node in collected[target]]
                manifest.write_text(json.dumps(actual))
                receipt.write_text(
                    json.dumps(
                        {
                            "status": "passed",
                            "test_exit_code": 0,
                            "cleanup": {"errors": []},
                        }
                    )
                )
                suite = ET.Element("testsuite", tests=str(len(actual)))
                for node in actual:
                    target, bracket, parameter = node.partition("[")
                    path, *parts = target.split("::")
                    ET.SubElement(
                        suite,
                        "testcase",
                        classname=".".join(
                            [path.removesuffix(".py").replace("/", "."), *parts[:-1]]
                        ),
                        name=parts[-1] + bracket + parameter,
                    )
                ET.ElementTree(suite).write(receipt.with_suffix(".junit.xml"))
                return 0

            with (
                patch(
                    "scripts.verify_module_data_boundary.main", side_effect=profile_run
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(_run_strict_default(args, selected), 0)

            self.assertEqual(
                invocations,
                {
                    "ordinary": ordinary_guards,
                    "fixture-audit-migration": (migration_guard,),
                },
            )

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

    def test_workspace_assembly_accepts_explicit_strict_and_default_profiles(self):
        target = "tests/test_workspace_module_assembly_live.py"
        self.assertIn(target, TEST_FILES)
        for profile in ("strict", "default"):
            with self.subTest(profile=profile):
                parsed = _arguments(
                    [
                        "--output",
                        "/tmp/unused.json",
                        "--workspace-consumer-profile",
                        profile,
                        "--test-file",
                        target,
                    ]
                )
                self.assertEqual(parsed.workspace_consumer_profile, profile)
                self.assertEqual(parsed.test_files, [target])

    def test_default_profile_combines_reviewed_m2b_targets(self):
        inventory = "tests/test_m2b_private_access_live.py"
        jobs = "tests/test_jobs_ordinary_ledger_live.py"
        replay = "tests/test_m2b_runtime_acl_replay.py"
        fault = (
            "tests/test_storage_fault_states.py"
            "::test_real_login_observes_and_reads_without_private_storage_access"
            "[lan-embedded]"
        )
        args = [
            "--output",
            "/tmp/unused.json",
            "--test-file",
            inventory,
            "--test-file",
            jobs,
            "--test-file",
            replay,
            "--test-file",
            fault,
        ]
        parsed = _arguments([*args, "--workspace-consumer-profile", "default"])
        self.assertEqual(parsed.test_files, [inventory, jobs, replay, fault])
        self.assertEqual(parsed.workspace_consumer_profile, "default")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _arguments(args)

    def test_existing_training_fence_stays_in_the_strict_selection(self):
        target = (
            "tests/test_training_dataset_owner_fences.py"
            "::test_finalization_fails_closed_on_locked_snapshot_rows"
        )
        self.assertIn(target, TEST_FILES)
        parsed = _arguments(["--output", "/tmp/unused.json", "--test-file", target])
        self.assertEqual(parsed.workspace_consumer_profile, "strict")

    def test_default_workspace_profile_rejects_unrelated_or_combined_targets(self):
        for targets in (
            [],
            ["tests/test_identity_recovery.py"],
            [
                "tests/test_workspace_module_assembly_live.py",
                "tests/test_workspace_worker_assembly_live.py",
            ],
            [
                "tests/test_workspace_module_assembly_live.py",
                "tests/test_m2b_private_access_live.py",
            ],
            [
                "tests/test_m2b_private_access_live.py",
                "tests/test_identity_recovery.py",
            ],
            ["tests/test_storage_fault_states.py"],
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

    def test_default_test_files_have_no_duplicates(self):
        duplicates = sorted(
            target for target, count in Counter(TEST_FILES).items() if count > 1
        )
        self.assertEqual(duplicates, [])

    def test_selected_and_strict_profile_targets_exist_under_api_tests(self):
        api_root = (ROOT / "apps/api").resolve()
        tests_root = (api_root / "tests").resolve()
        for target in (*TEST_FILES, *_STRICT_PROFILE_TARGETS):
            relative = Path(target.split("::", 1)[0])
            resolved = (api_root / relative).resolve()
            with self.subTest(target=target):
                self.assertFalse(relative.is_absolute())
                self.assertTrue(resolved.is_relative_to(tests_root))
                self.assertEqual(resolved.suffix, ".py")
                self.assertTrue(resolved.is_file())

    def test_strict_profile_targets_are_subset_of_default_test_files(self):
        missing = sorted(set(_STRICT_PROFILE_TARGETS) - set(TEST_FILES))
        self.assertEqual(missing, [])

    def test_retired_selectors_do_not_return_to_default_selection(self):
        selected_paths = {target.split("::", 1)[0] for target in TEST_FILES}
        strict_paths = {target.split("::", 1)[0] for target in _STRICT_PROFILE_TARGETS}
        self.assertEqual(sorted(selected_paths & RETIRED_MODULE_DATA_SELECTORS), [])
        self.assertEqual(sorted(strict_paths & RETIRED_MODULE_DATA_SELECTORS), [])

    def test_collect_only_writes_manifest_without_starting_docker(self):
        from scripts.verify_module_data_boundary import main as verify_main

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "collection.json"
            with redirect_stdout(io.StringIO()):
                code = verify_main(
                    ["--output", str(output), "--reclamation-only"],
                    collect_only=True,
                )
            self.assertEqual(code, 0)
            evidence = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(evidence["status"], "collected")
            manifest = output.with_name("collection.collection.json")
            collected = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(collected, evidence["collected_test_nodes"])
            self.assertTrue(collected)
            for target in _RECLAMATION_PROFILES:
                self.assertTrue(
                    any(
                        node == target
                        or node.startswith(f"{target}[")
                        or node.startswith(f"{target}::")
                        for node in collected
                    ),
                    target,
                )


if __name__ == "__main__":
    unittest.main()
