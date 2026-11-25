import json
import os
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET


RUNNERS = ("web", "api")


def prepare(plan, directory):
    if (
        plan["scope"] != "affected"
        or not isinstance(plan["docs"], bool)
        or not isinstance(plan["reason"], str)
    ):
        raise ValueError("expected an affected plan")
    if not any(plan[key] for key in ("docs", *RUNNERS)):
        raise ValueError("empty affected plan")
    for runner in RUNNERS:
        files = plan[runner]
        if not isinstance(files, list) or len(set(files)) != len(files):
            raise ValueError(f"expected distinct {runner} test paths")
        project = Path("apps", runner)
        args = []
        for name in files:
            path = Path(name)
            relative = path.relative_to(project)
            if (
                str(path) != name
                or path.resolve() != path.absolute()
                or not path.is_file()
            ):
                raise ValueError(f"expected a regular file at {name!r}")
            args.append(
                os.fsencode(path.absolute() if runner == "web" else relative) + b"\0"
            )
        (directory / f"{runner}.files").write_bytes(b"".join(args))
    for key in ("docs", *RUNNERS):
        print(f"{key}={str(bool(plan[key])).lower()}")


def vitest_cases(directory, files, problems):
    expected = {str(Path(p).absolute()): p for p in files}
    data = json.loads((directory / "web.json").read_text())
    if data["success"] is not True:
        problems.append("Vitest reported a failure")
    seen = set()
    passed = 0
    for suite in data["testResults"]:
        file = expected[suite["name"]]
        if file in seen:
            raise ValueError(f"duplicate suite for {file}")
        seen.add(file)
        if suite["status"] != "passed":
            problems.append(f"Vitest did not finish {file} successfully")
        for case in suite["assertionResults"]:
            status = case["status"]
            if status in {"todo", "skipped"}:
                status = "skipped"
            elif status not in {"passed", "failed"}:
                raise ValueError(f"unfinished Vitest case in {file}: {status!r}")
            passed += status == "passed"
            yield file, "", case["fullName"], status
    if passed != data["numPassedTests"]:
        problems.append("Vitest's pass count disagrees with its testcase results")


def pytest_cases(junit, files):
    expected = {str(Path(p).relative_to("apps/api")): p for p in files}
    for case in junit.iter("testcase"):
        file = expected[case.attrib["file"]]
        if case.find("error") is not None:
            status = "error"
        elif case.find("failure") is not None:
            status = "failed"
        elif case.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        yield file, case.attrib["classname"], case.attrib["name"], status


def receipts(plan, directory):
    counts = {p: Counter() for runner in RUNNERS for p in plan[runner]}
    problems = []
    for runner in RUNNERS:
        files = plan[runner]
        if not files:
            continue
        try:
            junit = ET.parse(directory / f"{runner}.xml").getroot()
            if junit.find(".//testcase") is None:
                raise ValueError("empty JUnit report")
            if (
                junit.find(".//error") is not None
                or junit.find(".//failure") is not None
                or any(
                    int(suite.get(attr, 0))
                    for suite in (junit, *junit.findall(".//testsuite"))
                    for attr in ("failures", "errors")
                )
            ):
                problems.append(f"{runner} JUnit reported failures or errors")
            if runner == "web":
                suites = [s.attrib["name"] for s in junit.iter("testsuite")]
                if sorted(f"apps/web/{s}" for s in suites) != sorted(files):
                    problems.append(
                        "Vitest JUnit does not contain the planned files exactly once"
                    )
                cases = vitest_cases(directory, files, problems)
            else:
                cases = pytest_cases(junit, files)
            seen = set()
            for file, classname, name, status in cases:
                key = (file, classname, name)
                if not name or key in seen:
                    raise ValueError(
                        f"missing or duplicate case name in {file}: {name!r}"
                    )
                seen.add(key)
                counts[file][status] += 1
        except (OSError, ET.ParseError, KeyError, TypeError, ValueError) as exc:
            problems.append(f"invalid {runner} report: {exc}")
    for file, result in counts.items():
        if not result["passed"] or result["failed"] or result["error"]:
            problems.append(
                f"{file} needs at least one passing case and no failures or errors"
            )
    return counts, problems


def summarize(plan, directory):
    counts, problems = receipts(plan, directory)
    checks = ["QUALITY"]
    if plan["docs"]:
        checks.append("DOCS")
    if plan["web"]:
        checks.extend(("TYPECHECK", "VITEST"))
    if plan["api"]:
        checks.append("PYTEST")
    for check in checks:
        outcome = os.environ[check]
        print(f"- {check.lower()}: {outcome}")
        if outcome != "success":
            problems.append(f"{check.lower()} was {outcome}")
    if counts:
        print(
            "\n| Test file | Passed | Failed | Errors | Skipped |\n| --- | ---: | ---: | ---: | ---: |"
        )
        for file, c in counts.items():
            print(
                f"| `{file}` | {c['passed']} | {c['failed']} | {c['error']} | {c['skipped']} |"
            )
    for problem in problems:
        print(problem, file=sys.stderr)
    return int(bool(problems))


if __name__ == "__main__":
    mode, reports = sys.argv[1:]
    plan = json.loads(os.environ["PLAN"])
    if mode == "prepare":
        prepare(plan, Path(reports))
    elif mode == "summarize":
        sys.exit(summarize(plan, Path(reports)))
    else:
        sys.exit("expected prepare or summarize")
