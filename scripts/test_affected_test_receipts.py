import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree as ET

from affected_test_receipts import receipts


class AffectedReceiptsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.reports = Path(temp.name)
        self.web = "apps/web/src/first.test.tsx"
        self.other = "apps/web/src/second.test.tsx"

    def vitest(self, suites):
        results = []
        junit = ET.Element("testsuites")
        passed = 0
        for file, cases in suites:
            suite = ET.SubElement(
                junit, "testsuite", name=file.removeprefix("apps/web/")
            )
            assertions = []
            for name, status in cases:
                ET.SubElement(suite, "testcase", name=name)
                assertions.append({"fullName": name, "status": status})
                passed += status == "passed"
            results.append(
                {
                    "name": str(Path(file).absolute()),
                    "status": "passed",
                    "assertionResults": assertions,
                }
            )
        (self.reports / "web.json").write_text(
            json.dumps(
                {"success": True, "numPassedTests": passed, "testResults": results}
            )
        )
        ET.ElementTree(junit).write(self.reports / "web.xml")

    def test_web_counts_explicit_passes_and_runtime_skips_per_file(self):
        self.vitest([(self.web, [("renders", "passed"), ("unsupported", "skipped")])])
        counts, problems = receipts({"web": [self.web], "api": []}, self.reports)
        self.assertEqual(counts, {self.web: {"passed": 1, "skipped": 1}})
        self.assertEqual(problems, [])

    def test_a_passing_web_file_cannot_cover_a_skipped_empty_or_missing_one(self):
        plan = {"web": [self.web, self.other], "api": []}
        passed = (self.web, [("renders", "passed")])
        for extra in ([(self.other, [("later", "todo")])], [(self.other, [])], []):
            with self.subTest(extra=extra):
                self.vitest([passed, *extra])
                _, problems = receipts(plan, self.reports)
                self.assertTrue(problems)

    def test_duplicate_extra_or_unfinished_web_evidence_is_rejected(self):
        passing = [("renders", "passed")]
        for suites in (
            [(self.web, passing), (self.web, passing)],
            [(self.web, passing * 2)],
            [(self.web, passing), (self.other, passing)],
            [(self.web, [*passing, ("unfinished", "pending")])],
            [(self.web, [*passing, ("broken", "failed")])],
        ):
            with self.subTest(suites=suites):
                self.vitest(suites)
                _, problems = receipts({"web": [self.web], "api": []}, self.reports)
                self.assertTrue(problems)

    def test_only_the_current_report_directory_can_satisfy_the_plan(self):
        self.vitest([(self.web, [("renders", "passed")])])
        current = self.reports / "current-attempt"
        current.mkdir()
        _, problems = receipts({"web": [self.web], "api": []}, current)
        self.assertTrue(problems)

    def test_real_pytest_counts_parameters_skips_and_their_exact_source_file(self):
        tests = self.reports / "tests"
        tests.mkdir()
        (self.reports / "pytest.ini").write_text("[pytest]\n")
        file = "apps/api/tests/test_month.py"
        (tests / "test_month.py").write_text(
            "import pytest\n"
            "@pytest.mark.parametrize('day', [28, 29])\n"
            "def test_day(day): assert day > 0\n"
            "def test_inactive(): pytest.skip('unsupported')\n"
        )
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-o",
                "junit_family=xunit1",
                "--junitxml=api.xml",
                "--",
                "tests/test_month.py",
            ],
            cwd=self.reports,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        counts, problems = receipts({"api": [file], "web": []}, self.reports)
        self.assertEqual(counts, {file: {"passed": 2, "skipped": 1}})
        self.assertEqual(problems, [])
        _, problems = receipts(
            {"api": [file, "apps/api/tests/missing.py"], "web": []}, self.reports
        )
        self.assertTrue(problems)

    def test_pytest_rejects_an_error_next_to_a_pass_and_duplicate_cases(self):
        file = "apps/api/tests/test_month.py"
        for name, child in (("other", "<error/>"), ("same", "")):
            with self.subTest(name=name):
                case = (
                    '<testcase file="tests/test_month.py" classname="tests.test_month"'
                )
                (self.reports / "api.xml").write_text(
                    f'<testsuites><testsuite>{case} name="same"/>{case} name="{name}">{child}</testcase></testsuite></testsuites>'
                )
                _, problems = receipts({"api": [file], "web": []}, self.reports)
                self.assertTrue(problems)


if __name__ == "__main__":
    unittest.main()
