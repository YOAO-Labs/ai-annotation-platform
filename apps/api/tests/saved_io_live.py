"""Start the shipped Source runtime only on the verifier's disposable resources."""

from contextlib import ExitStack, closing, contextmanager
import json
import os
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
import httpx
from sqlalchemy.engine import make_url
from yoao_assets_source.saved_io import TOKEN_SETTINGS

from app.config import settings
from tests.m2b_business_stack import _port, _spawn, _wait_http
from tests.test_asset_storage_cleanup_runtime import (
    _local_s3_client,
    _shared_bootstrap_minio,
    _verify_bucket_owner,
    _verified_resource_manifest,
    _verify_postgres,
)


def real_source(test_engine, directory, *, s3_credentials, allowlist=None, encryption_key=None):
    _verify_postgres(test_engine.url)
    resources = _verified_resource_manifest(test_engine, test_engine.url)
    assert _shared_bootstrap_minio(
        settings, container_id=os.environ["AAP_MODULE_DATA_MINIO_CONTAINER_ID"]
    ) is not None
    scheme = "https" if settings.minio_use_ssl else "http"
    storage_origin = f"{scheme}://{settings.minio_endpoint}"
    control = make_url(os.environ["AAP_MODULE_DATA_ASSET_SOURCE_URL"])
    physical = make_url(os.environ["AAP_MODULE_DATA_ASSETS_SOURCE_SAVED_IO_URL"])
    for url in (control, physical):
        assert (url.host, url.port, url.database) == (
            test_engine.url.host, test_engine.url.port, test_engine.url.database,
        )
        assert url.username != test_engine.url.username
    assert control.username != physical.username
    port = _port()
    origin = f"http://127.0.0.1:{port}"
    key = encryption_key or Fernet.generate_key().decode()
    connections = secrets.token_urlsafe(32)
    tokens = {scope: secrets.token_urlsafe(32) for scope in TOKEN_SETTINGS}
    with ExitStack() as processes, httpx.Client(trust_env=False, timeout=2) as ready:
        service = _spawn(
            processes, directory, "saved-io-source", [sys.executable, "-m", "yoao_assets_source_runtime"],
            {
                "PATH": os.defpath,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "ASSETS_SOURCE_HOST": "127.0.0.1",
                "ASSETS_SOURCE_PORT": str(port),
                "ASSETS_SOURCE_TOKEN": secrets.token_urlsafe(32),
                "ASSETS_SOURCE_IMPORT_VERIFY_TOKEN": secrets.token_urlsafe(32),
                "ASSETS_SOURCE_CONNECTIONS_TOKEN": connections,
                "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY": key,
                "ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST": json.dumps(
                    allowlist if allowlist is not None else [urlsplit(storage_origin).hostname]
                ),
                "ASSETS_SOURCE_DATABASE_URL": control.set(drivername="postgresql").render_as_string(hide_password=False),
                "ASSETS_SOURCE_SAVED_IO_DATABASE_URL": physical.set(drivername="postgresql").render_as_string(hide_password=False),
                "ASSETS_SOURCE_SAVED_IO_SPOOL_DIR": str(directory),
                "ASSETS_SOURCE_S3_ENDPOINT": storage_origin,
                "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT": storage_origin,
                "ASSETS_SOURCE_S3_ACCESS_KEY": s3_credentials[0],
                "ASSETS_SOURCE_S3_SECRET_KEY": s3_credentials[1],
                "ASSETS_SOURCE_DATASETS_BUCKET": resources["buckets"]["MINIO_DATASETS_BUCKET"],
                "ASSETS_SOURCE_IMPORT_BUCKET": resources["buckets"]["MINIO_IMPORT_BUCKET"],
                "ASSETS_SOURCE_MEDIA_CACHE_BUCKET": resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"],
                **{name: tokens[scope] for scope, name in TOKEN_SETTINGS.items()},
            },
            origin=origin,
        )
        _wait_http(ready, service, "registered Assets Source", "/ready")
        yield SimpleNamespace(
            origin=origin, tokens=tokens, encryption_key=key,
            connections_token=connections, storage_origin=storage_origin,
        )


@contextmanager
def registered_sink(test_engine):
    _verify_postgres(test_engine.url)
    resources = _verified_resource_manifest(test_engine, test_engine.url)
    assert _shared_bootstrap_minio(
        settings, container_id=os.environ["AAP_MODULE_DATA_MINIO_CONTAINER_ID"]
    ) is not None
    scripts = str(Path(__file__).resolve().parents[3] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from verify_module_data_boundary import (
        _cleanup_mc,
        _cleanup_storage_scope,
        _destroy_cleanup_storage_scope,
    )

    suffix = secrets.token_hex(8)
    bucket, prefix = f"yoao-saved-output-{suffix}", f"fixture/{suffix}"
    with closing(
        _local_s3_client(settings, settings.minio_access_key, settings.minio_secret_key)
    ) as storage:
        scope = _cleanup_storage_scope(
            SimpleNamespace(settings=settings, resources=resources, s3=storage)
        )
        storage.create_bucket(Bucket=bucket)
        storage.put_bucket_tagging(
            Bucket=bucket,
            Tagging={"TagSet": [{"Key": "yoao-worktree-owner", "Value": resources["owner"]}]},
        )
        _verify_bucket_owner(storage, bucket, resources["owner"])
        try:
            storage.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": "Enabled"},
            )
            assert storage.get_bucket_versioning(Bucket=bucket)["Status"] == "Enabled"
            access, secret = scope["access_key"], scope["secret_key"]
            _cleanup_mc(scope, ["admin", "user", "add", "scoped", access, secret])
            scope["user_created"] = True
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetBucketLocation", "s3:GetBucketVersioning"],
                        "Resource": [f"arn:aws:s3:::{bucket}"],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["s3:ListBucket", "s3:ListBucketVersions"],
                        "Resource": [f"arn:aws:s3:::{bucket}"],
                        "Condition": {"StringLike": {"s3:prefix": [prefix + "/*"]}},
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject",
                            "s3:AbortMultipartUpload", "s3:DeleteObject", "s3:DeleteObjectVersion",
                        ],
                        "Resource": [f"arn:aws:s3:::{bucket}/{prefix}/*"],
                    },
                ],
            }
            _cleanup_mc(
                scope, ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
                payload=json.dumps(policy),
            )
            scope["policy_created"] = True
            _cleanup_mc(scope, ["admin", "policy", "attach", "scoped", scope["policy"], "--user", access])
            yield SimpleNamespace(bucket=bucket, prefix=prefix, credentials=(access, secret), storage=storage)
        finally:
            try:
                _destroy_cleanup_storage_scope(scope)
            finally:
                _verify_bucket_owner(storage, bucket, resources["owner"])
                for page in storage.get_paginator("list_object_versions").paginate(Bucket=bucket):
                    objects = [
                        {"Key": item["Key"], "VersionId": item["VersionId"]}
                        for item in (*page.get("Versions", ()), *page.get("DeleteMarkers", ()))
                    ]
                    if objects:
                        assert not storage.delete_objects(
                            Bucket=bucket, Delete={"Objects": objects, "Quiet": True}
                        ).get("Errors")
                storage.delete_bucket(Bucket=bucket)
