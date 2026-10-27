"""Owned storage helpers shared by live module verifiers."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urlsplit
import json
import secrets
import sys

import boto3
from botocore.config import Config
from sqlalchemy.engine import make_url

from app.config import settings
from tests.test_asset_storage_cleanup_runtime import (
    _local_s3_client,
    _verified_resource_manifest,
    _verify_bucket_owner,
)
from tests.test_labeling_class_usage_reads_legacy import _legacy_runtime_url
from tests.workspace_configuration_helpers import ROOT


GUIDE_FILE = b"\x89PNG\r\n\x1a\n" + b"owned-guide-fixture\n"


def _cleanup_objects(s3, bucket: str, keys) -> None:
    """Remove every saved version of the owned fixture keys."""
    identifiers = set(keys)
    if not identifiers:
        return
    versions = []
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]:
            if item["Key"] in identifiers:
                versions.append({"Key": item["Key"], "VersionId": item["VersionId"]})
    if versions:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": versions, "Quiet": True})
        return
    s3.delete_objects(
        Bucket=bucket,
        Delete={"Objects": [{"Key": key} for key in identifiers], "Quiet": True},
    )


def endpoint_url() -> str:
    return (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )


def port_client(access_key: str, secret_key: str):
    endpoint = urlsplit(endpoint_url())
    if endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise AssertionError("acceptance MinIO endpoint must be loopback-only")
    return boto3.client(
        "s3",
        endpoint_url=endpoint.geturl(),
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=3,
            read_timeout=8,
            retries={"total_max_attempts": 1},
            s3={"addressing_style": "path"},
        ),
    )


def create_guide_storage_scope(resources: dict, owner_s3, bucket: str, denied_key: str):
    """Create one narrowly scoped MinIO principal with one exact denied key."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_module_data_boundary import (
        _cleanup_mc,
        _cleanup_storage_scope,
        _destroy_cleanup_storage_scope,
    )

    scope = _cleanup_storage_scope(
        SimpleNamespace(settings=settings, resources=resources, s3=owner_s3)
    )
    scope["bucket"] = bucket
    scope["policy"] = f"aap-guide-{secrets.token_hex(8)}"
    prefix_arn = f"arn:aws:s3:::{bucket}/projects/*/guide/*"
    denied_arn = f"arn:aws:s3:::{bucket}/{denied_key}"
    try:
        _cleanup_mc(
            scope,
            [
                "admin",
                "user",
                "add",
                "scoped",
                scope["access_key"],
                scope["secret_key"],
            ],
        )
        scope["user_created"] = True
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                    "Resource": [prefix_arn],
                },
                {
                    "Effect": "Deny",
                    "Action": ["s3:DeleteObjectVersion"],
                    "Resource": [prefix_arn],
                },
                {
                    "Effect": "Deny",
                    "Action": ["s3:DeleteObject"],
                    "Resource": [denied_arn],
                },
            ],
        }
        _cleanup_mc(
            scope,
            ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
            payload=json.dumps(policy),
        )
        scope["policy_created"] = True
        _cleanup_mc(
            scope,
            [
                "admin",
                "policy",
                "attach",
                "scoped",
                scope["policy"],
                "--user",
                scope["access_key"],
            ],
        )
        return scope, _destroy_cleanup_storage_scope
    except BaseException:
        _destroy_cleanup_storage_scope(scope)
        raise


async def assert_workspace_identity(test_engine) -> tuple[dict, str, object]:
    runtime_url = _legacy_runtime_url(test_engine)
    resources = _verified_resource_manifest(test_engine, make_url(runtime_url))
    bucket = resources["buckets"]["MINIO_BUCKET"]
    owner_s3 = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    _verify_bucket_owner(owner_s3, bucket, resources["owner"])
    assert settings.minio_bucket == bucket
    assert owner_s3.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled"
    return resources, bucket, owner_s3


__all__ = [
    "GUIDE_FILE",
    "_cleanup_objects",
    "assert_workspace_identity",
    "create_guide_storage_scope",
    "endpoint_url",
    "port_client",
]
