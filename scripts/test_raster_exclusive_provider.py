"""Exclusive Raster provider retirement and guarded destruction transitions."""

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import raster_exclusive_provider as provider
import run_raster_fault_e2e as runner
from storage_s3_contract import ContractError


class FakeOwner:
    def __init__(self, root):
        self.path = root / ".worktree/e2e/raster-fault-runs" / ("a" * 32) / "owner.json"
        self.container = "c" * 64
        self.network = "d" * 64
        self.volume = "raster-volume"
        self.resources = {
            "root": str(root), "mode": "e2e", "owner": "owned",
            "buckets": {"MINIO_BUCKET": "annotations", "MINIO_INPUTS_BUCKET": "inputs"},
        }
        items = [
            {"kind": "container", "id": self.container, "name": "raster-container"},
            {"kind": "network", "id": self.network, "name": "raster-network"},
            {"kind": "volume", "id": self.volume, "name": self.volume},
        ]
        self.data = {
            "checkout": str(root), "cleaned": False, "resources": items,
            "raster": {
                "bucket": "annotations",
                "resources": self.resources, "initial_bucket_count": 0,
                "container_id": self.container, "network_id": self.network,
                "network_name": "raster-network", "volume_id": self.volume,
                "endpoint": "127.0.0.1:19000", "state": "consumers_stopped",
                "consumer_exit": 0, "pre_retirement_native_identities": ["actual-job"],
                "images": {"state": "stopped", "shutdown_requested": True,
                           "exit_code": 0, "database_role_oid": 123},
            },
        }
        self.present = {self.container, self.network, self.volume}
        self.container_info = {
            "Config": {"Image": provider.MINIO_IMAGE,
                       "Cmd": ["server", "/data", "--console-address", ":9001"],
                       "Env": ["MINIO_ROOT_USER=access", "MINIO_ROOT_PASSWORD=secret"]},
            "State": {"Running": True, "Status": "running", "Pid": 42,
                      "StartedAt": "start"},
            "RestartCount": 0,
            "Mounts": [{"Type": "volume", "Name": self.volume,
                        "Destination": "/data", "RW": True}],
            "NetworkSettings": {"Networks": {"raster-network": {}}},
        }
        self.snapshots = []

    def save(self):
        self.snapshots.append(deepcopy(self.data))

    def verified(self, item):
        if item["id"] not in self.present:
            raise ContractError("resource_missing")
        if item["kind"] == "container":
            return self.container_info
        if item["kind"] == "volume":
            return {"Driver": "local"}
        return {"Driver": "bridge", "Name": "raster-network",
                "Containers": {self.container: {}} if self.container in self.present else {}}

    def inspect(self, item):
        return self.verified(item) if item["id"] in self.present else None


class RasterProviderTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.owner = FakeOwner(Path(directory))
        self.buckets = ["annotations", "inputs"]
        self.bucket_owner = "owned"
        self.exit_status = "0"
        self.calls = []
        self.backend = SimpleNamespace(
            resources=self.owner.resources,
            settings=SimpleNamespace(minio_endpoint="127.0.0.1:19000", minio_use_ssl=False,
                                     minio_access_key="access", minio_secret_key="secret"),
            s3=SimpleNamespace(list_buckets=lambda: {"Buckets": [
                {"Name": name} for name in self.buckets
            ]}),
            bucket_info=lambda _name: {"owner": self.bucket_owner},
        )
        self.enterContext(patch.object(provider, "Owner", FakeOwner))
        self.enterContext(patch.object(provider, "_root_credentials", return_value=("access", "secret")))
        self.enterContext(patch.object(provider, "host_port", return_value=19000))
        self.enterContext(patch.object(provider, "command", side_effect=self.command))

    def command(self, arguments):
        self.calls.append(arguments)
        if arguments[1] == "ps":
            return self.owner.container if self.owner.container in self.owner.present else ""
        if arguments[1] == "kill":
            return self.owner.container
        if arguments[1] == "wait":
            self.owner.container_info["State"] = {
                "Status": "exited", "Running": False, "Pid": 0,
                "ExitCode": int(self.exit_status), "OOMKilled": False, "Error": "",
                "StartedAt": "start", "FinishedAt": "finish",
            }
            return self.exit_status
        if arguments[1] == "rm":
            self.owner.present.remove(self.owner.container)
            return self.owner.container
        if arguments[1:3] == ["volume", "rm"]:
            self.owner.present.remove(self.owner.volume)
            return self.owner.volume
        if arguments[1:3] == ["network", "rm"]:
            self.owner.present.remove(self.owner.network)
            return self.owner.network
        raise AssertionError(arguments)

    def test_clean_exit_removes_only_exact_resources_with_receipts(self):
        provider.retire_provider(self.owner, self.backend)
        provider.require_retired_provider(self.owner, self.backend.resources)
        actions = [call[1:3] for call in self.calls if call[1] != "ps"]
        self.assertEqual(actions, [["kill", "--signal"], ["wait", self.owner.container],
                                   ["rm", self.owner.container], ["volume", "rm"],
                                   ["network", "rm"]])
        self.assertFalse(self.owner.present)
        receipt = self.owner.data["raster"]["provider_retirement"]
        self.assertEqual(receipt["state"], "retired")
        self.assertEqual(receipt["signal"], "SIGTERM")
        self.assertEqual(receipt["wait_exit"], 0)
        states = [snapshot["raster"].get("provider_retirement", {}).get("state")
                  for snapshot in self.owner.snapshots]
        self.assertLess(states.index("exit_observed"), states.index("container_remove_intent"))
        self.assertLess(states.index("container_removed"), states.index("volume_remove_intent"))
        self.assertLess(states.index("volume_removed"), states.index("network_remove_intent"))

    def test_unowned_or_extra_bucket_refuses_before_signal(self):
        for extra in (False, True):
            with self.subTest(extra=extra):
                self.bucket_owner = "owned" if extra else "foreign"
                self.buckets = ["annotations", "inputs", "foreign"] if extra else ["annotations", "inputs"]
                with self.assertRaises(ContractError):
                    provider.retire_provider(self.owner, self.backend)
                self.assertNotIn("provider_retirement", self.owner.data["raster"])
                self.assertFalse(any(call[1] == "kill" for call in self.calls))
                self.assertEqual(len(self.owner.present), 3)

    def test_unclean_exit_retains_resources_and_observed_status(self):
        self.exit_status = "137"
        with self.assertRaisesRegex(ContractError, "raster_provider_exit_unsettled"):
            provider.retire_provider(self.owner, self.backend)
        receipt = self.owner.data["raster"]["provider_retirement"]
        self.assertEqual((receipt["state"], receipt["wait_exit"]), ("wait_acknowledged", 137))
        self.assertEqual(len(self.owner.present), 3)
        self.assertFalse(any(call[1] in {"rm", "volume", "network"} for call in self.calls))
        with self.assertRaisesRegex(ContractError, "raster_provider_retirement_already_started"):
            provider.retire_provider(self.owner, self.backend)

    def test_partial_destruction_keeps_receipt_and_remaining_resources(self):
        real_command = self.command

        def fail_volume(arguments):
            if arguments[1:3] == ["volume", "rm"]:
                self.calls.append(arguments)
                raise ContractError("volume-remove-failed")
            return real_command(arguments)

        with patch.object(provider, "command", side_effect=fail_volume):
            with self.assertRaisesRegex(ContractError, "volume-remove-failed"):
                provider.retire_provider(self.owner, self.backend)
        receipt = self.owner.data["raster"]["provider_retirement"]
        self.assertEqual(receipt["state"], "volume_remove_intent")
        self.assertEqual(receipt["container_removed"], self.owner.container)
        self.assertEqual(self.owner.present, {self.owner.volume, self.owner.network})
        self.assertFalse(any(call[1:3] == ["network", "rm"] for call in self.calls))

    def test_absence_without_affirmative_receipts_does_not_authorize_destroy(self):
        self.owner.present.clear()
        for item in self.owner.data["resources"]:
            item["state"] = "absent"
        with self.assertRaisesRegex(ContractError, "raster_provider_retirement_unproven"):
            provider.require_retired_provider(self.owner, self.backend.resources)

    def test_live_extra_mount_refuses_before_signal(self):
        self.owner.container_info["Mounts"].append({"Type": "volume", "Name": "foreign"})
        with self.assertRaisesRegex(ContractError, "raster_store_physical_identity_changed"):
            provider.retire_provider(self.owner, self.backend)
        self.assertFalse(any(call[1] == "kill" for call in self.calls))

    def test_successful_consumer_without_producer_drain_receipt_refuses(self):
        self.owner.data["raster"]["images"]["shutdown_requested"] = False
        with self.assertRaisesRegex(ContractError, "raster_producer_boundary_unproven"):
            provider.retire_provider(self.owner, self.backend)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.owner.present), 3)


