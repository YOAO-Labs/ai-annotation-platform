"""Qualify the saved-reference CLI in this checkout's owned disposable test mode.

This bounded fixture is not a production inventory, migration or consumer test.
The fixture owner writes; the CLI receives only explicit SELECT column grants.
"""

import argparse
import asyncio
from datetime import datetime, timezone
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

from inventory_storage_references import FAMILIES, allowed_columns, quoted
from storage_migration_inventory import PURPOSES, InventoryLimits, write_inventory
from storage_migration_references import identity, verify
from verify_legacy_storage_rollback import ROOT, require, verified_environment
from worktree_env import WorktreeError, atomic_json
from worktree_recovery_fixture import database_facts

LEGACY = "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7"
BODY = b"bounded reference fixture"
SHA = hashlib.sha256(BODY).hexdigest()


def migrate(target):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *target],
        cwd=ROOT / "apps/api",
        capture_output=True,
        timeout=90,
    )
    require(result.returncode == 0, "Owned fixture migration failed")


async def columns(connection):
    from sqlalchemy import text

    result = {}
    for table, column in await connection.execute(
        text(
            "SELECT table_name,column_name FROM information_schema.columns "
            "WHERE table_schema='public'"
        )
    ):
        result.setdefault(table, set()).add(column)
    return result


async def grants(owner, role):
    async with owner.begin() as connection:
        fields = await columns(connection)
        for family in FAMILIES:
            names = sorted(allowed_columns(family, fields[family.table]))
            await connection.exec_driver_sql(
                f"GRANT SELECT ({','.join(map(quoted, names))}) ON "
                f'{quoted(family.table)} TO "{role}"'
            )
        for table, names in {
            "storage_locations": "id,kind,config_revision,enabled,connection_id,config",
            "storage_connections": "id,kind,scope,revision,config",
            "storage_bindings": "purpose,location_id,bucket,generation",
            "alembic_version": "version_num",
        }.items():
            await connection.exec_driver_sql(
                f'GRANT SELECT ({names}) ON {table} TO "{role}"'
            )


def saved(address, bucket, key, *, purpose="avatars", schema=1):
    return {
        "schema": schema,
        "purpose": purpose,
        "location_id": LEGACY if schema == 1 else str(uuid.uuid4()),
        "config_revision": 1,
        "binding_generation": schema,
        "endpoint_url": address,
        "bucket": bucket,
        "object_key": key,
        "storage_identity": identity(address, bucket),
    }


async def insert_avatar(owner, ref, token, version, state="retired"):
    from sqlalchemy import text

    async with owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO avatar_uploads(token,owner_id,storage_ref,sha256,size_bytes,"
                "object_version_id,state,retired_at) VALUES(:token,:owner,CAST(:ref AS jsonb),"
                ":sha,:size,:version,:state,:retired_at)"
            ),
            {
                "token": token,
                "owner": uuid.uuid4(),
                "ref": json.dumps(ref),
                "sha": SHA,
                "size": len(BODY),
                "version": version,
                "state": state,
                "retired_at": datetime.now(timezone.utc)
                if state == "retired"
                else None,
            },
        )


async def insert_bug(owner, ref, attachment, user, version=None, *, old=False):
    from sqlalchemy import text

    extra_columns = "" if old else ",object_version_id"
    extra_values = "" if old else ",:version"
    async with owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO bug_attachment_uploads(id,owner_id,storage_ref,sha256,size_bytes,"
                "mime_type,file_name,state,expires_at,retired_at"
                + extra_columns
                + ") VALUES(:id,:owner,CAST(:ref AS jsonb),:sha,:size,'image/png',"
                "'private-business-payload-sentinel.png','retired',now()+interval '1 day',now()"
                + extra_values
                + ")"
            ),
            {
                "id": attachment,
                "owner": user,
                "ref": json.dumps(ref),
                "sha": SHA,
                "size": len(BODY),
                "version": version,
            },
        )


def object_facts(client, buckets):
    facts = []
    for bucket in sorted(buckets):
        result = client.list_object_versions(Bucket=bucket, MaxKeys=100)
        require(not result.get("IsTruncated"), "Fixture object budget exceeded")
        for kind in ("Versions", "DeleteMarkers"):
            for row in result.get(kind, []):
                digest = None
                if kind == "Versions":
                    response = client.get_object(
                        Bucket=bucket, Key=row["Key"], VersionId=row["VersionId"]
                    )
                    try:
                        body = response["Body"].read(1025)
                        require(len(body) <= 1024, "Fixture body budget exceeded")
                        digest = hashlib.sha256(body).hexdigest()
                    finally:
                        response["Body"].close()
                facts.append(
                    [
                        bucket,
                        row["Key"],
                        row["VersionId"],
                        kind,
                        row.get("ETag"),
                        row.get("Size"),
                        digest,
                    ]
                )
    return hashlib.sha256(json.dumps(sorted(facts)).encode()).hexdigest(), len(facts)


