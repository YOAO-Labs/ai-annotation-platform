"""Qualify the Raster API process's real HTTP drain and shutdown exit status."""

import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import time
import unittest


_PROGRAM = """
import asyncio
import sys
from raster_api_process import serve_api

async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                if sys.argv[1] == "fail":
                    await send({"type": "lifespan.shutdown.failed", "message": "controlled shutdown failure"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                return
    else:
        print("REQUEST_HELD", flush=True)
        await asyncio.to_thread(sys.stdin.buffer.read, 1)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})

raise SystemExit(serve_api(app, port=0))
"""


class RasterApiShutdownTests(unittest.TestCase):
    def start_api(self, mode):
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", _PROGRAM, mode],
            cwd=Path(__file__).resolve().parent,
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.addCleanup(self.stop_api, process)
        output = bytearray()
        self.wait_output(process, output, b"Uvicorn running on")
        match = re.search(rb"http://127\.0\.0\.1:(\d+)", output)
        self.assertIsNotNone(match, output.decode(errors="replace"))
        return process, output, int(match.group(1))

    @staticmethod
    def stop_api(process):
        process.stdin.close()
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
        process.stdout.close()

    def wait_output(self, process, output, marker):
        deadline = time.monotonic() + 10
        while marker not in output:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, output.decode(errors="replace"))
            self.assertTrue(select.select([process.stdout], [], [], remaining)[0])
            data = os.read(process.stdout.fileno(), 65536)
            self.assertTrue(data, output.decode(errors="replace"))
            output.extend(data)

    def test_shutdown_waits_for_http_work_then_exits_zero(self):
        process, output, port = self.start_api("complete")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            self.wait_output(process, output, b"REQUEST_HELD")
            process.terminate()
            self.wait_output(process, output, b"Shutting down")
            self.assertIsNone(process.poll())
            process.stdin.write(b"x")
            process.stdin.flush()
            response = bytearray()
            while data := connection.recv(65536):
                response.extend(data)
            self.assertTrue(response.startswith(b"HTTP/1.1 200 "), response)
            self.assertEqual(process.wait(timeout=10), 0)

    def test_failed_lifespan_shutdown_is_not_success(self):
        process, _output, _port = self.start_api("fail")
        process.terminate()
        self.assertEqual(process.wait(timeout=10), 1)


if __name__ == "__main__":
    unittest.main()
