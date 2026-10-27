"""Forward observations preserve owners and never enter rollback or delete data."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch
import uuid

from storage_s3_contract import ContractError
from storage_migration_inventory import PURPOSES
import verify_storage_migration_finalization as base
import verify_storage_migration_observation as live


A, B, GROUP = (str(uuid.uuid4()) for _ in range(3))


def bindings(location=A, generation=1):
    return {
        purpose: {
            "location_id": location,
            "bucket": "source-" + purpose if location == A else "target-bucket",
            "generation": generation,
        }
        for purpose in PURPOSES
    }


def facts():
    return {
        "bindings": bindings(B, 2),
        "records": {
            "tasks": {
                "saved": {
                    "file_path": "saved-original",
                    "original_id": "original",
                    "thumbnail_output_id": "thumbnail",
                }
            },
            "task_originals": {
                "original": {
                    "creation_id": "creation",
                    "storage_ref": {"location_id": B, "purpose": "annotations"},
                    "sha256": "a" * 64,
                    "size_bytes": 3,
                    "version_id": "original-b1",
                }
            },
            "thumbnail_outputs": {
                "thumbnail": {
                    "storage_ref": {"location_id": B, "purpose": "media-cache"},
                    "sha256": "b" * 64,
                    "size_bytes": 4,
                    "version_id": "thumbnail-b1",
                }
            },
            "managed_original_creations": {
                "creation": {"staging_ref": {"location_id": B, "purpose": "import"}}
            },
        },
        "control": {
            "storage_migration_read_publications": {"state": "active"},
            "storage_migration_finalizations": {"state": "sealed"},
            "window": {"state": "stopped"},
            "gates": {A: {"state": "stopped"}, B: {"state": "active"}},
        },
        "allocation": {
            "original_ref": {
                "purpose": "annotations",
                "location_id": B,
                "bucket": "target-bucket",
                "binding_generation": 2,
            },
            "staging_ref": {
                "purpose": "import",
                "location_id": B,
                "bucket": "target-bucket",
                "binding_generation": 2,
            },
        },
    }


def copied():
    return {
        "state": "verified",
        "source": {"bucket": "source-annotations", "key": "old", "version_id": "v1"},
        "sha256": "a" * 64,
        "size_bytes": 3,
        "target_key": "retained/old",
        "receipt": {"version_id": "b1"},
    }


class ForwardFactsTests(unittest.TestCase):
    def test_binding_owner_and_each_forward_control_remain_required(self):
        valid = facts()
        active = {"state": "active", "after_bindings": bindings(B, 2)}
        live.require_forward(valid, active, valid["records"], A, B)
        for change in (
            "binding",
            "owner",
            "publication",
            "finalization",
            "window",
            "source_gate",
            "target_gate",
        ):
            value = deepcopy(valid)
            if change == "binding":
                value["bindings"]["annotations"]["generation"] = 3
            elif change == "owner":
                value["records"]["tasks"]["saved"]["file_path"] = "rewritten"
            elif change in {"publication", "finalization"}:
                table = (
                    "storage_migration_read_publications"
                    if change == "publication"
                    else "storage_migration_finalizations"
                )
                value["control"][table]["state"] = "withdrawn"
            elif change == "window":
                value["control"]["window"]["state"] = "closed"
            else:
                value["control"]["gates"][A if change == "source_gate" else B][
                    "state"
                ] = "draining"
            with self.subTest(change=change), self.assertRaises(ContractError):
                live.require_forward(value, active, valid["records"], A, B)

    def test_retention_keeps_markers_and_shared_bucket_counts_explicit(self):
        before, after = bindings(), bindings(B, 2)
        before["training"]["bucket"] = before["annotations"]["bucket"]
        buckets = {purpose: row["bucket"] for purpose, row in before.items()}
        inventory = [
            {"bucket": buckets["annotations"], "kind": "Versions", "key": "old"},
            {"bucket": buckets["annotations"], "kind": "DeleteMarkers", "key": "old"},
        ]
        rows = [copied(), {**copied(), "state": "skipped", "receipt": None}]
        result = live.retention_manifest(buckets, before, after, inventory, rows)
        self.assertEqual(set(result["purposes"]), set(before))
        self.assertTrue(result["shared_bucket_counts_are_not_additive"])
        for purpose in ("annotations", "training"):
            row = result["purposes"][purpose]
            self.assertEqual(row["source_versions"], 1)
            self.assertEqual(row["source_delete_markers"], 1)
            self.assertEqual(row["verified_copy_versions"], 1)
        self.assertEqual(len(result["verified_copy_mappings"]), 1)
        result["source_inventory"][0]["key"] = "changed"
        result["purposes"]["annotations"]["before_binding"]["bucket"] = "changed"
        self.assertEqual(inventory[0]["key"], "old")
        self.assertEqual(before["annotations"]["bucket"], "source-annotations")

    def test_retention_rejects_incomplete_scope_or_wrong_source_bucket(self):
        before = bindings()
        buckets = {purpose: row["bucket"] for purpose, row in before.items()}
        for value in (
            {p: b for p, b in buckets.items() if p != "training"},
            {**buckets, "training": "unrelated"},
        ):
            with self.assertRaises(ContractError):
                live.retention_manifest(value, before, bindings(B, 2), [], [])


class ObservationTests(unittest.TestCase):
    def fixture(self, directory):
        fixture = live.Observation()
        root = Path(directory)
        config, state = root / "source.json", root / "fence-state.json"
        config.write_text(
            json.dumps(
                {
                    "source_container": "source-id",
                    "gateway_container": "gateway-id",
                    "source_volume_name": "volume-id",
                    "public_endpoint": "http://127.0.0.1:9009",
                }
            )
        )
        state.write_text(
            json.dumps(
                {"application_claim": {"finalization_id": GROUP, "reserved": True}}
            )
        )
        fixture.target_state = root / "b" / "fence-state.json"
        fixture.target_bucket = "target-bucket"
        fixture.target_endpoint = "http://172.29.0.2:80"
        fixture.media_vips = [8, 14, 1]
        fixture.value = {
            "source_location_id": A,
            "target_location_id": B,
            "project_id": str(uuid.uuid4()),
            "cutover_id": GROUP,
            "new_task_id": "saved",
        }
        resources = [{"id": name} for name in ("gateway-id", "source-id", "volume-id")]
        running = {"source-id": True, "gateway-id": True}
        owner = Mock()
        owner.private = root
        owner.data = {"resources": resources}
        owner.verified.side_effect = lambda resource: {
            "State": {"Running": running.get(resource["id"], False)}
        }

        def action(resource, *args):
            self.assertEqual(args, ("stop", "--time", "2"))
            running[resource["id"]] = False

        owner.action.side_effect = action
        original = facts()
        fixture.facts = Mock(side_effect=lambda *args: deepcopy(original))
        http = Mock()
        http.requests = 10
        http.call.return_value = {
            "upload_url": fixture.target_endpoint + "/staging",
            "task_id": str(uuid.uuid4()),
        }
        source, target = object(), object()
        inventory = [{"bucket": "source-annotations", "kind": "Versions", "key": "old"}]
        active = {"state": "active", "after_bindings": bindings(B, 2)}
        cutover = Mock(return_value=active)
        context = {
            "owner": owner,
            "http": http,
            "summary": {},
            "environment": {},
            "native_environment": {},
            "config": config,
            "state": state,
            "source": source,
            "target": target,
            "buckets": {p: r["bucket"] for p, r in bindings().items()},
            "final_rows": [copied()],
            "finalization_id": GROUP,
            "child": str(uuid.uuid4()),
            "window_id": str(uuid.uuid4()),
        }
        forward = {
            "context": context,
            "cutover": cutover,
            "active": active,
            "publication": {"publication_id": GROUP},
            "before": bindings(),
            "records": original["records"],
            "read_saved": Mock(),
            "source_before": inventory,
            "target_before": ["target-facts"],
            "forward_started": {"utc": live.utc_now(), "monotonic": time.monotonic()},
        }

        def object_facts(client, buckets):
            if client is source:
                self.assertTrue(running["source-id"])
                return inventory
            self.assertIs(client, target)
            return ["target-facts"]

        return fixture, forward, running, object_facts

    def test_original_cutover_keeps_its_default_completion_path(self):
        self.assertFalse(live.Cutover().after_forward())

    def test_forward_observation_stops_only_owned_a_and_keeps_claims_and_b_writable(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture, forward, running, objects = self.fixture(directory)
            with (
                patch.object(live, "fence_cli", return_value={"held": True}) as native,
                patch.object(live, "object_facts", side_effect=objects),
                patch.object(live.time, "sleep"),
            ):
                self.assertTrue(fixture.after_forward(**forward))
            native.assert_called_once()
            self.assertFalse(any(running.values()))
            self.assertEqual(forward["read_saved"].call_count, 3)
            self.assertEqual(forward["cutover"].call_args_list, [call("status")] * 3)
            summary = forward["context"]["summary"]
            record = summary["observation_record"]
            self.assertEqual(
                [s["phase"] for s in record["samples"]],
                ["source_online", "source_online", "source_offline"],
            )
            self.assertFalse(summary["rollback_executed"])
            self.assertFalse(record["production_retirement_ready"])
            self.assertFalse(record["migration_source_delete_executed"])
            self.assertIsNone(record["legacy_a_read_url_observation"]["latest_expiry"])
            self.assertTrue(record["source_application_claim_retained"])
            self.assertTrue(record["fixture_cleanup_scheduled_after_evidence"])
            self.assertNotIn("generated_fixture_cleanup_after_evidence", record)
            self.assertTrue(record["future_allocations_on_b_with_a_offline"])
            self.assertFalse(fixture.target_state.exists())
            path = Path(directory) / "observation-record.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_bytes()), record)

    def test_observation_does_not_stop_a_with_changed_saved_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, forward, running, _ = self.fixture(directory)
            forward["records"] = {"different": {}}
            with (
                patch.object(live, "fence_cli", return_value={"held": True}),
                self.assertRaises(ContractError),
            ):
                fixture.after_forward(**forward)
            self.assertTrue(all(running.values()))
            forward["context"]["owner"].action.assert_not_called()

    def test_observation_does_not_stop_a_without_held_reserved_claim(self):
        for change in ("not_held", "unreserved"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture, forward, running, _ = self.fixture(directory)
                if change == "unreserved":
                    forward["context"]["state"].write_text(
                        json.dumps(
                            {
                                "application_claim": {
                                    "finalization_id": GROUP,
                                    "reserved": False,
                                }
                            }
                        )
                    )
                with (
                    patch.object(
                        live, "fence_cli", return_value={"held": change != "not_held"}
                    ),
                    self.assertRaises(ContractError),
                ):
                    fixture.after_forward(**forward)
                self.assertTrue(all(running.values()))
                forward["context"]["owner"].action.assert_not_called()

    def test_incompatible_fixture_options_fail_before_environment_access(self):
        for options, code in (
            (("--observation", "--cutover"), "fixture_conflict"),
            (
                ("--observation", "--pause-for-browser"),
                "observation_browser_not_supported",
            ),
        ):
            with (
                self.subTest(options=options),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                environment = Mock(
                    side_effect=AssertionError("environment must not load")
                )
                with patch.dict(
                    "sys.modules",
                    {
                        "verify_legacy_storage_rollback": SimpleNamespace(
                            verified_environment=environment
                        )
                    },
                ):
                    result = base.main(
                        [
                            "--manifest",
                            str(root / "owner.json"),
                            "--summary",
                            str(root / "summary.json"),
                            *options,
                        ]
                    )
                self.assertEqual(result, 1)
                self.assertEqual(
                    json.loads((root / "summary.json").read_bytes())["error_code"], code
                )
                environment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
