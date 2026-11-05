"""Run-bound relay for the existing Mask fixture subprocess protocol.

The socket only carries fixture requests and responses; credentials stay in the
launcher-owned seed process.  Its private directory is not an OS security
boundary against another process running as the same user.
"""

from __future__ import annotations

import json
from pathlib import Path
import socketserver
import subprocess
import threading

from storage_s3_contract import require

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/v1/__test/seed/"
RUN_SCOPED = {
    "configure-raster-mask",
    "inject-raster-mask",
    "inject-raster-prediction",
}
TASK_SCOPED = {"advance_task", "inject-prediction"}


class RasterFixtureRelay:
    """Supervise one private seed process across Playwright worker lifetimes."""

    def __init__(self, socket_path, suite_id, entries, environment):
        self.suite_id = suite_id
        self.runs = {}
        self.tasks = {}
        self.emails = set()
        self.lock = threading.Lock()
        self.failure = None
        for entry in entries:
            self._register(entry["run_id"], entry["namespace"], entry["seed"])
        self.seed = subprocess.Popen(
            [
                str(ROOT / "apps/api/.venv/bin/python"),
                str(ROOT / "apps/web/e2e/fixtures/seed-request.py"),
            ],
            cwd=ROOT / "apps/api",
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        relay = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    binding = json.loads(self.rfile.readline())
                    require(
                        binding
                        == {
                            "operation": "bind_fixture_suite",
                            "suite_id": relay.suite_id,
                        },
                        "raster_relay_suite_mismatch",
                    )
                    while line := self.rfile.readline():
                        with relay.lock:
                            request = json.loads(line)
                            relay._validate(request)
                            relay.seed.stdin.write(line)
                            relay.seed.stdin.flush()
                            response_line = relay.seed.stdout.readline()
                            require(
                                bool(response_line), "raster_fixture_process_exited"
                            )
                            response = json.loads(response_line)
                            if (
                                request.get("path") == PREFIX + "owned"
                                and response.get("status") == 200
                            ):
                                data = request["data"]
                                relay._register(
                                    data["run_id"],
                                    data["namespace"],
                                    json.loads(response["body"]),
                                )
                        self.wfile.write(response_line)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # The caller may have failed while a request settled; the seed
                    # response was consumed before admitting the next request.
                    return
                except Exception as error:
                    relay.failure = error
                    raise

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = False
            block_on_close = True

        try:
            self.seed.stdin.write(
                json.dumps(
                    {
                        "operation": "prepare_fixture_suite",
                        "data": {"suite_id": suite_id, "family": "owned"},
                    }
                ).encode()
                + b"\n"
            )
            self.seed.stdin.flush()
            initial = self.seed.stdout.readline()
            require(
                bool(initial) and json.loads(initial)["status"] == 200,
                "raster_fixture_prepare_failed",
            )
            self.server = Server(str(socket_path), Handler)
            self.thread = threading.Thread(
                target=self.server.serve_forever, name="raster-fixture-relay"
            )
            self.thread.start()
        except BaseException:
            self.seed.stdin.close()
            self.seed.wait()
            raise

    def _register(self, run_id, namespace, seed):
        require(run_id not in self.runs, "raster_fixture_run_reused")
        project = seed["project_id"]
        tasks = set(seed["task_ids"])
        emails = {seed["admin_email"], seed["annotator_email"], seed["reviewer_email"]}
        require(not self.tasks.keys() & tasks, "raster_fixture_task_reused")
        self.runs[run_id] = {
            "namespace": namespace,
            "project": project,
            "tasks": tasks,
            "emails": emails,
        }
        self.tasks.update({task: run_id for task in tasks})
        self.emails.update(emails)

    def _validate(self, request):
        require(isinstance(request, dict), "raster_relay_invalid_request")
        if request.get("operation") == "prepare_fixture_suite":
            require(
                request.get("data") == {"suite_id": self.suite_id, "family": "owned"},
                "raster_relay_suite_mismatch",
            )
            return
        require(
            request.get("operation") is None
            and request.get("method", "POST") == "POST",
            "raster_relay_operation_forbidden",
        )
        path = request.get("path")
        require(
            isinstance(path, str) and path.startswith(PREFIX),
            "raster_relay_path_forbidden",
        )
        operation = path[len(PREFIX) :]
        data = request.get("data")
        require(isinstance(data, dict), "raster_relay_invalid_data")
        if operation in {"owned", "owned-cleanup", "raster-fault-verify", "cleanup"}:
            require(
                data.get("suite_id") == self.suite_id, "raster_relay_suite_mismatch"
            )
            if operation == "owned":
                require(
                    data.get("run_id") not in self.runs, "raster_fixture_run_reused"
                )
                return
            if operation == "cleanup":
                return
        if operation == "login":
            require(data.get("email") in self.emails, "raster_relay_email_unowned")
            return
        require(
            operation
            in RUN_SCOPED | TASK_SCOPED | {"owned-cleanup", "raster-fault-verify"},
            "raster_relay_operation_forbidden",
        )
        run_id = data.get("run_id")
        if operation in TASK_SCOPED:
            run_id = self.tasks.get(data.get("task_id"))
        run = self.runs.get(run_id)
        require(run is not None, "raster_relay_run_unowned")
        if "namespace" in data:
            require(
                data["namespace"] == run["namespace"], "raster_relay_namespace_mismatch"
            )
        if "task_id" in data:
            require(data["task_id"] in run["tasks"], "raster_relay_task_mismatch")
        if "project_id" in data:
            require(
                data["project_id"] == run["project"], "raster_relay_project_mismatch"
            )
        for key in ("user_email", "annotator_email", "reviewer_email"):
            if data.get(key) is not None:
                require(data[key] in run["emails"], "raster_relay_email_mismatch")

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.seed.stdin.close()
        status = self.seed.wait()
        self.seed.stdout.close()
        require(status == 0, "raster_fixture_process_failed")
        if self.failure is not None:
            raise RuntimeError("raster_fixture_relay_failed") from self.failure
