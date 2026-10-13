"""Recovery policy tests use fake Docker state; never touch live services."""

import argparse
import copy
import importlib.util
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "recovery", Path(__file__).with_name("production-health-recover.py")
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
API_RESPONDS = m.api_responds


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = argparse.Namespace(
            project_dir=self.root,
            state_dir=self.root / "state",
            ca_file=self.root / "ca.crt",
            check=False,
        )
        self.containers = {}
        for service in m.SERVICES:
            self.containers[service] = {
                "Id": service + "-id",
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": m.PROJECT,
                        "com.docker.compose.service": service,
                        "com.docker.compose.oneoff": "False",
                        "com.docker.compose.project.working_dir": str(self.root),
                        "com.docker.compose.project.config_files": str(
                            self.root / "docker-compose.lan-prod.yml"
                        ),
                    }
                },
                "State": {
                    "Status": "running",
                    "StartedAt": "original",
                    "Health": {"Status": "healthy"},
                },
            }
        self.mutations = []
        self.now = 10000
        for name, replacement in (
            ("docker", self.docker),
            ("ingress_ok", lambda *args: True),
            ("api_responds", lambda *args: True),
        ):
            patcher = patch.object(m, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(m.ssl, "create_default_context")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(m.time, "time", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def docker(self, *args, **kwargs):
        if args[0] == "ps":
            return "\n".join(c["Id"] for c in self.containers.values())
        if args[0] == "inspect":
            return json.dumps(
                [next(c for c in self.containers.values() if c["Id"] == args[1])]
            )
        self.mutations.append(args)
        return ""

    def runs(self, count=3):
        result = None
        for _ in range(count):
            result = m.run(self.args)
            self.now += 120
        return result

    def test_healthy_is_noop(self):
        self.assertEqual(self.runs(), 0)
        self.assertEqual(self.mutations, [])

    def test_three_failures_only_target_production_id(self):
        self.containers["web"]["State"]["Health"]["Status"] = "unhealthy"
        self.runs(2)
        self.assertEqual(self.mutations, [])
        self.runs(1)
        self.assertEqual(self.mutations, [("restart", "--time", "60", "web-id")])
        self.runs(5)
        self.assertEqual(len(self.mutations), 1)

    def test_dependency_503_does_not_restart_api(self):
        self.containers["api"]["State"]["Health"]["Status"] = "unhealthy"
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.mutations, [])

    def test_database_timeout_does_not_restart_responsive_api(self):
        self.containers["api"]["State"]["Health"]["Status"] = "unhealthy"

        def docker(*args, **kwargs):
            if args[0] == "exec":
                if "/health/db" in args[-1]:
                    raise subprocess.CalledProcessError(1, args)
                return ""
            return self.docker(*args, **kwargs)

        with (
            patch.object(m, "api_responds", API_RESPONDS),
            patch.object(m, "docker", docker),
        ):
            self.runs()
        self.assertEqual(self.mutations, [])

    def test_unresponsive_api_restarts(self):
        self.containers["api"]["State"]["Health"]["Status"] = "unhealthy"
        with patch.object(m, "api_responds", return_value=False):
            self.runs()
        self.assertEqual(self.mutations, [("restart", "--time", "60", "api-id")])

    def test_stopped_service_starts_without_recreate(self):
        self.containers["celery-beat"]["State"]["Status"] = "exited"
        self.runs()
        self.assertEqual(self.mutations, [("start", "celery-beat-id")])

    def test_missing_service_refuses_all_mutations(self):
        del self.containers["web"]
        with self.assertRaisesRegex(RuntimeError, "Missing"):
            self.runs()
        self.assertEqual(self.mutations, [])

    def test_wrong_checkout_refuses(self):
        self.containers["api"]["Config"]["Labels"][
            "com.docker.compose.project.working_dir"
        ] = "/development"
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.runs()
        self.assertEqual(self.mutations, [])

    def test_wrong_project_refuses(self):
        self.containers["api"]["Config"]["Labels"]["com.docker.compose.project"] = (
            "ai-annotation-platform"
        )
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.runs()
        self.assertEqual(self.mutations, [])

    def test_maintenance_pauses(self):
        self.args.state_dir.mkdir()
        (self.args.state_dir / "maintenance").touch()
        self.containers["web"]["State"]["Status"] = "exited"
        self.assertEqual(self.runs(), 0)
        self.assertEqual(self.mutations, [])

    def test_gateway_failure_only_restarts_gateway(self):
        with patch.object(m, "ingress_ok", return_value=False):
            self.runs()
        self.assertEqual(self.mutations, [("restart", "--time", "60", "gateway-id")])

    def test_starting_and_paused_are_not_restarted(self):
        self.containers["api"]["State"]["Health"]["Status"] = "starting"
        self.containers["web"]["State"]["Status"] = "paused"
        self.runs()
        self.assertEqual(self.mutations, [])

    def test_read_only_does_not_accumulate_failures(self):
        self.args.check = True
        self.containers["web"]["State"]["Status"] = "exited"
        self.runs(4)
        self.assertEqual(self.mutations, [])
        self.assertFalse((self.args.state_dir / "health-state.json").exists())

    def test_interrupted_observations_reset_threshold(self):
        self.containers["web"]["State"]["Status"] = "exited"
        self.runs(2)
        self.now += 600
        self.runs(1)
        self.assertEqual(self.mutations, [])

    def test_replacement_resets_threshold(self):
        self.containers["web"]["State"]["Status"] = "exited"
        self.runs(2)
        self.containers["web"]["Id"] = "new-web-id"
        self.runs(1)
        self.assertEqual(self.mutations, [])

    def test_only_one_recovery_per_run(self):
        for s in ("web", "celery-beat"):
            self.containers[s]["State"]["Status"] = "exited"
        self.runs()
        self.assertEqual(len(self.mutations), 1)

    def test_success_between_failures_resets_threshold(self):
        self.containers["web"]["State"]["Health"]["Status"] = "unhealthy"
        self.runs(2)
        self.containers["web"]["State"]["Health"]["Status"] = "healthy"
        self.runs(1)
        self.containers["web"]["State"]["Health"]["Status"] = "unhealthy"
        self.runs(1)
        self.assertEqual(self.mutations, [])

    def test_hourly_budget_survives_restarts(self):
        self.containers["web"]["State"]["Health"]["Status"] = "unhealthy"
        self.runs(30)
        self.assertEqual(len(self.mutations), 3)

    def test_broker_fault_suppresses_worker_restart(self):
        self.containers["redis-production"]["State"]["Health"]["Status"] = "unhealthy"
        self.containers["celery-worker"]["State"]["Health"]["Status"] = "unhealthy"
        action, reason = m.classify(
            "celery-worker",
            self.containers["celery-worker"],
            self.containers,
            self.args.ca_file,
        )
        self.assertIsNone(action)
        self.assertIn("broker unhealthy", reason)

    def test_state_change_before_mutation_defers(self):
        self.containers["web"]["State"]["Health"]["Status"] = "unhealthy"
        self.runs(2)
        original = m.inspect
        calls = 0

        def inspect(cid):
            nonlocal calls
            result = original(cid)
            if cid == "web-id":
                calls += 1
                if calls > 1:
                    result["State"]["Health"]["Status"] = "healthy"
            return result

        with patch.object(m, "inspect", inspect):
            self.runs(1)
        self.assertEqual(self.mutations, [])

    def test_oneoff_migration_ignored(self):
        migration = copy.deepcopy(self.containers["api"])
        migration["Id"] = "migration-id"
        migration["Config"]["Labels"].update(
            {
                "com.docker.compose.service": "migrate",
                "com.docker.compose.oneoff": "True",
            }
        )
        self.containers["migrate"] = migration
        self.assertEqual(self.runs(), 0)
        self.assertEqual(self.mutations, [])


if __name__ == "__main__":
    unittest.main()