async def run_phase(
    owner, readonly_url, role, source, root, backend, expected_revision
):
    await grants(owner, role)
    async with owner.connect() as connection:
        before = await database_facts(connection)
    object_before = object_facts(backend.s3, backend.resources["buckets"].values())
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    output = root / (expected_revision + ".jsonl")
    environment = {
        **os.environ,
        "DATABASE_URL": readonly_url.render_as_string(hide_password=False),
        "DEBUG": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            str(ROOT / "scripts/inventory_storage_references.py"),
            "--source-inventory",
            str(source),
            "--output",
            str(output),
            "--temp-dir",
            str(root),
            "--page-size",
            "1",
            "--max-rows",
            "200",
            "--max-expanded",
            "100",
            "--max-pages",
            "200",
            "--max-source-entries",
            "100",
            "--max-seconds",
            "60",
            "--statement-seconds",
            "5",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        timeout=75,
    )
    require(result.returncode == 1, "Expected sealed incomplete fixture catalog")
    summary = verify(output)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    header = rows[0]
    require(header["database"]["enumeration_complete"], "SQL family enumeration failed")
    require(
        header["database"]["revisions"] == [expected_revision], "Wrong schema revision"
    )
    require(len(header["database"]["families"]) == 24, "Locator registry incomplete")
    require(
        header["source_report"]["file_sha256"] == source_digest,
        "Source digest mismatch",
    )
    require(
        not summary["migration_ready"], "Reference report cannot authorize migration"
    )
    raw = output.read_bytes() + result.stdout + result.stderr
    for forbidden in (
        b"private-business-payload-sentinel",
        b"private-config-payload-sentinel",
        b"encrypted-credentials-sentinel",
        readonly_url.password.encode(),
        b"secret_enc",
        b"authority_proof",
    ):
        require(forbidden not in raw, "Sensitive value escaped the projection")
    references = [row for row in rows if row["type"] == "reference"]
    require(
        all(
            row["owner_state"] == "retired"
            for row in references
            if row["family"] in {"avatar_uploads", "bug_attachment_uploads"}
        ),
        "Retired owners missing",
    )
    require(
        any(row.get("registered_location_check") == "missing" for row in references),
        "Orphan owner association missing",
    )
    if expected_revision == "0245":
        require(
            any(
                row["family"] == "bug_attachment_uploads"
                and row.get("schema") == 2
                and row.get("registered_location_check")
                == "identity_differs_or_unavailable"
                and row.get("address_basis") == "saved_endpoint"
                and row.get("endpoint_url") == header["source_report"]["endpoint"]
                and row["match"]["status"] == "exact_version"
                for row in references
            ),
            "Current binding reinterpreted saved historical owner",
        )
        require(
            any(row["type"] == "location" and row["enabled"] is False for row in rows),
            "Disabled location omitted",
        )
    async with owner.connect() as connection:
        require(
            await database_facts(connection) == before, "CLI modified business facts"
        )
    require(
        object_facts(backend.s3, backend.resources["buckets"].values())
        == object_before,
        "CLI modified object facts",
    )
    require(
        hashlib.sha256(source.read_bytes()).hexdigest() == source_digest,
        "CLI changed paired source",
    )
    require(output.stat().st_mode & 0o777 == 0o600, "Report is not private")
    require(not list(root.glob("aap-reference-*")), "Private spill directory remains")
    return {
        "revision": expected_revision,
        "families": 24,
        "reference_count": len(references),
        "matches": summary["matches"],
        "database_fingerprint_unchanged": True,
        "object_fingerprint_unchanged": True,
        "source_fingerprint_unchanged": True,
        "source_object_versions": object_before[1],
        "private_output": True,
        "sensitive_projection_excluded": True,
        "spills_cleaned": True,
    }


