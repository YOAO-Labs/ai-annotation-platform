"""Worktree lifecycle regression tests, independent of API test databases."""

import importlib.util
import unittest
import tempfile
from pathlib import Path


class MigrationGuardTests(unittest.TestCase):
    def runtime(self):
        self.assertIsNotNone(
            importlib.util.find_spec("worktree_runtime"),
            "A runtime owner must validate revisions before migration",
        )
        import worktree_runtime

        return worktree_runtime

    def test_unknown_database_revision_is_rejected_even_when_skipping(self):
        runtime = self.runtime()
        for skip in (False, True):
            with self.subTest(skip=skip), self.assertRaisesRegex(ValueError, "0167"):
                runtime.validate_revisions(["0165"], ["0165"], ["0167"], skip=skip)

    def test_duplicate_and_multiple_heads_fail_before_connecting(self):
        runtime = self.runtime()
        for revisions, heads in ((["a", "a"], ["a"]), (["a", "b"], ["a", "b"])):
            with self.subTest(heads=heads), self.assertRaises(ValueError):
                runtime.validate_revisions(revisions, heads, [], skip=False)

    def test_skip_requires_database_at_the_actual_checkout_head(self):
        runtime = self.runtime()
        with self.assertRaises(ValueError):
            runtime.validate_revisions(["a", "b"], ["b"], ["a"], skip=True)
        runtime.validate_revisions(["a", "b"], ["b"], ["b"], skip=True)
        runtime.validate_revisions(["a", "b"], ["b"], [], skip=False)


class OwnershipTests(unittest.TestCase):
    def test_present_but_untagged_resources_cannot_be_adopted(self):
        import worktree_runtime as runtime

        self.assertTrue(hasattr(runtime, "require_owner"))
        self.assertFalse(runtime.require_owner("database", None, "ours"))
        self.assertTrue(runtime.require_owner("database", {"owner": "ours"}, "ours"))
        for owner in (None, "other"):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                runtime.require_owner("database", {"owner": owner}, "ours")

    def test_destructive_confirmation_is_exact_and_foreign_inventory_blocks_all(self):
        import worktree_runtime as runtime

        self.assertTrue(hasattr(runtime, "validate_destruction"))
        from worktree_env import load_identity, topology

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "test")
            inventory = {
                "database": {"owner": resources["owner"], "sessions": 0},
                "redis": None,
                "buckets": {},
            }
            for confirmation in (None, "yes", resources["id"]):
                with self.assertRaises(ValueError):
                    runtime.validate_destruction(resources, inventory, confirmation)
            runtime.validate_destruction(
                resources, inventory, resources["confirmation"]
            )
            inventory["buckets"]["foreign"] = {"owner": "other"}
            with self.assertRaises(ValueError):
                runtime.validate_destruction(
                    resources, inventory, resources["confirmation"]
                )
            inventory["buckets"].clear()
            inventory["database"]["sessions"] = 1
            with self.assertRaises(ValueError):
                runtime.validate_destruction(
                    resources, inventory, resources["confirmation"]
                )

    def test_active_environment_lock_is_exclusive(self):
        import worktree_runtime as runtime

        self.assertTrue(hasattr(runtime, "environment_lock"))
        from worktree_env import load_identity, topology

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "dev")
            with runtime.environment_lock(resources):
                with self.assertRaisesRegex(ValueError, "使用中"):
                    with runtime.environment_lock(resources):
                        self.fail("Two owners acquired the same environment")
            with runtime.environment_lock(resources):
                pass


