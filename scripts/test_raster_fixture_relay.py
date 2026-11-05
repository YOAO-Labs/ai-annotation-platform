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


class RasterFixtureShutdownTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="raster-relay-test-")
        self.addCleanup(self.directory.cleanup)
        self.ready_read, ready_write = os.pipe()
        release_read, self.release_write = os.pipe()
        self.addCleanup(os.close, self.ready_read)
        self.addCleanup(os.close, self.release_write)
        command = (
            "import json, os, sys\n"
            "first = True\n"
            "for line in sys.stdin:\n"
            " if first: first = False\n"
            " else:\n"
            f"  os.write({ready_write}, b'1')\n"
            f"  os.read({release_read}, 1)\n"
            " print(json.dumps({'status':200,'body':'{}'}), flush=True)\n"
        )
        real_popen = subprocess.Popen

        def launch(_command, **kwargs):
            kwargs["pass_fds"] = (ready_write, release_read)
            return real_popen([sys.executable, "-u", "-c", command], **kwargs)

        try:
            with patch.object(
                raster_fixture_relay.subprocess, "Popen", side_effect=launch
            ):
                self.relay = RasterFixtureRelay(
                    Path(self.directory.name) / "fixture.sock",
                    SUITE,
                    [],
                    dict(os.environ),
                )
        finally:
            os.close(ready_write)
            os.close(release_read)
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

    def stop_seed(self):
        if self.relay.seed.poll() is None:
            try:
                os.write(self.release_write, b"1")
            except OSError:
                pass
            self.relay.seed.terminate()
            self.relay.seed.wait(timeout=2)

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
        self.client.sendall((json.dumps(request) + "\n").encode())
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
                    lambda: self.relay.stopping, timeout=2
                )
            )
            self.assertEqual(self.relay.inflight, 1)
            self.assertEqual(len(self.relay.connections), 1)
        os.write(self.release_write, b"1")
        closer.join(timeout=3)
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(self.relay.unsettled)

    def test_inflight_work_is_recorded_before_socket_teardown(self):
        request = {
            "operation": "prepare_fixture_suite",
            "data": {"suite_id": SUITE, "family": "owned"},
        }
        self.client.sendall((json.dumps(request) + "\n").encode())
        self.assertTrue(select.select([self.ready_read], [], [], 2)[0])
        self.assertEqual(os.read(self.ready_read, 1), b"1")
        recorded = []

        def on_unsettled(reason):
            recorded.append(
                (
                    reason,
                    self.relay.inflight,
                    len(self.relay.connections),
                    self.relay.server.socket.fileno() >= 0,
                )
            )

        started = time.monotonic()
        with self.assertRaisesRegex(ContractError, "raster_fixture_relay_unsettled"):
            self.relay.close(timeout=0.2, on_unsettled=on_unsettled)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(recorded, [("raster_fixture_relay_unsettled", 1, 1, True)])
        self.assertIsNone(self.relay.seed.poll())
        os.write(self.release_write, b"1")
        with self.relay.connection_condition:
            self.assertTrue(
                self.relay.connection_condition.wait_for(
                    lambda: not self.relay.connections, timeout=2
                )
            )
        self.relay.seed.stdin.close()
        self.assertEqual(self.relay.seed.wait(timeout=2), 0)
        self.relay.seed.stdout.close()


if __name__ == "__main__":
    unittest.main()
