"""Record a forward cutover and owned old-source shutdown without rollback.

This is a short disposable local observation, not a production retention or
traffic-removal authorization. The ordinary fixture owner removes its generated
resources only after the record is collected. No production data is deleted.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import sys
import time
from urllib.parse import urlsplit

from storage_s3_contract import require
from verify_storage_migration_cutover import Cutover, binding_vector, sha256
from verify_storage_migration_finalization import private_write
from verify_storage_source_fence import fence_cli, object_facts


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def require_forward(facts, active, records, source_location, target_location):
    require(active["state"] == "active", "observation_cutover_not_active")
    require(
        facts["bindings"] == binding_vector(active["after_bindings"])
        and facts["records"] == records,
        "observation_saved_facts_changed",
    )
    control = facts["control"]
    require(
        control["storage_migration_read_publications"]["state"] == "active"
        and control["storage_migration_finalizations"]["state"] == "sealed"
        and control["window"]["state"] == "stopped"
        and set(control["gates"]) == {source_location, target_location}
        and control["gates"][source_location]["state"] == "stopped"
        and control["gates"][target_location]["state"] == "active",
        "observation_forward_control_changed",
    )


def retention_manifest(buckets, before, after, inventory, final_rows):
    binding_vector(before)
    binding_vector(after)
    require(set(buckets) == set(before), "observation_retention_scope")
    require(
        all(before[purpose]["bucket"] == bucket for purpose, bucket in buckets.items()),
        "observation_source_bucket_changed",
    )
    purposes = {}
    for purpose, bucket in sorted(buckets.items()):
        actual = [row for row in inventory if row["bucket"] == bucket]
        copied = [
            row
            for row in final_rows
            if row["source"]["bucket"] == bucket and row["state"] == "verified"
        ]
        purposes[purpose] = {
            "handling": "migrate",
            "before_binding": deepcopy(before[purpose]),
            "after_binding": deepcopy(after[purpose]),
            "source_versions": sum(row["kind"] == "Versions" for row in actual),
            "source_delete_markers": sum(
                row["kind"] == "DeleteMarkers" for row in actual
            ),
            "verified_copy_versions": len(copied),
            "source_retention": "retain_bytes_and_saved_locators",
        }
    mappings = [
        {
            "source": deepcopy(row["source"]),
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "target_key": row["target_key"],
            "target_version_id": row["receipt"]["version_id"],
        }
        for row in final_rows
        if row["state"] == "verified"
    ]
    return {
        "purposes": purposes,
        "source_inventory": deepcopy(inventory),
        "verified_copy_mappings": mappings,
        "shared_bucket_counts_are_not_additive": True,
    }


class Observation(Cutover):
    def after_forward(self, **forward):
        context = forward["context"]
        owner, http, summary = (context[key] for key in ("owner", "http", "summary"))
        environment = context["environment"]
        active, records = forward["active"], forward["records"]
        source_location = self.value["source_location_id"]
        target_location = self.value["target_location_id"]
        started = forward["forward_started"]
        descriptor = json.loads(context["config"].read_bytes())
        native = fence_cli(
            context["config"],
            context["state"],
            context["native_environment"],
            "status",
        )
        require(native["held"], "observation_source_not_held")
        journal = context["state"].read_bytes()
        claim = json.loads(journal)["application_claim"]
        require(
            claim["finalization_id"] == context["finalization_id"]
            and claim["reserved"] is True,
            "observation_source_claim_changed",
        )
        resources = {}
        for role, identifier in (
            ("gateway", descriptor["gateway_container"]),
            ("source", descriptor["source_container"]),
            ("data", descriptor["source_volume_name"]),
        ):
            matches = [
                row for row in owner.data["resources"] if row["id"] == identifier
            ]
            require(len(matches) == 1, "observation_resource_owner_changed")
            resources[role] = matches[0]
            owner.verified(matches[0])
        samples = []

        def sample(phase):
            forward["read_saved"]()
            require(
                forward["cutover"]("status") == active, "observation_cutover_changed"
            )
            facts = self.facts(owner, environment)
            require_forward(facts, active, records, source_location, target_location)
            for role in ("gateway", "source"):
                require(
                    owner.verified(resources[role])["State"]["Running"]
                    is (phase == "source_online"),
                    "observation_source_state_changed",
                )
            samples.append(
                {
                    "observed_at": utc_now(),
                    "elapsed_seconds": round(
                        time.monotonic() - started["monotonic"], 3
                    ),
                    "phase": phase,
                    "cutover_state": active["state"],
                    "bindings": facts["bindings"],
                    "saved_receipts_sha256": sha256(
                        json.dumps(facts["records"], sort_keys=True).encode()
                    ),
                    "originals_and_thumbnail_content_verified": True,
                }
            )

        summary["stage"] = "forward_observation_online"
        sample("source_online")
        time.sleep(1)
        sample("source_online")
        require(
            object_facts(context["source"], context["buckets"].values())
            == forward["source_before"],
            "observation_source_versions_changed",
        )
        retained = retention_manifest(
            context["buckets"],
            forward["before"],
            active["after_bindings"],
            forward["source_before"],
            context["final_rows"],
        )
        retained["source_identity"] = {
            "location_id": source_location,
            "endpoint": descriptor["public_endpoint"],
        }
        retained["target_identity"] = {
            "location_id": target_location,
            "endpoint": self.target_endpoint,
            "bucket": self.target_bucket,
        }
        summary["stage"] = "forward_observation_old_source_offline"
        for role in ("gateway", "source"):
            owner.action(resources[role], "stop", "--time", "2")
        sample("source_offline")
        upload = http.call(
            "POST",
            "/api/v1/files/upload-init",
            {
                "project_id": self.value["project_id"],
                "file_name": "allocated-with-a-offline.png",
                "content_type": "image/png",
            },
        )
        require(
            urlsplit(upload["upload_url"]).netloc
            == urlsplit(self.target_endpoint).netloc,
            "observation_future_signature_not_b",
        )
        self.value["probe_task_id"] = upload["task_id"]
        facts = self.facts(owner, environment)
        require_forward(facts, active, records, source_location, target_location)
        for purpose, reference in (
            ("annotations", facts["allocation"]["original_ref"]),
            ("import", facts["allocation"]["staging_ref"]),
        ):
            binding = active["after_bindings"][purpose]
            require(
                reference["purpose"] == purpose
                and reference["location_id"] == binding["location_id"]
                and reference["bucket"] == binding["bucket"]
                and reference["binding_generation"] == binding["generation"],
                "observation_future_allocation_not_b",
            )
        require(
            context["state"].read_bytes() == journal
            and not self.target_state.exists()
            and object_facts(context["target"], [self.target_bucket])
            == forward["target_before"],
            "observation_retained_state_changed",
        )
        owner.verified(resources["data"])
        task = records["tasks"][self.value["new_task_id"]]
        original = records["task_originals"][task["original_id"]]
        thumbnail = records["thumbnail_outputs"][task["thumbnail_output_id"]]
        creation = records["managed_original_creations"][original["creation_id"]]
        observed = {
            "schema": "aap.storage-migration-observation-record/v1",
            "scope": "disposable_local_trial",
            "cutover_id": self.value["cutover_id"],
            "copy_id": context["child"],
            "window_id": context["window_id"],
            "finalization_id": context["finalization_id"],
            "publication_id": forward["publication"]["publication_id"],
            "started_at": started["utc"],
            "finished_at": utc_now(),
            "duration_seconds": round(time.monotonic() - started["monotonic"], 3),
            "samples": samples,
            "retention": retained,
            "new_write_receipts": {
                "task_id": self.value["new_task_id"],
                "original": {
                    key: deepcopy(original[key])
                    for key in ("storage_ref", "sha256", "size_bytes", "version_id")
                },
                "thumbnail": {
                    key: deepcopy(thumbnail[key])
                    for key in ("storage_ref", "sha256", "size_bytes", "version_id")
                },
                "import_staging_ref": deepcopy(creation["staging_ref"]),
            },
            "source_volume_retained_at_observation": True,
            "source_journal_sha256": sha256(journal),
            "source_native_held_verified_before_shutdown": True,
            "fresh_native_held_claim_after_shutdown": False,
            "source_application_claim_retained": True,
            "target_write_admission_active_without_rollback_claim": True,
            "future_allocations_on_b_with_a_offline": True,
            "legacy_a_read_url_observation": {
                "coverage": "not_observed_in_this_fixture",
                "latest_expiry": None,
            },
            "recovery_evidence": {
                "reference": "docs/plans/archive/1792661016_dataset-storage-operations-recovery.md#e12-b1-离线一致备份与隔离恢复交付",
                "scope": "existing_local_recovery_qualification",
                "new_backup_generation_created": False,
            },
            "production_observation_period_confirmed": False,
            "production_business_confirmation": False,
            "production_retirement_ready": False,
            "migration_source_delete_executed": False,
            "fixture_cleanup_scheduled_after_evidence": True,
        }
        encoded = json.dumps(observed, sort_keys=True).encode()
        require(len(encoded) <= 65536, "observation_record_budget")
        private_write(owner.private / "observation-record.json", encoded)
        summary.update(
            stage="complete",
            observation_record=observed,
            media_worker_image=self.media_image,
            media_worker_vips=self.media_vips,
            atomic_binding_count=len(forward["before"]),
            real_task_upload_and_celery_thumbnail=True,
            old_source_and_gateway_stopped=True,
            old_a_and_new_b_originals_authenticated=True,
            saved_owner_ref_sha_size_version_unchanged=True,
            post_cutover_allocation_b=True,
            rollback_executed=False,
            migration_source_delete_executed=False,
            http_completed_requests=http.requests,
            migration_ready=False,
        )
        return True


if __name__ == "__main__":
    from verify_storage_migration_finalization import main

    raise SystemExit(main([*sys.argv[1:], "--observation"]))
