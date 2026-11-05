"""A failed API must not force-settle an uncooperative consumer descendant."""

import ctypes
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

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
                "import pathlib, subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], start_new_session=True); "
                "path = pathlib.Path(sys.argv[2]); "
                "exec('while not path.exists():\\n time.sleep(0.01)'); "
                "time.sleep(0.6)"
            )
            try:
                with self.assertRaisesRegex(ContractError, "raster_consumer_descendant_exit_unsettled") as raised:
                    run_product(
                        [sys.executable, "-c", consumer, grandchild, str(marker)],
                        dict(os.environ), None, None, cwd=root,
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

    def test_parent_reaped_normal_descendant_does_not_block_success(self):
        consumer = (
            "import subprocess, sys; "
            "child = subprocess.Popen([sys.executable, '-c', 'pass']); "
            "sys.exit(child.wait())"
        )
        self.assertEqual(run_product(
            [sys.executable, "-c", consumer], dict(os.environ), None, None,
            cwd=Path(__file__).resolve().parent,
        ), 0)

    def test_adopted_signalled_child_is_not_discarded(self):
        child = subprocess.Popen([
            sys.executable, "-c",
            "import os,signal; os.kill(os.getpid(), signal.SIGTERM)",
        ])
        try:
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
            with self.assertRaisesRegex(ContractError, "raster_consumer_descendants_unsettled") as raised:
                require_no_children()
            outcome = next(item for item in raised.exception.adopted_outcomes if item["pid"] == child.pid)
            self.assertTrue(os.WIFSIGNALED(outcome["status"]))
            self.assertEqual(os.WTERMSIG(outcome["status"]), signal.SIGTERM)
            child.returncode = -signal.SIGTERM
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=2)

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
