"""Raster cleanup never forces Redis or drops its database on uncertain exit."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import worktree_resources as resources


class RasterRedisRetirementTests(unittest.TestCase):
    def setUp(self):
        self.backend = resources.LocalResources.__new__(resources.LocalResources)
        self.backend.resources = {"redis_name": "owned-redis", "owner": "owned"}
        self.prepared = resources._WorktreeDestroyPreparation(self.backend, None, None)
        self.prepared.storage = "raster_provider_retired"
        self.backend._destroy_preparation = self.prepared
        self.owner = SimpleNamespace(data={"raster": {}}, save=lambda: None)
        self.prepared.raster_owner = self.owner
        self.identifier = "e" * 64
        self.owner.data["raster"]["redis_birth"] = {
            "container_id": self.identifier, "pid": 70, "started_at": "start",
        }
        self.present = True
        self.wait_status = "0"
        self.calls = []
        self.container = {
            "Id": self.identifier, "Name": "/owned-redis", "RestartCount": 0,
            "Config": {"Image": "redis:7-alpine", "Labels": {resources.OWNER_LABEL: "owned"}},
            "State": {"Status": "running", "Running": True, "Pid": 70, "StartedAt": "start"},
        }
        self.enterContext(patch.object(resources, "docker", side_effect=self.docker))

    def docker(self, *args):
        self.calls.append(args)
        if args[:2] == ("container", "ls"):
            selector = args[args.index("--filter") + 1]
            if selector.startswith("id=") and selector[3:] != self.identifier:
                return ""
            return self.identifier if self.present else ""
        if args[:2] == ("container", "inspect"):
            return json.dumps([self.container])
        if args[0] == "kill":
            return self.identifier
        if args[0] == "wait":
            self.container["State"] = {
                "Status": "exited", "Running": False, "Pid": 0,
                "ExitCode": int(self.wait_status), "OOMKilled": False, "Error": "",
                "StartedAt": "start", "FinishedAt": "finish",
            }
            return self.wait_status
        if args[0] == "rm":
            self.present = False
            return self.identifier
        raise AssertionError(args)

    def test_graceful_exit_and_exact_removal_authorize_next_phase(self):
        self.backend._retire_prepared_raster_redis(self.prepared, self.owner)
        self.assertTrue(self.prepared.raster_redis_retired)
        receipt = self.owner.data["raster"]["redis_retirement"]
        self.assertEqual((receipt["state"], receipt["signal"], receipt["wait_exit"]),
                         ("retired", "SIGTERM", 0))
        self.assertIn(("kill", "--signal", "SIGTERM", self.identifier), self.calls)
        self.assertIn(("rm", self.identifier), self.calls)
        self.assertNotIn("-f", [argument for call in self.calls for argument in call])

    def test_unclean_exit_retains_container_and_blocks_database_drop(self):
        self.wait_status = "137"
        with self.assertRaises(resources.WorktreeError):
            self.backend._retire_prepared_raster_redis(self.prepared, self.owner)
        self.assertTrue(self.present)
        self.assertEqual(self.owner.data["raster"]["redis_retirement"]["wait_exit"], 137)
        self.assertFalse(self.prepared.raster_redis_retired)
        with self.assertRaises(resources.WorktreeError):
            self.backend._destroy_prepared_database(self.prepared)
        self.assertFalse(any(call[0] == "rm" for call in self.calls))

    def test_same_name_replacement_before_retirement_is_not_adopted(self):
        self.identifier = "f" * 64
        self.container["Id"] = self.identifier
        with self.assertRaises(resources.WorktreeError):
            self.backend._retire_prepared_raster_redis(self.prepared, self.owner)
        self.assertNotIn("redis_retirement", self.owner.data["raster"])
        self.assertFalse(any(call[0] == "kill" for call in self.calls))

    def test_same_container_restarted_before_retirement_is_not_adopted(self):
        self.container["State"].update(Pid=71, StartedAt="replacement")
        with self.assertRaises(resources.WorktreeError):
            self.backend._retire_prepared_raster_redis(self.prepared, self.owner)
        self.assertFalse(any(call[0] == "kill" for call in self.calls))

    def test_missing_birth_cannot_be_adopted(self):
        del self.owner.data["raster"]["redis_birth"]
        with self.assertRaises(resources.WorktreeError):
            self.backend._retire_prepared_raster_redis(self.prepared, self.owner)
        self.assertFalse(any(call[0] == "kill" for call in self.calls))

    def test_recreated_name_after_retirement_blocks_database_boundary(self):
        self.backend._retire_prepared_raster_redis(self.prepared, self.owner)
        self.identifier = "f" * 64
        self.present = True
        self.prepared.phase = "database_intent"
        with self.assertRaises(resources.WorktreeError):
            self.backend._require_raster_redis_retired(self.prepared)
        self.assertEqual(self.owner.data["raster"]["redis_retirement"]["state"], "retired")

    def test_original_birth_is_saved_before_first_redis_ping(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.backend.root = root
        self.backend.resources["mode"] = "e2e"
        del self.owner.data["raster"]["redis_birth"]
        self.owner.data["raster"]["resources"] = self.backend.resources
        self.backend._arm_raster_redis_birth(self.owner)
        observations = iter((None, {"owner": "owned", "running": True, "port": 1234}))
        self.backend.redis_info = lambda: next(observations)

        def docker(*args):
            if args[0] == "run":
                return self.identifier
            return self.docker(*args)

        def ping():
            self.assertEqual(self.owner.data["raster"]["redis_birth"], {
                "container_id": self.identifier, "pid": 70, "started_at": "start",
            })
            return True

        client = SimpleNamespace(ping=ping, close=lambda: None)
        with (patch.object(resources, "docker", side_effect=docker),
              patch.object(resources, "unused_loopback_port", return_value=1234),
              patch("redis.Redis", return_value=client)):
            self.backend.start_redis()
