"""Canonical Raster source retirement preserves native guards and exact process outcomes."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fixture_store
import fixture_suite_coordinator
import worktree_resources
from storage_s3_contract import ContractError


class SourceOwner:
    def __init__(self, root):
        self.path = root / ".worktree/e2e/fixture-store-owner.json"
        self.path.parent.mkdir(parents=True)
        self.private = self.path.parent / ("private-" + "c" * 32)
        self.private.mkdir()
        self.container = "a" * 64
        self.network = "b" * 64
        self.volume = "source-volume"
        self.present = {self.container, self.network, self.volume}
        self.resources = {
            "root": str(root),
            "mode": "e2e",
            "database": "owned-db",
            "owner": "owned",
        }
        identity = {
            "database": "owned-db",
            "ownership": "owned",
            "resource_root": str(root),
        }
        original = b"immutable source receipt"
        receipt = self.path.parent / ("fixture-original-receipt-" + "c" * 32 + ".json")
        receipt.write_bytes(original)
        receipt.chmod(0o600)
        self.data = {
            "checkout": str(root),
            "nonce": "c" * 32,
            "cleaned": False,
            "browser_sessions": [],
            "resources": [
                {"kind": "container", "id": self.container, "name": "source-container"},
                {"kind": "volume", "id": self.volume, "name": self.volume},
                {"kind": "network", "id": self.network, "name": "source-network"},
            ],
            "fixture_process_birth": {
                "container_id": self.container,
                "pid": 42,
                "started_at": "start",
            },
            "fixture_intent": {"database": identity},
            "fixture_store": {
                "state": "published",
                "database": identity,
                "receipt_sha256": hashlib.sha256(original).hexdigest(),
                "container_id": self.container,
                "volume_id": self.volume,
                "network_id": self.network,
                "network_name": "source-network",
                "bridge_address": "172.20.0.2",
                "endpoint": "http://172.20.0.2:9000",
                "admin_endpoint": "http://127.0.0.1:19000",
            },
            "fixture_teardown": {"state": "ready"},
        }
        self.container_info = {
            "Config": {
                "Image": fixture_store.MINIO_IMAGE,
                "Cmd": ["server", "/data", "--console-address", ":9001"],
            },
            "HostConfig": {"RestartPolicy": {"Name": "no"}, "AutoRemove": False},
            "State": {
                "Status": "running",
                "Running": True,
                "Pid": 42,
                "StartedAt": "start",
            },
            "RestartCount": 0,
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": self.volume,
                    "Destination": "/data",
                    "RW": True,
                }
            ],
            "NetworkSettings": {
                "Networks": {"source-network": {"IPAddress": "172.20.0.2"}}
            },
        }
        self.snapshots = []
        self.save()

    def save(self):
        self.snapshots.append(deepcopy(self.data))
        self.path.write_text(json.dumps(self.data))
        self.path.chmod(0o600)

    def verified(self, item):
        if item["id"] not in self.present:
            raise ContractError("resource_missing")
        if item["kind"] == "container":
            return self.container_info
        if item["kind"] == "volume":
            return {"Name": self.volume, "Driver": "local"}
        return {
            "Name": "source-network",
            "Driver": "bridge",
            "Containers": {self.container: {}}
            if self.container in self.present
            else {},
        }

    def inspect(self, item):
        return self.verified(item) if item["id"] in self.present else None

    def cleanup(self):
        raise AssertionError("generic forced cleanup must not run")


class RasterFixtureSourceRetirementTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.owner = SourceOwner(root)
        self.calls = []
        self.wait_status = "0"
        self.restart_after_wait = False
        self.fail_volume_remove = False
        self.enterContext(patch.object(fixture_store, "Owner", SourceOwner))
        self.enterContext(patch.object(fixture_store, "host_port", return_value=19000))
        self.enterContext(
            patch.object(fixture_store, "command", side_effect=self.command)
        )

    def command(self, arguments):
        self.calls.append(arguments)
        if arguments[1] == "ps":
            return (
                self.owner.container
                if self.owner.container in self.owner.present
                else ""
            )
        if arguments[1] == "kill":
            return self.owner.container
        if arguments[1] == "wait":
            self.owner.container_info["State"] = {
                "Status": "exited",
                "Running": False,
                "Pid": 0,
                "ExitCode": int(self.wait_status),
                "OOMKilled": False,
                "Error": "",
                "StartedAt": "replacement" if self.restart_after_wait else "start",
                "FinishedAt": "finish",
            }
            return self.wait_status
        if arguments[1] == "rm":
            self.owner.present.remove(self.owner.container)
            return self.owner.container
        if arguments[1:3] == ["volume", "rm"]:
            if self.fail_volume_remove:
                raise ContractError("volume_removal_failed")
            self.owner.present.remove(self.owner.volume)
            return self.owner.volume
        if arguments[1:3] == ["network", "rm"]:
            self.owner.present.remove(self.owner.network)
            return self.owner.network
        raise AssertionError(arguments)

    def test_clean_stop_and_exact_removal_archive_source_owner_without_force(self):
        fixture_store._raster_stop_for_destroy(self.owner, self.owner.resources)
        fixture_store._raster_require_stopped_for_destroy(
            self.owner, self.owner.resources
        )
        self.assertEqual(
            self.owner.data["fixture_teardown"]["raster_shutdown"]["state"], "stopped"
        )
        fixture_store._raster_remove_stopped_for_destroy(
            self.owner, self.owner.resources
        )
        fixture_store._raster_finish_removed_for_destroy(
            self.owner, self.owner.resources
        )
        self.assertFalse(self.owner.present)
        self.assertTrue(self.owner.data["cleaned"])
        self.assertFalse(self.owner.private.exists())
        with patch.object(
            fixture_suite_coordinator,
            "_resource_root",
            return_value=Path(self.owner.resources["root"]),
        ):
            fixture_suite_coordinator.finalize_store_destruction(
                self.owner, self.owner.resources
            )
        archive = self.owner.path.parent / (
            "fixture-store-owner-" + self.owner.data["nonce"] + ".json"
        )
        saved = json.loads(archive.read_text())
        self.assertEqual(
            saved["fixture_teardown"]["raster_shutdown"]["state"], "retired"
        )
        self.assertEqual(
            saved["fixture_teardown"]["raster_shutdown"]["exit_state"]["exit_code"], 0
        )
        actions = [call[1:3] for call in self.calls if call[1] != "ps"]
        self.assertEqual(
            actions,
            [
                ["kill", "--signal"],
                ["wait", self.owner.container],
                ["rm", self.owner.container],
                ["volume", "rm"],
                ["network", "rm"],
            ],
        )
        self.assertNotIn("-f", [argument for call in self.calls for argument in call])

    def test_unsettled_native_guard_or_extra_mount_refuses_before_signal(self):
        self.owner.data["fixture_teardown"]["state"] = "fencing"
        with self.assertRaisesRegex(
            ContractError, "fixture_raster_destroy_identity_changed"
        ):
            fixture_store._raster_stop_for_destroy(self.owner, self.owner.resources)
        self.owner.data["fixture_teardown"]["state"] = "ready"
        self.owner.container_info["Mounts"].append(
            {"Type": "volume", "Name": "foreign"}
        )
        with self.assertRaisesRegex(
            ContractError, "fixture_raster_shutdown_topology_changed"
        ):
            fixture_store._raster_stop_for_destroy(self.owner, self.owner.resources)
        self.assertFalse(any(call[1] == "kill" for call in self.calls))

    def test_unclean_or_replaced_process_keeps_database_authority(self):
        self.wait_status = "137"
        backend = worktree_resources.LocalResources.__new__(
            worktree_resources.LocalResources
        )
        backend.root = Path(self.owner.resources["root"])
        backend.resources = self.owner.resources
        prepared = worktree_resources._WorktreeDestroyPreparation(
            backend, self.owner, None
        )
        backend._destroy_preparation = prepared
        raster_owner = SimpleNamespace()
        prepared.raster_owner = raster_owner
        prepared.storage = "raster_provider_retired"
        prepared.raster_redis_retired = True
        with self.assertRaisesRegex(ContractError, "fixture_raster_exit_unsettled"):
            backend._stop_prepared_raster_fixture_source(prepared, raster_owner)
        self.assertEqual(
            self.owner.data["fixture_teardown"]["raster_shutdown"]["wait_exit"], 137
        )
        self.assertFalse(prepared.raster_source_stopped)
        with self.assertRaises(worktree_resources.WorktreeError):
            backend._destroy_prepared_database(prepared)
        self.assertEqual(len(self.owner.present), 3)
        self.assertFalse(any(call[1] == "rm" for call in self.calls))

    def test_zero_exit_with_changed_process_identity_is_not_settlement(self):
        self.restart_after_wait = True
        with self.assertRaisesRegex(ContractError, "fixture_raster_exit_unsettled"):
            fixture_store._raster_stop_for_destroy(self.owner, self.owner.resources)
        self.assertEqual(
            self.owner.data["fixture_teardown"]["raster_shutdown"]["state"],
            "wait_acknowledged",
        )
        self.assertEqual(len(self.owner.present), 3)

    def test_restart_before_first_teardown_snapshot_is_not_original(self):
        self.owner.container_info["State"].update(Pid=99, StartedAt="replacement")
        with self.assertRaisesRegex(
            ContractError, "fixture_raster_shutdown_topology_changed"
        ):
            fixture_store._raster_stop_for_destroy(self.owner, self.owner.resources)
        self.assertFalse(any(call[1] == "kill" for call in self.calls))

    def test_missing_original_birth_is_not_adopted(self):
        del self.owner.data["fixture_process_birth"]
        with self.assertRaisesRegex(
            ContractError, "fixture_raster_shutdown_topology_changed"
        ):
            fixture_store._raster_stop_for_destroy(self.owner, self.owner.resources)
        self.assertFalse(any(call[1] == "kill" for call in self.calls))

    def test_running_again_cannot_be_removed_even_after_successful_stop(self):
        fixture_store._raster_stop_for_destroy(self.owner, self.owner.resources)
        self.owner.container_info["State"]["Running"] = True
        with self.assertRaisesRegex(ContractError, "fixture_raster_stop_unproven"):
            fixture_store._raster_remove_stopped_for_destroy(
                self.owner, self.owner.resources
            )
        self.assertFalse(any(call[1] == "rm" for call in self.calls))

    def test_restarted_source_blocks_database_drop_after_stop_receipt(self):
        backend = worktree_resources.LocalResources.__new__(
            worktree_resources.LocalResources
        )
        backend.root = Path(self.owner.resources["root"])
        backend.resources = self.owner.resources
        prepared = worktree_resources._WorktreeDestroyPreparation(
            backend, self.owner, None
        )
        backend._destroy_preparation = prepared
        prepared.raster_owner = SimpleNamespace()
        prepared.storage = "raster_provider_retired"
        prepared.raster_redis_retired = True
        backend._stop_prepared_raster_fixture_source(prepared, prepared.raster_owner)
        self.owner.container_info["State"]["Running"] = True
        with self.assertRaisesRegex(ContractError, "fixture_raster_stop_unproven"):
            backend._destroy_prepared_database(prepared)
        self.assertEqual(prepared.phase, "prepared")
        self.assertEqual(len(self.owner.present), 3)

    def test_partial_removal_keeps_source_evidence_and_remaining_resources(self):
        fixture_store._raster_stop_for_destroy(self.owner, self.owner.resources)
        self.fail_volume_remove = True
        with self.assertRaisesRegex(ContractError, "volume_removal_failed"):
            fixture_store._raster_remove_stopped_for_destroy(
                self.owner, self.owner.resources
            )
        receipt = self.owner.data["fixture_teardown"]["raster_shutdown"]
        self.assertEqual(receipt["state"], "volume_remove_intent")
        self.assertEqual(receipt["container_removed"], self.owner.container)
        self.assertEqual(self.owner.present, {self.owner.volume, self.owner.network})
        self.assertTrue(self.owner.private.exists())
        self.assertFalse(self.owner.data["cleaned"])

    def test_absence_without_positive_retirement_cannot_complete_owner(self):
        self.owner.present.clear()
        for item in self.owner.data["resources"]:
            item["state"] = "absent"
        with self.assertRaisesRegex(
            ContractError, "fixture_raster_retirement_unproven"
        ):
            fixture_store._raster_finish_removed_for_destroy(
                self.owner, self.owner.resources
            )
        self.assertTrue(self.owner.private.exists())
        self.assertFalse(self.owner.data["cleaned"])
