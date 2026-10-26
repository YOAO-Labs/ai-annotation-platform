"""Exercise one atomic nine-purpose cutover and preserve real B writes on rollback.

Only the launcher's empty test mode and this harness's exact resource owner are
accepted. SQL regressions prove atomicity and unknown commits separately; the
live fixture creates real Task originals and independent media-cache thumbnails.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from storage_migration_inventory import PURPOSES
from storage_s3_contract import require
from verify_storage_migration_finalization import (
    ROOT,
    browser_pause,
    free_port,
    host_port,
    operator,
    private_write,
    replace_config,
)
from verify_storage_migration_original_reads import OriginalReads
from verify_storage_source_fence import fence_cli, object_facts, request
from worktree_env import atomic_json


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def binding_vector(value):
    require(isinstance(value, dict) and set(value) == PURPOSES, "cutover_binding_scope")
    for row in value.values():
        require(
            isinstance(row, dict)
            and set(row) == {"location_id", "bucket", "generation"}
            and str(uuid.UUID(row["location_id"])) == row["location_id"]
            and isinstance(row["bucket"], str)
            and type(row["generation"]) is int
            and row["generation"] > 0,
            "cutover_binding_facts",
        )
    return value


def restored_bindings(before):
    return {
        purpose: {**row, "generation": row["generation"] + 2}
        for purpose, row in binding_vector(before).items()
    }


def check_forward(group, before, target_location, bucket):
    require(group["state"] == "active", "cutover_not_active")
    require(
        group["before_bindings"] == binding_vector(before), "cutover_before_changed"
    )
    after = binding_vector(group["after_bindings"])
    require(
        after
        == {
            purpose: {
                "location_id": target_location,
                "bucket": bucket,
                "generation": row["generation"] + 1,
            }
            for purpose, row in before.items()
        },
        "cutover_after_changed",
    )


def application_path(http, value):
    parsed, endpoint = urlsplit(value), urlsplit(http.endpoint)
    require(
        (not parsed.scheme and not parsed.netloc)
        or (parsed.scheme, parsed.netloc) == (endpoint.scheme, endpoint.netloc),
        "cutover_http_owner_changed",
    )
    require(
        parsed.path.startswith("/api/v1/") and not parsed.fragment,
        "cutover_http_path_changed",
    )
    return parsed.path + ("?" + parsed.query if parsed.query else "")


def signed_put_live(url, *, now=None):
    """Prove rejection is physical fencing while the original signature is live."""
    query = parse_qs(urlsplit(url).query)
    require(
        all(len(query.get(name, [])) == 1 for name in ("X-Amz-Date", "X-Amz-Expires")),
        "cutover_upload_signature_changed",
    )
    started = datetime.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc
    )
    expires = int(query["X-Amz-Expires"][0])
    require(0 < expires <= 900, "cutover_upload_expiry_changed")
    require(
        started
        <= (now or datetime.now(timezone.utc))
        < started + timedelta(seconds=expires),
        "cutover_upload_already_expired",
    )


def cutover_operator(owner, environment, action, actor, identifier, **flags):
    arguments = [
        sys.executable,
        str(ROOT / "scripts/cutover_storage_migration.py"),
        action,
        "--actor-id",
        actor,
        "--cutover-id",
        identifier,
    ]
    for name, value in flags.items():
        arguments.extend(("--" + name.replace("_", "-"), str(value)))
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        timeout=180,
    )
    require(
        len(result.stdout) + len(result.stderr) <= 1024**2, "operator_output_budget"
    )
    if result.returncode:
        failure = {"action": action, "expected": 0, "actual": result.returncode}
        try:
            code = json.loads(result.stderr).get("code")
        except (ValueError, AttributeError):
            code = None
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            failure["code"] = code
        owner.data["operator_failure"] = failure
        owner.save()
        require(False, "cutover_operator_exit")
    value = json.loads(result.stdout)
    require(
        isinstance(value, dict)
        and value.get("schema") == "aap.storage-migration-cutover-cli/v1"
        and isinstance(value.get("item"), dict),
        "cutover_operator_response",
    )
    return value["item"]


async def snapshot(input_path, output_path, *, wait_thumbnail=False):
    """Read only the named saved owners; legitimate new rows may be added."""
    from sqlalchemy import select, text
    from app.db.models.dataset import DatasetItem
    from app.db.models.dataset_asset import (
        DatasetAsset,
        DatasetAssetLocation,
        StorageBinding,
    )
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.db.models.task import Task
    from app.db.models.task_original import TaskOriginal
    from app.db.models.thumbnail_output import ThumbnailOutput
    from app.workers._db import task_session

    value = json.loads(Path(input_path).read_bytes())
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "test_mode_required")
    deadline = time.monotonic() + 40
    while True:
        async with task_session() as db:
            require(
                await db.scalar(text("SELECT current_database()")) == value["database"],
                "cutover_database_changed",
            )
            new_task = (
                await db.get(Task, uuid.UUID(value["new_task_id"]))
                if value.get("new_task_id")
                else None
            )
            thumbnail = (
                await db.get(ThumbnailOutput, new_task.thumbnail_output_id)
                if new_task is not None and new_task.thumbnail_output_id is not None
                else None
            )
            ready = thumbnail is not None and thumbnail.state == "ready"
            if wait_thumbnail and not ready:
                require(time.monotonic() < deadline, "cutover_thumbnail_not_ready")
            else:
                records = {}

                async def record(model, identifier, names):
                    row = await db.get(model, uuid.UUID(identifier))
                    require(row is not None, "cutover_saved_owner_missing")
                    result = {name: getattr(row, name) for name in names.split()}
                    records.setdefault(model.__tablename__, {})[str(row.id)] = result
                    return row

                task_fields = "id project_id dataset_item_id file_path file_name file_type status original_id thumbnail_path thumbnail_output_id"
                original_fields = "id creation_id task_id project_id location_id storage_identity bucket object_key storage_ref sha256 size_bytes etag version_id"
                creation_fields = "id owner_kind task_id project_id location_id storage_identity bucket object_key configuration staging_ref staging_bucket staging_key sha256 size_bytes etag version_id prepared receipt verification status"
                for identifier in (
                    value["task_id"],
                    value["linked_task_id"],
                    value.get("new_task_id"),
                ):
                    if identifier is None:
                        continue
                    task = await record(Task, identifier, task_fields)
                    if task.original_id is not None:
                        original = await record(
                            TaskOriginal, str(task.original_id), original_fields
                        )
                        await record(
                            ManagedOriginalCreation,
                            str(original.creation_id),
                            creation_fields,
                        )
                await record(
                    DatasetItem,
                    value["item_id"],
                    "id dataset_id file_path file_name content_hash file_size width height",
                )
                await record(
                    DatasetAsset,
                    value["asset_id"],
                    "id dataset_item_id size_bytes checksum_algorithm checksum_value verification_state",
                )
                records["dataset_asset_locations"] = [
                    {
                        name: getattr(row, name)
                        for name in (
                            "asset_id",
                            "location_id",
                            "bucket",
                            "object_key",
                            "role",
                            "reference_state",
                        )
                    }
                    for row in await db.scalars(
                        select(DatasetAssetLocation)
                        .where(
                            DatasetAssetLocation.asset_id
                            == uuid.UUID(value["asset_id"])
                        )
                        .order_by(
                            DatasetAssetLocation.location_id,
                            DatasetAssetLocation.bucket,
                            DatasetAssetLocation.object_key,
                        )
                    )
                ]
                if thumbnail is not None:
                    await record(
                        ThumbnailOutput,
                        str(thumbnail.id),
                        "id owner_kind owner_id storage_ref input_ref state sha256 size_bytes etag version_id uncertain_write",
                    )
                allocation = None
                if value.get("probe_task_id"):
                    probe = await db.get(Task, uuid.UUID(value["probe_task_id"]))
                    candidates = list(
                        await db.scalars(
                            select(ManagedOriginalCreation).where(
                                ManagedOriginalCreation.task_id
                                == uuid.UUID(value["probe_task_id"])
                            )
                        )
                    )
                    require(
                        probe is not None
                        and probe.status == "uploading"
                        and probe.original_id is None
                        and len(candidates) == 1,
                        "cutover_probe_allocation_changed",
                    )
                    candidate = candidates[0]
                    allocation = {
                        "task_id": str(probe.id),
                        "creation_id": str(candidate.id),
                        "original_ref": candidate.configuration["original_ref"],
                        "staging_ref": candidate.staging_ref,
                    }
                bindings = {
                    row.purpose: {
                        "location_id": str(row.location_id),
                        "bucket": row.bucket,
                        "generation": row.generation,
                    }
                    for row in await db.scalars(
                        select(StorageBinding).order_by(StorageBinding.purpose)
                    )
                }
                control = {}
                if value.get("cutover_id"):
                    for table in (
                        "storage_migration_read_publications",
                        "storage_migration_finalizations",
                    ):
                        row = (
                            (
                                await db.execute(
                                    text(
                                        f"SELECT state, revision FROM {table} WHERE id=:id"
                                    ),
                                    {"id": uuid.UUID(value["cutover_id"])},
                                )
                            )
                            .mappings()
                            .one()
                        )
                        control[table] = dict(row)
                    control["window"] = dict(
                        (
                            await db.execute(
                                text(
                                    "SELECT state, revision FROM storage_migration_windows WHERE id=:id"
                                ),
                                {"id": uuid.UUID(value["window_id"])},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    control["gates"] = {
                        str(row["location_id"]): {
                            "state": row["state"],
                            "revision": row["revision"],
                        }
                        for row in (
                            await db.execute(
                                text(
                                    "SELECT location_id,state,revision FROM storage_maintenance_gates WHERE location_id IN (:a,:b)"
                                ),
                                {
                                    "a": uuid.UUID(value["source_location_id"]),
                                    "b": uuid.UUID(value["target_location_id"]),
                                },
                            )
                        ).mappings()
                    }
                result = json.loads(
                    json.dumps(
                        {
                            "records": records,
                            "bindings": bindings,
                            "control": control,
                            "allocation": allocation,
                        },
                        default=str,
                    )
                )
                private_write(output_path, json.dumps(result, sort_keys=True).encode())
                return
        await asyncio.sleep(0.2)


class Cutover(OriginalReads):
    worker_queues = "default,media"

    def provision_target(self, owner, credentials, env_file):
        from storage_source_fence_docker import (
            DockerAdapter,
            MINIO_IMAGE,
            NGINX_IMAGE,
            gateway_config,
        )

        directory = owner.private / "target-native"
        directory.mkdir(mode=0o700)
        gateway_directory = directory / "gateway"
        gateway_directory.mkdir(mode=0o700)
        network = owner.resource("network", "network", args=("--internal",))
        frontend = owner.resource("network", "frontend", args=("--driver", "bridge"))
        subnet = ipaddress.ip_network(
            owner.verified(network)["IPAM"]["Config"][0]["Subnet"]
        )
        require(subnet.version == 4 and subnet.num_addresses > 16, "fixture_subnet")
        source_ip, gateway_ip = str(subnet[10]), str(subnet[11])
        volume = owner.resource("volume", "data")
        source = owner.service(
            "source",
            MINIO_IMAGE,
            args=(
                "--pull=never",
                "--network",
                network["id"],
                "--ip",
                source_ip,
                "--volume",
                volume["id"] + ":/data",
                "--env-file",
                str(env_file),
            ),
            cmd=("server", "/data", "--console-address", ":9001"),
        )
        replace_config(
            gateway_directory / "nginx.conf", gateway_config(source_ip, False)
        )
        owner.role = "gateway"
        gateway = owner.container(
            NGINX_IMAGE,
            args=(
                "--pull=never",
                "--network",
                frontend["id"],
                "--sysctl",
                "net.ipv4.ip_forward=0",
                "--publish",
                f"127.0.0.1:{free_port()}:80",
                "--entrypoint",
                "nginx",
                "--volume",
                str(gateway_directory) + ":/etc/nginx:ro",
            ),
            cmd=("-g", "daemon off;"),
        )
        owner.action(gateway, "network", "connect", "--ip", gateway_ip, network["id"])
        owner.start(gateway)
        management = f"http://127.0.0.1:{host_port(owner, gateway, 80)}"
        self.target_config = directory / "fence.json"
        self.target_state = directory / "fence-state.json"
        self.target_credentials = credentials
        self.target_bucket = "final-target-" + owner.data["nonce"][:16]
        config = {
            "schema": "aap.storage-source-fence-config/v1",
            "owner": owner.data["nonce"],
            "checkout": str(ROOT),
            "source_container": source["id"],
            "gateway_container": gateway["id"],
            "network_id": network["id"],
            "gateway_frontend_network_id": frontend["id"],
            "source_volume_name": volume["id"],
            "gateway_config_directory": str(gateway_directory),
            "public_endpoint": management,
            "purpose_buckets": dict.fromkeys(PURPOSES, self.target_bucket),
        }
        private_write(self.target_config, json.dumps(config).encode())
        self.target_endpoint = DockerAdapter(
            config, credential_prefix="AAP_TARGET_FENCE"
        ).application_endpoint()
        return self.target_endpoint, urlsplit(self.target_endpoint).hostname

    def prepare(self, owner, environment, seed, source, buckets):
        super().prepare(owner, environment, seed, source, buckets)
        from PIL import Image
        from sqlalchemy.engine import make_url

        image = io.BytesIO()
        Image.new("RGB", (64, 48), (149, 53, 91)).save(image, format="PNG")
        self.new_raw = image.getvalue()
        self.value.update(database=make_url(environment["DATABASE_URL"]).database)
        self.snapshot_input = owner.private / "cutover-input.json"

    def facts(self, owner, environment, *, wait_thumbnail=False):
        atomic_json(self.snapshot_input, self.value)
        output = owner.private / ("cutover-facts-" + uuid.uuid4().hex + ".json")
        self.child(
            owner,
            environment,
            "from verify_storage_migration_cutover import snapshot\n"
            + f"asyncio.run(snapshot({str(self.snapshot_input)!r},{str(output)!r},wait_thumbnail={wait_thumbnail!r}))",
        )
        return json.loads(output.read_bytes())

    def read_original(self, http, task_id, expected_sha, expected_size, content_id):
        identity = http.call("GET", f"/api/v1/files/tasks/{task_id}/original-identity")
        require(
            identity["sha256"] == expected_sha
            and identity["size_bytes"] == expected_size
            and identity["content_id"] == content_id,
            "cutover_original_identity_changed",
        )
        path = application_path(http, identity["download_path"])
        raw = http.call("GET", path, binary=True)
        require(
            sha256(raw) == expected_sha and len(raw) == expected_size,
            "cutover_original_bytes_changed",
        )
        token, http.token = http.token, None
        try:
            http.call("GET", path, expected=401)
        finally:
            http.token = token

    def after_seal(self, **context):
        owner, http, summary = (context[key] for key in ("owner", "http", "summary"))
        environment = context["environment"]
        actor, identifier = context["seed"]["actor_id"], context["finalization_id"]
        combined = {
            **context["native_environment"],
            "AAP_TARGET_FENCE_ACCESS_KEY": self.target_credentials[0],
            "AAP_TARGET_FENCE_SECRET_KEY": self.target_credentials[1],
        }
        native_b = {
            **environment,
            "AAP_SOURCE_FENCE_ACCESS_KEY": self.target_credentials[0],
            "AAP_SOURCE_FENCE_SECRET_KEY": self.target_credentials[1],
        }
        source_flags = {"config": context["config"], "state": context["state"]}
        target_flags = {
            "target_config": self.target_config,
            "target_state": self.target_state,
        }

        def cutover(action, **flags):
            return cutover_operator(owner, combined, action, actor, identifier, **flags)

        baseline = self.facts(owner, environment)
        before = binding_vector(baseline["bindings"])
        source_location = baseline["records"]["task_originals"][
            self.value["original_id"]
        ]["location_id"]
        self.value.update(
            cutover_id=identifier,
            window_id=context["window_id"],
            source_location_id=source_location,
            target_location_id=context["seed"]["target_location_id"],
        )
        source_before = object_facts(context["source"], context["buckets"].values())
        summary["stage"] = "cutover_publish_reads"
        publication = operator(
            owner,
            context["native_environment"],
            "publish-reads",
            actor,
            identifier,
            context["config"],
            context["state"],
            expected_revision=context["sealed"]["revision"],
        )["item"]
        require(
            publication["publication_id"] == identifier
            and publication["state"] == "active",
            "cutover_publication_changed",
        )
        review_path = owner.private / "cutover-review.json"
        summary["stage"] = "cutover_preview"
        review = cutover(
            "preview",
            **source_flags,
            review_file=review_path,
            expected_finalization_revision=context["sealed"]["revision"],
            expected_publication_revision=publication["revision"],
        )
        require(
            set(review["capabilities"]) == PURPOSES
            and set(review["context"]["targets"]) == PURPOSES,
            "cutover_review_scope",
        )
        require(
            review_path.is_file()
            and not review_path.is_symlink()
            and review_path.stat().st_mode & 0o077 == 0,
            "cutover_review_not_private",
        )
        summary["stage"] = "cutover_activate"
        active = cutover("activate", **source_flags, review_file=review_path)
        check_forward(
            active, before, self.value["target_location_id"], self.target_bucket
        )
        require(cutover("status") == active, "cutover_status_changed")
        current = self.facts(owner, environment)
        require(
            current["records"] == baseline["records"]
            and current["bindings"] == active["after_bindings"],
            "cutover_rewrote_saved_owners",
        )

        summary["stage"] = "cutover_real_b_upload"
        upload = http.call(
            "POST",
            "/api/v1/files/upload-init",
            {
                "project_id": self.value["project_id"],
                "file_name": "created-on-b.png",
                "content_type": "image/png",
            },
        )
        signed_put_live(upload["upload_url"])
        require(
            urlsplit(upload["upload_url"]).netloc
            == urlsplit(self.target_endpoint).netloc,
            "cutover_upload_did_not_use_b",
        )
        status, _, _ = request(
            upload["upload_url"],
            method="PUT",
            body=self.new_raw,
            headers={"Content-Type": "image/png"},
        )
        require(status == 200, "cutover_upload_put_failed")
        http.call("POST", "/api/v1/files/upload-complete/" + upload["task_id"])
        self.value["new_task_id"] = upload["task_id"]
        written = self.facts(owner, environment, wait_thumbnail=True)
        records = written["records"]
        task = records["tasks"][upload["task_id"]]
        original = records["task_originals"][task["original_id"]]
        creation = records["managed_original_creations"][original["creation_id"]]
        thumbnail = records["thumbnail_outputs"][task["thumbnail_output_id"]]
        for purpose, reference in (
            ("annotations", original["storage_ref"]),
            ("import", creation["staging_ref"]),
            ("media-cache", thumbnail["storage_ref"]),
        ):
            require(
                reference["purpose"] == purpose
                and reference["location_id"] == self.value["target_location_id"]
                and reference["endpoint_url"] == self.target_endpoint
                and reference["bucket"] == self.target_bucket
                and reference["binding_generation"]
                == active["after_bindings"][purpose]["generation"],
                "cutover_new_owner_not_b",
            )
        require(
            original["sha256"] == sha256(self.new_raw)
            and original["size_bytes"] == len(self.new_raw)
            and original["storage_ref"] == creation["configuration"]["original_ref"]
            and original["version_id"]
            and thumbnail["version_id"]
            and thumbnail["state"] == "ready"
            and thumbnail["uncertain_write"] is False,
            "cutover_new_receipts_unproven",
        )

        def read_saved():
            self.read_original(
                http,
                self.value["task_id"],
                self.value["sha256"],
                self.value["size_bytes"],
                self.value["original_id"],
            )
            self.read_original(
                http,
                upload["task_id"],
                original["sha256"],
                original["size_bytes"],
                original["id"],
            )
            details = http.call("GET", "/api/v1/tasks/" + upload["task_id"])
            require(
                details["id"] == upload["task_id"] and details.get("thumbnail_url"),
                "cutover_task_thumbnail_missing",
            )
            path = application_path(http, details["thumbnail_url"])
            data = http.call("GET", path, binary=True)
            require(
                sha256(data) == thumbnail["sha256"]
                and len(data) == thumbnail["size_bytes"],
                "cutover_thumbnail_bytes_changed",
            )
            token, http.token = http.token, None
            try:
                http.call("GET", "/api/v1/tasks/" + upload["task_id"], expected=401)
            finally:
                http.token = token

        read_saved()
        require(
            self.facts(owner, environment)["records"] == records,
            "cutover_read_changed_saved_receipts",
        )
        target_before = object_facts(context["target"], [self.target_bucket])
        require(
            object_facts(context["source"], context["buckets"].values())
            == source_before,
            "cutover_new_write_changed_a",
        )

        summary["stage"] = "cutover_b_native_stop"
        draining = cutover("begin-rollback", expected_revision=active["revision"])
        require(draining["state"] == "rollback_draining", "cutover_not_draining")
        native = fence_cli(self.target_config, self.target_state, native_b, "init")
        native = fence_cli(
            self.target_config,
            self.target_state,
            native_b,
            "seal",
            revision=native["revision"],
            operation=str(uuid.uuid4()),
        )
        require(native["held"], "cutover_b_native_not_held")
        bound = cutover(
            "bind-rollback", **target_flags, expected_revision=draining["revision"]
        )
        require(
            bound["state"] == "rollback_bound" and bound["rollback_claim"],
            "cutover_rollback_not_bound",
        )
        actual = self.facts(owner, environment)
        require(
            actual["bindings"] == restored_bindings(before),
            "cutover_rollback_vector_changed",
        )
        require(
            actual["control"]["gates"][self.value["target_location_id"]]["state"]
            == "stopped",
            "cutover_b_not_stopped",
        )
        require(actual["records"] == records, "cutover_bind_changed_saved_receipts")

        summary["stage"] = "cutover_restore_a"
        restored = cutover(
            "restore-source",
            **source_flags,
            **target_flags,
            expected_revision=bound["revision"],
        )
        require(
            restored["state"] == "rolled_back"
            and restored["source_release_operation_id"]
            and restored["rollback_claim"],
            "cutover_rollback_incomplete",
        )
        require(cutover("status") == restored, "cutover_final_status_changed")
        actual = self.facts(owner, environment)
        require(
            actual["bindings"] == restored_bindings(before)
            and actual["records"] == records,
            "cutover_rollback_changed_saved_owners",
        )
        require(
            actual["control"]["storage_migration_read_publications"]["state"]
            == "withdrawn",
            "cutover_publication_not_withdrawn",
        )
        require(
            actual["control"]["storage_migration_finalizations"]["state"]
            == "invalidated"
            and actual["control"]["window"]["state"] == "closed",
            "cutover_a_not_released",
        )
        require(
            set(actual["control"]["gates"])
            == {source_location, self.value["target_location_id"]}
            and all(
                gate["state"] == "active"
                for gate in actual["control"]["gates"].values()
            ),
            "cutover_read_admission_not_restored",
        )
        require(
            fence_cli(
                context["config"],
                context["state"],
                context["native_environment"],
                "status",
            )["held"]
            is False,
            "cutover_a_native_not_released",
        )
        held = fence_cli(self.target_config, self.target_state, native_b, "status")
        require(held["held"], "cutover_b_reopened")
        denied_release = fence_cli(
            self.target_config,
            self.target_state,
            native_b,
            "release",
            revision=held["revision"],
            operation=str(uuid.uuid4()),
            expected=2,
        )
        require(
            denied_release.get("code") == "rollback_fence_bound",
            "cutover_b_release_not_interlocked",
        )
        require(
            fence_cli(self.target_config, self.target_state, native_b, "status")[
                "held"
            ],
            "cutover_b_claim_not_retained",
        )
        claim = json.loads(self.target_state.read_bytes())["rollback_claim"]
        require(
            claim["cutover_id"] == identifier and claim["reserved"] is True,
            "cutover_b_reservation_not_durable",
        )
        signed_put_live(upload["upload_url"])
        status, _, _ = request(
            upload["upload_url"],
            method="PUT",
            body=b"must not be accepted",
            headers={"Content-Type": "image/png"},
        )
        require(status == 405, "cutover_old_b_put_not_fenced")
        read_saved()
        require(
            self.facts(owner, environment)["records"] == records,
            "cutover_final_reads_changed_receipts",
        )
        next_upload = http.call(
            "POST",
            "/api/v1/files/upload-init",
            {
                "project_id": self.value["project_id"],
                "file_name": "allocated-after-rollback.png",
                "content_type": "image/png",
            },
        )
        self.value["probe_task_id"] = next_upload["task_id"]
        allocation = self.facts(owner, environment)["allocation"]
        for purpose, reference in (
            ("annotations", allocation["original_ref"]),
            ("import", allocation["staging_ref"]),
        ):
            expected = restored_bindings(before)[purpose]
            require(
                reference["purpose"] == purpose
                and reference["location_id"] == expected["location_id"]
                and reference["bucket"] == expected["bucket"]
                and reference["binding_generation"] == expected["generation"],
                "cutover_next_allocation_not_a",
            )
        require(
            object_facts(context["target"], [self.target_bucket]) == target_before,
            "cutover_b_versions_changed_after_rollback",
        )
        require(
            object_facts(context["source"], context["buckets"].values())
            == source_before,
            "cutover_a_versions_changed_after_rollback",
        )
        if context["args"].pause_for_browser:
            summary["stage"] = "cutover_browser_pause"
            browser_pause(
                owner,
                http,
                context["api_port"],
                context["endpoint"],
                actor,
                context["window_id"],
                context["child"],
                context["first"],
                context["email"],
                context["password"],
                context["args"].browser_wait_seconds,
                extra={
                    "project_id": self.value["project_id"],
                    "task_id": upload["task_id"],
                    "cutover_id": identifier,
                    "project_path": f"/projects/{self.value['project_id']}/tasks",
                },
            )
            require(
                self.facts(owner, environment)["records"] == records
                and object_facts(context["target"], [self.target_bucket])
                == target_before,
                "cutover_browser_changed_saved_state",
            )
        summary.update(
            stage="complete",
            atomic_binding_count=len(before),
            before_bindings=before,
            after_bindings=active["after_bindings"],
            rollback_bindings=restored_bindings(before),
            real_new_write_purposes=["annotations", "media-cache"],
            real_import_staging_on_b=True,
            real_task_upload_and_celery_thumbnail=True,
            old_a_and_new_b_originals_authenticated=True,
            thumbnail_authorized_capability_and_sha=True,
            rollback_generation_increment=2,
            saved_owner_ref_sha_size_version_unchanged=True,
            source_release_operation_id=restored["source_release_operation_id"],
            source_native_released=True,
            target_native_held_and_reserved=True,
            target_native_phase=held["phase"],
            target_rollback_claim_reserved=True,
            publication_state="withdrawn",
            finalization_state="invalidated",
            source_window_state="closed",
            post_rollback_allocation_a=True,
            application_gates_active=True,
            target_application_read_only_gate=False,
            unexpired_old_b_put_denied=True,
            source_versions_unchanged=True,
            target_versions_unchanged_after_rollback=True,
            saved_receipts_sha256=sha256(json.dumps(records, sort_keys=True).encode()),
            http_completed_requests=http.requests,
            migration_ready=False,
        )


if __name__ == "__main__":
    from verify_storage_migration_finalization import main

    raise SystemExit(main([*sys.argv[1:], "--cutover"]))
