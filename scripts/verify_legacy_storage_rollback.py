"""Qualify a frozen application's controlled read-only storage rollback locally.

Run through dev:worktree exec --mode test. Uses only that owned namespace,
creates/revokes its own temporary DB role and MinIO service account, and retains
synthetic fixture rows until the launcher owner destroys the test namespace.
This is not a deployment switch or a database downgrade command.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

from worktree_env import (  # noqa: E402
    WorktreeError,
    application_environment,
    atomic_json,
    load_identity,
    topology,
)
from worktree_resources import LocalResources  # noqa: E402
from worktree_runtime import validate_inventory  # noqa: E402

LEGACY_COMMIT = "e169d9ef53a9eda60abd4e67bf553ab4e6ab618e"


def require(condition, message):
    if not condition:
        raise WorktreeError(message)


def verified_environment():
    from app.config import settings

    require(__debug__, "Qualification requires assertions")
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "Use owned test mode")
    resources = topology(ROOT, load_identity(ROOT), "test")
    backend = LocalResources(resources, settings, code_root=ROOT)
    try:
        inventory = backend.inspect()
        validate_inventory(resources, inventory)
        require(inventory["database"] is not None, "Initialize test mode first")
        expected = backend.environment()
        for key in (
            "DATABASE_URL",
            "MIGRATION_DATABASE_URL",
            "REDIS_URL",
            "MINIO_ENDPOINT",
            *resources["buckets"],
        ):
            require(os.environ.get(key) == expected[key], "Test target mismatch")
        return backend
    except BaseException:
        backend.s3.close()
        raise


def readonly_policy(buckets, originals):
    """HEAD existing buckets, GET only reviewed legacy objects; no write grants."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": [f"arn:aws:s3:::{bucket}" for bucket in buckets],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [
                    f"arn:aws:s3:::{bucket}/{key}" for bucket, key in originals
                ],
            },
        ],
    }


