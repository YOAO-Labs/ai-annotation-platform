"""Cold Audit CLI recovery with its function-only DB and scoped archive credentials."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

from botocore.exceptions import ClientError
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_audit.maintenance_postgres import AuditMaintenancePostgresData
from app.config import settings
from tests.test_asset_storage_cleanup_runtime import (
    _container_s3_endpoint,
    _docker,
    _inspect_docker,
    _local_s3_client,
    _required_env,
    _verified_resource_manifest,
    _verify_postgres,
)
from tests.test_audit_archive_read_binding import _configure_archive_read
from tests.test_identity_module_assembly_live import _restored_database
from tests.test_identity_recovery import _postgres_tool

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_AUDIT_MAINTENANCE_MODULE_ACCEPTANCE") != "1",
        reason="requires verifier-owned resources and a frozen Audit runtime image",
    ),
]


def _image() -> dict:
    receipt = json.loads(
        Path(_required_env("AAP_AUDIT_MAINTENANCE_MODULE_IMAGE_ARTIFACT")).read_text()
    )
    assert receipt["status"] == "passed"
    assert receipt["image_id"] == _required_env("AAP_AUDIT_MAINTENANCE_MODULE_IMAGE_ID")
    assert receipt["source_manifest"]
    for name, digest in receipt["source_manifest"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    details = json.loads(_docker("image", "inspect", receipt["image_id"]))[0]
    assert details["Id"] == receipt["image_id"]
    assert details["Config"]["User"] == "10006:10006"
    assert details["Config"]["Entrypoint"] == ["aap-audit-maintenance"]
    assert details["Config"]["Labels"]["org.aap.acceptance-id"] == receipt["identity"]
    return receipt


def _credentials(label: str) -> tuple[str, str]:
    prefix = f"AAP_MODULE_DATA_AUDIT_ARCHIVE_{label}"
    return _required_env(prefix + "_ACCESS_KEY"), _required_env(prefix + "_SECRET_KEY")


def _expect_s3_denied(operation) -> None:
    with pytest.raises(ClientError) as denied:
        operation()
    assert denied.value.response["Error"]["Code"] in {"AccessDenied", "403"}


@asynccontextmanager
async def _cli(image, directory, database_url, bucket, command):
    """Keep credentials in read-only files and share only the verified PG network."""
    directory.mkdir(mode=0o700)
    access_key, secret_key = _credentials("WRITE")
    values = {
        "database": database_url.set(
            drivername="postgresql", host="127.0.0.1", port=5432
        ).render_as_string(hide_password=False),
        "access-key": access_key,
        "secret-key": secret_key,
    }
    for name, value in values.items():
        path = directory / name
        path.write_text(value)
        path.chmod(0o444)
    directory.chmod(0o555)
    token = uuid.uuid4().hex
    name = "aap-audit-maintenance-" + token[:12]
    container = None
    try:
        container = await asyncio.to_thread(
            _docker,
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            "org.aap.acceptance=audit-maintenance-module",
            "--label",
            f"org.aap.acceptance-id={token}",
            "--network",
            "container:" + _required_env("AAP_MODULE_DATA_PG_CONTAINER_ID"),
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "64",
            "--cpus",
            "1",
            "--memory",
            "512m",
            "--mount",
            f"type=bind,source={directory},target=/run/audit-secrets,readonly",
            "--env",
            "AUDIT_MAINTENANCE_DATABASE_URL_FILE=/run/audit-secrets/database",
            "--env",
            "AUDIT_MAINTENANCE_ARCHIVE_S3_ACCESS_KEY_ID_FILE=/run/audit-secrets/access-key",
            "--env",
            "AUDIT_MAINTENANCE_ARCHIVE_S3_SECRET_ACCESS_KEY_FILE=/run/audit-secrets/secret-key",
            "--env",
            "AUDIT_MAINTENANCE_ARCHIVE_S3_ENDPOINT_URL="
            + _container_s3_endpoint(settings),
            "--env",
            "AUDIT_MAINTENANCE_ARCHIVE_S3_BUCKET=" + bucket,
            image["image_id"],
            command,
        )
        details = _inspect_docker(container)
        assert details["Config"]["Labels"]["org.aap.acceptance-id"] == token
        assert details["Config"]["Image"] == image["image_id"]
        assert details["HostConfig"]["ReadonlyRootfs"] is True
        assert details["HostConfig"]["NetworkMode"] == "container:" + _required_env(
            "AAP_MODULE_DATA_PG_CONTAINER_ID"
        )
        assert not details["HostConfig"].get("PortBindings")
        assert len(details["Mounts"]) == 1
        assert details["Mounts"][0]["Destination"] == "/run/audit-secrets"
        assert details["Mounts"][0]["RW"] is False
        yield container
    finally:
        try:
            if container is not None:
                try:
                    details = _inspect_docker(container)
                    assert details["Config"]["Labels"]["org.aap.acceptance-id"] == token
                    logs = await asyncio.to_thread(
                        subprocess.run,
                        ["docker", "logs", container],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    for stream in ("stdout", "stderr"):
                        content = getattr(logs, stream)
                        for secret in values.values():
                            content = content.replace(secret, "[redacted]")
                        path = directory.parent / f"{directory.name}-{stream}.log"
                        path.write_text(content)
                        path.chmod(0o600)
                finally:
                    # This immutable ID is the receipt of this context's docker run.
                    await asyncio.to_thread(_docker, "rm", "--force", container)
        finally:
            directory.chmod(0o700)
            for name in values:
                (directory / name).unlink(missing_ok=True)
            directory.rmdir()


async def _result(container) -> dict:
    result = await asyncio.to_thread(_docker, "wait", container, timeout=90)
    assert result == "0", "Audit CLI failed; secret-bearing output is withheld"
    return json.loads(await asyncio.to_thread(_docker, "logs", container))


async def test_cold_archive_process_loss_orphan_takeover_and_full_restore(
    test_engine,
    tmp_path,
    monkeypatch,
    httpx_client,
    auth_headers,
    annotator,
    record_property,
):
    image = _image()
    maintenance = make_url(_required_env("AAP_MODULE_DATA_AUDIT_MAINTENANCE_URL"))
    assert maintenance.username == "audit_maintenance_client"
    assert maintenance.password and not maintenance.query
    assert (maintenance.host, maintenance.port, maintenance.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    _verify_postgres(maintenance)
    resources = _verified_resource_manifest(test_engine, maintenance)
    bucket = resources["buckets"]["MINIO_AUDIT_ARCHIVE_BUCKET"]
    assert bucket == _required_env("MINIO_AUDIT_ARCHIVE_BUCKET")
    writer = _local_s3_client(settings, *_credentials("WRITE"))
    reader = _local_s3_client(settings, *_credentials("READ"))
    month = date(2001, 2, 1)
    partition_name = "audit_logs_y2001m02"
    key = "2001/02.jsonl.gz"
    action = "audit.cold_recovery." + uuid.uuid4().hex
    marker = "aap_audit_pause_" + uuid.uuid4().hex[:12]
    advisory_key = uuid.uuid4().int % (1 << 62)
    try:
        async with async_sessionmaker(
            test_engine, expire_on_commit=False
        ).begin() as db:
            data = AuditMaintenancePostgresData(db)
            await data.ensure_partition(month)
            await db.execute(
                text(
                    "INSERT INTO public.audit_logs(id,action,created_at,detail_json) "
                    "VALUES (9223372036854775807,:action,'2001-02-02T03:04:05Z',"
                    '\'[null,{"integer":9007199254740993},true,"文本"]\'::jsonb)'
                ),
                {"action": action},
            )
        async with _cli(
            image, tmp_path / "ensure", maintenance, bucket, "ensure-partitions"
        ) as container:
            assert isinstance((await _result(container))["created"], list)
        async with _cli(
            image, tmp_path / "refresh", maintenance, bucket, "refresh-daily"
        ) as container:
            assert (await _result(container)) == {"refreshed": True}

        # Pause the exact DROP after PUT so SIGKILL leaves a real orphan object.
        async with test_engine.begin() as db:
            await db.execute(
                text(f"""
                CREATE FUNCTION public.{marker}() RETURNS event_trigger
                LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
                AS $pause$ BEGIN
                    IF EXISTS (
                        SELECT FROM pg_event_trigger_dropped_objects()
                        WHERE object_type='table' AND schema_name='public'
                          AND object_name='{partition_name}'
                    ) THEN
                        PERFORM pg_advisory_xact_lock({advisory_key});
                    END IF;
                END $pause$
            """)
            )
            await db.execute(
                text(f"REVOKE ALL ON FUNCTION public.{marker}() FROM PUBLIC")
            )
            await db.execute(
                text(
                    f"CREATE EVENT TRIGGER {marker} ON sql_drop "
                    f"EXECUTE FUNCTION public.{marker}()"
                )
            )
        try:
            async with test_engine.connect() as control:
                await control.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_key}
                )
                async with _cli(
                    image,
                    tmp_path / "interrupted",
                    maintenance,
                    bucket,
                    "archive-partitions",
                ) as container:
                    async with asyncio.timeout(25):
                        while True:
                            waiting = await control.scalar(
                                text(
                                    "SELECT count(*) FROM pg_locks "
                                    "WHERE locktype='advisory' AND NOT granted "
                                    "AND classid=:high AND objid=:low"
                                ),
                                {
                                    "high": advisory_key >> 32,
                                    "low": advisory_key & 0xFFFFFFFF,
                                },
                            )
                            if waiting:
                                break
                            await asyncio.sleep(0.1)
                    assert (
                        reader.head_object(Bucket=bucket, Key=key)["ContentLength"] > 0
                    )
                    await asyncio.to_thread(
                        _docker, "kill", "--signal", "KILL", container
                    )
                    assert await asyncio.to_thread(_docker, "wait", container) != "0"
                await control.rollback()
        finally:
            async with test_engine.begin() as db:
                await db.execute(text(f"DROP EVENT TRIGGER {marker}"))
                await db.execute(text(f"DROP FUNCTION public.{marker}()"))

        async with test_engine.connect() as db:
            assert await db.scalar(
                text("SELECT to_regclass(:name)"), {"name": "public." + partition_name}
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM aap_audit_v1.audit_events "
                        "WHERE action='audit.archive' AND target_id=:partition"
                    ),
                    {"partition": partition_name},
                )
                == 0
            )
        _expect_s3_denied(lambda: writer.get_object(Bucket=bucket, Key=key))
        _expect_s3_denied(lambda: writer.delete_object(Bucket=bucket, Key=key))
        _expect_s3_denied(
            lambda: reader.put_object(Bucket=bucket, Key=key, Body=b"forbidden")
        )
        for client in (writer, reader):
            _expect_s3_denied(
                lambda: client.list_objects_v2(
                    Bucket=resources["buckets"]["MINIO_DATASETS_BUCKET"]
                )
            )

        snapshot = tmp_path / "audit-module.dump"
        snapshot.write_bytes(
            _postgres_tool(
                test_engine,
                [
                    "pg_dump",
                    "-U",
                    "owner",
                    "-Fc",
                    "--dbname",
                    str(test_engine.url.database),
                ],
            )
        )
        snapshot.chmod(0o600)
        async with _restored_database(test_engine, snapshot) as clone:
            clone_url = maintenance.set(database=clone)
            async with _cli(
                image, tmp_path / "restored", clone_url, bucket, "archive-partitions"
            ) as container:
                result = await _result(container)
                assert partition_name in result["archived_partitions"]
            engine = create_async_engine(test_engine.url.set(database=clone))
            try:
                async with engine.connect() as db:
                    assert (
                        await db.scalar(
                            text("SELECT to_regclass(:name)"),
                            {"name": "public." + partition_name},
                        )
                        is None
                    )
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM aap_audit_v1.audit_events "
                                "WHERE action='audit.archive' AND target_id=:partition"
                            ),
                            {"partition": partition_name},
                        )
                        == 1
                    )
                async with _cli(
                    image, tmp_path / "retry", clone_url, bucket, "archive-partitions"
                ) as container:
                    assert (
                        partition_name
                        not in (await _result(container))["archived_partitions"]
                    )
            finally:
                await engine.dispose()
        body = reader.get_object(Bucket=bucket, Key=key)["Body"]
        try:
            rows = [
                json.loads(line) for line in gzip.decompress(body.read()).splitlines()
            ]
        finally:
            body.close()
        row = next(row for row in rows if row["action"] == action)
        assert row["id"] == 9223372036854775807
        assert row["detail_json"] == [None, {"integer": 9007199254740993}, True, "文本"]
        read_key, read_secret = _credentials("READ")
        for name, value in (("reader-key", read_key), ("reader-secret", read_secret)):
            path = tmp_path / name
            path.write_text(value)
            path.chmod(0o600)
        _configure_archive_read(
            monkeypatch,
            endpoint_url=("https" if settings.minio_use_ssl else "http")
            + "://"
            + settings.minio_endpoint,
            bucket=bucket,
            access_key_id_file=str(tmp_path / "reader-key"),
            secret_access_key_file=str(tmp_path / "reader-secret"),
        )
        archive_list = await httpx_client.get(
            "/api/v1/audit-logs/archives", headers=auth_headers
        )
        assert archive_list.status_code == 200
        assert any(item["object_key"] == key for item in archive_list.json())
        archive = await httpx_client.get(
            "/api/v1/audit-logs/archives/2001/2", headers=auth_headers
        )
        assert archive.status_code == 200
        assert (
            next(item for item in archive.json()["items"] if item["action"] == action)
            == row
        )
        _, token = annotator
        denied = await httpx_client.get(
            "/api/v1/audit-logs/archives/2001/2",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert denied.status_code == 403
        evidence = {
            "image_id": image["image_id"],
            "image_source": image["source_commit"],
            "dump_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            "process_loss_after_put": True,
            "orphan_retained_then_replaced": True,
            "restored_partition_retired": True,
            "archive_event_count": 1,
            "max_bigint_and_native_json_preserved": True,
            "writer_and_reader_scopes_checked": True,
            "original_asgi_http_archive_reader": True,
            "cold_api": False,
        }
        record_property("audit_maintenance_module", json.dumps(evidence))
        (tmp_path / "audit-module-evidence.json").write_text(
            json.dumps(evidence, indent=2) + "\n"
        )
    finally:
        writer.close()
        reader.close()
