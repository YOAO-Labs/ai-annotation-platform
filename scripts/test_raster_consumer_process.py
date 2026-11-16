"""A failed API must not force-settle an uncooperative consumer descendant."""

import ctypes
import os
from pathlib import Path
import platform
import select
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from run_raster_fault_e2e import require_no_children
from raster_consumer_process import _unsettled_consumers, run_product
from storage_s3_contract import ContractError


@unittest.skipUnless(
    sys.platform == "linux" and platform.machine() in {"x86_64", "aarch64"},
    "requires Linux pidfds",
)
class RasterConsumerShutdownTests(unittest.TestCase):
    def test_api_failure_drains_group_and_detached_descendant(self):
        libc = ctypes.CDLL(None, use_errno=True)
        old = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(old), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        self.addCleanup(libc.prctl, 36, old.value, 0, 0, 0)
        with tempfile.TemporaryDirectory(prefix="raster-consumer-test-") as root:
            marker = Path(root) / "child-pid"
            grandchild = (
                "import os, pathlib, signal, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )
            consumer = (
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], start_new_session=True); "
                "time.sleep(60)"
            )
            api = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(1.5)"]
            )
            seed = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            )
            self.addCleanup(self.stop, api)
            self.addCleanup(self.stop, seed)
            try:
                with self.assertRaises(BaseExceptionGroup) as raised:
                    run_product(
                        [sys.executable, "-c", consumer, grandchild, str(marker)],
                        dict(os.environ),
                        api,
                        SimpleNamespace(seed=seed, failure=None),
                        cwd=root,
                    )
                self.assertTrue(
                    any(
                        isinstance(error, ContractError)
                        and str(error) == "raster_api_exited"
                        for error in raised.exception.exceptions
                    )
                )
                self.assertTrue(
                    any(
                        isinstance(error, ContractError)
                        and str(error) == "raster_consumer_group_unsettled"
                        for error in raised.exception.exceptions
                    )
                )
                self.assertTrue(marker.exists(), "detached child did not start")
                pid = int(marker.read_text())
                self.assertEqual(os.waitpid(pid, os.WNOHANG), (0, 0))
                self.assertIn(pid, _unsettled_consumers[-1][1])
            finally:
                if marker.exists():
                    self.stop_adopted(int(marker.read_text()))
                while _unsettled_consumers:
                    child, tracked = _unsettled_consumers.pop()
                    child.wait(timeout=2)
                    for descriptor in tracked.values():
                        os.close(descriptor)

    def test_zero_leader_does_not_hide_signal_terminated_live_descendant(self):
        libc = ctypes.CDLL(None, use_errno=True)
        old = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(old), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        self.addCleanup(libc.prctl, 36, old.value, 0, 0, 0)
        with tempfile.TemporaryDirectory(prefix="raster-consumer-exit-") as root:
            marker = Path(root) / "child-pid"
            grandchild = (
                "import os, pathlib, sys, time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )
            consumer = (
                "import os, pathlib, subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], start_new_session=True); "
                "path = pathlib.Path(sys.argv[2]); "
                "exec('while not path.exists():\\n time.sleep(0.01)'); "
                "time.sleep(0.6); "
                "child.terminate(); "
                "os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT); "
                "os._exit(0)"
            )
            try:
                with self.assertRaises(ContractError) as raised:
                    run_product(
                        [sys.executable, "-c", consumer, grandchild, str(marker)],
                        dict(os.environ),
                        None,
                        None,
                        cwd=root,
                    )
                pid = int(marker.read_text())
                self.assertEqual(raised.exception.leader_status, 0)
                self.assertEqual(
                    raised.exception.descendant_outcomes[pid],
                    {"code": os.CLD_KILLED, "status": signal.SIGTERM},
                )
            finally:
                if marker.exists():
                    self.stop_adopted(int(marker.read_text()))
                while _unsettled_consumers:
                    child, tracked = _unsettled_consumers.pop()
                    child.wait(timeout=2)
                    for descriptor in tracked.values():
                        os.close(descriptor)

    def test_adopted_exits_are_reaped_and_failures_preserved(self):
        import raster_consumer_process as consumer

        libc = ctypes.CDLL(None, use_errno=True)
        old = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(old), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        self.addCleanup(libc.prctl, 36, old.value, 0, 0, 0)
        grandchild = """
import os, pathlib, sys, time
root = pathlib.Path(sys.argv[1])
(root / "pid").write_text(str(os.getpid()))
while not (root / "release").exists():
    time.sleep(0.01)
sys.exit(int(sys.argv[2]))
"""
        leader = """
import os, subprocess, sys
child = subprocess.Popen(
    [sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]],
    start_new_session=True,
)
os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
os._exit(0)
"""
        for status in (0, 7):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory(prefix="raster-adopted-exit-") as root,
            ):
                marker = Path(root) / "pid"
                capture = consumer._track_consumer_descendants

                def release_captured_child(root_pid, tracked):
                    capture(root_pid, tracked)
                    if marker.exists() and int(marker.read_text()) in tracked:
                        (Path(root) / "release").touch()

                try:
                    with patch.object(
                        consumer, "_track_consumer_descendants", release_captured_child
                    ):
                        command = [
                            sys.executable,
                            "-c",
                            leader,
                            grandchild,
                            root,
                            str(status),
                        ]
                        if status:
                            with self.assertRaises(ContractError) as raised:
                                run_product(
                                    command, dict(os.environ), None, None, cwd=root
                                )
                            self.assertEqual(raised.exception.leader_status, 0)
                            self.assertEqual(
                                raised.exception.descendant_outcomes[
                                    int(marker.read_text())
                                ],
                                {"code": os.CLD_EXITED, "status": status},
                            )
                        else:
                            self.assertEqual(
                                run_product(
                                    command, dict(os.environ), None, None, cwd=root
                                ),
                                0,
                            )
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(int(marker.read_text()), os.WNOHANG)
                    require_no_children()
                finally:
                    if marker.exists():
                        self.stop_adopted(int(marker.read_text()))
                    while _unsettled_consumers:
                        child, tracked = _unsettled_consumers.pop()
                        child.wait(timeout=2)
                        for descriptor in tracked.values():
                            os.close(descriptor)

    def test_successful_leader_observes_live_children_without_signalling(self):
        import raster_consumer_process as consumer

        libc = ctypes.CDLL(None, use_errno=True)
        old = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(old), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        self.addCleanup(libc.prctl, 36, old.value, 0, 0, 0)
        grandchild = """
import os, pathlib, signal, sys, time
root = pathlib.Path(sys.argv[1])
def interrupted(signum, frame):
    (root / "unexpected-signal").write_text(str(signum))
    sys.exit(17)
signal.signal(signal.SIGTERM, interrupted)
parent = os.getppid()
(root / "pid").write_text(str(os.getpid()))
while os.getppid() == parent:
    time.sleep(0.01)
time.sleep(0.3 if sys.argv[2] == "natural" else 60)
"""
        leader = """
import os, pathlib, subprocess, sys, time
subprocess.Popen(
    [sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]],
    start_new_session=True,
)
while not (pathlib.Path(sys.argv[2]) / "captured").exists():
    time.sleep(0.01)
os._exit(0)
"""
        for mode in ("natural", "stuck"):
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory(prefix="raster-natural-close-") as root,
            ):
                marker = Path(root) / "pid"
                capture = consumer._track_consumer_descendants

                def release_captured_child(root_pid, tracked):
                    capture(root_pid, tracked)
                    if marker.exists() and int(marker.read_text()) in tracked:
                        (Path(root) / "captured").touch()

                try:
                    with patch.object(
                        consumer, "_track_consumer_descendants", release_captured_child
                    ):
                        command = [sys.executable, "-c", leader, grandchild, root, mode]
                        if mode == "natural":
                            self.assertEqual(
                                run_product(
                                    command, dict(os.environ), None, None, cwd=root
                                ),
                                0,
                            )
                            with self.assertRaises(ChildProcessError):
                                os.waitpid(int(marker.read_text()), os.WNOHANG)
                        else:
                            with self.assertRaises(ContractError) as raised:
                                run_product(
                                    command, dict(os.environ), None, None, cwd=root
                                )
                            self.assertEqual(
                                str(raised.exception), "raster_consumer_group_unsettled"
                            )
                            pid = int(marker.read_text())
                            self.assertEqual(os.waitpid(pid, os.WNOHANG), (0, 0))
                            self.assertIn(pid, _unsettled_consumers[-1][1])
                    self.assertFalse((Path(root) / "unexpected-signal").exists())
                finally:
                    if marker.exists():
                        self.stop_adopted(int(marker.read_text()))
                    while _unsettled_consumers:
                        child, tracked = _unsettled_consumers.pop()
                        child.wait(timeout=2)
                        for descriptor in tracked.values():
                            os.close(descriptor)

    def test_procfs_exit_race_preserves_live_descendant_tracking(self):
        import raster_consumer_process as consumer

        command = [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"]
        tracked = {}
        with (
            subprocess.Popen(command, stdin=subprocess.PIPE) as exiting,
            subprocess.Popen(command, stdin=subprocess.PIPE) as live,
        ):
            stat_path = Path(f"/proc/{exiting.pid}/stat")
            actual_read = Path.read_text
            reaped = False
            try:
                with stat_path.open() as stale_stat:

                    def read_during_reap(path, *args, **kwargs):
                        nonlocal reaped
                        if path == stat_path:
                            return stale_stat.read()
                        value = actual_read(path, *args, **kwargs)
                        if (
                            not reaped
                            and path.name == "children"
                            and str(exiting.pid) in value.split()
                        ):
                            exiting.stdin.close()
                            self.assertEqual(exiting.wait(timeout=5), 0)
                            reaped = True
                        return value

                    with patch.object(Path, "read_text", read_during_reap):
                        consumer._track_consumer_descendants(os.getpid(), tracked)
                self.assertTrue(reaped)
                self.assertNotIn(exiting.pid, tracked)
                self.assertIn(live.pid, tracked)
                self.assertTrue(consumer._pidfd_live(tracked[live.pid]))
            finally:
                for descriptor in tracked.values():
                    os.close(descriptor)
                for child in (exiting, live):
                    if not child.stdin.closed:
                        child.stdin.close()
                    self.assertEqual(child.wait(timeout=5), 0)

    def test_descendant_reaped_before_signal_delivery_does_not_block_success(self):
        import raster_consumer_process as consumer
        from linux_pidfd import open_pidfd

        parent_code = """
import subprocess, sys
child = subprocess.Popen(
    [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
    stdin=subprocess.PIPE,
)
print(child.pid, flush=True)
sys.stdin.buffer.read(1)
child.stdin.write(b"x")
child.stdin.flush()
print(child.wait(), flush=True)
sys.stdin.buffer.read(1)
"""
        leader = subprocess.Popen(
            [sys.executable, "-c", "pass"], start_new_session=True
        )
        parent = subprocess.Popen(
            [sys.executable, "-u", "-c", parent_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        descriptor = None
        try:
            os.waitid(os.P_PID, leader.pid, os.WEXITED | os.WNOWAIT)
            self.assertTrue(select.select([parent.stdout], [], [], 5)[0])
            child_pid = int(parent.stdout.readline())
            descriptor = open_pidfd(child_pid)
            actual_send = consumer.pidfd_send_signal

            def reap_before_delivery(fd, sig):
                if fd == descriptor:
                    parent.stdin.write(b"x")
                    parent.stdin.flush()
                    self.assertTrue(select.select([parent.stdout], [], [], 5)[0])
                    self.assertEqual(parent.stdout.readline(), b"0\n")
                return actual_send(fd, sig)

            with patch.object(consumer, "pidfd_send_signal", reap_before_delivery):
                self.assertEqual(
                    consumer._drain_consumer(leader, {child_pid: descriptor}), 0
                )
        finally:
            parent.stdin.close()
            parent.wait(timeout=5)
            parent.stdout.close()
            parent.stderr.close()
            leader.wait(timeout=5)
            if descriptor is not None:
                os.close(descriptor)

    def test_adopted_signalled_child_is_not_discarded(self):
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,signal; os.kill(os.getpid(), signal.SIGTERM)",
            ]
        )
        try:
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
            with self.assertRaises(ContractError) as raised:
                require_no_children()
            outcome = next(
                item
                for item in raised.exception.adopted_outcomes
                if item["pid"] == child.pid
            )
            self.assertTrue(os.WIFSIGNALED(outcome["status"]))
            self.assertEqual(os.WTERMSIG(outcome["status"]), signal.SIGTERM)
            child.returncode = -signal.SIGTERM
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=2)

    def test_interrupted_native_drain_retains_detached_descendant(self):
        import fixture_native_playwright as supervisor
        import raster_consumer_process as consumer
        from app.services import fixture_native_runtime as runtime
        from linux_pidfd import open_pidfd

        libc = ctypes.CDLL(None, use_errno=True)
        old = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(old), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        self.addCleanup(libc.prctl, 36, old.value, 0, 0, 0)
        descendant_code = """
import os, signal, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(os.getpid(), flush=True)
os.read(int(sys.argv[1]), 1)
"""
        leader_code = """
import signal, subprocess, sys
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
subprocess.Popen(
    [sys.executable, "-c", sys.argv[1], sys.argv[2]],
    pass_fds=(int(sys.argv[2]),),
    start_new_session=True,
)
while True:
    signal.pause()
"""
        for module, options in (
            (runtime, {"descendants": True}),
            (supervisor, {}),
        ):
            with self.subTest(owner=module.__name__):
                release_read, release_write = os.pipe()
                leader = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        leader_code,
                        descendant_code,
                        str(release_read),
                    ],
                    pass_fds=(release_read,),
                    stdout=subprocess.PIPE,
                    start_new_session=True,
                )
                descriptor = None
                descendant_pid = None
                try:
                    descriptor = open_pidfd(leader.pid)
                    self.assertTrue(select.select([leader.stdout], [], [], 5)[0])
                    descendant_pid = int(leader.stdout.readline())
                    with patch.object(
                        consumer, "_drain_consumer", side_effect=InterruptedError
                    ):
                        with self.assertRaises(InterruptedError):
                            module._drain(leader, descriptor, **options)
                    self.assertEqual(os.waitpid(descendant_pid, os.WNOHANG), (0, 0))
                    with self.assertRaisesRegex(
                        ContractError, "raster_consumer_group_unsettled"
                    ):
                        module._drain(leader, descriptor, **options)
                    self.assertEqual(os.waitpid(descendant_pid, os.WNOHANG), (0, 0))
                    os.write(release_write, b"x")
                    outcome = os.waitid(
                        os.P_PID, descendant_pid, os.WEXITED | os.WNOWAIT
                    )
                    self.assertEqual(
                        (outcome.si_code, outcome.si_status), (os.CLD_EXITED, 0)
                    )
                    module._drain(leader, descriptor, **options)
                    self.assertEqual(leader.returncode, 0)
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(descendant_pid, os.WNOHANG)
                finally:
                    os.close(release_write)
                    os.close(release_read)
                    self.stop(leader)
                    if descendant_pid is not None:
                        self.stop_adopted(descendant_pid)
                    leader.stdout.close()
                    for tracked_fd in (
                        getattr(leader, "fixture_native_descendants", None) or {}
                    ).values():
                        os.close(tracked_fd)
                    if descriptor is not None:
                        os.close(descriptor)

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)

    @staticmethod
    def stop_adopted(pid):
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
            if not reaped:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        except ChildProcessError:
            pass


if __name__ == "__main__":
    unittest.main()
