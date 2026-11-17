"""Finite Native identity policies and actual SDK provisioning for acceptance."""

from __future__ import annotations

import json

import boto3
from botocore.config import Config

from storage_s3_contract import require, sha256

NATIVE_CONNECTION_BASE_PREFIX = "aap-native-b"


def policy_fingerprint():
    policies = [
        _native_source_storage_policy({"bucket": "native-contract-a"}),
        _native_connection_storage_policy({"bucket": "native-contract-b"}),
        _native_binding_probe_policy({"bucket": "native-contract-a"}),
    ]
    return sha256(json.dumps(policies, sort_keys=True, separators=(",", ":")).encode())


def _native_binding_probe_policy(scope: dict) -> dict:
    """Permit ordinary binding probes without granting Native object authority."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Deny",
                "Action": ["s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
                "Resource": ["*"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                ],
                "Resource": [f"arn:aws:s3:::{scope['bucket']}/.probe-media-cache/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{scope['bucket']}"],
                "Condition": {
                    "StringLike": {"s3:prefix": [".probe-media-cache/*"]},
                },
            },
        ],
    }


def _native_source_storage_policy(scope: dict) -> dict:
    bucket_arn = f"arn:aws:s3:::{scope['bucket']}"
    native_objects = [
        f"{bucket_arn}/image-pyramids/objects/*",
        f"{bucket_arn}/_aap_managed/media-cache/image-pyramids/objects/*",
    ]
    native_prefixes = [
        "image-pyramids/objects/*",
        "_aap_managed/media-cache/image-pyramids/objects/*",
    ]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Deny",
                "Action": [
                    "s3:AbortMultipartUpload",
                    "s3:ListMultipartUploadParts",
                ],
                "Resource": ["*"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                ],
                "Resource": native_objects,
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:ListBucketVersions"],
                "Resource": [bucket_arn],
                "Condition": {"StringLike": {"s3:prefix": native_prefixes}},
            },
            {
                # Metadata-only permission requires complete bounded pages.
                # Explicit denials above prevent a peer from expanding
                # PutObject into abort or part-inspection authority.
                "Effect": "Allow",
                "Action": ["s3:ListBucketMultipartUploads"],
                "Resource": [bucket_arn],
            },
        ],
    }


def _native_connection_storage_policy(scope: dict) -> dict:
    bucket_arn = f"arn:aws:s3:::{scope['bucket']}"
    prefix = NATIVE_CONNECTION_BASE_PREFIX
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Deny",
                "Action": [
                    "s3:AbortMultipartUpload",
                    "s3:ListMultipartUploadParts",
                ],
                "Resource": ["*"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                ],
                "Resource": [f"{bucket_arn}/{prefix}/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:ListBucketVersions"],
                "Resource": [bucket_arn],
                "Condition": {"StringLike": {"s3:prefix": [f"{prefix}/*"]}},
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucketMultipartUploads"],
                "Resource": [bucket_arn],
            },
        ],
    }


def client_for(service, endpoint, credentials, budget):
    # Match the real consumers: no checksum suppression or implicit SDK retries.
    client = boto3.client(
        service,
        endpoint_url=endpoint,
        aws_access_key_id=credentials[0],
        aws_secret_access_key=credentials[1],
        config=Config(
            signature_version="s3v4" if service == "s3" else "v4",
            region_name="us-east-1",
            s3={"addressing_style": "path"},
            proxies={},
            connect_timeout=3,
            read_timeout=15,
            retries={"total_max_attempts": 1},
        ),
    )
    client.meta.events.register("before-parameter-build." + service, budget.hook)
    return client


def identity_state(iam, identity):
    from fixture_store import _canonical_native_output_policy

    user, key, policy_arn, policy = identity
    require(iam.get_user(UserName=user)["User"]["UserName"] == user, "iam_user")
    keys = iam.list_access_keys(UserName=user)
    require(not keys.get("IsTruncated"), "iam_keys_truncated")
    require(
        [(item["AccessKeyId"], item["Status"]) for item in keys["AccessKeyMetadata"]]
        == [(key["AccessKeyId"], "Active")],
        "iam_exact_active_key",
    )
    groups = iam.list_groups_for_user(UserName=user)
    require(not groups.get("IsTruncated") and not groups["Groups"], "iam_no_groups")
    attached = iam.list_attached_user_policies(UserName=user)
    require(
        not attached.get("IsTruncated")
        and [item["PolicyArn"] for item in attached["AttachedPolicies"]]
        == [policy_arn],
        "iam_exact_policy",
    )
    inline = iam.list_user_policies(UserName=user)
    require(
        not inline.get("IsTruncated") and not inline["PolicyNames"], "iam_no_inline"
    )
    description = iam.get_policy(PolicyArn=policy_arn)["Policy"]
    actual = iam.get_policy_version(
        PolicyArn=policy_arn, VersionId=description["DefaultVersionId"]
    )["PolicyVersion"]["Document"]
    require(
        _canonical_native_output_policy(actual)
        == _canonical_native_output_policy(policy),
        "iam_policy_roundtrip",
    )


def provision_identity(iam, user, policy):
    iam.create_user(UserName=user)
    arn = iam.create_policy(PolicyName=user, PolicyDocument=json.dumps(policy))[
        "Policy"
    ]["Arn"]
    iam.attach_user_policy(UserName=user, PolicyArn=arn)
    returned = iam.create_access_key(UserName=user)["AccessKey"]
    require(
        returned["UserName"] == user and returned["Status"] == "Active",
        "iam_created_key",
    )
    key = {field: returned[field] for field in ("AccessKeyId", "SecretAccessKey")}
    identity = (user, key, arn, policy)
    identity_state(iam, identity)
    return identity
