"""Source CLI consumers against one exclusive verifier-owned runtime."""

import asyncio
from contextlib import closing
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.user import User
from app.services.dataset_assets import LEGACY_LOCATION_ID
from tests.test_asset_storage_cleanup_runtime import (
    _required_env,
    _verified_resource_manifest,
    _verify_bucket_owner,
    _verify_postgres,
)
from tests.test_assets_catalog_native import _seed_actor
from tests.test_assets_operator_runtime import _consumer_engine, _resources
from tests.test_labeling_module_api_cold import _minio_dependency

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the dedicated owned module-data profile",
    ),
]

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def test_db_url():
    """Qualify both connections before the session migration fixture can run."""
    from tests.conftest import _resolve_test_db_url

    owner_url = make_url(_resolve_test_db_url())
    operator_url = make_url(_required_env("AAP_MODULE_DATA_ASSET_OPERATOR_URL"))
    oid = _required_env("AAP_MODULE_DATA_PG_DATABASE_OID")
    assert re.fullmatch(r"[1-9][0-9]*", oid)
    assert owner_url.username == "owner"
    assert operator_url.username == "yoao_asset_operator_client"
    for url in (owner_url, operator_url):
        assert url.drivername == "postgresql+asyncpg"
        assert url.host == "127.0.0.1" and not url.query
        _verify_postgres(url)

    async def verify():
        owner = create_async_engine(owner_url, hide_parameters=True)
        operator = create_async_engine(operator_url, hide_parameters=True)
        try:
            resources = _verified_resource_manifest(owner, operator_url)
            assert resources["database"].endswith("_test")
            expected = (
                resources["database"],
                int(oid),
                "owner",
                resources["owner"],
            )
            for engine in (owner, operator):
                async with engine.connect() as connection:
                    actual = (
                        await connection.execute(
                            text(
                                "SELECT d.datname,d.oid::bigint,"
                                "pg_catalog.pg_get_userbyid(d.datdba),"
                                "pg_catalog.shobj_description(d.oid,'pg_database') "
                                "FROM pg_catalog.pg_database d "
                                "WHERE d.datname=current_database()"
                            )
                        )
                    ).one()
                    assert tuple(actual) == expected
                    assert await connection.scalar(text("SELECT session_user")) == (
                        engine.url.username
                    )
        finally:
            await operator.dispose()
            await owner.dispose()

    asyncio.run(verify())
    return owner_url.render_as_string(hide_password=False)


@pytest.fixture
def image_profile(test_engine, tmp_path):
    resources, storage = _resources(test_engine)
    _minio_dependency(resources)
    owner_client = storage.client
    access_key = _required_env("AAP_IMAGE_IMPORT_S3_ACCESS_KEY")
    secret_key = _required_env("AAP_IMAGE_IMPORT_S3_SECRET_KEY")
    assert access_key != settings.minio_access_key
    assert secret_key != settings.minio_secret_key
    scheme = "https" if settings.minio_use_ssl else "http"
    client = boto3.client(
        "s3",
        endpoint_url=f"{scheme}://{settings.minio_endpoint}",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(s3={"addressing_style": "path"}, proxies={}),
    )
    root = tmp_path / "consumer-root"
    api = root / "apps" / "api"
    shutil.copytree(
        ROOT / "apps/api/app",
        api / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env", ".env.*"),
    )
    scripts = api / "scripts"
    scripts.mkdir()
    (scripts / "__init__.py").touch()
    for name in ("import_images.py", "prepare_original_catalog.py"):
        shutil.copyfile(ROOT / "apps/api/scripts" / name, scripts / name)
    home = root / "home"
    home.mkdir()
    common = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(api),
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "MINIO_ENDPOINT": str(settings.minio_endpoint),
        "MINIO_USE_SSL": str(settings.minio_use_ssl).lower(),
        **resources["buckets"],
    }
    consumer_env = {
        **common,
        "DATABASE_URL": _required_env("AAP_MODULE_DATA_ASSET_OPERATOR_URL"),
        "MINIO_ACCESS_KEY": access_key,
        "MINIO_SECRET_KEY": secret_key,
    }
    owner_env = {
        **common,
        "DATABASE_URL": test_engine.url.render_as_string(hide_password=False),
        "MINIO_ACCESS_KEY": str(settings.minio_access_key),
        "MINIO_SECRET_KEY": str(settings.minio_secret_key),
    }

    def run(module, args, *, owner=False):
        return subprocess.run(
            [sys.executable, "-m", module, *map(str, args)],
            cwd=api,
            env=owner_env if owner else consumer_env,
            capture_output=True,
            text=True,
            check=False,
        )

    try:
        yield resources, owner_client, client, run
    finally:
        client.close()


