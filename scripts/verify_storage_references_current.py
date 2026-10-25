"""Qualify current saved locators using an owned empty test database and S3.

This verifies the migration catalog, not application workflows or a cutover.
The CLI runs with column-only SELECT grants; no application services are started.
"""

import argparse
import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import traceback
import uuid

from inventory_storage_references import FAMILIES
from storage_migration_copy import Checkpoint, plan
from storage_migration_inventory import PURPOSES, InventoryLimits, write_inventory
from storage_migration_references import identity, verify
from verify_legacy_storage_rollback import ROOT, require, verified_environment
from verify_storage_references import grants, object_facts
from worktree_env import WorktreeError, atomic_json
from worktree_recovery_fixture import database_facts

BODY = b"current saved locator fixture"
SHA = hashlib.sha256(BODY).hexdigest()
LEGACY = uuid.UUID("4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7")
OTHER = "http://outside-selected-source.invalid:9000"
SENTINEL = "private-business-document-sentinel"


class Fixture:
    def __init__(self, backend, address):
        self.backend, self.address = backend, address
        self.location = uuid.uuid4()
        self.rows, self.expected, self.updates = [], [], []
        self.buckets = {
            purpose: backend.resources["buckets"][
                "MINIO_BUCKET"
                if purpose == "annotations"
                else "MINIO_" + purpose.upper().replace("-", "_") + "_BUCKET"
            ]
            for purpose in PURPOSES
        }

    def ref(self, purpose, *, schema=2, address=None, location=None, **fields):
        address = address or self.address
        return {
            "schema": schema,
            "purpose": purpose,
            "location_id": str(location or self.location),
            "config_revision": 1,
            "binding_generation": 2,
            "endpoint_url": address,
            "bucket": self.buckets[purpose],
            "storage_identity": identity(address, self.buckets[purpose]),
            **fields,
        }

    @staticmethod
    def root(purpose):
        return "公司/_aap_managed/" + purpose + "/"

    def put(self, purpose, key):
        return self.backend.s3.put_object(
            Bucket=self.buckets[purpose],
            Key=key,
            Body=BODY,
            Metadata={"fixture": "current-locator"},
        )

    def add(self, table, **row):
        self.rows.append((table, row))

    def claim(self, family, key, status, *, endpoint=None, prefix=None):
        self.expected.append(
            {
                "family": family,
                "key": key,
                "prefix": prefix,
                "endpoint_url": endpoint or self.address,
                "status": status,
            }
        )

    def creation(
        self, identifier, key, purpose, *, receipt, instant, kind="dataset", **values
    ):
        ref = self.ref(
            purpose,
            schema=1 if purpose == "datasets" else 2,
            key_prefix=self.root(purpose),
            object_key=key,
        )
        self.add(
            "managed_original_creations",
            id=identifier,
            owner_kind=kind,
            location_id=self.location,
            storage_identity=ref["storage_identity"],
            bucket=ref["bucket"],
            object_key=key,
            logical_path="fixture.bin",
            origin="zip",
            max_bytes=1024,
            timeout_seconds=60,
            content_type="application/octet-stream",
            authority=(
                {
                    "auxiliary": {
                        "kind": "map",
                        "source_contract": {"private": SENTINEL},
                    }
                }
                if kind == "dataset_auxiliary"
                else {"private": SENTINEL}
            ),
            configuration={
                "schema": 2,
                "endpoint": self.address,
                "original_ref": ref,
                "private": SENTINEL,
            },
            input_fingerprint=SHA,
            maintenance_revision=1,
            status="verified",
            prepared={"fixture": True},
            sha256=SHA,
            md5=hashlib.md5(BODY, usedforsecurity=False).hexdigest(),
            size_bytes=len(BODY),
            frozen_at=instant,
            receipt={"fixture": True},
            etag=receipt["ETag"],
            version_id=receipt["VersionId"],
            acknowledged_at=instant,
            operation_id=uuid.uuid4(),
            claimed_at=instant,
            write_started_at=instant,
            verification={"fixture": True},
            readback_at=instant,
            settled_at=instant,
            completed_at=instant,
            **values,
        )

    async def seed(self, owner):
        from app.db.base import Base
        import app.db.models  # noqa: F401

        now = datetime.now(timezone.utc)
        user, project, dataset, item, asset, task = [uuid.uuid4() for _ in range(6)]
        for bucket in self.buckets.values():
            self.backend.s3.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
            )
        self.add(
            "users",
            id=user,
            email=uuid.uuid4().hex + "@fixture.invalid",
            name="Migration locator fixture",
            password_hash=SENTINEL,
            is_active=False,
        )
        self.add(
            "projects",
            id=project,
            display_id="locator-project",
            name="Locator fixture",
            type_label="Image",
            type_key="image",
            owner_id=user,
        )
        self.add(
            "datasets",
            id=dataset,
            display_id="locator-dataset",
            name="Locator fixture",
            created_by=user,
        )
        self.add(
            "dataset_items",
            id=item,
            dataset_id=dataset,
            file_name="fixture.bin",
            file_path="fixture.bin",
        )
        self.add("dataset_assets", id=asset, dataset_item_id=item, media_type="image")
        self.add(
            "storage_locations",
            id=LEGACY,
            name="Original registered source",
            kind="s3",
            config={"endpoint": self.address, "use_ssl": False},
        )
        self.add(
            "storage_locations",
            id=self.location,
            name="Changed current endpoint",
            kind="s3",
            enabled=False,
            config={"endpoint": OTHER, "use_ssl": False},
        )
        for purpose, bucket in self.buckets.items():
            self.add(
                "storage_bindings",
                purpose=purpose,
                location_id=self.location,
                bucket=bucket,
                generation=3,
            )

        token = uuid.uuid4().hex
        key = self.root("avatars") + token + ".webp"
        result = self.put("avatars", key)
        self.add(
            "avatar_uploads",
            token=token,
            owner_id=user,
            storage_ref=self.ref("avatars", object_key=key),
            sha256=SHA,
            size_bytes=len(BODY),
            object_version_id=result["VersionId"],
            state="retired",
            retired_at=now,
        )
        self.claim("avatar_uploads", key, "exact_version")

        identifier = uuid.uuid4()
        key = (
            self.root("bug-reports") + f"bug-report-attachments/{user}/{identifier}.png"
        )
        result = self.put("bug-reports", key)
        self.add(
            "bug_attachment_uploads",
            id=identifier,
            owner_id=user,
            storage_ref=self.ref("bug-reports", object_key=key),
            sha256=SHA,
            size_bytes=len(BODY),
            object_version_id=result["VersionId"],
            mime_type="image/png",
            file_name="fixture.png",
            expires_at=now + timedelta(days=1),
        )
        self.claim("bug_attachment_uploads", key, "exact_version")

        identifier = uuid.uuid4()
        key = self.root("audit-archive") + f"2026/10/{identifier}.jsonl.gz"
        self.put("audit-archive", key)
        self.add(
            "audit_archive_outputs",
            id=identifier,
            job_id=uuid.uuid4(),
            year=2026,
            month=10,
            source_table="audit_logs_y2026m10",
            parent_oid=1,
            source_oid=2,
            month_start=date(2026, 10, 1),
            month_end=date(2026, 11, 1),
            row_count=1,
            source_size_bytes=len(BODY),
            source_sha256=SHA,
            sha256=SHA,
            size_bytes=len(BODY),
            storage_ref=self.ref("audit-archive", object_key=key),
        )
        self.claim("audit_archive_outputs", key, "unpinned_key")

        identifier, job = uuid.uuid4(), uuid.uuid4()
        key = self.root("export") + f"image/{project}/{job}/{identifier}.zip"
        self.put("export", key)
        self.add(
            "export_outputs",
            id=identifier,
            project_id=project,
            job_id=job,
            media="image",
            format="fixture",
            file_count=1,
            sha256=SHA,
            size_bytes=len(BODY),
            storage_ref=self.ref("export", object_key=key),
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
        self.claim("export_outputs", key, "unpinned_key")

        identifier = uuid.uuid4()
        prefix = self.root("import") + f"prediction-inputs/{identifier}/"
        relative = f"original/{SHA}.png"
        result = self.put("import", prefix + relative)
        ref = self.ref("import", key_prefix=prefix)
        self.add(
            "prediction_input_leases",
            id=identifier,
            owner_id=uuid.uuid4(),
            owner_kind="batch_predict",
            task_id=task,
            contract_version=2,
            storage_ref=ref,
            maintenance_revision=1,
            object_count=1,
            size_bytes=len(BODY),
            bucket=ref["bucket"],
            storage_identity=ref["storage_identity"],
            state="active",
            uncertain_write=False,
            created_at=now,
            expires_at=now + timedelta(hours=2),
            retain_until=now + timedelta(hours=2, days=7),
            next_cleanup_at=now,
            inventory={
                "schema": 1,
                "objects": {
                    relative: {
                        "sha256": SHA,
                        "size_bytes": len(BODY),
                        "media_type": "image/png",
                        "version_id": result["VersionId"],
                        "etag": result["ETag"],
                        "write_nonce": str(uuid.uuid4()),
                        "state": "verified",
                    }
                },
            },
        )
        planned = self.rows[-1][1]["inventory"]
        verified = deepcopy(planned)
        planned["objects"][relative].update(state="planned", etag=None, version_id=None)
        self.updates.append(
            ("prediction_input_leases", identifier, {"inventory": verified})
        )
        self.claim("prediction_input_leases", prefix + relative, "exact_version")

        identifier = uuid.uuid4()
        prefix = self.root("media-cache") + f"image-pyramids/objects/{identifier}/"
        inventory = {}
        for suffix in ("manifest.json", "overview.webp"):
            key = prefix + suffix
            result = self.put("media-cache", key)
            inventory[key] = {
                "sha256": SHA,
                "size_bytes": len(BODY),
                "version_id": result["VersionId"],
                "state": "present",
            }
            self.claim("image_pyramid_outputs", key, "exact_version")
        neighbor = (
            self.root("media-cache")
            + f"image-pyramids/objects/{uuid.uuid4()}/manifest.json"
        )
        result = self.put("media-cache", neighbor)
        inventory[neighbor] = {
            "sha256": SHA,
            "size_bytes": len(BODY),
            "version_id": result["VersionId"],
            "state": "present",
        }
        # Existing rows may contain a valid-byte receipt outside their owner scope.
        self.claim("image_pyramid_outputs", neighbor, "unlocated")
        self.add(
            "image_pyramid_outputs",
            id=identifier,
            asset_id=asset,
            generation=1,
            lease_token=uuid.uuid4(),
            state="building",
            inventory=None,
            storage_ref=self.ref(
                "media-cache", prefix=prefix, key_prefix=self.root("media-cache")
            ),
        )
        self.updates.append(
            (
                "image_pyramid_outputs",
                identifier,
                {"state": "ready", "inventory": {"schema": 1, "objects": inventory}},
            )
        )

        for family, purpose, kind, logical in (
            (
                "task_originals",
                "annotations",
                "task",
                "__aap_task_originals__/" + str(task),
            ),
            (
                "dataset_auxiliary_originals",
                "datasets",
                "dataset_auxiliary",
                "__aap_auxiliary_originals__/" + str(dataset),
            ),
        ):
            identifier = uuid.uuid4()
            key = self.root(purpose) + logical + "/" + str(identifier) + "/payload"
            result = self.put(purpose, key)
            scope = (
                {"project_id": project, "task_id": task}
                if kind == "task"
                else {"dataset_id": dataset}
            )
            self.creation(
                identifier,
                key,
                purpose,
                receipt=result,
                instant=now,
                kind=kind,
                **scope,
            )
            # Standalone Task creations use the existing direct-upload contract.
            if kind == "task":
                row = self.rows[-1][1]
                staging = self.root("import") + f"__aap_upload_staging__/{identifier}/"
                row.update(
                    origin="direct",
                    staging_bucket=self.buckets["import"],
                    staging_key=staging + "payload",
                    staging_ref=self.ref("import", address=OTHER, key_prefix=staging),
                )
                self.put("import", staging + "payload")
                self.claim(
                    "managed_original_creations",
                    staging + "payload",
                    "outside_selected_source",
                    endpoint=OTHER,
                )
            ref = self.ref(
                purpose,
                schema=2 if kind == "task" else 1,
                object_key=key,
                key_prefix=self.root(purpose),
            )
            self.add(
                family,
                id=identifier,
                creation_id=identifier,
                **scope,
                location_id=self.location,
                storage_identity=ref["storage_identity"],
                bucket=ref["bucket"],
                object_key=key,
                storage_ref=ref,
                sha256=SHA,
                size_bytes=len(BODY),
                etag=result["ETag"],
                version_id=result["VersionId"],
                created_at=now,
                **(
                    {"kind": "map", "source_contract": {"private": SENTINEL}}
                    if kind != "task"
                    else {}
                ),
            )
            self.claim(family, key, "exact_version")
            if purpose == "datasets":
                self.add(
                    "dataset_asset_locations",
                    asset_id=asset,
                    location_id=self.location,
                    bucket=ref["bucket"],
                    object_key=key,
                    storage_ref=ref,
                    reference_state="present",
                    etag=result["ETag"],
                )
                self.claim("dataset_asset_locations", key, "unpinned_key")

        identifier = uuid.uuid4()
        prefix = (
            self.root("media-cache") + f"original-cache/v1/{asset}/{SHA}/{identifier}/"
        )
        self.put("media-cache", prefix + "payload")
        ref = self.ref(
            "media-cache", prefix=prefix, key_prefix=self.root("media-cache")
        )
        self.add(
            "original_cache_entries",
            id=identifier,
            scope_location_id=self.location,
            source_id=uuid.uuid4(),
            asset_id=asset,
            sha256=SHA,
            size_bytes=len(BODY),
            source_revision=1,
            policy_revision=1,
            location_revision=1,
            location_id=self.location,
            bucket=ref["bucket"],
            binding_generation=2,
            storage_identity=ref["storage_identity"],
            storage_ref=ref,
            state="uploading",
            uncertain_write=False,
            last_accessed_at=now,
            expires_at=now + timedelta(days=1),
            writer_until=now,
            retain_until=now,
            next_check_at=now,
        )
        self.claim("original_cache_entries", prefix + "payload", "unpinned_key")

        identifier = uuid.uuid4()
        prefix = self.root("media-cache") + f"thumbnails/objects/{identifier}/"
        result = self.put("media-cache", prefix + "thumbnail.webp")
        self.add(
            "thumbnail_outputs",
            id=identifier,
            owner_kind="dataset_thumbnail",
            owner_id=item,
            input_ref={"private": SENTINEL},
            storage_ref=self.ref(
                "media-cache", prefix=prefix, key_prefix=self.root("media-cache")
            ),
            state="ready",
            sha256=SHA,
            size_bytes=len(BODY),
            etag=result["ETag"],
            version_id=result["VersionId"],
            writer_until=now,
            next_check_at=now,
            ready_at=now,
        )
        self.claim("thumbnail_outputs", prefix + "thumbnail.webp", "exact_version")

        identifier = uuid.uuid4()
        logical = f"mask-formats/{project}/{user}/{identifier}/fixture.zip"
        prefix = self.root("import") + logical.rsplit("/", 1)[0] + "/"
        self.put("import", prefix + "fixture.zip")
        ref = self.ref("import", key_prefix=prefix)
        self.add(
            "mask_format_uploads",
            id=identifier,
            project_id=project,
            uploader_id=user,
            logical_object_key=logical,
            content_type="application/zip",
            storage_ref=ref,
            created_at=now,
            upload_expires_at=now + timedelta(seconds=900),
        )
        self.claim("mask_format_uploads", prefix + "fixture.zip", "unpinned_key")
        self.add(
            "mask_format_imports",
            id=uuid.uuid4(),
            project_id=project,
            format_id="fixture",
            adapter_version="1",
            manifest_version="1",
            staged_object_key=logical,
            staging_ref=ref,
            staged_sha256=SHA,
            mapping_digest=SHA,
            options_digest=SHA,
            plan_digest=SHA,
            plan_json={"private": SENTINEL},
            token_hash=SHA,
            receipt_expires_at=now + timedelta(hours=1),
        )
        self.claim("mask_format_imports", prefix + "fixture.zip", "unpinned_key")

        identifier = uuid.uuid4()
        prefix = self.root("training") + f"projects/{project}/datasets/{identifier}/"
        self.put("training", prefix + "dataset.zip")
        ref = self.ref(
            "training",
            prefix=prefix,
            key_prefix=self.root("training"),
            origin="new",
            evidence=None,
        )
        ref["purpose"] = "training-dataset"
        self.add(
            "training_storage_deletions",
            bucket=ref["bucket"],
            prefix=prefix,
            storage_ref=ref,
        )
        self.claim("training_storage_deletions", None, "prefix_scope", prefix=prefix)
        self.add(
            "training_storage_deletions",
            bucket=ref["bucket"],
            prefix=f"projects/{project}/datasets/{uuid.uuid4()}/",
            storage_ref=None,
        )
        async with owner.begin() as connection:
            for table, values in self.rows:
                try:
                    await connection.execute(
                        Base.metadata.tables[table].insert().values(**values)
                    )
                except Exception as exc:
                    cause = getattr(getattr(exc, "orig", None), "__cause__", None)
                    constraint = getattr(cause, "constraint_name", None)
                    raise WorktreeError(
                        "Fixture insert failed for "
                        + table
                        + ":"
                        + (constraint or type(exc).__name__)
                    ) from None
            for table, identifier, values in self.updates:
                record = Base.metadata.tables[table]
                await connection.execute(
                    record.update().where(record.c.id == identifier).values(**values)
                )


async def qualify(backend):
    from app.config import settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    owner = create_async_engine(backend.url, echo=False, hide_parameters=True)
    role = "aap_reference_current_" + uuid.uuid4().hex[:12]
    created = False
    readonly = None
    try:
        async with owner.connect() as connection:
            facts = await database_facts(connection)
            require(
                all(
                    row["count"] == 0
                    for table, row in facts.items()
                    if table != "alembic_version"
                ),
                "Requires empty owned test database",
            )
            revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            require(revision == "0254", "Current fixture requires schema 0254")
        require(
            object_facts(backend.s3, backend.resources["buckets"].values())[1] == 0,
            "Requires empty owned test buckets",
        )
        address = (
            ("https" if settings.minio_use_ssl else "http")
            + "://"
            + settings.minio_endpoint
        )
        fixture = Fixture(backend, address)
        await fixture.seed(owner)
        password = secrets.token_hex(24)
        async with owner.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
            )
            created = True
            for query in (
                f'GRANT CONNECT ON DATABASE "{backend.resources["database"]}" TO "{role}"',
                f'GRANT USAGE ON SCHEMA public TO "{role}"',
                f'ALTER ROLE "{role}" SET default_transaction_read_only=on',
            ):
                await connection.exec_driver_sql(query)
        await grants(owner, role)
        readonly_url = backend.url.set(username=role, password=password)
        readonly = create_async_engine(readonly_url, echo=False, hide_parameters=True)
        async with owner.connect() as connection:
            before = await database_facts(connection)
        physical_before = object_facts(
            backend.s3, backend.resources["buckets"].values()
        )
        with tempfile.TemporaryDirectory(prefix="aap-reference-current-") as directory:
            private = Path(directory)
            source, output = private / "source.jsonl", private / "references.jsonl"
            source_summary = write_inventory(
                source,
                backend.s3,
                fixture.buckets,
                source={"endpoint": address},
                limits=InventoryLimits(
                    page_size=10,
                    max_entries=100,
                    max_pages=200,
                    max_object_bytes=1024,
                    max_total_bytes=65536,
                ),
            )
            require(source_summary["complete"], "Small source inventory incomplete")
            source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
            response = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    str(ROOT / "scripts/inventory_storage_references.py"),
                    "--source-inventory",
                    str(source),
                    "--output",
                    str(output),
                    "--temp-dir",
                    directory,
                    "--page-size",
                    "1",
                    "--max-rows",
                    "300",
                    "--max-expanded",
                    "100",
                    "--max-pages",
                    "300",
                    "--max-source-entries",
                    "100",
                    "--max-seconds",
                    "60",
                    "--statement-seconds",
                    "5",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "DATABASE_URL": readonly_url.render_as_string(hide_password=False),
                    "DEBUG": "false",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                capture_output=True,
                timeout=75,
            )
            require(
                response.returncode == 1,
                "Expected sealed catalog with explicit unknown history",
            )
            summary = verify(output)
            records = [json.loads(line) for line in output.read_text().splitlines()]
            header = records[0]
            require(
                header["database"]["enumeration_complete"],
                "Current SQL enumeration incomplete",
            )
            require(
                header["database"]["revisions"] == [revision], "Wrong observed revision"
            )
            require(
                len(header["database"]["families"]) == len(FAMILIES) == 27,
                "Missing current family",
            )
            references = [row for row in records if row["type"] == "reference"]
            for expected in fixture.expected:
                candidates = [
                    row
                    for row in references
                    if all(
                        row.get(k) == v for k, v in expected.items() if k != "status"
                    )
                ]
                require(
                    any(
                        row["match"]["status"] == expected["status"]
                        and (
                            bool(row.get("unknown"))
                            if expected["status"] == "unlocated"
                            else not row.get("unknown")
                        )
                        for row in candidates
                    ),
                    "Saved locator mismatch for " + expected["family"],
                )
            require(
                any(
                    row.get("registered_location_check")
                    == "identity_differs_or_unavailable"
                    and row.get("address_basis") == "saved_endpoint"
                    for row in references
                ),
                "Current configuration masked saved endpoint",
            )
            require(
                any(row.get("unknown") == "missing_saved_owner" for row in references),
                "Unknown history disappeared",
            )
            for record in references:
                require(
                    record.get("unknown") != "unknown_saved_schema",
                    "Supported schema misclassified",
                )
            raw = output.read_bytes() + response.stdout + response.stderr
            require(
                SENTINEL.encode() not in raw and password.encode() not in raw,
                "Sensitive field escaped projection",
            )
            require(output.stat().st_mode & 0o777 == 0o600, "Catalog not private")
            require(not list(private.glob("aap-reference-*")), "Private spill remains")
            copy = plan(
                private / "copy.sqlite",
                source,
                output,
                {
                    "endpoint_url": "http://unused-plan-target.invalid:9000",
                    "bucket": "unused-target",
                    "key_prefix": "copy/",
                    "access_key_id": "unused-plan-only",
                    "secret_access_key": "unused-plan-only",
                },
                handling=dict.fromkeys(PURPOSES, "migrate"),
            )
            require(
                copy["source"]["exact_catalog_matches"] > 0,
                "Current catalog not accepted by existing copy planner",
            )
            denied_keys = {
                row["key"] for row in fixture.expected if row["status"] == "unlocated"
            }
            with Checkpoint(private / "copy.sqlite") as checkpoint:
                denied = [
                    row
                    for row in checkpoint.rows()
                    if row["source"]["key"] in denied_keys
                ]
            require(
                len(denied) == len(denied_keys)
                and all(not row["source_exact_catalog_match"] for row in denied),
                "Out-of-scope inventory became an exact copy-plan claim",
            )
            require(
                not copy["migration_ready"] and not summary["migration_ready"],
                "Catalog authorized cutover",
            )
            require(
                hashlib.sha256(source.read_bytes()).hexdigest() == source_sha,
                "Source report changed",
            )
        async with owner.connect() as connection:
            require(
                await database_facts(connection) == before, "CLI modified business rows"
            )
        require(
            object_facts(backend.s3, backend.resources["buckets"].values())
            == physical_before,
            "CLI modified source objects",
        )
        denials = 0
        for query in (
            "UPDATE storage_locations SET name=name WHERE false",
            "SELECT secret_enc FROM storage_connections",
        ):
            try:
                async with readonly.begin() as connection:
                    await connection.execute(text(query))
            except Exception as exc:
                require(
                    getattr(getattr(exc, "orig", None), "sqlstate", None)
                    in {"25006", "42501"},
                    "Readonly denial had an unexpected cause",
                )
                denials += 1
        require(denials == 2, "Readonly role has unintended privileges")
        return {
            "scope": "current saved-locator catalog; owned test; no app workflows or cutover",
            "revision": revision,
            "families_enumerated": len(FAMILIES),
            "populated_families": len(
                {
                    table
                    for table, _ in fixture.rows
                    if table in {f.table for f in FAMILIES}
                }
            ),
            "expected_locators_checked": len(fixture.expected),
            "references": len(references),
            "matches": summary["matches"],
            "source_versions": physical_before[1],
            "business_tables": len(before),
            "business_fingerprint_unchanged": True,
            "source_fingerprint_unchanged": True,
            "source_report_unchanged": True,
            "readonly_denials": denials,
            "private_output": True,
            "spills_cleaned": True,
            "existing_copy_plan_matches": copy["source"]["exact_catalog_matches"],
            "out_of_scope_inventory_not_exact": True,
            "migration_ready": False,
        }
    finally:
        if readonly:
            await readonly.dispose()
        if created:
            async with owner.begin() as connection:
                await connection.exec_driver_sql(f'DROP OWNED BY "{role}"')
                await connection.exec_driver_sql(f'DROP ROLE "{role}"')
            async with owner.connect() as connection:
                require(
                    not await connection.scalar(
                        text("SELECT 1 FROM pg_roles WHERE rolname=:role"),
                        {"role": role},
                    ),
                    "Owned readonly role remains",
                )
        await owner.dispose()
        backend.s3.close()


def main():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--report", type=Path, required=True)
    args = command.parse_args()
    try:
        result = asyncio.run(qualify(verified_environment()))
        result["temporary_role_removed"] = True
        atomic_json(args.report, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        detail = str(exc) if isinstance(exc, WorktreeError) else type(exc).__name__
        if hasattr(exc, "orig"):
            detail += ":" + str(getattr(exc.orig, "sqlstate", None))
        frames = traceback.extract_tb(exc.__traceback__)
        frame = next(
            (item for item in reversed(frames) if item.filename == __file__), frames[-1]
        )
        print(
            f"Current reference qualification failed: {detail} ({Path(frame.filename).name}:{frame.lineno})",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
