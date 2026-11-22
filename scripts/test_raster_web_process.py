"""Exercise exact web-owner closure without substituting process outcomes."""

import ctypes
import http.server
import os
from pathlib import Path
import platform
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from linux_pidfd import open_pidfd, pidfd_alive
from raster_web_process import RasterWebServer, _unsettled_web_servers
from storage_s3_contract import ContractError


@unittest.skipUnless(
    sys.platform == "linux" and platform.machine() in {"x86_64", "aarch64"},
    "requires Linux pidfds",
)
class RasterWebShutdownTests(unittest.TestCase):
    def setUp(self):
        libc = ctypes.CDLL(None, use_errno=True)
        old = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(old), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        self.addCleanup(libc.prctl, 36, old.value, 0, 0, 0)

    def start_owner(self, code, *arguments):
        web = RasterWebServer(
            {"PLAYWRIGHT_ISOLATED_WEB_PORT": "1"}, cwd="/", preview=False
        )
        web.child = subprocess.Popen(
            [sys.executable, "-u", "-c", code, *arguments],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
        )
        web.descriptor = open_pidfd(web.child.pid)
        self.addCleanup(self.clean_owner, web)
        self.assertTrue(select.select([web.child.stdout], [], [], 5)[0])
        self.assertEqual(web.child.stdout.readline(), b"ready\n")
        return web

    @staticmethod
    def clean_owner(web):
        if web.child.poll() is None:
            web.child.kill()
        web.child.wait(timeout=5)
        web.child.stdout.close()
        web.child.stdin.close()
        if web in _unsettled_web_servers:
            _unsettled_web_servers.remove(web)
            for descriptor in (*web.tracked.values(), web.descriptor):
                os.close(descriptor)
            if web.ready_fd is not None:
                os.close(web.ready_fd)

    def test_occupied_http_port_cannot_admit_an_unready_owner(self):
        class ForeignHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"not-the-owned-web-server")

            def log_message(self, *_args):
                pass

        foreign = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ForeignHandler)
        thread = threading.Thread(target=foreign.serve_forever)
        thread.start()
        web = RasterWebServer(
            {"PLAYWRIGHT_ISOLATED_WEB_PORT": str(foreign.server_port)},
            cwd="/",
            preview=False,
        )
        actual_popen = subprocess.Popen

        def delayed_owner(_command, **options):
            return actual_popen(
                [sys.executable, "-c", "import os, time; time.sleep(0.5); os._exit(7)"],
                stdout=subprocess.PIPE,
                **options,
            )

        admitted = False
        try:
            with patch("raster_web_process.subprocess.Popen", delayed_owner):
                with self.assertRaises(BaseExceptionGroup):
                    with web:
                        admitted = True
            self.assertFalse(admitted)
            self.assertEqual(web.child.wait(timeout=5), 7)
            self.assertTrue(thread.is_alive())
        finally:
            if web.child is not None:
                self.clean_owner(web)
            foreign.shutdown()
            foreign.server_close()
            thread.join()

    def test_close_waits_for_children_and_preserves_owner_exit_status(self):
        child_code = """
import pathlib, signal, sys
root = pathlib.Path(sys.argv[1])
def interrupted(signum, frame):
    (root / "child-signalled").write_text(str(signum))
    sys.exit(17)
signal.signal(signal.SIGTERM, interrupted)
print("ready", flush=True)
sys.stdin.buffer.read()
(root / "child-closed").touch()
"""
        owner_code = """
import subprocess, sys
child = subprocess.Popen(
    [sys.executable, "-u", "-c", sys.argv[1], sys.argv[2]],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
)
assert child.stdout.readline() == b"ready\\n"
print("ready", flush=True)
assert sys.stdin.buffer.readline() == b"close\\n"
child.stdin.close()
assert child.wait(timeout=2) == 0
sys.exit(int(sys.argv[3]))
"""
        for status in (0, 7):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory(prefix="raster-web-close-") as root,
            ):
                web = self.start_owner(owner_code, child_code, root, str(status))
                web.observe()
                if status:
                    with self.assertRaises(ContractError) as raised:
                        web.close()
                    self.assertEqual(
                        raised.exception.web_outcome,
                        {"exit_code": status, "shutdown_requested": True},
                    )
                    self.assertIn(web, _unsettled_web_servers)
                else:
                    web.close()
                    self.assertEqual(web.child.returncode, 0)
                self.assertTrue((Path(root) / "child-closed").exists())
                self.assertFalse((Path(root) / "child-signalled").exists())

    def test_stalled_owner_remains_live_and_retained(self):
        web = self.start_owner(
            "import time; print('ready', flush=True); time.sleep(60)"
        )
        with self.assertRaises(ContractError) as raised:
            web.close()
        self.assertEqual(str(raised.exception), "raster_consumer_group_unsettled")
        self.assertIsNone(web.child.poll())
        self.assertTrue(pidfd_alive(web.descriptor))
        self.assertIn(web, _unsettled_web_servers)

    def test_unexpected_zero_exit_is_not_an_owned_shutdown(self):
        web = self.start_owner("print('ready', flush=True)")
        os.waitid(os.P_PID, web.child.pid, os.WEXITED | os.WNOWAIT)
        with self.assertRaises(ContractError) as raised:
            web.close()
        self.assertEqual(
            raised.exception.web_outcome,
            {"exit_code": 0, "shutdown_requested": False},
        )
        self.assertIn(web, _unsettled_web_servers)


if __name__ == "__main__":
    unittest.main()