class RasterGuardOrderingTests(unittest.TestCase):
    def test_failed_fixture_guard_cannot_start_provider_or_drop_roles(self):
        events = []

        class Backend:
            @contextmanager
            def fixture_authority_fence(self):
                events.append("fence")
                yield

            @contextmanager
            def _prepare_owned_destruction(self, _confirmation):
                events.append("fixture-guard")
                raise ContractError("fixture-not-settled")
                yield

        async def role(_backend, _owner, action):
            events.append("role-" + action)

        owner = SimpleNamespace(data={"raster": {"images": {"state": "stopped"}}})
        with (patch.object(runner, "verify_exclusive_provider", side_effect=lambda *_: events.append("verify")),
              patch.object(runner, "image_role", side_effect=role),
              patch.object(runner, "retire_provider", side_effect=lambda *_: events.append("retire"))):
            with self.assertRaisesRegex(ContractError, "fixture-not-settled"):
                runner.destroy_with_images_role(Backend(), owner, "owned")
        self.assertEqual(events, ["fence", "verify", "role-check", "fixture-guard"])

    def test_failed_provider_retirement_preserves_database_and_role(self):
        events = []

        class Backend:
            @contextmanager
            def fixture_authority_fence(self):
                yield

            @contextmanager
            def _prepare_owned_destruction(self, _confirmation):
                yield object()

            def _destroy_prepared_database(self, _prepared):
                events.append("database-dropped")

        async def role(_backend, _owner, action):
            events.append("role-" + action)

        owner = SimpleNamespace(data={"raster": {"images": {"state": "stopped"}}})
        with (patch.object(runner, "verify_exclusive_provider"),
              patch.object(runner, "image_role", side_effect=role),
              patch.object(runner, "retire_provider", side_effect=ContractError("exit-unknown"))):
            with self.assertRaisesRegex(ContractError, "exit-unknown"):
                runner.destroy_with_images_role(Backend(), owner, "owned")
        self.assertEqual(events, ["role-check"])

    def test_provider_and_redis_retire_before_database_and_original_login(self):
        events = []

        class Backend:
            @contextmanager
            def fixture_authority_fence(self):
                events.append("fence")
                yield

            @contextmanager
            def _prepare_owned_destruction(self, _confirmation):
                events.append("fixture-guards")
                yield object()

            def _accept_retired_raster_provider(self, _prepared, _owner):
                events.append("provider-receipt")

            def _retire_prepared_raster_redis(self, _prepared, _owner):
                events.append("redis-retired")

            def _destroy_prepared_database(self, _prepared):
                events.append("database-dropped")

            def _destroy_prepared_services(self, _prepared):
                events.append("services-removed")

            def _finish_prepared_destruction(self, _prepared):
                events.append("local-removed")

        async def role(_backend, _owner, action):
            events.append("role-" + action)

        owner = SimpleNamespace(
            data={"raster": {"images": {"state": "stopped"}}},
            save=lambda: None,
        )
        with (patch.object(runner, "verify_exclusive_provider", side_effect=lambda *_: events.append("verify")),
              patch.object(runner, "image_role", side_effect=role),
              patch.object(runner, "retire_provider", side_effect=lambda *_: events.append("provider-retired"))):
            runner.destroy_with_images_role(Backend(), owner, "owned")
        self.assertEqual(events, [
            "fence", "verify", "role-check", "fixture-guards", "provider-retired",
            "provider-receipt", "redis-retired", "database-dropped", "services-removed",
            "role-drop", "local-removed",
        ])
