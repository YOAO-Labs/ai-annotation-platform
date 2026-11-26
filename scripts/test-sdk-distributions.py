"""Run SDK tests once, against installed distributions or their repository inputs."""

import argparse
import hashlib
import os
from collections import Counter
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "packages/python-sdk"
SHARED = ROOT / "apps/_shared"
REPOSITORY_ONLY = ("tests/test_docs_snippets.py", "tests/test_openapi_contract.py")


def unpack_tests(dist: Path, name: str, into: Path) -> Path:
    (archive,) = dist.glob(f"{name}-*.tar.gz")
    root = archive.name.removesuffix(".tar.gz")
    with tarfile.open(archive) as package:
        tests = (m for m in package if m.name.startswith(f"{root}/tests/"))
        package.extractall(into, members=tests, filter="data")
    return into / root


def test_files(root: Path) -> set[str]:
    return {
        p.relative_to(root).as_posix()
        for p in (root / "tests").rglob("*")
        if p.suffix in {".py", ".json"} and p.is_file()
    }


def junit_key(nodeid: str) -> tuple[str, str]:
    address, bracket, parameters = nodeid.partition("[")
    module, *classes, function = address.split("::")
    return (
        ".".join((module.removesuffix(".py").replace("/", "."), *classes)),
        function + bracket + parameters,
    )


def matches_sdk_collection(collection: Path, observed: Counter) -> bool:
    selected = Counter(
        junit_key(line)
        for line in collection.read_text().splitlines()
        if line.startswith("tests/") and "::" in line
    )
    if selected and selected == observed and all(n == 1 for n in selected.values()):
        return True
    print(
        f"SDK case mismatch: missing={selected - observed}, extra={observed - selected}",
        file=sys.stderr,
    )
    return False


def main(reports: Path) -> int:
    reports = reports.resolve()
    reports.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="yoao-sdk-tests-") as temporary:
        work = Path(temporary).resolve()
        if work.is_relative_to(ROOT):
            raise ValueError("TMPDIR must be outside the checkout for artifact tests")
        dist = work / "dist"
        python = work / "venv/bin/python"
        env = {**os.environ, "PATH": f"{python.parent}{os.pathsep}{os.environ['PATH']}"}
        env.pop("PYTHONPATH", None)

        def run(*args, cwd=work):
            subprocess.run(args, cwd=cwd, env=env, check=True)

        run("uv", "build", "--python", sys.executable, "--out-dir", dist, SDK)
        with (reports / "distributions.sha256").open("w") as sums:
            for artifact in sorted((*dist.glob("*.whl"), *dist.glob("*.tar.gz"))):
                with artifact.open("rb") as blob:
                    digest = hashlib.file_digest(blob, "sha256").hexdigest()
                print(f"{digest}  {artifact.name}", file=sums)
        run("uv", "venv", "--python", sys.executable, python.parent.parent)
        constraints = work / "constraints.txt"
        run(
            "uv",
            "export",
            "--quiet",
            "--project",
            SDK,
            "--locked",
            "--all-extras",
            "--no-emit-local",
            "--no-hashes",
            "--output-file",
            constraints,
        )
        install = (
            "uv",
            "pip",
            "install",
            "--python",
            python,
            "--constraints",
            constraints,
        )
        (sdk_sdist,) = dist.glob("yoao_sdk-*.tar.gz")
        (sdk_wheel,) = dist.glob("yoao_sdk-*.whl")
        run(*install, sdk_sdist)
        run(
            python,
            "-I",
            "-c",
            """
import json, subprocess, sys
from importlib.resources import files
from pathlib import Path
from yoao import describe_operation
import yoao_task_contract, yoao_lifecycle_contract, training_contract, yoao_inference_contract
assert describe_operation('training_runs.create')['name'] == 'POST /api/v1/projects/{project_id}/training-runs'
assert files('yoao').joinpath('AGENT_GUIDE.md').is_file()
cli = Path(sys.executable).with_name('yoao')
result = subprocess.run([cli, '--error-format', 'json', '--version'], capture_output=True, text=True)
assert result.returncode == 1 and json.loads(result.stderr)['kind'] == 'dependency_error', result
""",
        )
        run(
            *install,
            "--reinstall-package",
            "yoao-sdk",
            f"{sdk_wheel}[test,plugins,protocol]",
        )
        run(
            "uv",
            "pip",
            "install",
            "--python",
            python,
            "--no-deps",
            "--find-links",
            ROOT / ".artifacts/python/plugin-sdk",
            "--requirements",
            SHARED / "requirements-plugin-runtime.txt",
        )
        run(python.parent / "yoao", "--version")

        sdk = unpack_tests(dist, "yoao_sdk", work)
        expected = test_files(SDK) - set(REPOSITORY_ONLY)
        if difference := expected ^ test_files(sdk):
            raise ValueError(
                f"SDK source and archive test inventory differ: {sorted(difference)}"
            )
        suites = [
            ("sdk-repository", SDK, REPOSITORY_ONLY),
            ("sdk-installed", sdk, ("tests",)),
        ]

        collection = reports / "sdk-collection.txt"
        with collection.open("w") as output:
            collected = subprocess.run(
                [python, "-I", "-m", "pytest", "--collect-only", "-q", "tests"],
                cwd=SDK,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        observed = Counter()
        failed = collected.returncode != 0
        for name, cwd, nodes in suites:
            report = reports / f"{name}.xml"
            result = subprocess.run(
                [python, "-I", "-m", "pytest", "-q", f"--junitxml={report}", *nodes],
                cwd=cwd,
                env=env,
            )
            if result.returncode:
                failed = True
            if not report.is_file():
                print(f"{name}: missing JUnit", file=sys.stderr)
                failed = True
                continue
            cases = list(ElementTree.parse(report).iter("testcase"))
            if name in ("sdk-repository", "sdk-installed"):
                observed.update(
                    (c.attrib["classname"], c.attrib["name"]) for c in cases
                )
            if not cases or any(c.find("skipped") is not None for c in cases):
                print(f"{name}: empty or skipped artifact cases", file=sys.stderr)
                failed = True
        if not matches_sdk_collection(collection, observed):
            failed = True
        return int(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "reports", type=Path, help="new directory for per-suite JUnit reports"
    )
    raise SystemExit(main(parser.parse_args().reports))
