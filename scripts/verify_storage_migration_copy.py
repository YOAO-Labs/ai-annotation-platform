"""Qualify only initial-copy CLI behavior in this checkout's disposable test mode.

The fixture owns source writes and exact random target buckets/accounts. CLI
children receive a read-only source account and a private target JSON file.
No application services or business consumers are started or modified.
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
import traceback
import uuid

from copy_storage_migration import client
from storage_migration_copy import Checkpoint, Limits
from storage_migration_inventory import InventoryLimits, PURPOSES, write_inventory
from storage_migration_references import (
    Budget,
    Limits as ReferenceLimits,
    canonical,
    write_catalog,
)
from verify_legacy_storage_rollback import Accounts, ROOT, require, verified_environment
from worktree_env import WorktreeError, atomic_json
from worktree_recovery_fixture import database_facts


def facts(s3, buckets):
    """Bounded complete source version, content, rule and unfinished-upload evidence."""
    values = []
    for bucket in sorted(buckets):
        versions = s3.list_object_versions(Bucket=bucket, MaxKeys=100)
        uploads = s3.list_multipart_uploads(Bucket=bucket, MaxUploads=100)
        require(
            not versions.get("IsTruncated") and not uploads.get("IsTruncated"),
            "Fixture pagination budget",
        )
        lifecycle = None
        try:
            lifecycle = s3.get_bucket_lifecycle_configuration(Bucket=bucket).get(
                "Rules"
            )
        except Exception as exc:
            require(
                str(getattr(exc, "response", {}).get("Error", {}).get("Code"))
                == "NoSuchLifecycleConfiguration",
                "Cannot observe lifecycle",
            )
        values.append(
            [
                bucket,
                "rules",
                s3.get_bucket_versioning(Bucket=bucket).get("Status"),
                lifecycle,
            ]
        )
        for kind in ("Versions", "DeleteMarkers"):
            for item in versions.get(kind, []):
                data = None
                metadata = None
                if kind == "Versions":
                    response = s3.get_object(
                        Bucket=bucket, Key=item["Key"], VersionId=item["VersionId"]
                    )
                    try:
                        body = response["Body"].read(65537)
                        require(len(body) <= 65536, "Fixture byte budget")
                        data = hashlib.sha256(body).hexdigest()
                        metadata = response.get("Metadata")
                    finally:
                        response["Body"].close()
                values.append(
                    [
                        bucket,
                        kind,
                        item["Key"],
                        item["VersionId"],
                        item["IsLatest"],
                        item.get("ETag"),
                        item.get("Size"),
                        data,
                        metadata,
                    ]
                )
        for item in uploads.get("Uploads", []):
            values.append([bucket, "multipart", item["Key"], item["UploadId"]])
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, default=str).encode()
    ).hexdigest()


class Account(Accounts):
    """Use the existing exact-ID revocation helper without creating a DB role."""

    def create_s3(self, policy):
        policy_path = self.directory / (self.access_key + ".json")
        atomic_json(policy_path, policy)
        require(
            self.missing_account(
                self.mc("info", "rollback", self.access_key, success=False)
            ),
            "Refuse existing target account",
        )
        self.s3_created = True
        response = self.mc(
            "add",
            "rollback",
            self.settings.minio_access_key,
            "--access-key",
            self.access_key,
            "--policy",
            str(policy_path),
            "--name",
            "initial-copy-fixture",
            "--expiry",
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        return json.loads(response.stdout)["secretKey"]


def policy(buckets, *, target=False):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetBucketLocation",
                    "s3:GetBucketVersioning",
                    "s3:ListBucket",
                    "s3:ListBucketVersions",
                    *(
                        []
                        if target
                        else [
                            "s3:GetLifecycleConfiguration",
                            "s3:ListBucketMultipartUploads",
                        ]
                    ),
                ],
                "Resource": [f"arn:aws:s3:::{bucket}" for bucket in buckets],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    *(["s3:PutObject"] if target else []),
                ],
                "Resource": [f"arn:aws:s3:::{bucket}/*" for bucket in buckets],
            },
        ],
    }


def cli(arguments, environment, *, event=None, failure=False, expect=(0, 1)):
    command = [
        sys.executable,
        str(ROOT / "scripts/copy_storage_migration.py"),
        *map(str, arguments),
    ]
    if event or failure:
        # Fault injection lives in this owner-only harness, never in the operator CLI.
        wrapper = """
