"""Rehearse an offline recovery using only two new random owned test namespaces.

No existing source/target argument is accepted. No API, Web, Worker or Beat is
started. A successful report qualifies the synthetic rehearsal, not production.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from worktree_env import (
    WorktreeError,
    atomic_json,
    application_environment,
    load_identity,
    state_path,
    topology,
)
from worktree_resources import LocalResources, OWNER_TAG, migration_graph
from worktree_runtime import (
    environment_lock,
    require_owner,
    validate_inventory,
    validate_revisions,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))
FAIL_STAGES = ("source", "backup", "destroy-source", "restore-db", "restore-objects")


def require_fresh_inventory(backend):
    observed = backend.inspect()
    validate_inventory(backend.resources, observed)
    if (
        observed["database"] is not None
        or observed["redis"] is not None
        or any(value is not None for value in observed["buckets"].values())
    ):
        raise WorktreeError("演练拒绝使用任何既有资源")


def require_offline(backend):
    observed = backend.inspect()
    validate_inventory(backend.resources, observed)
    if not observed["database"] or observed["database"]["sessions"]:
        raise WorktreeError("离线演练数据库缺失或仍有客户端")


async def require_empty_database(backend):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    observed = await backend.database()
    require_owner(backend.resources["database"], observed, backend.resources["owner"])
    if not observed or observed["sessions"]:
        raise WorktreeError("恢复目标缺失或仍有客户端")
    engine = create_async_engine(backend.url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            count = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f')"
                )
            )
            if count:
                raise WorktreeError("拒绝向非空恢复目标叠加备份")
    finally:
        await engine.dispose()


def prepare_empty_target(backend):
    """Own the same resources as the launcher, without migrating over the dump."""
    require_fresh_inventory(backend)
    atomic_json(state_path(backend.root, "test", "resources.json"), backend.manifest)
    backend.touched = True
    asyncio.run(backend.database("create"))
    for name in set(backend.resources["buckets"].values()):
        if backend.bucket_info(name) is not None:
            raise WorktreeError("恢复目标桶在创建前被占用")
        backend.s3.create_bucket(Bucket=name)
        backend.s3.put_bucket_tagging(
            Bucket=name,
            Tagging={
                "TagSet": [{"Key": OWNER_TAG, "Value": backend.resources["owner"]}]
            },
        )
        require_owner(name, backend.bucket_info(name), backend.resources["owner"])
    backend.start_redis()
    asyncio.run(require_empty_database(backend))


def read_key(path: Path, expected_sha256: str) -> str:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_mode & 0o077
        or path.stat().st_uid != os.getuid()
    ):
        raise WorktreeError("恢复密钥缺失或权限不是私有")
    content = path.read_bytes()
    if len(content) > 4096 or hashlib.sha256(content).hexdigest() != expected_sha256:
        raise WorktreeError("恢复密钥与备份代次不匹配")
    try:
        value = json.loads(content)["connector_encryption_key"]
        if not isinstance(value, str):
            raise ValueError("Expected a string key")
        from cryptography.fernet import Fernet

        Fernet(value.encode())
    except (ValueError, TypeError, KeyError) as exc:
        raise WorktreeError("恢复密钥格式不合法") from exc
    return value


def fixture(
    backend,
    action,
    external,
    key_path,
    key_sha256,
    output,
    *,
    expected=None,
    source_buckets=None,
):
    scoped = backend.environment()
    environment = application_environment(scoped)
    if action == "verify":
        # The offline target mapper alone may update guarded physical locator
        # fields. This owner connection is never passed to API/worker processes.
        from sqlalchemy.engine import make_url

        owner_url = scoped["MIGRATION_DATABASE_URL"]
        if make_url(owner_url).database != backend.resources["database"]:
            raise WorktreeError("恢复映射连接不属于目标数据库")
        environment["DATABASE_URL"] = owner_url
    environment.update(
        DEBUG="false",
        LOG_LEVEL="WARNING",
        PYTHONPATH=os.pathsep.join([str(ROOT / "scripts"), str(ROOT / "apps/api")]),
        STORAGE_FILESYSTEM_ROOTS=json.dumps({"test": str(external)}),
        CONNECTOR_ENCRYPTION_KEY=read_key(key_path, key_sha256),
    )
    command = [
        sys.executable,
        str(ROOT / "scripts/worktree_recovery_fixture.py"),
        action,
        "--root",
        str(backend.root),
        "--external",
        str(external),
        "--output",
        str(output),
    ]
    if expected:
        command.extend(
            [
                "--expected",
                str(expected),
                "--source-buckets",
                json.dumps(source_buckets),
            ]
        )
    log = output.with_suffix(".log")
    with log.open("xb") as stream:
        os.chmod(log, 0o600)
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            stdout=stream,
            stderr=stream,
            timeout=240,
        )
    if result.returncode:
        # Child output is private and may contain SQL details. Report frames only.
        import re

        frames = re.findall(
            r'File "([^"]+)", line (\d+)', log.read_text(errors="replace")
        )
        frame = next(
            (
                (Path(name).name, line)
                for name, line in reversed(frames)
                if str(ROOT) in name
            ),
            ("child", "unknown"),
        )
        raise WorktreeError(
            f"应用恢复夹具 {action} 失败（{frame[0]}:{frame[1]}，退出码{result.returncode}）"
        )
    return json.loads(output.read_text())


def rehearse(options):
    if not __debug__:
        raise WorktreeError("恢复演练拒绝Python优化模式，必须保留全部校验")
    from app.config import settings
    from cryptography.fernet import Fernet
    from storage_backup_bundle import (
        BackupError,
        Limits,
        create_generation,
        verify_generation,
        restore_objects,
    )
    from validate_migrations import (
        Anchor,
        detect_dump_backend,
        pg_dump_database,
        pg_restore_database,
    )

    # No user-supplied root can turn this command into a destructive restore tool.
    root = Path(tempfile.mkdtemp(prefix="aap-recovery-"))
    backends = []
    timings = {}
    report = {
        "schema": 1,
        "status": "failed",
        "qualification": "offline-synthetic-only",
        "production_rpo_rto": "not-qualified",
        "known_writes_during_backup": None,
        "cleanup": "pending",
    }
    stage = "allocation"
    started = time.monotonic()
    failure = None
    cleanup_errors = []

    def finish(name, before):
        timings[name] = round(time.monotonic() - before, 3)
        if options.fail_after == name:
            raise WorktreeError(f"演练故障注入：{name}")

    locks = ExitStack()
    try:
        for name in ("source", "target"):
            path = root / name
            path.mkdir(mode=0o700)
            backend = LocalResources(
                topology(path, load_identity(path, create=True), "test"),
                settings,
                code_root=ROOT,
            )
            backends.append(backend)
            locks.enter_context(environment_lock(backend.resources))
            require_fresh_inventory(backend)
        source, target = backends
        key_path = root / "separate-recovery-key.json"
        atomic_json(
            key_path, {"connector_encryption_key": Fernet.generate_key().decode()}
        )
        key_sha = hashlib.sha256(key_path.read_bytes()).hexdigest()
        external = root / "external-originals"
        stage, before = "source", time.monotonic()
        source.prepare()
        # Ensure the all-purpose manifest includes every bucket, including empty ones.
        for role, bucket in source.resources["buckets"].items():
            source.s3.put_object(
                Bucket=bucket,
                Key="recovery/sentinel/" + role,
                Body=(role + "\n").encode(),
                ContentType="text/plain",
                Metadata={"recovery-role": role},
            )
        source_facts_path = root / "source-facts.json"
        facts = fixture(source, "seed", external, key_path, key_sha, source_facts_path)
        require_offline(source)
        finish(stage, before)

        stage, before = "backup", time.monotonic()
        generation = str(uuid.uuid4())
        revisions, heads = migration_graph(ROOT)
        validate_revisions(revisions, heads, asyncio.run(source.revisions()), skip=True)
        anchor = Anchor(
            url=source.url,
            base_database=source.resources["database"],
            owner=source.resources["owner"],
            mode="test",
            dump_dir=root,
        )
        dump_backend = detect_dump_backend(anchor)
        dump = root / "source.dump"
        dump.touch(mode=0o600, exist_ok=False)
        # Fresh synthetic row budget and no clients bound the existing dump helper.
        pg_dump_database(dump_backend, anchor, source.resources["database"], dump)
        os.chmod(dump, 0o600)
        limits = Limits()
        facts.update(
            backup_generation=generation,
            code_commit=subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            alembic_heads=heads,
            key_material={"sha256": key_sha, "storage": "separate-private-file"},
            configuration={
                "minio_endpoint": settings.minio_endpoint,
                "minio_use_ssl": settings.minio_use_ssl,
                "buckets": source.resources["buckets"],
            },
            consistency={
                "scope": "offline-owned-fixture",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "active_clients": 0,
                "queue_restore": False,
            },
        )
        directory = root / "generation"

        def verify_offline_database():
            after = fixture(
                source,
                "inspect",
                external,
                key_path,
                key_sha,
                root / "after-backup.json",
            )
            if any(after[key] != facts[key] for key in after):
                raise WorktreeError("数据库在备份期间变化；此代次不可用于恢复")
            require_offline(source)
            report["known_writes_during_backup"] = 0

        manifest = create_generation(
            directory,
            s3=source.s3,
            bucket_roles=source.resources["buckets"],
            dump_path=dump,
            facts=facts,
            limits=limits,
            before_publish=verify_offline_database,
        )
        if options.fault == "object":
            damaged = directory / manifest["objects"][0]["file"]
            damaged.write_bytes(b"corrupt")
        elif options.fault == "dump":
            (directory / manifest["database"]["file"]).write_bytes(b"corrupt")
        elif options.fault == "key":
            atomic_json(
                key_path,
                {"connector_encryption_key": Fernet.generate_key().decode()},
            )
        manifest = verify_generation(directory, limits=limits)
        facts = manifest["facts"]
        read_key(key_path, key_sha)
        finish(stage, before)

        stage, before = "destroy-source", time.monotonic()
        source.destroy(source.resources["confirmation"])
        source.touched = False
        require_fresh_inventory(source)
        shutil.rmtree(external)
        source_facts_path.unlink()
        dump.unlink()
        (root / "after-backup.json").unlink()
        report["source_destroyed_before_restore"] = True
        finish(stage, before)

        stage, before = "restore-db", time.monotonic()
        read_key(key_path, key_sha)
        prepare_empty_target(target)
        asyncio.run(require_empty_database(target))
        target_anchor = Anchor(
            url=target.url,
            base_database=target.resources["database"],
            owner=target.resources["owner"],
            mode="test",
            dump_dir=root,
        )
        pg_restore_database(
            dump_backend,
            target_anchor,
            target.resources["database"],
            directory / manifest["database"]["file"],
        )
        validate_revisions(revisions, heads, asyncio.run(target.revisions()), skip=True)
        asyncio.run(target.grant_runtime())
        require_offline(target)
        finish(stage, before)

        stage, before = "restore-objects", time.monotonic()
        object_result = restore_objects(
            directory,
            s3=target.s3,
            bucket_roles=target.resources["buckets"],
            limits=limits,
        )
        finish(stage, before)
        stage, before = "application-verification", time.monotonic()
        result = fixture(
            target,
            "verify",
            external,
            key_path,
            key_sha,
            root / "target-result.json",
            expected=directory / "manifest.json",
            source_buckets=source.resources["buckets"],
        )
        require_offline(target)
        require_fresh_inventory(source)
        import redis

        with redis.Redis.from_url(
            target.environment()["REDIS_URL"], socket_timeout=3
        ) as client:
            if client.dbsize():
                raise WorktreeError("恢复目标出现未经确认的队列或Redis状态")
        finish(stage, before)
        report.update(
            status="passed",
            backup_generation=generation,
            alembic_heads=heads,
            object_restore=object_result,
            application=result,
            database_tables=len(facts["full_database"]),
            database_rows=sum(
                value["count"] for value in facts["full_database"].values()
            ),
        )
    except Exception as exc:
        failure = exc
        report["failed_stage"] = stage
        report["error"] = (
            str(exc) if isinstance(exc, WorktreeError) else type(exc).__name__
        )
        if isinstance(exc, BackupError):
            report["backup_error_code"] = exc.code
    finally:
        for backend in reversed(backends):
            try:
                if backend.touched:
                    backend.destroy(backend.resources["confirmation"])
                require_fresh_inventory(backend)
            except Exception:
                cleanup_errors.append(backend.resources["confirmation"])
            finally:
                backend.s3.close()
        locks.close()
        report["stage_seconds"] = timings
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if cleanup_errors:
            report.update(
                status="failed",
                cleanup="requires-review",
                recovery_directory=str(root),
                cleanup_owners=cleanup_errors,
            )
        else:
            shutil.rmtree(root)
            report["cleanup"] = "verified"
    if options.report:
        atomic_json(Path(options.report), report)
    # Do not print the row-by-row physical mapping or private temporary paths on success.
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "application"},
            ensure_ascii=False,
        )
    )
    if cleanup_errors or failure:
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", help="Write a private, credential-free rehearsal report"
    )
    parser.add_argument("--fail-after", choices=FAIL_STAGES)
    parser.add_argument("--fault", choices=("object", "dump", "key"))
    raise SystemExit(rehearse(parser.parse_args()))
