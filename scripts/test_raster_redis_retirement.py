"""Raster cleanup never forces Redis or drops its database on uncertain exit."""

import json
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