def _denied(operation):
    with pytest.raises(ClientError) as failure:
        operation()
    assert failure.value.response["Error"]["Code"] in {"AccessDenied", "403"}


def _versions(client, bucket):
    page = client.list_object_versions(Bucket=bucket)
    assert not page.get("IsTruncated")
    return {
        (item["Key"], item["VersionId"])
        for item in page.get("Versions", []) + page.get("DeleteMarkers", [])
    }


async def _row_identities(owner, *, include_creations=True):
    queries = [
        "SELECT id FROM public.datasets",
        "SELECT id FROM public.dataset_items",
        "SELECT id FROM public.dataset_assets",
        "SELECT asset_id,location_id,bucket FROM public.dataset_asset_locations",
        "SELECT operation_id FROM public.dataset_operator_operations",
        "SELECT id FROM public.storage_operations",
    ]
    if include_creations:
        queries.append("SELECT id FROM public.managed_original_creations")
    return tuple([set((await owner.execute(text(query))).all()) for query in queries])


def _import(run, directory, name, limit=None):
    args = [directory, "--name", name]
    if limit is not None:
        args.extend(("--limit", str(limit)))
    return run("scripts.import_images", args)


async def test_image_import_identity_behavior(
    test_engine, tmp_path, image_profile, record_property
):
    resources, owner_client, client, run = image_profile
    bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    operator = _consumer_engine(test_engine)
    suffix = uuid.uuid4().hex
    directory = tmp_path / "images"
    directory.mkdir()
    contents = {"a.png": b"first image", "b.jpg": b"second image"}
    for name, value in contents.items():
        (directory / name).write_bytes(value)

    try:
        async with owner_maker() as owner:
            assert await owner.scalar(text("SELECT count(*) FROM public.users")) == 0
            assert await owner.scalar(text("SELECT count(*) FROM public.datasets")) == 0
            before_empty = await _row_identities(owner)
        assert not _versions(owner_client, bucket)
        assert not owner_client.list_multipart_uploads(Bucket=bucket).get("Uploads")
        _verify_bucket_owner(owner_client, bucket, resources["owner"])
        owner_client.delete_bucket(Bucket=bucket)
        with pytest.raises(ClientError) as absent:
            owner_client.head_bucket(Bucket=bucket)
        assert absent.value.response["Error"]["Code"] in {
            "404",
            "NoSuchBucket",
            "NotFound",
        }

        async with operator.connect() as connection:
            assert (
                await connection.scalar(text("SELECT session_user"))
                == "yoao_asset_operator_client"
            )
            assert (
                await connection.scalar(text("SELECT current_user"))
                == "yoao_asset_operator_client"
            )
            for query in (
                "SELECT id FROM public.users LIMIT 1",
                "SELECT id FROM yoao_identity_v1.account_profiles LIMIT 1",
                "SELECT id FROM public.storage_locations LIMIT 1",
                "SELECT purpose FROM public.storage_bindings LIMIT 1",
            ):
                with pytest.raises(DBAPIError) as error:
                    await connection.execute(text(query))
                assert error.value.orig.sqlstate == "42501"
                await connection.rollback()

        empty = _import(run, directory, "identity-empty-" + suffix)
        assert empty.returncode == 0
        with pytest.raises(ClientError) as still_absent:
            owner_client.head_bucket(Bucket=bucket)
        assert still_absent.value.response["Error"]["Code"] in {
            "404",
            "NoSuchBucket",
            "NotFound",
        }
        async with owner_maker.begin() as owner:
            assert await _row_identities(owner) == before_empty
            assert await owner.scalar(text("SELECT count(*) FROM public.datasets")) == 0
            first = User(
                id=uuid.uuid4(),
                email=f"inactive-{suffix}@seed.test",
                name="inactive",
                password_hash="fixture",
                role="viewer",
                is_active=False,
            )
            owner.add(first)
            await owner.execute(
                text("DELETE FROM public.storage_bindings WHERE purpose='datasets'")
            )
        async with owner_maker() as owner:
            before_missing = await _row_identities(owner)
        missing = _import(run, directory, "identity-missing-target-" + suffix)
        assert missing.returncode != 0 and "storage_unavailable" in missing.stderr
        async with owner_maker() as owner:
            assert await _row_identities(owner) == before_missing
        with pytest.raises(ClientError):
            owner_client.head_bucket(Bucket=bucket)

        prepared = run("scripts.prepare_original_catalog", [], owner=True)
        assert prepared.returncode == 0, "owner preparation failed"
        assert (
            owner_client.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled"
        )
        owner_client.put_bucket_tagging(
            Bucket=bucket,
            Tagging={
                "TagSet": [{"Key": "yoao-worktree-owner", "Value": resources["owner"]}]
            },
        )
        _verify_bucket_owner(owner_client, bucket, resources["owner"])
        neighbor_key = "neighbor/" + suffix
        neighbor = owner_client.put_object(
            Bucket=bucket, Key=neighbor_key, Body=b"neighbor"
        )
        neighbor_version = neighbor["VersionId"]
        _denied(
            lambda: client.get_object(
                Bucket=bucket, Key=neighbor_key, VersionId=neighbor_version
            )
        )
        _denied(
            lambda: client.put_object(
                Bucket=bucket, Key=neighbor_key, Body=b"overwrite"
            )
        )
        _denied(
            lambda: client.delete_object(
                Bucket=bucket, Key=neighbor_key, VersionId=neighbor_version
            )
        )
        _denied(
            lambda: client.delete_object(
                Bucket=bucket,
                Key=f"__aap_managed_datasets__/{uuid.uuid4()}/unknown",
                VersionId=neighbor_version,
            )
        )
        _denied(lambda: client.list_buckets())
        _denied(lambda: client.get_bucket_policy(Bucket=bucket))

        async with owner_maker.begin() as owner:
            await owner.execute(
                text(
                    "CREATE TABLE public.image_import_test_log "
                    "(dataset_id uuid, login_name text)"
                )
            )
            await owner.execute(
                text(
                    "CREATE FUNCTION public.image_import_test_audit() RETURNS trigger "
                    "LANGUAGE plpgsql SECURITY DEFINER "
                    "SET search_path=pg_catalog,pg_temp AS $$ BEGIN "
                    "INSERT INTO public.image_import_test_log VALUES (NEW.id,session_user); "
                    "RETURN NEW; END $$"
                )
            )
            await owner.execute(
                text(
                    "REVOKE ALL ON FUNCTION public.image_import_test_audit() FROM PUBLIC"
                )
            )
            await owner.execute(
                text(
                    "CREATE TRIGGER image_import_test_audit AFTER INSERT ON public.datasets "
                    "FOR EACH ROW EXECUTE FUNCTION public.image_import_test_audit()"
                )
            )

        before_drift = _versions(owner_client, bucket)
        async with owner_maker.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE public.storage_bindings SET bucket=:bucket "
                    "WHERE purpose='datasets'"
                ),
                {"bucket": bucket + "-drift"},
            )
        async with owner_maker() as owner:
            counts = await _row_identities(owner, include_creations=False)
            before_ids = set(
                await owner.scalars(
                    text("SELECT id FROM public.managed_original_creations")
                )
            )
        drift = _import(run, directory, "identity-drift-" + suffix)
        assert drift.returncode != 0 and "storage_unavailable" in drift.stderr
        async with owner_maker() as owner:
            assert await _row_identities(owner, include_creations=False) == counts
            creations = (
                await owner.execute(
                    text(
                        "SELECT id,bucket,status,error_code,prepared,receipt "
                        "FROM public.managed_original_creations"
                    )
                )
            ).all()
            new_creations = [row for row in creations if row.id not in before_ids]
            assert len(new_creations) == 1
            refused = new_creations[0]
            assert refused.bucket == bucket + "-drift"
            assert refused.status == "failed"
            assert refused.error_code == "storage_unavailable"
            assert refused.prepared is None and refused.receipt is None
        assert _versions(owner_client, bucket) == before_drift
        async with owner_maker.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE public.storage_bindings SET bucket=:bucket "
                    "WHERE purpose='datasets'"
                ),
                {"bucket": bucket},
            )
        async with owner_maker.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE public.storage_locations SET config=jsonb_set("
                    "config,'{use_ssl}',to_jsonb(NOT (config->>'use_ssl')::boolean)) "
                    "WHERE id=:id"
                ),
                {"id": LEGACY_LOCATION_ID},
            )
        async with owner_maker() as owner:
            counts = await _row_identities(owner, include_creations=False)
            before_ids = set(
                await owner.scalars(
                    text("SELECT id FROM public.managed_original_creations")
                )
            )
        scheme_drift = _import(run, directory, "identity-scheme-drift-" + suffix)
        assert (
            scheme_drift.returncode != 0
            and "storage_unavailable" in scheme_drift.stderr
        )
        async with owner_maker() as owner:
            assert await _row_identities(owner, include_creations=False) == counts
            creations = (
                await owner.execute(
                    text(
                        "SELECT id,bucket,status,error_code,prepared,receipt "
                        "FROM public.managed_original_creations"
                    )
                )
            ).all()
            refused = [row for row in creations if row.id not in before_ids]
            assert len(refused) == 1
            assert refused[0].bucket == bucket
            assert refused[0].status == "failed"
            assert refused[0].error_code == "storage_unavailable"
            assert refused[0].prepared is None and refused[0].receipt is None
        assert _versions(owner_client, bucket) == before_drift
        async with owner_maker.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE public.storage_locations SET config=jsonb_set("
                    "config,'{use_ssl}',to_jsonb(NOT (config->>'use_ssl')::boolean)) "
                    "WHERE id=:id"
                ),
                {"id": LEGACY_LOCATION_ID},
            )

        candidates = [first.id]

        async def import_and_verify(scenario, limit, expected_names):
            name = f"identity-{scenario}-{suffix}"
            result = _import(run, directory, name, limit)
            assert result.returncode == 0, "restricted image import failed"
            async with owner_maker() as owner:
                dataset = (
                    await owner.execute(
                        text(
                            "SELECT id,created_by,file_count FROM public.datasets "
                            "WHERE name=:name"
                        ),
                        {"name": name},
                    )
                ).one()
                audit = (
                    await owner.execute(
                        text(
                            "SELECT login_name FROM public.image_import_test_log "
                            "WHERE dataset_id=:id"
                        ),
                        {"id": dataset.id},
                    )
                ).one()
                rows = (
                    (
                        await owner.execute(
                            text(
                                "SELECT i.file_name,i.file_path,i.file_size,i.content_hash, "
                                "i.metadata->>'storage_relative_path' AS relative_path, "
                                "c.bucket,c.object_key,c.version_id,c.sha256,c.status "
                                "FROM public.dataset_items i "
                                "JOIN public.dataset_assets a ON a.dataset_item_id=i.id "
                                "JOIN public.dataset_asset_locations l ON l.asset_id=a.id "
                                "AND l.role='managed_original' "
                                "JOIN public.managed_original_creations c ON c.id=l.creation_id "
                                "WHERE i.dataset_id=:dataset ORDER BY i.file_name"
                            ),
                            {"dataset": dataset.id},
                        )
                    )
                    .mappings()
                    .all()
                )
            assert audit.login_name == "yoao_asset_operator_client"
            assert dataset.created_by in candidates
            assert dataset.file_count == len(expected_names)
            assert [row["file_name"] for row in rows] == expected_names
            for row in rows:
                expected = contents[row["file_name"]]
                assert row["relative_path"] == row["file_name"]
                assert row["file_path"] == row["object_key"]
                assert row["bucket"] == bucket and row["status"] == "verified"
                assert row["file_size"] == len(expected)
                assert (
                    row["content_hash"]
                    == row["sha256"]
                    == hashlib.sha256(expected).hexdigest()
                )
                assert row["version_id"] not in (None, "", "null")
                response = client.get_object(
                    Bucket=bucket, Key=row["object_key"], VersionId=row["version_id"]
                )
                assert response["VersionId"] == row["version_id"]
                with closing(response["Body"]) as body:
                    assert body.read() == expected
            return dataset.created_by

        assert (
            await import_and_verify("inactive-viewer", None, ["a.png", "b.jpg"])
            == first.id
        )
        async with owner_maker.begin() as owner:
            second = User(
                id=uuid.uuid4(),
                email=f"second-{suffix}@seed.test",
                name="second",
                password_hash="fixture",
                role="viewer",
                is_active=False,
            )
            owner.add(second)
            candidates.append(second.id)
        await import_and_verify("multiple", None, ["a.png", "b.jpg"])
        await import_and_verify("limit", 1, ["a.png"])
        result = owner_client.get_object(
            Bucket=bucket, Key=neighbor_key, VersionId=neighbor_version
        )
        with closing(result["Body"]) as body:
            assert body.read() == b"neighbor"
        record_property(
            "identity_image_import_cases",
            ["empty", "inactive_viewer", "multiple", "limit"],
        )
    finally:
        await operator.dispose()


