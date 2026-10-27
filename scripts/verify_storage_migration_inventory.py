"""Qualify source-only inventory on the launcher's disposable local test mode.

Creates synthetic object versions and an exact temporary read-only S3 account.
The launcher owner must subsequently destroy the test namespace.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from storage_migration_inventory import verify_inventory, write_inventory
from verify_legacy_storage_rollback import Accounts, require, verified_environment
from worktree_env import atomic_json


def records(path):
    return list(map(json.loads, path.read_text().splitlines()))


def source_facts(path):
    # Observation timestamps and headers differ; bucket/object/upload facts must not.
    def stable(record):
        result = {key: value for key, value in record.items() if key != "observed_at"}
        if "verification" in result:
            result["verification"] = {
                key: value
                for key, value in result["verification"].items()
                if key != "verified_at"
            }
        return result

    return sorted(
        [
            stable(record)
            for record in records(path)
            if record["type"] in {"bucket", "object", "delete_marker", "multipart"}
        ],
        key=lambda record: json.dumps(record, sort_keys=True),
    )


def policy(buckets):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket",
                    "s3:GetBucketLocation",
                    "s3:ListBucketVersions",
                    "s3:ListBucketMultipartUploads",
                    "s3:GetBucketVersioning",
                    "s3:GetLifecycleConfiguration",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}" for bucket in buckets],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Resource": [f"arn:aws:s3:::{bucket}/*" for bucket in buckets],
            },
        ],
    }


def require_empty_fixture(facts, client, buckets):
    require(
        all(
            name == "alembic_version" or row["count"] == 0
            for name, row in facts.items()
        ),
        "Qualification requires an empty disposable test database",
    )
    for bucket in buckets:
        listing = client.list_object_versions(Bucket=bucket, MaxKeys=1)
        uploads = client.list_multipart_uploads(Bucket=bucket, MaxUploads=1)
        require(
            not listing.get("Versions")
            and not listing.get("DeleteMarkers")
            and not uploads.get("Uploads"),
            "Qualification requires empty disposable test buckets",
        )


async def qualify(backend, directory, report):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.config import settings
    from app.db.models.async_job import AsyncJob
    from app.db.models.dataset_asset import StorageBinding, StorageLocation
    from app.services.dataset_assets import (
        PURPOSE_BUCKET_ATTRIBUTES,
        ensure_legacy_bindings,
    )
    from app.services.storage import storage_service
    from worktree_recovery_fixture import database_facts

    owner = create_async_engine(backend.url, hide_parameters=True)
    factory = async_sessionmaker(owner, expire_on_commit=False)
    purposes = {
        key: getattr(storage_service, attr)
        for key, attr in PURPOSE_BUCKET_ATTRIBUTES.items()
    }
    buckets = sorted(set(purposes.values()))
    account = Accounts(directory, backend)
    account.access_key = "aapi" + uuid.uuid4().hex[:16]
    account.role = "aap_inventory_" + uuid.uuid4().hex[:16]
    readonly = None
    report["temporary_service_account"] = account.access_key
    try:
        async with factory() as db:
            require_empty_fixture(await database_facts(db), backend.s3, buckets)
        for purpose in ("datasets", "training"):
            backend.s3.put_bucket_versioning(
                Bucket=purposes[purpose], VersioningConfiguration={"Status": "Enabled"}
            )
        key = "盘点样本/原件 %.bin"
        expected = []
        for purpose, bucket in sorted(purposes.items()):
            data = ("synthetic " + purpose).encode()
            response = backend.s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType="application/octet-stream",
                Metadata={"purpose": purpose},
            )
            expected.append(
                (
                    bucket,
                    key,
                    response.get("VersionId"),
                    hashlib.sha256(data).hexdigest(),
                )
            )
        for purpose in ("datasets", "training"):
            bucket = purposes[purpose]
            if purpose == "training":
                backend.s3.put_bucket_versioning(
                    Bucket=bucket, VersioningConfiguration={"Status": "Suspended"}
                )
            data = ("second " + purpose).encode()
            response = backend.s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType="application/octet-stream",
                Metadata={"purpose": purpose},
            )
            expected.append(
                (
                    bucket,
                    key,
                    response.get("VersionId")
                    or ("null" if purpose == "training" else None),
                    hashlib.sha256(data).hexdigest(),
                )
            )
        bucket = purposes["datasets"]
        deleted_key = "盘点样本/已删除.bin"
        response = backend.s3.put_object(
            Bucket=bucket, Key=deleted_key, Body=b"retained version"
        )
        expected.append(
            (
                bucket,
                deleted_key,
                response["VersionId"],
                hashlib.sha256(b"retained version").hexdigest(),
            )
        )
        backend.s3.delete_object(Bucket=bucket, Key=deleted_key)
        backend.s3.put_bucket_lifecycle_configuration(
            Bucket=bucket,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "synthetic-retention",
                        "Status": "Enabled",
                        "Filter": {"Prefix": "never-matches/"},
                        "Expiration": {"Days": 365},
                    }
                ]
            },
        )
        backend.s3.create_multipart_upload(Bucket=bucket, Key="盘点样本/未完成.bin")

        async with factory() as db:
            await ensure_legacy_bindings(db)
            location = StorageLocation(
                id=uuid.uuid4(),
                name="Inventory uncovered fixture",
                kind="filesystem",
                config={"root": "/not-probed"},
                config_revision=1,
                enabled=False,
            )
            db.add(location)
            await db.flush()
            binding = await db.get(StorageBinding, "avatars")
            binding.location_id = location.id
            db.add(AsyncJob(kind="export", status="running", payload={}, result={}))
            await db.commit()
            before_db = await database_facts(db)
        before_path = directory / "before.jsonl"
        require(
            write_inventory(before_path, backend.s3, purposes)["complete"],
            "Baseline inventory incomplete",
        )
        policy_path = directory / "policy.json"
        atomic_json(policy_path, policy(buckets))
        require(
            account.missing_account(
                account.mc("info", "rollback", account.access_key, success=False)
            ),
            "Refuse existing service account",
        )
        account.s3_created = True
        credential = json.loads(
            account.mc(
                "add",
                "rollback",
                settings.minio_access_key,
                "--access-key",
                account.access_key,
                "--policy",
                str(policy_path),
                "--name",
                account.role,
                "--expiry",
                (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            ).stdout
        )
        readonly = boto3.client(
            "s3",
            endpoint_url=f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}",
            aws_access_key_id=account.access_key,
            aws_secret_access_key=credential["secretKey"],
            config=Config(
                connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 1}
            ),
        )
        denied = []
        for operation, kwargs in (
            ("put_object", dict(Bucket=bucket, Key="write-trap", Body=b"forbidden")),
            ("delete_object", dict(Bucket=bucket, Key=key)),
        ):
            try:
                getattr(readonly, operation)(**kwargs)
            except ClientError as exc:
                require(
                    exc.response["ResponseMetadata"]["HTTPStatusCode"] == 403,
                    "Unexpected write refusal",
                )
                denied.append(operation)
            else:
                raise RuntimeError("Read-only account unexpectedly wrote")
        output = directory / "readonly.jsonl"
        environment = {
            **os.environ,
            "MINIO_ACCESS_KEY": account.access_key,
            "MINIO_SECRET_KEY": credential["secretKey"],
            "DEBUG": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        command = [
            sys.executable,
            str(Path(__file__).with_name("inventory_storage_migration.py")),
            "--output",
            str(output),
            "--page-size",
            "1",
        ]
        result = subprocess.run(
            command, env=environment, capture_output=True, timeout=180
        )
        require(result.returncode == 0, "Read-only CLI inventory failed")
        summary = verify_inventory(output)
        header = records(output)[0]
        require(
            summary["complete"] and not summary["migration_ready"],
            "Invalid completion scope",
        )
        require(
            source_facts(before_path) == source_facts(output),
            "Read-only inventory differs",
        )
        require(
            header["observations"]["uncovered_binding_purposes"] == ["avatars"],
            "Uncovered binding hidden",
        )
        require(
            any(
                row["kind"] == "export" and row["count"] == 1
                for row in header["observations"]["potential_writers"]
            ),
            "Potential writer missing",
        )
        observed = {
            (
                row["bucket"],
                row["key"],
                row.get("version_id"),
                row["verification"]["sha256"],
            )
            for row in records(output)
            if row["type"] == "object"
        }
        require(observed == set(expected), "Full version SHA/identity mismatch")
        require(
            summary["counts"]["delete_markers"] == 1
            and summary["counts"]["multipart"] == 1,
            "Version or upload omitted",
        )
        # The production CLI must also retain a valid but incomplete report on budget refusal.
        incomplete = directory / "limited.jsonl"
        refused = subprocess.run(
            [*command[:-4], "--output", str(incomplete), "--max-entries", "1"],
            env=environment,
            capture_output=True,
            timeout=180,
        )
        require(
            refused.returncode == 1 and not verify_inventory(incomplete)["complete"],
            "Budget result disguised as complete",
        )
        after_path = directory / "after.jsonl"
        require(
            write_inventory(after_path, backend.s3, purposes)["complete"],
            "Final source inventory incomplete",
        )
        require(
            source_facts(before_path) == source_facts(after_path),
            "Source facts changed",
        )
        async with factory() as db:
            after_db = await database_facts(db)
        require(before_db == after_db, "Business rows changed")
        report.update(
            status="passed",
            checks={
                "real_readonly_cli": True,
                "all_versions_sha256": len(expected),
                "delete_markers": 1,
                "unfinished_multipart": 1,
                "write_denials": denied,
                "uncovered_binding": "avatars",
                "potential_writer_observed": True,
                "incomplete_budget_report": True,
                "source_objects_configuration_unchanged": True,
                "business_rows_unchanged": True,
            },
            database={
                "tables": len(after_db),
                "rows": sum(row["count"] for row in after_db.values()),
            },
            counts=summary["counts"],
        )
    finally:
        try:
            if readonly is not None:
                readonly.close()
        finally:
            try:
                created = account.s3_created
                await account.close(owner)
                report.setdefault("cleanup", {})[
                    "service_account_independently_absent"
                ] = not account.s3_created if created else "not_created"
            finally:
                await owner.dispose()


def main():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--report", type=Path, required=True)
    options = command.parse_args()
    backend = verified_environment()
    report = {
        "status": "running",
        "scope": "owned local selected-source read-only inventory",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="aap-e07-live-") as temporary:
            asyncio.run(qualify(backend, Path(temporary), report))
    except BaseException as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        backend.s3.close()
        atomic_json(options.report, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