class CommandTests(unittest.TestCase):
    def test_third_party_value_errors_cannot_print_connection_secrets(self):
        import worktree_runtime as runtime

        message = runtime.safe_error(
            ValueError("postgresql://user:fixture-secret@localhost/db")
        )
        self.assertNotIn("fixture-secret", message)

    def test_doctor_does_not_report_starting_services_as_healthy(self):
        from unittest.mock import AsyncMock, Mock, patch
        import worktree_runtime as runtime
        from worktree_env import load_identity, topology

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "dev")
            backend = Mock(resources=resources, code_root=root)
            backend.database = AsyncMock(
                return_value={"owner": resources["owner"], "sessions": 0}
            )
            backend.revisions = AsyncMock(return_value=["a"])
            backend.owned_runtime_role = AsyncMock(return_value=None)
            backend.verify_runtime_login = AsyncMock(return_value=True)
            backend.redis_info.return_value = {
                "owner": resources["owner"],
                "running": True,
                "port": 6391,
            }
            backend.bucket_info.return_value = {"owner": resources["owner"]}
            with (
                patch(
                    "worktree_resources.migration_graph", return_value=(["a"], ["a"])
                ),
                patch.object(
                    runtime,
                    "session_record",
                    return_value={
                        "command": "up",
                        "children": [],
                        "worker_requested": True,
                        "worker": "ordinary@local",
                    },
                ),
                patch.object(runtime, "process_matches", return_value=True),
                patch("redis.Redis"),
            ):
                report = runtime.diagnose(backend)
                self.assertFalse(report["healthy"])
                self.assertEqual(report["state"], "starting")
                self.assertEqual(report["worker"], "ordinary@local")
                self.assertEqual(report["maintenance_worker"], "starting")

    def test_cli_help_exposes_lifecycle_commands_without_connecting(self):
        import subprocess
        import sys

        script = Path(__file__).with_name("worktree_runtime.py")
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("doctor", result.stdout)
        self.assertIn("--confirm", result.stdout)

    def test_command_modes_and_exec_separator_are_explicit(self):
        import worktree_runtime as runtime

        self.assertTrue(hasattr(runtime, "parse_arguments"))
        default = runtime.parse_arguments([])
        self.assertEqual((default.command, default.mode), ("up", "dev"))
        command = runtime.parse_arguments(
            ["exec", "--mode", "e2e", "--with-worker", "--", "pnpm", "test:e2e"]
        )
        self.assertEqual(command.execute, ["pnpm", "test:e2e"])
        self.assertEqual(command.mode, "e2e")
        self.assertTrue(command.with_worker)
        self.assertEqual(
            runtime.parse_arguments(["exec", "--", "python", "-V"]).mode, "test"
        )
        self.assertEqual(
            runtime.parse_arguments(["--", "--api-port", "8300"]).api_port, 8300
        )
        with self.assertRaises(SystemExit):
            runtime.parse_arguments(["exec"])
        with self.assertRaises(SystemExit):
            runtime.parse_arguments(["--api-port", "0"])

    def test_inference_recovery_requires_one_explicit_e2e_operation(self):
        import worktree_runtime as runtime

        recovery = runtime.parse_arguments(
            ["recover-inference", "--", "python", "recover.py"]
        )
        self.assertEqual(recovery.mode, "e2e")
        self.assertEqual(recovery.execute, ["python", "recover.py"])
        self.assertTrue(
            runtime.parse_arguments(
                ["recover-inference", "--finish-drain"]
            ).finish_drain
        )
        for args in (
            ["recover-inference"],
            ["recover-inference", "--finish-drain", "--", "true"],
            ["recover-inference", "--mode", "test", "--finish-drain"],
            ["destroy", "--finish-drain"],
        ):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                runtime.parse_arguments(args)

    def test_prelaunch_recovery_preserves_only_a_proven_predecessor(self):
        from types import SimpleNamespace
        import worktree_runtime as runtime
        from worktree_env import atomic_json, load_identity, state_path, topology

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "e2e")
            backend = SimpleNamespace(resources=resources, code_root=root)
            path = state_path(root, "e2e", "run.json")
            predecessor = {
                "owner": resources["owner"],
                "children": [],
                "phase": "completed",
                "supervisor_reaped": True,
                "descendant_boundary": {"state": "settled"},
            }
            attempt = {
                "owner": resources["owner"],
                "children": [],
                "phase": "prelaunch",
                "predecessor": predecessor,
                "prelaunch_failures": ["WorktreeError"],
            }
            atomic_json(path, attempt)
            runtime.reject_orphans(backend, allow_inference=True)
            for phase in ("verifying", "admitting", "admitted"):
                with self.subTest(phase=phase):
                    atomic_json(path, {**attempt, "phase": phase})
                    with self.assertRaises(runtime.WorktreeError):
                        runtime.reject_orphans(backend, allow_inference=True)
            predecessor["descendant_boundary"]["state"] = "draining"
            atomic_json(path, attempt)
            with self.assertRaises(runtime.WorktreeError):
                runtime.reject_orphans(backend, allow_inference=True)

    @unittest.skipUnless(
        __import__("sys").platform == "linux", "requires Linux process birth"
    )
    def test_process_identity_uses_kernel_birth_not_wallclock(self):
        import os
        import worktree_runtime as runtime

        scope = str(Path(__file__).resolve().parents[1])
        record = runtime.process_handle(os.getpid(), scope)
        self.assertIsNotNone(record)
        self.assertNotIn("command", record)
        self.assertTrue(
            runtime.process_matches({**record, "started": "old-wallclock"}, scope)
        )
        for replacement in (
            {"start_ticks": record["start_ticks"] + 1},
            {"boot_id": "other-boot"},
            {"scope": str(Path(scope) / "other")},
            {"command_hash": "other-command"},
        ):
            with self.subTest(replacement=replacement):
                self.assertFalse(
                    runtime.process_matches({**record, **replacement}, scope)
                )
        for field in ("start_ticks", "boot_id"):
            unproven = {key: value for key, value in record.items() if key != field}
            with self.subTest(missing=field), self.assertRaises(runtime.WorktreeError):
                runtime.process_matches(unproven, scope)

    def test_manual_scenario_is_limited_to_e2e_up(self):
        import worktree_runtime as runtime

        options = runtime.parse_arguments(
            ["up", "--mode", "e2e", "--scenario", "filtering"]
        )
        self.assertEqual(options.scenario, "filtering")
        for args in (
            ["--scenario", "filtering"],
            ["up", "--mode", "test", "--scenario", "filtering"],
            ["reset", "--mode", "e2e", "--scenario", "filtering"],
            ["exec", "--mode", "e2e", "--scenario", "filtering", "--", "true"],
        ):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                runtime.parse_arguments(args)

    @unittest.skipUnless(
        __import__("sys").platform == "linux",
        "requires Linux subreaper and pidfds",
    )
    def test_zero_exiting_wrapper_does_not_settle_detached_consumer(self):
        import json
        import subprocess
        import sys
        import time

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ready = directory / "ready"
            wrapper_done = directory / "wrapper-done"
            release = directory / "release"
            record = directory / "record.json"
            grandchild = """
import pathlib, sys, time
ready, release = map(pathlib.Path, sys.argv[1:])
ready.write_text("ready")
deadline = time.monotonic() + 10
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
sys.exit(0 if release.exists() else 9)
"""
            wrapper = """
import subprocess, sys
subprocess.Popen([sys.executable, "-c", sys.argv[1], *sys.argv[2:]], start_new_session=True)
"""
            runner_code = """
import pathlib, subprocess, sys
sys.path.insert(0, sys.argv[1])
from worktree_runtime import establish_e2e_subreaper, settle_e2e_descendants
establish_e2e_subreaper()
wrapper, grandchild, ready, release, wrapper_done, record = sys.argv[2:]
child = subprocess.Popen([sys.executable, "-c", wrapper, grandchild, ready, release])
assert child.wait() == 0
pathlib.Path(wrapper_done).write_text("done")
settle_e2e_descendants({}, pathlib.Path(record), None)
"""
            runner = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    runner_code,
                    str(Path(__file__).parent),
                    wrapper,
                    grandchild,
                    str(ready),
                    str(release),
                    str(wrapper_done),
                    str(record),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 8
                while not (
                    ready.exists() and wrapper_done.exists() and record.exists()
                ):
                    if runner.poll() is not None or time.monotonic() >= deadline:
                        self.fail("detached consumer did not reach the held state")
                    time.sleep(0.02)
                self.assertIsNone(
                    runner.poll(), "zero wrapper exit closed the boundary"
                )
                release.write_text("release")
                stdout, stderr = runner.communicate(timeout=15)
                self.assertEqual(runner.returncode, 0, (stdout, stderr))
                boundary = json.loads(record.read_text())["descendant_boundary"]
                self.assertEqual(boundary["state"], "settled")
                self.assertTrue(
                    any(item["state"] == "reaped" for item in boundary["adopted"])
                )
            finally:
                release.write_text("release")
                if runner.poll() is None:
                    runner.communicate(timeout=15)