class Accounts:
    """Own exact random credentials, never modify an existing role or user."""

    def __init__(self, directory, backend):
        from app.config import settings

        self.directory, self.backend, self.settings = directory, backend, settings
        self.role = "aap_rollback_" + uuid.uuid4().hex[:16]
        self.access_key = "aapr" + uuid.uuid4().hex[:16]
        self.db_created = self.s3_created = False
        scheme = "https" if settings.minio_use_ssl else "http"
        self.mc_environment = {
            **os.environ,
            "MC_CONFIG_DIR": str(directory / "mc"),
            "MC_HOST_rollback": f"{scheme}://{quote(settings.minio_access_key, safe='')}:{quote(settings.minio_secret_key, safe='')}@{settings.minio_endpoint}",
        }

    def mc(self, *args, success=True):
        result = subprocess.run(
            ["mc", "--json", "admin", "user", "svcacct", *args],
            env=self.mc_environment,
            capture_output=True,
            timeout=30,
        )
        if success:
            if result.returncode:
                detail = json.loads(result.stdout)
                error = detail.get("error", {})
                cause = error.get("cause", {})
                raise WorktreeError(
                    "MinIO service account command failed: "
                    + str(
                        cause.get("message") or error.get("message") or "unknown error"
                    )
                )
        return result

    @staticmethod
    def missing_account(result):
        return result.returncode != 0 and (
            "specified service account does not exist" in result.stdout.decode().lower()
            or "XMinioInvalidIAMCredentials" in result.stdout.decode()
        )

    async def create(self, owner, policy):
        from sqlalchemy import text

        password = secrets.token_urlsafe(32)
        async with owner.begin() as connection:
            require(
                not await connection.scalar(
                    text("SELECT 1 FROM pg_roles WHERE rolname=:name"),
                    {"name": self.role},
                ),
                "Refuse existing role",
            )
            # Both interpolated values are generated above from fixed alphabets.
            self.db_created = True
            await connection.exec_driver_sql(
                f"CREATE ROLE \"{self.role}\" LOGIN PASSWORD '{password}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
            )
        async with owner.begin() as connection:
            for sql in (
                f'GRANT CONNECT ON DATABASE "{self.backend.resources["database"]}" TO "{self.role}"',
                f'GRANT USAGE ON SCHEMA public TO "{self.role}"',
                f"GRANT SELECT ON users, projects, project_members, task_batches, "
                f"datasets, dataset_items, project_datasets, tasks, annotations, "
                f'predictions, image_pyramid_assets, task_dataset_item_links TO "{self.role}"',
                f'ALTER ROLE "{self.role}" SET default_transaction_read_only = on',
            ):
                await connection.exec_driver_sql(sql)
        policy_path = self.directory / "readonly-policy.json"
        atomic_json(policy_path, policy)
        expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        require(
            self.missing_account(
                self.mc("info", "rollback", self.access_key, success=False)
            ),
            "Refuse existing or unobservable service account",
        )
        # Set a recoverable owned ID before the request; a lost response can
        # still be revoked without logging or guessing a generated credential.
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
            self.role,
            "--expiry",
            expiry,
        )
        credential = json.loads(response.stdout)
        self.secret_key = credential["secretKey"]
        self.database_url = self.backend.url.set(
            username=self.role, password=password
        ).render_as_string(hide_password=False)

    async def close(self, owner):
        from sqlalchemy import text

        failures = []
        try:
            if self.s3_created:
                before = self.mc("info", "rollback", self.access_key, success=False)
                if not self.missing_account(before):
                    require(
                        before.returncode == 0,
                        "Cannot inspect temporary service account",
                    )
                    self.mc("rm", "rollback", self.access_key)
                require(
                    self.missing_account(
                        self.mc("info", "rollback", self.access_key, success=False)
                    ),
                    "Cannot confirm service account removal",
                )
                self.s3_created = False
        except Exception:
            failures.append("MinIO service account")
        try:
            if self.db_created:
                async with owner.begin() as connection:
                    exists = await connection.scalar(
                        text("SELECT 1 FROM pg_roles WHERE rolname=:name"),
                        {"name": self.role},
                    )
                    if exists:
                        await connection.exec_driver_sql(f'DROP OWNED BY "{self.role}"')
                        await connection.exec_driver_sql(f'DROP ROLE "{self.role}"')
                    require(
                        not await connection.scalar(
                            text("SELECT 1 FROM pg_roles WHERE rolname=:name"),
                            {"name": self.role},
                        ),
                        "Readonly role still exists",
                    )
                self.db_created = False
        except Exception:
            failures.append("database role")
        require(
            not failures, "Temporary credential cleanup failed: " + ", ".join(failures)
        )