async def test_local_image_import_uses_native_rows_and_retains_uploads_on_sql_failure(
    test_engine, tmp_path, image_profile
):
    resources, owner_client, client, run = image_profile
    bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    prepared = run("scripts.prepare_original_catalog", [], owner=True)
    assert prepared.returncode == 0, "owner preparation failed"
    async with owner_maker.begin() as owner:
        if not await owner.scalar(text("SELECT EXISTS(SELECT 1 FROM public.users)")):
            await _seed_actor(owner, role="viewer")
        await owner.execute(
            text("CREATE SEQUENCE public.image_import_test_reject_seen")
        )
        await owner.execute(
            text(
                "REVOKE ALL ON SEQUENCE public.image_import_test_reject_seen FROM PUBLIC"
            )
        )
        await owner.execute(
            text(
                "CREATE FUNCTION public.image_import_test_reject() RETURNS trigger "
                "LANGUAGE plpgsql SECURITY DEFINER "
                "SET search_path=pg_catalog,pg_temp AS $$ BEGIN "
                "PERFORM pg_catalog.nextval('public.image_import_test_reject_seen'::regclass); "
                "RAISE EXCEPTION 'image import rollback fixture'; END $$"
            )
        )
        await owner.execute(
            text("REVOKE ALL ON FUNCTION public.image_import_test_reject() FROM PUBLIC")
        )
        await owner.execute(
            text(
                "CREATE TRIGGER image_import_test_reject BEFORE INSERT "
                "ON public.dataset_items FOR EACH ROW "
                "EXECUTE FUNCTION public.image_import_test_reject()"
            )
        )
    directory = tmp_path / "rollback-images"
    directory.mkdir()
    payloads = {"first.png": b"first", "second.jpg": b"second", "third.png": b"third"}
    for name, value in payloads.items():
        (directory / name).write_bytes(value)
    name = "operator-image-rollback-" + uuid.uuid4().hex
    completed = False
    try:
        before_versions = _versions(owner_client, bucket)
        async with owner_maker() as owner:
            before_counts = await _row_identities(owner, include_creations=False)
            before_ids = set(
                await owner.scalars(
                    text("SELECT id FROM public.managed_original_creations")
                )
            )
            before_usage = set(
                await owner.scalars(
                    text("SELECT id FROM public.storage_usage_mutations")
                )
            )
        result = _import(run, directory, name)
        assert result.returncode != 0
        assert "storage_unavailable" in result.stderr
        async with owner_maker() as owner:
            assert (
                await owner.scalar(
                    text("SELECT is_called FROM public.image_import_test_reject_seen")
                )
                is True
            )
            assert (
                await _row_identities(owner, include_creations=False) == before_counts
            )
            assert (
                await owner.scalar(
                    text("SELECT count(*) FROM public.datasets WHERE name=:name"),
                    {"name": name},
                )
                == 0
            )
            creations = (
                await owner.execute(
                    text(
                        "SELECT id,dataset_id,bucket,object_key,version_id,status,"
                        "settled_at,sha256 FROM public.managed_original_creations"
                    )
                )
            ).all()
            retained = [row for row in creations if row.id not in before_ids]
            assert len(retained) == 3
            assert len({row.dataset_id for row in retained}) == 1
            assert not await owner.scalar(
                text("SELECT EXISTS(SELECT 1 FROM public.datasets WHERE id=:id)"),
                {"id": retained[0].dataset_id},
            )
            usage = (
                await owner.execute(
                    text(
                        "SELECT id,owner_id,producer,state,physical_outcome "
                        "FROM public.storage_usage_mutations"
                    )
                )
            ).all()
            created_usage = [row for row in usage if row.id not in before_usage]
            assert {row.owner_id for row in created_usage} == {
                row.id for row in retained
            }
            assert len(created_usage) == 3
            assert all(
                row.producer == "managed_original_creation"
                and row.state == "confirmed"
                and row.physical_outcome == "written"
                for row in created_usage
            )
        added = _versions(owner_client, bucket) - before_versions
        assert len(added) == 3
        assert {(row.object_key, row.version_id) for row in retained} == added
        assert all(
            row.bucket == bucket
            and row.status == "verified"
            and row.settled_at is not None
            for row in retained
        )
        observed = []
        for row in retained:
            assert row.object_key.startswith("__aap_managed_datasets__/")
            assert row.version_id not in ("", "null", None)
            response = client.get_object(
                Bucket=bucket, Key=row.object_key, VersionId=row.version_id
            )
            with closing(response["Body"]) as body:
                value = body.read()
            assert hashlib.sha256(value).hexdigest() == row.sha256
            observed.append(value)
        assert sorted(observed) == sorted(payloads.values())
        assert not owner_client.list_multipart_uploads(Bucket=bucket).get("Uploads")
        completed = True
    finally:
        if completed:
            async with owner_maker.begin() as owner:
                await owner.execute(
                    text(
                        "DROP TRIGGER image_import_test_reject ON public.dataset_items"
                    )
                )
                await owner.execute(
                    text("DROP FUNCTION public.image_import_test_reject()")
                )
                await owner.execute(
                    text("DROP SEQUENCE public.image_import_test_reject_seen")
                )