@unittest.skipUnless(__import__("sys").platform == "linux", "requires Linux pidfds")
class DirectChildBoundaryTests(unittest.TestCase):
    def test_acknowledged_command_with_crashed_or_cooperatively_stopped_worker(self):
        import signal
        import subprocess
        import sys
        import time

        import worktree_runtime as runtime
        from linux_pidfd import open_pidfd, pidfd_send_signal
        from worktree_env import read_state

        worker_code = """
import signal, sys
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print("ready", flush=True)
if sys.argv[1] == "crash":
    sys.stdin.buffer.read(1)
    sys.exit(23)
while True:
    signal.pause()
"""
        supervisor_code = """
import sys
print("ready", flush=True)
sys.stdin.buffer.read(1)
sys.exit(int(sys.argv[1]))
"""
        for command_exit, crash in ((0, False), (0, True), (1, False), (1, True)):
            with (
                self.subTest(command_exit=command_exit, crash=crash),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                worker = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        worker_code,
                        "crash" if crash else "normal",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                supervisor = subprocess.Popen(
                    [sys.executable, "-u", "-c", supervisor_code, str(command_exit)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                children = [worker, supervisor]
                try:
                    self.assertEqual(worker.stdout.readline(), b"ready\n")
                    self.assertEqual(supervisor.stdout.readline(), b"ready\n")
                    record = {"children": []}
                    for child, role in ((worker, "worker"), (supervisor, "supervisor")):
                        runtime.register_child(
                            record, runtime.process_handle(child.pid, str(root)), role
                        )
                    self.assertIsNone(runtime._e2e_child_status(worker))
                    if crash:
                        worker.stdin.write(b"x")
                        worker.stdin.flush()
                    supervisor.stdin.write(b"x")
                    supervisor.stdin.flush()
                    deadline = time.monotonic() + 8
                    while runtime._e2e_child_status(supervisor) is None or (
                        crash and runtime._e2e_child_status(worker) is None
                    ):
                        if time.monotonic() >= deadline:
                            self.fail("direct child did not reach its expected exit")
                        time.sleep(0.02)
                    path = root / "run.json"
                    if crash:
                        with self.assertRaises(runtime.WorktreeError):
                            runtime.finish_e2e_direct_children(
                                children,
                                record,
                                path,
                                command_exit=command_exit,
                                request_stop=True,
                                deadline=time.monotonic() + 8,
                            )
                    else:
                        runtime.finish_e2e_direct_children(
                            children,
                            record,
                            path,
                            command_exit=command_exit,
                            request_stop=True,
                            deadline=time.monotonic() + 8,
                        )
                    saved = read_state(path)
                    exits = {item["role"]: item for item in saved["children"]}
                    self.assertEqual(exits["supervisor"]["exit_code"], command_exit)
                    self.assertTrue(exits["supervisor"]["command_exit_acknowledged"])
                    self.assertEqual(exits["worker"]["exit_code"], 23 if crash else 0)
                    self.assertEqual(exits["worker"]["shutdown_requested"], not crash)
                    self.assertEqual("unsettled_consumers" in saved, crash)
                    self.assertNotIn("supervisor_reaped", saved)
                finally:
                    for child in children:
                        if child.returncode is None:
                            try:
                                descriptor = open_pidfd(child.pid)
                                try:
                                    pidfd_send_signal(descriptor, signal.SIGTERM)
                                finally:
                                    __import__("os").close(descriptor)
                            except ProcessLookupError:
                                pass
                            child.wait(timeout=10)
                        for stream in (child.stdin, child.stdout, child.stderr):
                            if stream is not None:
                                stream.close()

    def test_canonical_exec_requires_command_and_descendant_receipts(self):
        import json
        import signal
        import subprocess
        import sys

        runner = """
from contextlib import nullcontext
import os, pathlib, sys, types

root, state, case = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(root / "scripts"))
from worktree_runtime import parse_arguments, run_processes, safe_error

async def no_op():
    pass

def close():
    (state / "closed.json").write_bytes(
        (state / ".worktree/e2e/run.json").read_bytes()
    )

def inference_owner(*args, **kwargs):
    return types.SimpleNamespace(
        _lifecycle_lock=nullcontext(),
        start=lambda: None,
        verified_selection=lambda *args, **kwargs: {},
        supervision_ready=lambda: True,
        process_identity_inventory=list,
        close=close,
    )

sys.modules["fixture_inference_services"] = types.SimpleNamespace(
    FixtureInferenceServices=inference_owner
)
if case == "missing":
    root = state
    (root / "scripts").mkdir()
    (root / "scripts/dev-worktree.mjs").write_text(
        "export function runIsolatedCommand() { return 0; }"
    )
os.environ.pop("PLAYWRIGHT_AI_REQUEST_WORKER", None)
backend = types.SimpleNamespace(
    resources={"root": str(state), "mode": "e2e", "owner": "test-owner"},
    code_root=root,
    prepare=lambda **_: None,
    prepare_fixture_inference_database=no_op,
    environment=lambda: {**os.environ, "HOME": str(state)},
)
options = parse_arguments(["exec", "--mode", "e2e", "--", sys.executable, "-c", sys.argv[4]])
try:
    raise SystemExit(run_processes(backend, options))
except Exception as error:
    print(safe_error(error), file=sys.stderr)
    raise SystemExit(1)
"""
        root = Path(__file__).resolve().parents[1]
        spawn = (
            "import os, subprocess, sys; "
            "child = subprocess.Popen([sys.executable, '-c', %r], start_new_session=True); "
            "os._exit(1)"
        )
        killed = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
        for case, command, expected_exit in (
            ("ordinary", spawn % "pass", 1),
            ("other-status", "raise SystemExit(23)", 23),
            ("missing", "raise SystemExit(0)", 1),
            ("signalled", killed, 1),
            ("forced-descendant", spawn % killed, 1),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        runner,
                        str(root),
                        str(state),
                        case,
                        command,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, expected_exit, result.stderr)
                run_path = state / ".worktree/e2e/run.json"
                closed_path = state / "closed.json"
                if case in {"ordinary", "other-status"}:
                    self.assertFalse(run_path.exists(), result.stderr)
                    saved = json.loads(closed_path.read_text())
                    self.assertNotIn("unsettled_consumers", saved)
                    self.assertTrue(saved["supervisor_reaped"])
                    boundary = saved["descendant_boundary"]
                    self.assertEqual(boundary["state"], "settled")
                    self.assertEqual(
                        [item["exit_code"] for item in boundary["adopted"]],
                        [0] if case == "ordinary" else [],
                    )
                else:
                    self.assertFalse(closed_path.exists())
                    saved = json.loads(run_path.read_text())
                    self.assertIn("unsettled_consumers", saved)
                    self.assertNotIn("supervisor_reaped", saved)
                    if case == "forced-descendant":
                        self.assertEqual(
                            saved["unsettled_consumers"]["reason"],
                            "descendant_boundary_unproved",
                        )
                        self.assertEqual(
                            saved["descendant_boundary"]["adopted"][0]["exit_code"],
                            -signal.SIGKILL,
                        )
                    else:
                        self.assertNotIn("descendant_boundary", saved)
                [supervisor] = saved["children"]
                self.assertEqual(
                    supervisor["command_exit_acknowledged"],
                    case not in {"missing", "signalled"},
                )


class WorkerQueueTests(unittest.TestCase):
    def test_only_own_direct_inbox_is_accepted_in_addition_to_exact_queues(self):
        from worktree_runtime import worker_queues_match
        from celery.utils.nodenames import worker_direct

        node = "owned@local"
        queues = [{"name": "default"}, {"name": "media"}]
        self.assertTrue(worker_queues_match(node, "default,media", queues))
        self.assertTrue(
            worker_queues_match(
                node, "default,media", queues + [{"name": worker_direct(node).name}]
            )
        )
        self.assertFalse(
            worker_queues_match(
                node,
                "default,media",
                queues + [{"name": worker_direct("other@local").name}],
            )
        )
        self.assertFalse(worker_queues_match(node, "default,media", queues[:1]))


if __name__ == "__main__":
    unittest.main()