async def seed(factory, directory, backend, patches):
    from PIL import Image
    from sqlalchemy import select

    from app.core.security import create_access_token
    from app.db.models.annotation import Annotation
    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
    from app.db.models.task import Task
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.db.models.user import User
    from app.services.dataset_assets import register_item_asset
    from tests.factory import create_project, make_user_dict
    from tests.test_dataset_content import create_content_world
    from tests.test_dataset_references import create_reference_world

    async with factory() as db:
        actor = User(
            **make_user_dict(
                "super_admin",
                f"rollback-{uuid.uuid4()}@example.test",
                "Rollback qualification",
            )
        )
        db.add(actor)
        await db.commit()
        content = await create_content_world(db, actor, directory, patches)
        registered, _, reference_task, _ = await create_reference_world(
            db, content, actor
        )
        reference = await db.get(DatasetItem, uuid.UUID(str(registered["item_id"])))
        buffer = io.BytesIO()
        Image.new("RGB", (19, 11), "blue").save(buffer, format="PNG")
        raw = buffer.getvalue()
        dataset = Dataset(
            display_id="old-" + uuid.uuid4().hex[:12],
            name="Legacy before rename",
            created_by=actor.id,
            file_count=1,
        )
        db.add(dataset)
        await db.flush()
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name="原图.png",
            file_path=f"old-{uuid.uuid4()}/旧名称/原图.png",
            file_type="image",
            file_size=len(raw),
            width=19,
            height=11,
        )
        db.add(item)
        await db.flush()
        await register_item_asset(
            db, item, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
        )
        project = await create_project(db, owner_id=actor.id)
        task = Task(
            display_id="old-" + uuid.uuid4().hex[:12],
            project_id=project.id,
            dataset_item_id=item.id,
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="image",
            status="in_progress",
        )
        db.add_all([task, ProjectDataset(project_id=project.id, dataset_id=dataset.id)])
        await db.flush()
        db.add(
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=actor.id,
                class_name="car",
                geometry={"type": "bbox", "x": 1, "y": 2, "width": 3, "height": 4},
            )
        )
        db.add(
            TrainingDatasetVersion(
                project_id=project.id,
                creator_id=actor.id,
                request_id=uuid.uuid4(),
                request_digest="b" * 64,
                name="Frozen before rollback",
                status="ready",
                snapshot={"tasks": [str(task.id)], "legacy_path": item.file_path},
                snapshot_digest="c" * 64,
                manifest={"historical": True},
            )
        )
        await db.commit()
        require(
            await db.scalar(
                select(DatasetItem.id).where(DatasetItem.id == reference.id)
            ),
            "Reference registration missing",
        )
        bucket = backend.resources["buckets"]["MINIO_DATASETS_BUCKET"]
        backend.s3.put_object(
            Bucket=bucket, Key=item.file_path, Body=raw, ContentType="image/png"
        )
        # A real colliding opaque key must still be unreadable to the old app.
        backend.s3.put_object(
            Bucket=bucket, Key=reference.file_path, Body=b"opaque-key-trap"
        )
        return {
            "actor": actor,
            "token": create_access_token(str(actor.id), actor.role),
            "dataset": dataset,
            "item": item,
            "task": task,
            "reference": reference,
            "reference_task": reference_task,
            "reference_dataset": content[3],
            "reference_file": content[0] / "图片 空格.png",
            "reference_raw": content[1],
            "reference_sha": registered["sha256"],
            "raw": raw,
            "bucket": bucket,
        }


def archive(directory, commit):
    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", commit + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    path = directory / "application.tar"
    subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            "--output",
            str(path),
            resolved,
            "apps/api/app",
            "apps/_shared",
        ],
        cwd=ROOT,
        check=True,
    )
    target = directory / "legacy"
    target.mkdir()
    with tarfile.open(path) as source:
        source.extractall(target, filter="data")
    return target, resolved


@contextmanager
def running_legacy(directory, archive_root, accounts):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    environment = application_environment(dict(os.environ))
    environment.update(
        DATABASE_URL=accounts.database_url,
        MIGRATION_DATABASE_URL="",
        MINIO_ACCESS_KEY=accounts.access_key,
        MINIO_SECRET_KEY=accounts.secret_key,
        MINIO_PUBLIC_URL="",
        STORAGE_FILESYSTEM_ROOTS="{}",
        CONNECTOR_ENCRYPTION_KEY="",
        E2E_SEED_ENABLED="false",
        AUDIT_ASYNC="false",
        DEBUG="false",
        PYTHONPATH=os.pathsep.join(
            str(path)
            for path in [
                archive_root / "apps/api",
                *sorted((archive_root / "apps/_shared").glob("*/src")),
            ]
        ),
        PYTHONDONTWRITEBYTECODE="1",
    )
    command = (
        "from pathlib import Path; import app.main,uvicorn; "
        "assert Path(app.main.__file__).resolve().is_relative_to(Path.cwd()); "
        f"uvicorn.run(app.main.app,fd={listener.fileno()},log_level='warning',access_log=False)"
    )
    log_path = directory / "legacy-server.log"
    process = None
    try:
        with log_path.open("wb") as output:
            process = subprocess.Popen(
                [str(ROOT / "apps/api/.venv/bin/python"), "-c", command],
                cwd=archive_root / "apps/api",
                env=environment,
                pass_fds=(listener.fileno(),),
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            listener.close()
            yield process, f"http://127.0.0.1:{port}", log_path
    finally:
        listener.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


async def current_reads(world, factory):
    import httpx
    from app.main import app
    from app.deps import get_db

    async def database():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://current",
            headers={"Authorization": "Bearer " + world["token"]},
        ) as client:
            for task, sha, raw in (
                (world["task"], hashlib.sha256(world["raw"]).hexdigest(), world["raw"]),
                (
                    world["reference_task"],
                    world["reference_sha"],
                    world["reference_raw"],
                ),
            ):
                response = await client.get(
                    f"/api/v1/files/tasks/{task.id}/content", params={"sha256": sha}
                )
                require(
                    response.status_code == 200 and response.content == raw,
                    "Current verified read failed",
                )
    finally:
        app.dependency_overrides.pop(get_db, None)


