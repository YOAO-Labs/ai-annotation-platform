"""Run-scoped Mask fixture authorization across failed owned creation."""

import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

import raster_fixture_relay
from raster_fixture_relay import PREFIX, RasterFixtureRelay
from storage_s3_contract import ContractError

SUITE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RUN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NAMESPACE = "ns01"
PROJECT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
TASK = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
SEED = {
    "project_id": PROJECT,
    "task_ids": [TASK],
    "admin_email": "admin@example.invalid",
    "annotator_email": "annotator@example.invalid",
    "reviewer_email": "reviewer@example.invalid",
    "ml_backend_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
}


class RasterFixtureIntentTests(unittest.TestCase):
    def setUp(self):
        self.relay = RasterFixtureRelay.__new__(RasterFixtureRelay)
        self.relay.suite_id = SUITE
        self.relay.intents = {}
        self.relay.prepared = set()
        self.relay.operations = {}
        self.relay.runs = {}
        self.relay.tasks = {}
        self.relay.emails = set()
        self.scope = {"suite_id": SUITE, "run_id": RUN, "namespace": NAMESPACE}

    def request(self, operation, data):
        self.relay._validate({"path": PREFIX + operation, "data": data})

    def test_failed_owned_intent_allows_only_exact_native_cleanup(self):
        self.request("owned", self.scope)
        self.request("owned-cleanup", self.scope)
        with self.assertRaises(ContractError):
            self.request("owned-cleanup", {**self.scope, "namespace": "other"})
        with self.assertRaises(ContractError):
            self.request("owned-cleanup", {**self.scope, "suite_id": "other"})
        with self.assertRaises(ContractError):
            self.request("inject-raster-mask", {"run_id": RUN, "task_id": TASK})
        with self.assertRaises(ContractError):
            self.request("login", {"email": SEED["annotator_email"]})

    def test_invalid_success_body_does_not_publish_task_authority(self):
        self.request("owned", self.scope)
        with self.assertRaises(ContractError):
            self.relay._register(RUN, NAMESPACE, {**SEED, "task_ids": ["not-a-uuid"]})
        self.request("owned-cleanup", self.scope)
        with self.assertRaises(ContractError):
            self.request("inject-raster-mask", {"run_id": RUN, "task_id": TASK})

    def test_successful_owned_response_publishes_only_its_scope(self):
        self.request("owned", self.scope)
        self.relay._register(RUN, NAMESPACE, SEED)
        self.request("login", {"email": SEED["annotator_email"]})
        self.request(
            "inject-raster-mask",
            {"run_id": RUN, "task_id": TASK, "user_email": SEED["annotator_email"]},
        )
        with self.assertRaises(ContractError):
            self.request("inject-raster-mask", {"run_id": RUN, "task_id": "foreign"})

    def test_image_preparation_cannot_escape_owned_project_or_actor(self):
        self.request("owned", self.scope)
        self.relay._register(RUN, NAMESPACE, SEED)
        request = {
            "run_id": RUN,
            "project_id": PROJECT,
            "user_email": SEED["annotator_email"],
        }
        self.request("prepare-raster-images", request)
        for changed in (
            {"run_id": "ffffffff-ffff-4fff-8fff-ffffffffffff"},
            {"project_id": "ffffffff-ffff-4fff-8fff-ffffffffffff"},
            {"user_email": "foreign@example.invalid"},
        ):
            with self.assertRaises(ContractError):
                self.request("prepare-raster-images", {**request, **changed})


class RasterFixtureShutdownTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="raster-relay-test-")
        self.addCleanup(self.directory.cleanup)
        self.ready_read, ready_write = os.pipe()
        release_read, self.release_write = os.pipe()
        self.addCleanup(os.close, self.ready_read)
        self.addCleanup(os.close, self.release_write)
        self.failures = []
        command = (
            "import json, os, sys\n"
            "bootstrap = len(sys.argv) > 1\n"
            "for line in sys.stdin:\n"
            " request = json.loads(line)\n"
            " if not bootstrap and request['action'] == 'execute':\n"
            f"  os.write({ready_write}, b'1')\n"
            f"  os.read({release_read}, 1)\n"
            " payload = request.get('payload', {})\n"
            " reply = {'operation_id':request['operation_id']}\n"
            " if request['action'] == 'ack': reply['state'] = 'acknowledged'\n"
            " elif payload.get('path','').endswith('/login'): reply.update(status=422,body=json.dumps({'detail':{'code':'fixture_scope_changed','sql':'private-secret-and-sql'}}))\n"
            " else: reply.update(status=200,body='{}')\n"
            " print(json.dumps(reply), flush=True)\n"
        )
        real_popen = subprocess.Popen
        launches = 0

        def launch(_command, **kwargs):
            nonlocal launches
            launches += 1
            kwargs["pass_fds"] = (ready_write, release_read)
            return real_popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    command,
                    *(["bootstrap"] if launches == 1 else []),
                ],
                **kwargs,
            )

        patcher = patch.object(
            raster_fixture_relay.subprocess, "Popen", side_effect=launch
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(os.close, ready_write)
        self.addCleanup(os.close, release_read)
        self.relay = RasterFixtureRelay(
            Path(self.directory.name) / "fixture.sock",
            SUITE,
            [{"run_id": RUN, "namespace": NAMESPACE, "seed": SEED}],
            dict(os.environ),
            lambda request, response: self.failures.append((request, response)),
            lambda operation_id: self.assertTrue(operation_id),
        )
        self.addCleanup(self.stop_seed)
        self.client = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.client.close)
        self.client.connect(str(Path(self.directory.name) / "fixture.sock"))
        self.client.sendall(
            (
                json.dumps({"operation": "bind_fixture_suite", "suite_id": SUITE})
                + "\n"
            ).encode()
        )
        with self.relay.connection_condition:
            self.assertTrue(
                self.relay.connection_condition.wait_for(
                    lambda: len(self.relay.connections) == 1, timeout=2
                )
            )
        self.stream = self.client.makefile("rb")
        self.addCleanup(self.stream.close)

    def execute(self, payload):
        operation_id = str(uuid.uuid4())
        envelope = {
            "action": "execute",
            "operation_id": operation_id,
            "payload": payload,
        }
        self.client.sendall((json.dumps(envelope) + "\n").encode())
        return operation_id

    def acknowledge(self, operation_id):
        self.client.sendall(
            (
                json.dumps({"action": "ack", "operation_id": operation_id}) + "\n"
            ).encode()
        )
        self.assertEqual(json.loads(self.stream.readline())["state"], "acknowledged")

    def stop_seed(self):
        if self.relay.seed.poll() is None:
            try:
                os.write(self.release_write, b"1")
            except OSError:
                pass
            self.relay.seed.terminate()
            self.relay.seed.wait(timeout=2)

    def test_non_200_is_recorded_privately_and_response_is_redacted(self):
        request = {"path": PREFIX + "login", "data": {"email": SEED["admin_email"]}}
        operation_id = self.execute(request)
        self.assertTrue(select.select([self.ready_read], [], [], 2)[0])
        self.assertEqual(os.read(self.ready_read, 1), b"1")
        os.write(self.release_write, b"1")
        response = json.loads(self.stream.readline())
        self.assertEqual(response["status"], 422)
        self.assertNotIn("private-secret-and-sql", response["body"])
        self.assertEqual(
            json.loads(response["body"]), {"detail": {"code": "fixture_scope_changed"}}
        )
        self.acknowledge(operation_id)
        self.assertEqual(
            self.failures,
            [
                (
                    request,
                    {
                        "operation_id": operation_id,
                        "status": 422,
                        "body": json.dumps(
                            {
                                "detail": {
                                    "code": "fixture_scope_changed",
                                    "sql": "private-secret-and-sql",
                                }
                            }
                        ),
                    },
                )
            ],
        )
        self.relay.close(timeout=1)

    def test_idle_open_socket_is_closed_without_hanging(self):
        started = time.monotonic()
        self.relay.close(timeout=1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.client.recv(1), b"")

    def test_settled_work_drains_before_closing_the_socket(self):
        request = {
            "operation": "prepare_fixture_suite",
            "data": {"suite_id": SUITE, "family": "owned"},
        }
        operation_id = self.execute(request)
        self.assertTrue(select.select([self.ready_read], [], [], 2)[0])
        self.assertEqual(os.read(self.ready_read, 1), b"1")
        errors = []

        def close_relay():
            try:
                self.relay.close(timeout=2)
            except BaseException as error:
                errors.append(error)

        closer = threading.Thread(target=close_relay, daemon=True)
        closer.start()
        with self.relay.connection_condition:
            self.assertTrue(
                self.relay.connection_condition.wait_for(
                    lambda: self.relay.closing, timeout=2
                )
            )
            self.assertEqual(self.relay.inflight, 1)
            self.assertEqual(len(self.relay.connections), 1)
        os.write(self.release_write, b"1")
        self.assertEqual(
            json.loads(self.stream.readline())["operation_id"], operation_id
        )
        self.acknowledge(operation_id)
        closer.join(timeout=3)
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(self.relay.unsettled)

    def test_lost_reply_remains_recoverable_through_relay(self):
        operation_id = self.execute(
            {
                "operation": "prepare_fixture_suite",
                "data": {"suite_id": SUITE, "family": "owned"},
            }
        )
        self.assertTrue(select.select([self.ready_read], [], [], 2)[0])
        self.assertEqual(os.read(self.ready_read, 1), b"1")
        self.client.shutdown(socket.SHUT_RDWR)
        os.write(self.release_write, b"1")
        with self.relay.connection_condition:
            self.assertTrue(
                self.relay.connection_condition.wait_for(
                    lambda: self.relay.inflight == 0, timeout=2
                )
            )
        snapshots = []
        with self.assertRaisesRegex(ContractError, "raster_fixture_relay_unsettled"):
            self.relay.close(
                timeout=0.2,
                on_unsettled=lambda reason, snapshot: snapshots.append(snapshot),
            )
        self.assertEqual(
            snapshots[0]["operations"],
            [
                {
                    "operation_id": operation_id,
                    "response_state": "completed",
                    "acknowledgement": "unknown",
                }
            ],
        )
        with socket.socket(socket.AF_UNIX) as recovery:
            recovery.connect(str(Path(self.directory.name) / "fixture.sock"))
            recovery.sendall(
                (
                    json.dumps(
                        {
                            "operation": "bind_fixture_suite",
                            "suite_id": SUITE,
                        }
                    )
                    + "\n"
                ).encode()
            )
            recovery.sendall(
                (
                    json.dumps(
                        {
                            "action": "recover",
                            "operation_id": operation_id,
                        }
                    )
                    + "\n"
                ).encode()
            )
            with recovery.makefile("rb") as replies:
                self.assertEqual(
                    json.loads(replies.readline())["operation_id"], operation_id
                )
                recovery.sendall(
                    (
                        json.dumps(
                            {
                                "action": "ack",
                                "operation_id": operation_id,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                self.assertEqual(
                    json.loads(replies.readline())["state"], "acknowledged"
                )
        self.relay.close(timeout=2)

    def test_inflight_work_is_recorded_before_socket_teardown(self):
        request = {
            "operation": "prepare_fixture_suite",
            "data": {"suite_id": SUITE, "family": "owned"},
        }
        operation_id = self.execute(request)
        self.assertTrue(select.select([self.ready_read], [], [], 2)[0])
        self.assertEqual(os.read(self.ready_read, 1), b"1")
        recorded = []

        def on_unsettled(reason, snapshot):
            recorded.append(
                (
                    reason,
                    snapshot,
                    self.relay.server.socket.fileno() >= 0,
                )
            )

        started = time.monotonic()
        with self.assertRaisesRegex(ContractError, "raster_fixture_relay_unsettled"):
            self.relay.close(timeout=0.2, on_unsettled=on_unsettled)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(
            recorded,
            [
                (
                    "raster_fixture_relay_unsettled",
                    {
                        "inflight": 1,
                        "connections": 1,
                        "clients": 1,
                        "operations": [
                            {
                                "operation_id": operation_id,
                                "response_state": "unknown",
                                "acknowledgement": "unknown",
                            }
                        ],
                    },
                    True,
                )
            ],
        )
        self.assertIsNone(self.relay.seed.poll())
        os.write(self.release_write, b"1")
        self.assertEqual(
            json.loads(self.stream.readline())["operation_id"], operation_id
        )
        self.acknowledge(operation_id)
        self.relay.close(timeout=2)


if __name__ == "__main__":
    unittest.main()
