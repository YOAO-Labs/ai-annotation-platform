"""A failed API must not force-settle an uncooperative consumer descendant."""

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from raster_consumer_process import _unsettled_consumers, run_product
from storage_s3_contract import ContractError


@unittest.skipUnless(
    sys.platform == "linux"
    and hasattr(os, "pidfd_open")
    and hasattr(signal, "pidfd_send_signal"),
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