import os, signal, sys
sys.path.insert(0, sys.argv.pop(1))
import copy_storage_migration as cli
original = cli.execute
point = sys.argv.pop(1)
def event(name, row):
    if point in {'signal_sigint', 'signal_sigterm'} and name == 'before_put':
        os.kill(os.getpid(), signal.SIGINT if point == 'signal_sigint' else signal.SIGTERM)
    elif name == point:
        os._exit(91)
def execute(*args, **kwargs):
    return original(*args, **kwargs, event=event)
cli.execute = execute
if point == 'source_failure':
    original_source = cli.configured_source
    def source(*args):
        result = original_source(*args)
        original_get = result.get_object
        failed = False
        def denied(**kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError('fixture denied')
            return original_get(**kwargs)
        result.get_object = denied
        return result
    cli.configured_source = source
raise SystemExit(cli.main())
"""
        command = [
            sys.executable,
            "-c",
            wrapper,
            str(ROOT / "scripts"),
            event or "source_failure",
            *map(str, arguments),
        ]
    result = subprocess.run(
        command, cwd=ROOT, env=environment, capture_output=True, timeout=90
    )
    require(
        result.returncode in expect,
        "Copy CLI returned unexpected exit code " + str(result.returncode),
    )
    if event and not event.startswith("signal_"):
        require(result.returncode == 91, "Interruption window was not exercised")
        return None
    require(not result.stderr, "Copy CLI emitted an unexpected error")
    return json.loads(result.stdout)


def rows(path):
    with Checkpoint(path) as state:
        return list(state.rows())


def target_facts(s3, bucket):
    response = s3.list_object_versions(Bucket=bucket, MaxKeys=100)
    require(not response.get("IsTruncated"), "Target fixture budget")
    return sorted(
        (row["Key"], row["VersionId"], row.get("ETag"))
        for row in response.get("Versions", [])
    )


def clean_bucket(s3, bucket, owner):
    tags = s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
    require(
        {row["Key"]: row["Value"] for row in tags}.get("aap-copy-fixture") == owner,
        "Target ownership mismatch",
    )
    response = s3.list_object_versions(Bucket=bucket, MaxKeys=100)
    require(not response.get("IsTruncated"), "Target cleanup budget")
    for kind in ("Versions", "DeleteMarkers"):
        for row in response.get(kind, []):
            s3.delete_object(Bucket=bucket, Key=row["Key"], VersionId=row["VersionId"])
    s3.delete_bucket(Bucket=bucket)
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception as exc:
        require(
            str(getattr(exc, "response", {}).get("Error", {}).get("Code"))
            in {"404", "NoSuchBucket"},
            "Cannot prove target bucket absence",
        )
    else:
        raise WorktreeError("Target bucket still exists")


async def qualify(backend, directory):
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.services.dataset_assets import PURPOSE_BUCKET_ATTRIBUTES

    engine = create_async_engine(backend.url, hide_parameters=True)
    owner = uuid.uuid4().hex
    bucket = "aap-copy-" + owner[:24]
    created = False
    source_account, target_account = (
        Account(directory, backend),
        Account(directory, backend),
    )
    target_account.access_key = "aapc" + uuid.uuid4().hex[:16]
    readonly = None
    evidence = {}
    try:
        async with engine.connect() as connection:
            business_before = await database_facts(connection)
        require(
            all(
                name == "alembic_version" or row["count"] == 0
                for name, row in business_before.items()
            ),
            "Fixture requires empty business tables",
        )
        # The StorageService attribute names match the environment settings.
        from app.services.storage import StorageService

        storage = StorageService()
        try:
            purposes = {
                purpose: getattr(storage, attr)
                for purpose, attr in PURPOSE_BUCKET_ATTRIBUTES.items()
            }
        finally:
            storage.client.close()
        # Two reviewed purposes deliberately share one physical source bucket.
        shared = purposes["datasets"]
        purposes["media-cache"] = shared
        source_buckets = sorted(set(purposes.values()))
        for source_bucket in set(backend.resources["buckets"].values()):
            require(
                not backend.s3.list_object_versions(
                    Bucket=source_bucket, MaxKeys=1
                ).get("Versions"),
                "Source fixture is not empty",
            )
        backend.s3.put_object(
            Bucket=shared,
            Key="mutable-null/空 %.bin",
            Body=b"null-slot",
            ContentType="application/test",
            Metadata={"fixture": "null"},
        )
        backend.s3.put_bucket_versioning(
            Bucket=shared, VersioningConfiguration={"Status": "Enabled"}
        )
        expected = []
        for key, body in [
            ("empty", b""),
            ("历史/%2F/中文 %.bin", b"old"),
            ("历史/%2F/中文 %.bin", b"new"),
            ("deleted", b"deleted-body"),
        ]:
            response = backend.s3.put_object(
                Bucket=shared,
                Key=key,
                Body=body,
                ContentType="application/octet-stream",
                CacheControl="max-age=17",
                ContentDisposition="inline",
                Metadata={"fixture": "actual-source"},
            )
            expected.append(
                (key, response.get("VersionId"), hashlib.sha256(body).hexdigest())
            )
        backend.s3.delete_object(Bucket=shared, Key="deleted")
        backend.s3.create_multipart_upload(Bucket=shared, Key="unfinished")
        source_secret = source_account.create_s3(policy(source_buckets))
        readonly = client(
            backend.s3.meta.endpoint_url,
            source_account.access_key,
            source_secret,
            Limits(),
        )
        denied = 0
        for operation in ("put_object", "delete_object"):
            try:
                getattr(readonly, operation)(
                    Bucket=shared,
                    Key="denied-probe",
                    **({"Body": b"denied"} if operation == "put_object" else {}),
                )
            except Exception as exc:
                require(
                    str(getattr(exc, "response", {}).get("Error", {}).get("Code"))
                    == "AccessDenied",
                    "Unexpected source permission probe response",
                )
                denied += 1
        require(denied == 2, "CLI source credentials permit writes")
        source = directory / "source.jsonl"
        summary = write_inventory(
            source,
            readonly,
            purposes,
            source={"endpoint": backend.s3.meta.endpoint_url},
            handling=dict.fromkeys(PURPOSES, "migrate"),
            limits=InventoryLimits(
                max_object_bytes=65536,
                max_total_bytes=1024**2,
                max_entries=100,
                max_pages=100,
            ),
        )
        require(summary["complete"], "Bounded copy input inventory incomplete")
        spool = directory / "spool.jsonl"
        spool.write_bytes(
            b"".join(
                canonical(
                    {
                        "type": "reference",
                        "family": "fixture",
                        "owner_id": ["synthetic"],
                        "role": "saved_owner",
                        "endpoint_url": backend.s3.meta.endpoint_url,
                        "bucket": shared,
                        "key": key,
                        "version_id": version,
                        "sha256": sha,
                    }
                )
                for key, version, sha in expected
            )
        )
        catalog = directory / "references.jsonl"
        write_catalog(
            catalog,
            spool,
            {"enumeration_complete": True, "families": ["fixture"]},
            source,
            directory,
            Budget(ReferenceLimits(max_seconds=60)),
        )
        require(
            not any(
                item["Name"] == bucket for item in backend.s3.list_buckets()["Buckets"]
            ),
            "Refuse existing target bucket",
        )
        backend.s3.create_bucket(Bucket=bucket)
        created = True
        backend.s3.put_bucket_tagging(
            Bucket=bucket,
            Tagging={"TagSet": [{"Key": "aap-copy-fixture", "Value": owner}]},
        )
        backend.s3.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        target_secret = target_account.create_s3(policy([bucket], target=True))
        target_file = directory / "target.json"
        atomic_json(
            target_file,
            {
                "endpoint_url": backend.s3.meta.endpoint_url,
                "bucket": bucket,
                "key_prefix": "initial/",
                "access_key_id": target_account.access_key,
                "secret_access_key": target_secret,
            },
        )
        environment = {
            **os.environ,
            "MINIO_ACCESS_KEY": source_account.access_key,
            "MINIO_SECRET_KEY": source_secret,
            "DEBUG": "false",
            "LOG_LEVEL": "WARNING",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        source_before = facts(backend.s3, source_buckets)
        input_before = [
            hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, catalog)
        ]
        windows = []
        for window in ("before_item", "before_put", "after_put", "after_item"):
            checkpoint = directory / (window + ".sqlite")
            base = ["--checkpoint", checkpoint, "--target-config", target_file]
            planned = cli(
                ["plan", *base, "--source-inventory", source, "--references", catalog],
                environment,
            )
            require(
                planned["decisions"].get("delete_marker") == 1
                and planned["facts"].get("multipart") == 1,
                "Non-body facts were lost",
            )
            cli(["run", *base], environment, event=window, expect=(91,))
            before_resume = target_facts(backend.s3, bucket)
            resumed = cli(["run", *base], environment)
            require(
                resumed["copy_subset_complete"]
                and resumed["counts"].get("verified") == 5,
                "Resume did not verify every source version",
            )
            after_resume = target_facts(backend.s3, bucket)
            require(
                all(item in after_resume for item in before_resume),
                "Resume replaced target version",
            )
            require(
                len({key for key, version, etag in after_resume}) == len(after_resume),
                "Resume uploaded duplicate target versions",
            )
            verified = cli(["verify", *base], environment)
            require(
                verified["copy_subset_complete"]
                and verified["io"]["upload_bytes"] == 0,
                "Verify uploaded or failed",
            )
            checkpoint_rows = rows(checkpoint)
            for row in checkpoint_rows:
                if row["decision"] != "copy":
                    continue
                receipt = row["receipt"]
                require(
                    receipt["version_id"]
                    not in {row["source"]["version_id"], None, "null"},
                    "Target version was assumed from source",
                )
                response = backend.s3.get_object(
                    Bucket=bucket,
                    Key=row["target_key"],
                    VersionId=receipt["version_id"],
                )
                try:
                    body = response["Body"].read(65537)
                    require(
                        hashlib.sha256(body).hexdigest() == row["sha256"]
                        and len(body) == row["size_bytes"],
                        "Independent target bytes differ",
                    )
                    require(
                        response["Metadata"].get("aap-copy-nonce") == row["nonce"],
                        "Independent target creation metadata differs",
                    )
                    require(
                        response["Metadata"].get("fixture")
                        == row["content"]["Metadata"]["fixture"],
                        "Actual source metadata was not preserved",
                    )
                finally:
                    response["Body"].close()
            require(
                any(row["source"]["version_id"] == "null" for row in checkpoint_rows),
                "Mutable null slot was lost",
            )
            require(
                not list(directory.glob(".aap-copy-*")),
                "Private staging remains after restart",
            )
            windows.append(
                {
                    "window": window,
                    "verified": resumed["counts"]["verified"],
                    "no_duplicate_target_versions": True,
                }
            )
        for point in ("signal_sigint", "signal_sigterm"):
            checkpoint = directory / (point + ".sqlite")
            base = ["--checkpoint", checkpoint, "--target-config", target_file]
            cli(
                ["plan", *base, "--source-inventory", source, "--references", catalog],
                environment,
            )
            paused = cli(["run", *base], environment, event=point, expect=(1,))
            require(paused["paused"] == "signal_pause", "Signal did not pause CLI")
            require(
                cli(["run", *base], environment)["copy_subset_complete"],
                "Signal restart failed",
            )
        checkpoint = directory / "retry.sqlite"
        base = ["--checkpoint", checkpoint, "--target-config", target_file]
        cli(
            ["plan", *base, "--source-inventory", source, "--references", catalog],
            environment,
        )
        failed = cli(["run", *base], environment, failure=True)
        require(
            failed["counts"].get("failed") == 1, "Failure fixture was not exercised"
        )
        skipped = cli(["run", *base], environment)
        require(
            skipped["counts"].get("failed") == 1 and skipped["io"]["objects"] == 0,
            "Failed objects retried without flag",
        )
        retried = cli(["run", *base, "--retry-failed"], environment)
        require(retried["copy_subset_complete"], "Explicit failed retry failed")
        # A foreign existing target must remain one unchanged version.
        conflict = directory / "conflict.sqlite"
        base = ["--checkpoint", conflict, "--target-config", target_file]
        cli(
            ["plan", *base, "--source-inventory", source, "--references", catalog],
            environment,
        )
        foreign_row = next(row for row in rows(conflict) if row["decision"] == "copy")
        foreign = backend.s3.put_object(
            Bucket=bucket, Key=foreign_row["target_key"], Body=b"foreign"
        )
        cli(["run", *base], environment)
        cli(["run", *base], environment)
        foreign_facts = [
            item
            for item in target_facts(backend.s3, bucket)
            if item[0] == foreign_row["target_key"]
        ]
        require(
            len(foreign_facts) == 1 and foreign_facts[0][1] == foreign["VersionId"],
            "Foreign target was overwritten",
        )
        require(
            not cli(["status", "--checkpoint", conflict], environment)[
                "copy_subset_complete"
            ],
            "Conflict reported complete",
        )
        require(
            facts(backend.s3, source_buckets) == source_before,
            "CLI modified source facts",
        )
        require(
            [
                hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (source, catalog)
            ]
            == input_before,
            "CLI modified sealed input",
        )
        async with engine.connect() as connection:
            require(
                await database_facts(connection) == business_before,
                "CLI modified business facts",
            )
        evidence.update(
            {
                "scope": "local owned MinIO initial copy only; no application cutover",
                "windows": windows,
                "failed_objects_retried_explicitly": 1,
                "signals_resumed": ["SIGINT", "SIGTERM"],
                "source_write_denials": denied,
                "target_versions_observed": len(target_facts(backend.s3, bucket)),
                "target_versions_verified_by_cli": len(target_facts(backend.s3, bucket))
                - 1,
                "target_versions_independently_read": sum(
                    item["verified"] for item in windows
                ),
                "raw_special_empty_historical_null_keys": True,
                "shared_physical_bucket": True,
                "source_facts_unchanged": True,
                "business_tables_unchanged": len(business_before),
                "sealed_inputs_unchanged": True,
                "unknown_target_not_overwritten": True,
                "private_staging_cleaned": True,
                "migration_ready": False,
            }
        )
        return evidence
    finally:
        if readonly:
            readonly.close()
        failures = []
        for account in (source_account, target_account):
            try:
                await account.close(engine)
            except Exception:
                failures.append("account_cleanup")
        if created:
            try:
                clean_bucket(backend.s3, bucket, owner)
            except Exception:
                failures.append("target_cleanup")
        evidence["temporary_accounts_removed"] = not failures
        evidence["target_bucket_independently_absent"] = not failures
        await engine.dispose()
        backend.s3.close()
        require(not failures, "Fixture cleanup failed: " + ",".join(failures))


def main():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--report", type=Path, required=True)
    args = command.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix="aap-copy-verification-") as directory:
            result = asyncio.run(qualify(verified_environment(), Path(directory)))
        atomic_json(args.report, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        detail = str(exc) if isinstance(exc, WorktreeError) else type(exc).__name__
        if hasattr(exc, "operation_name"):
            detail += (
                ":"
                + exc.operation_name
                + ":"
                + str(exc.response.get("Error", {}).get("Code"))
            )
        frames = traceback.extract_tb(exc.__traceback__)
        frame = next(
            (item for item in reversed(frames) if item.filename == __file__), frames[-1]
        )
        detail += f" ({Path(frame.filename).name}:{frame.lineno})"
        print("Initial-copy qualification failed: " + detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