async def qualify(backend, options, directory, report):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    import httpx
    import pytest
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from worktree_recovery_fixture import inspect_database

    owner = create_async_engine(backend.url, poolclass=NullPool, hide_parameters=True)
    factory = async_sessionmaker(owner, expire_on_commit=False)
    accounts = Accounts(directory, backend)
    report["temporary_accounts"] = {
        "database_role": accounts.role,
        "service_account": accounts.access_key,
    }
    atomic_json(options.report, {**report, "status": "running"})
    readonly = ro_s3 = None
    try:
        with pytest.MonkeyPatch.context() as patches:
            from app.config import settings

            patches.setattr(settings, "debug", False)
            world = await seed(factory, directory, backend, patches)
            await current_reads(world, factory)
            async with factory() as db:
                before = await inspect_database(db)
            await accounts.create(
                owner,
                readonly_policy(
                    backend.resources["buckets"].values(),
                    [(world["bucket"], world["item"].file_path)],
                ),
            )
            readonly = create_async_engine(
                accounts.database_url, poolclass=NullPool, hide_parameters=True
            )
            async with readonly.connect() as connection:
                require(
                    await connection.scalar(text("SHOW transaction_read_only")) == "on",
                    "Readonly transaction not enforced",
                )
                require(
                    not await connection.scalar(
                        text(
                            "SELECT rolsuper OR rolcreaterole OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
                        )
                    ),
                    "Privileged rollback role",
                )
            async with readonly.connect() as connection:
                try:
                    await connection.execute(
                        text("SELECT * FROM storage_connections LIMIT 0")
                    )
                except Exception as exc:
                    require(
                        getattr(getattr(exc, "orig", None), "sqlstate", None)
                        == "42501",
                        "Expected source credential table denial",
                    )
                else:
                    raise WorktreeError("Old app could select source credentials")
            # Even overriding the transaction default cannot grant table writes.
            for statement in (
                "UPDATE dataset_items SET file_name=file_name WHERE false",
                "DELETE FROM dataset_items WHERE false",
                "INSERT INTO dataset_items SELECT * FROM dataset_items WHERE false",
                "TRUNCATE dataset_items",
            ):
                async with readonly.connect() as connection:
                    await connection.execute(text("SET TRANSACTION READ WRITE"))
                    try:
                        await connection.execute(text(statement))
                    except Exception as exc:
                        require(
                            getattr(getattr(exc, "orig", None), "sqlstate", None)
                            == "42501",
                            "Expected DB privilege denial",
                        )
                    else:
                        raise WorktreeError("Readonly role unexpectedly wrote")
            report["checks"]["database_privileges"] = (
                "select-only; four write classes denied"
            )
            ro_s3 = boto3.client(
                "s3",
                endpoint_url=backend.s3.meta.endpoint_url,
                aws_access_key_id=accounts.access_key,
                aws_secret_access_key=accounts.secret_key,
                config=Config(signature_version="s3v4", retries={"max_attempts": 0}),
            )
            for method, kwargs in (
                ("put_object", {"Key": world["item"].file_path, "Body": b"changed"}),
                ("delete_object", {"Key": world["item"].file_path}),
                ("get_object", {"Key": world["reference"].file_path}),
            ):
                try:
                    getattr(ro_s3, method)(Bucket=world["bucket"], **kwargs)
                except ClientError as exc:
                    require(
                        exc.response["ResponseMetadata"]["HTTPStatusCode"] == 403,
                        "Expected S3 policy denial",
                    )
                else:
                    raise WorktreeError(
                        "Readonly S3 policy unexpectedly allowed access"
                    )
            report["checks"]["object_privileges"] = (
                "PUT, DELETE and opaque-key trap GET denied"
            )
            archive_root, resolved = archive(directory, options.legacy_commit)
            report["legacy_commit"] = resolved
            with running_legacy(directory, archive_root, accounts) as (
                process,
                origin,
                log,
            ):
                async with (
                    httpx.AsyncClient(
                        base_url=origin,
                        headers={"Authorization": "Bearer " + world["token"]},
                        timeout=10,
                    ) as client,
                    httpx.AsyncClient(timeout=10) as downloads,
                ):
                    deadline = time.monotonic() + 45
                    while True:
                        require(
                            process.poll() is None,
                            "Frozen application exited before readiness",
                        )
                        try:
                            response = await client.get("/health")
                            if response.status_code == 503:
                                checks = response.json().get("checks", {})
                                if (
                                    all(
                                        checks.get(name, {}).get("status") == "ok"
                                        for name in ("db", "redis", "minio")
                                    )
                                    and checks.get("celery", {}).get("status")
                                    == "error"
                                    and checks["celery"].get("active_count") == 0
                                ):
                                    report["checks"]["frozen_health"] = (
                                        "DB, Redis and MinIO healthy; aggregate degraded because workers are deliberately stopped"
                                    )
                                    break
                        except httpx.HTTPError:
                            pass
                        require(
                            time.monotonic() < deadline,
                            "Frozen application readiness timeout",
                        )
                        await asyncio.sleep(0.1)
                    for url, field in (
                        (f"/api/v1/datasets/{world['dataset'].id}/items", "items"),
                        (f"/api/v1/tasks/{world['task'].id}", None),
                    ):
                        response = await client.get(url)
                        if field is None and response.status_code == 500:
                            require(
                                "cannot execute SELECT FOR SHARE in a read-only transaction"
                                in log.read_text(),
                                "Unexpected frozen task detail failure",
                            )
                            report["limitations"] = [
                                "Frozen task details acquire FOR SHARE and remain unavailable in a truly read-only transaction"
                            ]
                            report["checks"]["frozen_task_detail"] = (
                                "expected readonly refusal; no write privileges granted to bypass it"
                            )
                            continue
                        require(
                            response.status_code == 200,
                            "Frozen legacy metadata read failed",
                        )
                        record = response.json()[field][0] if field else response.json()
                        downloaded = await downloads.get(record["file_url"])
                        require(
                            downloaded.status_code == 200
                            and downloaded.content == world["raw"],
                            "Frozen original bytes differ",
                        )
                    report["checks"]["frozen_legacy_reads"] = (
                        "dataset URL returns identical original bytes"
                    )
                    response = await client.get(
                        f"/api/v1/datasets/{world['reference_dataset'].id}/items"
                    )
                    require(
                        response.status_code == 200,
                        "Reference metadata fixture not visible",
                    )
                    signed = response.json()["items"][0]["file_url"]
                    require(
                        (await downloads.get(signed)).status_code == 403,
                        "Old app resolved opaque key trap",
                    )
                    report["mutation_refusals"] = []
                    for method, url, payload in (
                        (
                            "PUT",
                            f"/api/v1/datasets/{world['reference_dataset'].id}",
                            {"name": "must not change"},
                        ),
                        (
                            "DELETE",
                            f"/api/v1/datasets/{world['reference_dataset'].id}/items/{world['reference'].id}",
                            None,
                        ),
                        (
                            "DELETE",
                            f"/api/v1/datasets/{world['reference_dataset'].id}",
                            None,
                        ),
                    ):
                        log_offset = log.stat().st_size
                        denied = await client.request(method, url, json=payload)
                        require(
                            denied.status_code == 500,
                            "Expected old handler DB write rejection",
                        )
                        for _ in range(50):
                            refusal = log.read_bytes()[log_offset:].decode(
                                errors="replace"
                            )
                            error = next(
                                (
                                    name
                                    for name in (
                                        "ReadOnlySQLTransactionError",
                                        "InsufficientPrivilegeError",
                                    )
                                    if name in refusal
                                ),
                                None,
                            )
                            if error:
                                break
                            await asyncio.sleep(0.02)
                        require(
                            error,
                            "Old HTTP write failure lacks its own DB permission refusal",
                        )
                        report["mutation_refusals"].append(
                            {"method": method, "database_error": error}
                        )
                    report["checks"]["frozen_external_mutations"] = (
                        "rename, item delete and dataset delete each have their own DB permission refusal"
                    )
            await readonly.dispose()
            readonly = None
            await accounts.close(owner)
            report["cleanup"]["temporary_credentials_revoked"] = True
            await current_reads(world, factory)
            async with factory() as db:
                after = await inspect_database(db)
            require(before == after, "Full database changed during rollback")
            require(
                world["reference_file"].read_bytes() == world["reference_raw"],
                "External bytes changed",
            )
            body = backend.s3.get_object(
                Bucket=world["bucket"], Key=world["item"].file_path
            )["Body"]
            try:
                require(body.read() == world["raw"], "Managed bytes changed")
            finally:
                body.close()
            report["checks"]["return_to_current"] = (
                "both verified original reads succeed; full database and both originals unchanged"
            )
            report["database"] = before["full_database"]
    finally:
        if ro_s3 is not None:
            ro_s3.close()
        if readonly is not None:
            await readonly.dispose()
        try:
            await accounts.close(owner)
            report["cleanup"]["temporary_credentials_revoked"] = True
        finally:
            await owner.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-commit", default=LEGACY_COMMIT)
    parser.add_argument("--report", required=True, type=Path)
    options = parser.parse_args()
    require(shutil.which("mc"), "Existing MinIO mc CLI is required")
    require(not options.report.exists(), "Refuse to overwrite a report")
    backend = verified_environment()
    report = {
        "scope": "owned local controlled read-only application rollback",
        "checks": {},
        "cleanup": {},
        "scope_limits": [
            "Controlled application rollback preserves strict current content identity; no runtime read bypass is part of this contract",
            "Production target and other rollback commits require their own qualification",
        ],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="aap-legacy-rollback-") as temporary:
            try:
                asyncio.run(qualify(backend, options, Path(temporary), report))
                report["status"] = "passed"
            except BaseException as exc:
                report["status"] = "failed"
                report["failure_type"] = type(exc).__name__
                report["failure_frames"] = [
                    {
                        "file": Path(frame.filename).name,
                        "line": frame.lineno,
                        "function": frame.name,
                    }
                    for frame in traceback.extract_tb(exc.__traceback__)
                ]
                if isinstance(exc, WorktreeError):
                    report["reason"] = str(exc)
                log = Path(temporary) / "legacy-server.log"
                if log.exists():
                    diagnostic = options.report.with_suffix(".legacy.log")
                    descriptor = os.open(
                        diagnostic, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(log.read_bytes())
                raise
            finally:
                atomic_json(options.report, report)
    finally:
        backend.s3.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "reason": str(exc)
                    if isinstance(exc, WorktreeError)
                    else "See private qualification report",
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