async def qualify(backend):
    import boto3
    import botocore
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.config import settings

    owner = create_async_engine(backend.url, hide_parameters=True)
    role = "aap_reference_" + uuid.uuid4().hex[:16]
    created = False
    head_restored = True
    readonly = None
    phases = []
    try:
        async with owner.connect() as connection:
            facts = await database_facts(connection)
            require(
                all(facts[f.table]["count"] == 0 for f in FAMILIES),
                "Fixture requires empty owner families",
            )
            require(
                list(
                    await connection.scalars(
                        text("SELECT version_num FROM alembic_version")
                    )
                )
                == ["0245"],
                "Fixture requires head 0245",
            )
        require(
            object_facts(backend.s3, backend.resources["buckets"].values())[1] == 0,
            "Fixture requires empty owned buckets",
        )
        password = secrets.token_hex(24)
        async with owner.begin() as connection:
            require(
                not await connection.scalar(
                    text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
                ),
                "Refuse existing role",
            )
            await connection.exec_driver_sql(
                f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
            )
            created = True
            for statement in (
                f'GRANT CONNECT ON DATABASE "{backend.resources["database"]}" TO "{role}"',
                f'GRANT USAGE ON SCHEMA public TO "{role}"',
                f'ALTER ROLE "{role}" SET default_transaction_read_only=on',
            ):
                await connection.exec_driver_sql(statement)
        readonly_url = backend.url.set(username=role, password=password)
        readonly = create_async_engine(readonly_url, hide_parameters=True)
        address = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
        purposes = {
            purpose: backend.resources["buckets"][
                "MINIO_BUCKET"
                if purpose == "annotations"
                else "MINIO_" + purpose.upper().replace("-", "_") + "_BUCKET"
            ]
            for purpose in PURPOSES
        }
        bucket = purposes["avatars"]
        tokens = [uuid.uuid4().hex for _ in range(5)]
        versions = []
        for token in tokens:
            versions.append(
                backend.s3.put_object(
                    Bucket=bucket, Key=token + ".webp", Body=BODY
                ).get("VersionId", "null")
            )
        backend.s3.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        for index in (0, 1):
            versions[index] = backend.s3.put_object(
                Bucket=bucket, Key=tokens[index] + ".webp", Body=BODY
            )["VersionId"]
        bug_id, user = uuid.uuid4(), uuid.uuid4()
        old_key = f"bug-report-attachments/{user}/{bug_id}.png"
        bug_bucket = purposes["bug-reports"]
        backend.s3.put_object(Bucket=bug_bucket, Key=old_key, Body=BODY)
        backend.s3.put_bucket_versioning(
            Bucket=bug_bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        new_id = uuid.uuid4()
        new_key = f"company/_aap_managed/bug-reports/bug-report-attachments/{user}/{new_id}.png"
        new_version = backend.s3.put_object(
            Bucket=bug_bucket, Key=new_key, Body=BODY
        ).get("VersionId", "null")
        # These raw source keys must survive without URL/path normalization.
        # MinIO rejects literal dot segments on PUT; the pure report tests cover
        # those opaque keys independently of this provider's write restrictions.
        for key in ("raw/%2F/key", "raw/%2f/key", "raw/a/b", "raw/a_boundary"):
            backend.s3.put_object(Bucket=bucket, Key=key, Body=BODY)
        prefix = f"projects/{uuid.uuid4()}/datasets/{uuid.uuid4()}/"
        backend.s3.put_object(Bucket=bucket, Key=prefix + "raw/%2F", Body=BODY)
        with tempfile.TemporaryDirectory(prefix="aap-reference-live-") as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            result = write_inventory(
                source,
                backend.s3,
                purposes,
                source={"endpoint": address},
                limits=InventoryLimits(
                    page_size=10,
                    max_entries=100,
                    max_pages=200,
                    max_object_bytes=1024,
                    max_total_bytes=16384,
                ),
            )
            require(result["complete"], "Small source report incomplete")
            head_restored = False
            await asyncio.to_thread(migrate, ["downgrade", "0244"])
            await insert_avatar(
                owner,
                saved(address, bucket, tokens[0] + ".webp"),
                tokens[0],
                versions[0],
            )
            await insert_bug(
                owner,
                saved(address, bug_bucket, old_key, purpose="bug-reports"),
                bug_id,
                user,
                old=True,
            )
            phases.append(
                await run_phase(
                    owner, readonly_url, role, source, root, backend, "0244"
                )
            )
            require(
                phases[-1]["matches"].get("exact_version", 0)
                + phases[-1]["matches"].get("mutable_null", 0)
                == 1,
                "Old exact owner missing",
            )
            async with owner.begin() as connection:
                await connection.execute(
                    text("DELETE FROM bug_attachment_uploads WHERE id=:id"),
                    {"id": bug_id},
                )
            await asyncio.to_thread(migrate, ["upgrade", "head"])
            head_restored = True
            new_ref = saved(
                address, bug_bucket, new_key, purpose="bug-reports", schema=2
            )
            await insert_bug(
                owner,
                new_ref,
                new_id,
                user,
                new_version,
            )
            await insert_avatar(
                owner,
                saved(
                    "http://different-source.invalid:9000", bucket, tokens[1] + ".webp"
                ),
                tokens[1],
                versions[1],
            )
            await insert_avatar(
                owner,
                saved(address, bucket, tokens[2] + ".webp"),
                tokens[2],
                "saved-but-not-observed",
            )
            await insert_avatar(
                owner, saved(address, bucket, tokens[3] + ".webp"), tokens[3], None
            )
            await insert_avatar(
                owner, saved(address, bucket, tokens[4] + ".webp"), tokens[4], "null"
            )
            async with owner.begin() as connection:
                connection_id = uuid.uuid4()
                await connection.execute(
                    text(
                        "INSERT INTO training_storage_deletions(id,bucket,prefix,storage_ref) VALUES(:id,:bucket,'raw/a/',CAST(:ref AS jsonb))"
                    ),
                    {"id": uuid.uuid4(), "bucket": bucket, "ref": None},
                )
                prefix_ref = saved(address, bucket, "", purpose="training-dataset")
                prefix_ref.pop("object_key")
                prefix_ref.update(prefix=prefix, origin="new", evidence=None)
                await connection.execute(
                    text(
                        "INSERT INTO training_storage_deletions(id,bucket,prefix,storage_ref) VALUES(:id,:bucket,:prefix,CAST(:ref AS jsonb))"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "bucket": bucket,
                        "prefix": prefix,
                        "ref": json.dumps(prefix_ref),
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO storage_connections(id,name,kind,config,secret_enc,scope) VALUES(:id,'fixture','s3',CAST(:config AS jsonb),:secret,'global')"
                    ),
                    {
                        "id": connection_id,
                        "config": json.dumps(
                            {
                                "endpoint": "http://different-current.invalid:9000",
                                "bucket": bug_bucket,
                                "base_prefix": "company/",
                                "ignored": "private-config-payload-sentinel",
                            }
                        ),
                        "secret": b"encrypted-credentials-sentinel",
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO storage_locations(id,name,kind,config,connection_id,enabled) VALUES(:id,'reference-fixture','s3','{}'::jsonb,:connection,false)"
                    ),
                    {
                        "id": uuid.UUID(new_ref["location_id"]),
                        "connection": connection_id,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO storage_bindings(purpose,location_id,bucket,generation) VALUES('bug-reports',:id,:bucket,2)"
                    ),
                    {"id": uuid.UUID(new_ref["location_id"]), "bucket": bug_bucket},
                )
            phases.append(
                await run_phase(
                    owner, readonly_url, role, source, root, backend, "0245"
                )
            )
            for expected in (
                "outside_selected_source",
                "not_observed",
                "unpinned_key",
                "unlocated",
                "prefix_scope",
            ):
                require(
                    phases[-1]["matches"].get(expected, 0) > 0,
                    "Missing expected locator distinction",
                )
            # Verify the effective role, not just a SET TRANSACTION string.
            denials = 0
            for query in (
                "UPDATE storage_connections SET name=name WHERE false",
                "SELECT secret_enc FROM storage_connections",
            ):
                try:
                    async with readonly.begin() as connection:
                        await connection.execute(text(query))
                except Exception:
                    denials += 1
            require(denials == 2, "Readonly role unexpectedly has write/secret access")
        evidence = {
            "scope": "local owned test; shared host PostgreSQL and MinIO; no app services",
            "provider": {
                "boto3": boto3.__version__,
                "botocore": botocore.__version__,
                "postgresql": None,
            },
            "phases": phases,
            "readonly_denials": 2,
            "head_restored": head_restored,
            "migration_ready": False,
        }
        async with owner.connect() as connection:
            evidence["provider"]["postgresql"] = await connection.scalar(
                text("SHOW server_version")
            )
        return evidence
    finally:
        if readonly:
            await readonly.dispose()
        if not head_restored:
            async with owner.begin() as connection:
                await connection.execute(text("DELETE FROM bug_attachment_uploads"))
            await asyncio.to_thread(migrate, ["upgrade", "head"])
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
                    "Temporary role remains",
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
        if hasattr(exc, "operation_name"):
            detail += (
                ":"
                + exc.operation_name
                + ":"
                + str(exc.response.get("Error", {}).get("Code"))
            )
        if hasattr(exc, "orig"):
            detail += ":" + str(getattr(exc.orig, "sqlstate", None))
        frames = traceback.extract_tb(exc.__traceback__)
        frame = next(
            (item for item in reversed(frames) if item.filename == __file__), frames[-1]
        )
        detail += f" ({Path(frame.filename).name}:{frame.lineno})"
        print("Saved-reference qualification failed: " + detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
