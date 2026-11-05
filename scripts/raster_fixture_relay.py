"""Run-bound relay for the existing Mask fixture subprocess protocol.

The socket only carries fixture requests and responses; credentials stay in the
launcher-owned seed process.  Its private directory is not an OS security
boundary against another process running as the same user.
"""

from __future__ import annotations
import builtins

import json
import re
from pathlib import Path
import socket
import socketserver
import subprocess
import threading
import time
from uuid import UUID

from storage_s3_contract import ContractError, require

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
        self.intents = {}
        self.tasks = {}
        self.emails = set()
        self.lock = threading.Lock()
        self.connections = set()
        self.connection_condition = threading.Condition()
        self.inflight = 0
        self.unsettled = False
        self.stopping = False
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
                seed_operation = False
                try:
                    binding_line = self.rfile.readline()
                    if not binding_line and relay.stopping:
                        return
                    binding = json.loads(binding_line)
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
                            with relay.connection_condition:
                                if relay.stopping:
                                    return
                                relay.inflight += 1
                            try:
                                request = json.loads(line)
                                relay._validate(request)
                                seed_operation = True
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
                                seed_operation = False
                            except BaseException as error:
                                if seed_operation:
                                    with relay.connection_condition:
                                        relay.failure = error
                                        relay.unsettled = True
                                raise
                            finally:
                                with relay.connection_condition:
                                    if seed_operation:
                                        relay.unsettled = True
                                    relay.inflight -= 1
                                    relay.connection_condition.notify_all()
                        self.wfile.write(response_line)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError) as error:
                    if seed_operation:
                        relay.failure = error
                    elif not relay.stopping and not isinstance(
                        error, (BrokenPipeError, ConnectionResetError)
                    ):
                        relay.failure = error
                    return
                except Exception as error:
                    relay.failure = error
                    raise

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            block_on_close = False

            def get_request(self):
                request, address = super().get_request()
                with relay.connection_condition:
                    relay.connections.add(request)
                    relay.connection_condition.notify_all()
                    if relay.stopping:
                        try:
                            request.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                return request, address

            def close_request(self, request):
                try:
                    super().close_request(request)
                finally:
                    with relay.connection_condition:
                        relay.connections.discard(request)
                        relay.connection_condition.notify_all()

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
                target=self.server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="raster-fixture-relay",
                daemon=True,
            )
            self.thread.start()
        except BaseException:
            self.seed.stdin.close()
            self.seed.wait()
            raise

    @staticmethod
    def _valid_uuid(value):
        try:
            return isinstance(value, str) and str(UUID(value)) == value
        except ValueError:
            return False

    @classmethod
    def _identity(cls, run_id, namespace):
        require(
            cls._valid_uuid(run_id)
            and isinstance(namespace, str)
            and re.fullmatch(r"[a-z0-9]{4,12}", namespace),
            "raster_fixture_identity_invalid",
        )

    def _register(self, run_id, namespace, seed):
        self._identity(run_id, namespace)
        require(run_id not in self.runs, "raster_fixture_run_reused")
        require(
            self.intents.get(run_id, namespace) == namespace,
            "raster_relay_namespace_mismatch",
        )
        require(
            isinstance(seed, dict)
            and set(seed)
            == {
                "project_id",
                "task_ids",
                "admin_email",
                "annotator_email",
                "reviewer_email",
                "ml_backend_id",
            },
            "raster_fixture_seed_invalid",
        )
        project = seed["project_id"]
        task_ids = seed["task_ids"]
        values = (seed["admin_email"], seed["annotator_email"], seed["reviewer_email"])
        require(
            self._valid_uuid(project)
            and self._valid_uuid(seed["ml_backend_id"])
            and isinstance(task_ids, list)
            and bool(task_ids)
            and all(self._valid_uuid(task) for task in task_ids)
            and all(isinstance(email, str) and email for email in values),
            "raster_fixture_seed_invalid",
        )
        tasks = set(task_ids)
        emails = set(values)
        require(
            len(tasks) == len(task_ids) and not self.tasks.keys() & tasks,
            "raster_fixture_task_reused",
        )
        self.intents[run_id] = namespace
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
                run_id, namespace = data.get("run_id"), data.get("namespace")
                self._identity(run_id, namespace)
                require(run_id not in self.intents, "raster_fixture_run_reused")
                # Retain the exact attempted scope even when the native owner
                # commits an admission and then returns a failed response.
                self.intents[run_id] = namespace
                return
            if operation == "owned-cleanup":
                run_id, namespace = data.get("run_id"), data.get("namespace")
                self._identity(run_id, namespace)
                require(
                    self.intents.get(run_id) == namespace, "raster_relay_run_unowned"
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

    def close(self, timeout=4, on_unsettled=None):
        """Fence admission and settle native work before closing client sockets."""
        deadline = time.monotonic() + timeout
        journal_failure = None

        def fail_shutdown(code):
            failures = [ContractError(code)]
            if self.failure is not None:
                failures.insert(0, self.failure)
            if journal_failure is not None:
                failures.append(journal_failure)
            if len(failures) > 1:
                raise builtins.BaseExceptionGroup(
                    "raster fixture and shutdown failures", failures
                )
            raise failures[0]

        with self.connection_condition:
            self.stopping = True
            self.connection_condition.notify_all()
        shutdown = threading.Thread(target=self.server.shutdown, daemon=True)
        shutdown.start()
        shutdown.join(max(0, deadline - time.monotonic()))
        with self.connection_condition:
            while self.inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.connection_condition.wait(remaining)
            self.unsettled = self.unsettled or bool(self.inflight)
        if self.unsettled and on_unsettled is not None:
            try:
                on_unsettled("raster_fixture_relay_unsettled")
            except BaseException as error:
                journal_failure = error
        self.server.server_close()
        with self.connection_condition:
            for connection in tuple(self.connections):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            while self.connections:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.connection_condition.wait(remaining)
            settled = not self.connections
        if shutdown.is_alive() or not settled or self.unsettled:
            fail_shutdown("raster_fixture_relay_unsettled")
        self.thread.join(max(0, deadline - time.monotonic()))
        if self.thread.is_alive():
            fail_shutdown("raster_fixture_relay_unsettled")
        self.seed.stdin.close()
        try:
            status = self.seed.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            fail_shutdown("raster_fixture_process_unsettled")
        self.seed.stdout.close()
        if status != 0:
            fail_shutdown("raster_fixture_process_failed")
        if self.failure is not None:
            raise RuntimeError("raster_fixture_relay_failed") from self.failure
