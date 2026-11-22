"""Run-bound relay for the existing Mask fixture subprocess protocol.

The socket carries only fixture requests and responses. The launcher-owned seed
client reconnects to the same private authority that prepared the fixtures.
Neither socket is a security boundary against another process with the same UID.
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
from uuid import UUID, uuid4

from storage_s3_contract import ContractError, require

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/v1/__test/seed/"
RUN_SCOPED = {
    "prepare-raster-images",
    "configure-raster-mask",
    "inject-raster-mask",
    "inject-raster-prediction",
}
TASK_SCOPED = {"advance_task", "inject-prediction"}


def safe_failure_body(body):
    try:
        detail = json.loads(body).get("detail")
        if isinstance(detail, dict):
            for key in ("code", "reason"):
                value = detail.get(key)
                if isinstance(value, str) and re.fullmatch(
                    r"[a-z][a-z0-9_]{0,79}", value
                ):
                    return json.dumps({"detail": {key: value}})
    except (ValueError, AttributeError):
        pass
    return json.dumps({"detail": "Fixture operation failed; see private owner journal"})


class RasterFixtureRelay:
    """Supervise one private seed client across Playwright worker lifetimes."""

    def __init__(
        self,
        socket_path,
        suite_id,
        entries,
        environment,
        on_seed_failure,
        on_seed_intent,
    ):
        self.suite_id = suite_id
        self.runs = {}
        self.intents = {}
        self.prepared = set()
        self.tasks = {}
        self.emails = set()
        self.lock = threading.Lock()
        self.connections = set()
        self.connection_condition = threading.Condition()
        self.inflight = 0
        self.unsettled = False
        self.closing = False
        self.operations = {}
        self.clients = set()
        self.stopping = False
        self.failure = None
        self.on_seed_failure = on_seed_failure
        for entry in entries:
            self._register(entry["run_id"], entry["namespace"], entry["seed"])

        def start_client():
            return subprocess.Popen(
                [
                    str(ROOT / "apps/api/.venv/bin/python"),
                    str(ROOT / "apps/web/e2e/fixtures/seed-request.py"),
                ],
                cwd=ROOT / "apps/api",
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )

        self.seed = start_client()
        relay = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                client = None
                executing = False
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
                    client = start_client()
                    with relay.connection_condition:
                        relay.clients.add(client)
                    while line := self.rfile.readline():
                        request = json.loads(line)
                        operation_id = request.get("operation_id")
                        action = request.get("action")
                        require(
                            relay._valid_uuid(operation_id),
                            "raster_relay_operation_id_invalid",
                        )
                        require(
                            action in {"execute", "recover", "ack"}
                            and set(request)
                            == (
                                {"action", "operation_id", "payload"}
                                if action == "execute"
                                else {"action", "operation_id"}
                            ),
                            "raster_relay_invalid_envelope",
                        )
                        with relay.connection_condition:
                            if relay.stopping:
                                return
                            relay.inflight += 1
                        try:
                            with relay.lock:
                                entry = relay.operations.get(operation_id)
                                if action == "execute":
                                    payload = request["payload"]
                                    require(
                                        relay.failure is None, "raster_relay_fenced"
                                    )
                                    if relay.closing:
                                        require(
                                            isinstance(payload, dict)
                                            and payload.get("path")
                                            in {
                                                PREFIX + "owned-cleanup",
                                                PREFIX + "cleanup",
                                            }
                                            and not any(
                                                not item["acknowledged"]
                                                for item in relay.operations.values()
                                            ),
                                            "raster_relay_fenced",
                                        )
                                    if entry is None:
                                        relay._validate(payload)
                                        entry = {
                                            "payload": payload,
                                            "acknowledged": False,
                                            "registered": False,
                                            "response_state": "unknown",
                                        }
                                        relay.operations[operation_id] = entry
                                    else:
                                        require(
                                            entry["payload"] == payload,
                                            "raster_relay_operation_changed",
                                        )
                                else:
                                    require(
                                        entry is not None,
                                        "raster_relay_operation_unowned",
                                    )
                                payload = entry["payload"]
                            executing = True
                            client.stdin.write(line)
                            client.stdin.flush()
                            response_line = client.stdout.readline()
                            require(
                                bool(response_line), "raster_fixture_process_exited"
                            )
                            response = json.loads(response_line)
                            require(
                                response.get("operation_id") == operation_id,
                                "raster_fixture_response_changed",
                            )
                            known = response.get("operation_response", response)
                            with relay.lock:
                                if response.get("state") == "pending":
                                    entry["response_state"] = "pending"
                                elif response.get("state") == "acknowledged" or (
                                    (
                                        response.get("status") is not None
                                        or response.get("owner_error")
                                    )
                                    and response.get("owner_error")
                                    not in {
                                        "OperationOutcomeUnresolved",
                                        "OperationIdentityConflict",
                                    }
                                ):
                                    entry["response_state"] = "completed"
                                if (
                                    known.get("status") is not None
                                    and known["status"] != 200
                                ) or response.get("disposal_error"):
                                    try:
                                        relay.on_seed_failure(payload, response.copy())
                                    except BaseException as error:
                                        relay.failure = error
                                        response["relay_journal_error"] = type(
                                            error
                                        ).__name__
                                if (
                                    payload.get("path") == PREFIX + "owned-prepare"
                                    and known.get("status") == 200
                                    and not response.get("owner_error")
                                    and not response.get("journal_error")
                                    and not response.get("disposal_error")
                                ):
                                    entry["preparation_succeeded"] = True
                                if (
                                    payload.get("path") == PREFIX + "owned"
                                    and known.get("status") == 200
                                    and not entry["registered"]
                                ):
                                    data = payload["data"]
                                    relay._register(
                                        data["run_id"],
                                        data["namespace"],
                                        json.loads(known["body"]),
                                    )
                                    entry["registered"] = True
                                if response.get("owner_error") in {
                                    "OperationOutcomeUnresolved",
                                    "OperationIdentityConflict",
                                }:
                                    entry["acknowledged"] = True
                                if (
                                    action == "ack"
                                    and response.get("state") == "acknowledged"
                                ):
                                    if not entry["acknowledged"] and entry.get(
                                        "preparation_succeeded"
                                    ):
                                        relay.prepared.add(payload["data"]["run_id"])
                                    entry["acknowledged"] = True
                            if (
                                response.get("status") is not None
                                and not 200 <= response["status"] < 300
                            ):
                                response["body"] = safe_failure_body(response["body"])
                            executing = False
                            self.wfile.write(json.dumps(response).encode() + b"\n")
                            self.wfile.flush()
                        except BaseException as error:
                            if executing:
                                relay.failure = error
                            raise
                        finally:
                            with relay.connection_condition:
                                relay.inflight -= 1
                                relay.connection_condition.notify_all()
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as error:
                    relay.failure = error
                finally:
                    if client is not None and not executing:
                        try:
                            client.stdin.close()
                            client.wait(timeout=4)
                            client.stdout.close()
                        except BaseException as error:
                            relay.failure = error
                        else:
                            with relay.connection_condition:
                                relay.clients.discard(client)
                                relay.connection_condition.notify_all()

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
            operation_id = str(uuid4())
            on_seed_intent(operation_id)
            initial_request = {
                "action": "execute",
                "operation_id": operation_id,
                "payload": {
                    "operation": "prepare_fixture_suite",
                    "data": {"suite_id": suite_id, "family": "owned"},
                },
            }
            self.seed.stdin.write(json.dumps(initial_request).encode() + b"\n")
            self.seed.stdin.flush()
            initial = self.seed.stdout.readline()
            require(bool(initial), "raster_fixture_process_exited")
            initial_response = json.loads(initial)
            require(
                initial_response.get("operation_id") == operation_id,
                "raster_fixture_response_changed",
            )
            if initial_response.get("status") != 200 or initial_response.get(
                "journal_error"
            ):
                known = initial_response.get("operation_response")
                causes = []
                if known is not None:
                    causes.append(ContractError(f"raster_seed_http_{known['status']}"))
                if initial_response.get("owner_error") and initial_response[
                    "owner_error"
                ] != initial_response.get("disposal_error"):
                    causes.append(
                        ContractError(
                            f"raster_fixture_prepare_{initial_response['owner_error']}"
                        )
                    )
                if initial_response.get("disposal_error"):
                    causes.append(
                        ContractError(
                            f"raster_seed_disposal_{initial_response['disposal_error']}"
                        )
                    )
                if not causes:
                    causes.append(
                        ContractError(
                            f"raster_fixture_prepare_{initial_response.get('status')}"
                        )
                    )
                if initial_response.get("journal_error"):
                    causes.append(
                        ContractError(
                            f"raster_seed_journal_{initial_response['journal_error']}"
                        )
                    )
                try:
                    self.on_seed_failure(initial_request["payload"], initial_response)
                except BaseException as error:
                    causes.append(error)
                if len(causes) > 1:
                    raise builtins.BaseExceptionGroup(
                        "raster prepare and evidence failed", causes
                    )
                raise causes[0]
            self.seed.stdin.write(
                json.dumps({"action": "ack", "operation_id": operation_id}).encode()
                + b"\n"
            )
            self.seed.stdin.flush()
            require(
                json.loads(self.seed.stdout.readline()).get("state") == "acknowledged",
                "raster_fixture_prepare_unacknowledged",
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
        if operation in {
            "owned",
            "owned-prepare",
            "owned-cleanup",
            "raster-fault-verify",
            "cleanup",
        }:
            require(
                data.get("suite_id") == self.suite_id, "raster_relay_suite_mismatch"
            )
            if operation in {"owned", "owned-prepare"}:
                run_id, namespace = data.get("run_id"), data.get("namespace")
                self._identity(run_id, namespace)
                if operation == "owned" and run_id in self.prepared:
                    require(
                        self.intents.get(run_id) == namespace,
                        "raster_relay_namespace_mismatch",
                    )
                    self.prepared.remove(run_id)
                else:
                    require(run_id not in self.intents, "raster_fixture_run_reused")
                    self.intents[run_id] = namespace
                return
            if operation == "owned-cleanup":
                run_id, namespace = data.get("run_id"), data.get("namespace")
                self._identity(run_id, namespace)
                require(
                    self.intents.get(run_id) == namespace, "raster_relay_run_unowned"
                )
                require(
                    not any(
                        not entry["acknowledged"]
                        and (
                            entry["payload"].get("data", {}).get("run_id")
                            or self.tasks.get(
                                entry["payload"].get("data", {}).get("task_id")
                            )
                        )
                        == run_id
                        for entry in self.operations.values()
                    ),
                    "raster_relay_run_unsettled",
                )
                self.prepared.discard(run_id)
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

        def outstanding():
            with self.lock:
                return any(
                    not entry["acknowledged"] for entry in self.operations.values()
                )

        with self.connection_condition:
            self.closing = True
            self.connection_condition.notify_all()
            while self.inflight or outstanding():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.connection_condition.wait(remaining)
            self.unsettled = bool(self.inflight or outstanding() or self.failure)
            with self.lock:
                snapshot = {
                    "inflight": self.inflight,
                    "connections": len(self.connections),
                    "clients": len(self.clients),
                    "operations": [
                        {
                            "operation_id": operation_id,
                            "response_state": entry["response_state"],
                            "acknowledgement": "unknown",
                        }
                        for operation_id, entry in self.operations.items()
                        if not entry["acknowledged"]
                    ],
                }
        if self.unsettled and on_unsettled is not None:
            try:
                on_unsettled("raster_fixture_relay_unsettled", snapshot)
            except BaseException as error:
                journal_failure = error
        if self.unsettled:
            fail_shutdown("raster_fixture_relay_unsettled")
        with self.connection_condition:
            self.stopping = True
            self.connection_condition.notify_all()
        shutdown = threading.Thread(target=self.server.shutdown, daemon=True)
        shutdown.start()
        shutdown.join(max(0, deadline - time.monotonic()))
        self.server.server_close()
        with self.connection_condition:
            for connection in tuple(self.connections):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            while self.connections or self.clients:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.connection_condition.wait(remaining)
            settled = not self.connections and not self.clients
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
