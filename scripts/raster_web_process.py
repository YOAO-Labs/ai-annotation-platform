"""Own Vite directly and await its explicit close, never a process-group signal."""

import builtins
import os
import select
import subprocess
import time

from linux_pidfd import open_pidfd, pidfd_alive, waitid_pidfd
from raster_consumer_process import _drain_consumer, _track_consumer_descendants
from storage_s3_contract import ContractError, require
from worktree_runtime import http_ready

_unsettled_web_servers = []


class RasterWebServer:
    def __init__(self, environment, *, cwd, preview):
        self.cwd = cwd
        self.preview = preview
        self.port = int(environment["PLAYWRIGHT_ISOLATED_WEB_PORT"])
        self.environment = {
            key: environment[key]
            for key in ("PATH", "HOME", "LANG", "MINIO_PROXY_TARGET")
            if key in environment
        }
        self.environment["PORT"] = str(self.port)
        self.environment["CI"] = "true"
        if preview:
            api_port = int(environment["PLAYWRIGHT_ISOLATED_API_PORT"])
            self.environment.update(
                API_PROXY_TARGET=f"http://127.0.0.1:{api_port}",
                VITE_WS_HOST=f"127.0.0.1:{api_port}",
            )
        self.child = None
        self.descriptor = None
        self.ready_fd = None
        self.tracked = {}

    def __enter__(self):
        self.ready_fd, ready_writer = os.pipe()
        try:
            self.child = subprocess.Popen(
                [
                    "node",
                    "./scripts/raster-vite.mjs",
                    "preview" if self.preview else "dev",
                ],
                cwd=self.cwd,
                env={**self.environment, "RASTER_WEB_READY_FD": str(ready_writer)},
                start_new_session=True,
                stdin=subprocess.PIPE,
                pass_fds=(ready_writer,),
            )
        except BaseException:
            os.close(self.ready_fd)
            self.ready_fd = None
            raise
        finally:
            os.close(ready_writer)
        try:
            self.descriptor = open_pidfd(self.child.pid)
            deadline = time.monotonic() + 120
            readiness = b""
            ready = False
            while True:
                self.observe()
                if not ready and select.select([self.ready_fd], [], [], 0)[0]:
                    part = os.read(self.ready_fd, 7)
                    if part:
                        readiness += part
                        require(len(readiness) <= 6, "raster_web_readiness_invalid")
                    else:
                        require(readiness == b"ready\n", "raster_web_readiness_invalid")
                        os.close(self.ready_fd)
                        self.ready_fd = None
                        ready = True
                if ready and http_ready(
                    self.port, "/" if self.preview else "/e2e/fixtures/mask-worker.html"
                ):
                    return self
                require(time.monotonic() < deadline, "raster_web_start_timeout")
                time.sleep(0.1)
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise

    def observe(self):
        _track_consumer_descendants(self.child.pid, self.tracked)
        outcome = waitid_pidfd(self.descriptor, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if outcome is not None:
            error = ContractError("raster_web_exited")
            error.web_outcome = {"code": outcome.si_code, "status": outcome.si_status}
            raise error

    def close(self):
        try:
            _track_consumer_descendants(self.child.pid, self.tracked)
            requested = self.descriptor is not None and pidfd_alive(self.descriptor)
            if requested:
                self.child.stdin.write(b"close\n")
                self.child.stdin.flush()
            result = _drain_consumer(self.child, self.tracked, request_stop=False)
            if not requested or result != 0:
                error = ContractError("raster_web_exit_unsettled")
                error.web_outcome = {
                    "exit_code": result,
                    "shutdown_requested": requested,
                }
                raise error
        except BaseException:
            _unsettled_web_servers.append(self)
            raise
        else:
            self.child.stdin.close()
            if self.ready_fd is not None:
                os.close(self.ready_fd)
                self.ready_fd = None
            for descriptor in self.tracked.values():
                os.close(descriptor)
            if self.descriptor is not None:
                os.close(self.descriptor)

    def __exit__(self, _kind, failure, _traceback):
        try:
            self.close()
        except BaseException as shutdown:
            if failure is not None:
                raise builtins.BaseExceptionGroup(
                    "raster web consumer and shutdown failed", [failure, shutdown]
                ) from None
            raise
