"""Provision the private, physically isolated original store for fixture suites.

The caller owns the authority transaction and may publish the returned target only
with its immutable receipt and fixture root. An interrupted provision is retained
in the existing exact-ID owner manifest for explicit inspection and recovery.
"""

from __future__ import annotations
from contextlib import closing

import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import stat
import urllib.parse
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from verify_storage_candidates import Owner, command, host_port, private_write
from verify_storage_source_fence import until_ready
from storage_s3_contract import ContractError, require


MINIO_IMAGE = "icr.io/fusion-open/minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e"


def _client(endpoint, access, secret):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        config=Config(
            signature_version="s3v4",
            connect_timeout=3,
            read_timeout=15,
            retries={"total_max_attempts": 1},
            s3={"addressing_style": "path"},
            proxies={},
        ),
    )


def _empty_inventory(client, bucket):
    objects = client.list_objects_v2(Bucket=bucket, MaxKeys=1)
    versions = client.list_object_versions(Bucket=bucket, MaxKeys=1)
    uploads = client.list_multipart_uploads(Bucket=bucket, MaxUploads=1)
    require(
        not objects.get("Contents")
        and not objects.get("IsTruncated")
        and not versions.get("Versions")
        and not versions.get("DeleteMarkers")
        and not versions.get("IsTruncated")
        and not uploads.get("Uploads")
        and not uploads.get("IsTruncated"),
        "fixture_store_not_empty",
    )


def verify_prerequisites():
    require(shutil.which("mc") is not None, "fixture_mc_unavailable")
    require(shutil.which("docker") is not None, "fixture_docker_unavailable")
    result = subprocess.run(
        ["docker", "image", "inspect", MINIO_IMAGE], capture_output=True, timeout=60
    )
    if result.returncode:
        if b"no such" in result.stderr.lower():
            raise ContractError("fixture_image_not_cached")
        raise ContractError("fixture_docker_inspection_failed")
    image = json.loads(result.stdout)
    require(
        len(image) == 1
        and image[0].get("Os") == "linux"
        and image[0].get("Architecture") == "amd64"
        and MINIO_IMAGE in image[0].get("RepoDigests", []),
        "fixture_image_not_cached",
    )


def provision(owner: Owner, database_identity: dict):
    """Create exactly one store; never adopt an existing bucket or owner manifest."""
    require(not owner.data["resources"], "fixture_owner_not_fresh")
    require("fixture_store" not in owner.data, "fixture_store_already_recorded")
    require(
        owner.data.get("fixture_intent", {}).get("database") == database_identity
        and owner.data["fixture_intent"].get("state") == "provisioning",
        "fixture_intent_missing",
    )
    root_access = "fixture" + secrets.token_hex(12)
    root_secret = secrets.token_urlsafe(32)
    access = "fixture" + secrets.token_hex(12)
    secret = secrets.token_urlsafe(32)
    reader_access = "fixture" + secrets.token_hex(12)
    reader_secret = secrets.token_urlsafe(32)
    require(
        len({root_access, access, reader_access}) == 3
        and len({root_secret, secret, reader_secret}) == 3,
        "fixture_accounts_not_distinct",
    )
    bucket = "fixture-original-" + owner.data["nonce"]
    canary = "fixture-canary-" + owner.data["nonce"]
    env_file = owner.private / "minio.env"
    private_write(
        env_file,
        f"MINIO_ROOT_USER={root_access}\nMINIO_ROOT_PASSWORD={root_secret}\n".encode(),
    )
    volume_name = f"aap-e07-{owner.data['nonce']}-{owner.counter + 1}"
    require(
        owner.inspect({"kind": "volume", "name": volume_name, "id": None}) is None,
        "fixture_volume_already_exists",
    )
    volume = owner.create("volume")
    network_name = f"aap-e07-{owner.data['nonce']}-{owner.counter + 1}"
    require(
        owner.inspect({"kind": "network", "name": network_name, "id": None}) is None,
        "fixture_network_already_exists",
    )
    network = owner.create("network")
    container = owner.container(
        MINIO_IMAGE,
        args=(
            "--pull=never",
            "--network",
            network["id"],
            "--publish",
            "127.0.0.1::9000",
            "--env-file",
            str(env_file),
            "--volume",
            volume["id"] + ":/data",
        ),
        cmd=("server", "/data", "--console-address", ":9001"),
    )
    owner.start(container)
    started = owner.verified(container)
    process = started.get("State") or {}
    require(
        process.get("Status") == "running"
        and process.get("Running") is True
        and isinstance(process.get("Pid"), int)
        and process["Pid"] > 0
        and bool(process.get("StartedAt"))
        and started.get("RestartCount") == 0,
        "fixture_source_birth_unconfirmed",
    )
    owner.data["fixture_process_birth"] = {
        "container_id": container["id"],
        "pid": process["Pid"],
        "started_at": process["StartedAt"],
    }
    owner.save()
    admin_endpoint = f"http://127.0.0.1:{host_port(owner, container, 9000)}"
    observed = owner.verified(container)
    address = observed["NetworkSettings"]["Networks"][network["name"]]["IPAddress"]
    ip = ipaddress.ip_address(address)
    require(
        ip.version == 4
        and ip.is_private
        and not (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
        ),
        "fixture_bridge_address_invalid",
    )
    endpoint = f"http://{address}:9000"
    policy_name = "fixture-original-" + owner.data["nonce"]
    mc_env = {
        "PATH": os.environ["PATH"],
        "MC_CONFIG_DIR": str(owner.private / "mc"),
        "MC_HOST_fixture": (
            "http://"
            + urllib.parse.quote(root_access, safe="")
            + ":"
            + urllib.parse.quote(root_secret, safe="")
            + "@"
            + urllib.parse.urlsplit(admin_endpoint).netloc
        ),
    }
    policy_file = owner.private / "policy.json"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetBucketLocation",
                    "s3:GetBucketVersioning",
                    "s3:ListBucket",
                    "s3:ListBucketVersions",
                    "s3:ListBucketMultipartUploads",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                    "s3:ListMultipartUploadParts",
                    "s3:AbortMultipartUpload",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}/*"],
            },
        ],
    }
    private_write(policy_file, json.dumps(policy).encode())
    reader_policy_file = owner.private / "reader-policy.json"
    reader_policy_name = "fixture-reader-" + owner.data["nonce"]
    private_write(
        reader_policy_file,
        json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetBucketLocation"],
                        "Resource": [f"arn:aws:s3:::{bucket}"],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                        "Resource": [f"arn:aws:s3:::{bucket}/*"],
                    },
                ],
            }
        ).encode(),
    )
    with closing(_client(admin_endpoint, root_access, root_secret)) as root:
        until_ready(root)
        require(not root.list_buckets().get("Buckets"), "fixture_volume_not_empty")
        owner.data["fixture_intent"]["initial_bucket_count"] = 0
        owner.save()
        root.create_bucket(Bucket=bucket)
        root.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        root.create_bucket(Bucket=canary)
        command(
            ["mc", "admin", "user", "add", "fixture"],
            input_data=f"{access}\n{secret}\n".encode(),
            env=mc_env,
        )
        command(
            [
                "mc",
                "admin",
                "policy",
                "create",
                "fixture",
                policy_name,
                str(policy_file),
            ],
            env=mc_env,
        )
        command(
            [
                "mc",
                "admin",
                "policy",
                "attach",
                "fixture",
                policy_name,
                "--user",
                access,
            ],
            env=mc_env,
        )
        command(
            ["mc", "admin", "user", "add", "fixture"],
            input_data=f"{reader_access}\n{reader_secret}\n".encode(),
            env=mc_env,
        )
        command(
            [
                "mc",
                "admin",
                "policy",
                "create",
                "fixture",
                reader_policy_name,
                str(reader_policy_file),
            ],
            env=mc_env,
        )
        command(
            [
                "mc",
                "admin",
                "policy",
                "attach",
                "fixture",
                reader_policy_name,
                "--user",
                reader_access,
            ],
            env=mc_env,
        )
        with closing(_client(admin_endpoint, access, secret)) as limited:
            require(
                limited.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled",
                "fixture_versioning_unproven",
            )
            try:
                limited.head_bucket(Bucket=canary)
            except ClientError as exc:
                require(
                    exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    == 403,
                    "fixture_account_scope_unproven",
                )
            else:
                raise ValueError("fixture_account_scope_unproven")
        root.delete_bucket(Bucket=canary)
        _empty_inventory(root, bucket)
        owner.data["fixture_intent"]["writer_canary_denied"] = True
        owner.save()
    credentials_file = owner.private / "data-plane.json"
    private_write(
        credentials_file,
        json.dumps({"access_key": access, "secret_key": secret}).encode(),
    )
    reader_file = owner.private / "read-plane.json"
    private_write(
        reader_file,
        json.dumps({"access_key": reader_access, "secret_key": reader_secret}).encode(),
    )
    owner.data["fixture_store"] = {
        "database": database_identity,
        "endpoint": endpoint,
        "bucket": bucket,
        "container_id": container["id"],
        "network_id": network["id"],
        "network_name": network["name"],
        "bridge_address": address,
        "admin_endpoint": admin_endpoint,
        "volume_id": volume["id"],
        "credentials_file": str(credentials_file),
        "reader_file": str(reader_file),
        "state": "provisioned_unpublished",
    }
    owner.data["fixture_intent"]["state"] = "provisioned_unpublished"
    owner.save()
    return endpoint, bucket, {"access_key": access, "secret_key": secret}


def _private_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600,
            "fixture_private_file",
        )
        return stream.read()


def credentials(owner, store):
    path = Path(store["credentials_file"])
    require(path == owner.private / "data-plane.json", "fixture_credentials_path")
    value = json.loads(_private_bytes(path))
    require(
        set(value) == {"access_key", "secret_key"}
        and all(isinstance(value[key], str) and value[key] for key in value),
        "fixture_credentials_invalid",
    )
    return value


def reader_credentials(owner, store):
    path = Path(store["reader_file"])
    require(path == owner.private / "read-plane.json", "fixture_reader_path")
    value = json.loads(_private_bytes(path))
    require(
        set(value) == {"access_key", "secret_key"}
        and all(isinstance(value[key], str) and value[key] for key in value),
        "fixture_reader_invalid",
    )
    require(
        value["access_key"] != credentials(owner, store)["access_key"],
        "fixture_reader_not_distinct",
    )
    return value


def _canonical_policy(value):
    require(
        isinstance(value, dict)
        and set(value) == {"Version", "Statement"}
        and value["Version"] == "2012-10-17"
        and isinstance(value["Statement"], list),
        "fixture_policy_invalid",
    )
    statements = []
    for statement in value["Statement"]:
        require(
            isinstance(statement, dict)
            and set(statement) == {"Effect", "Action", "Resource"}
            and statement["Effect"] == "Allow",
            "fixture_policy_invalid",
        )
        normalized = {"Effect": "Allow"}
        for field in ("Action", "Resource"):
            entries = statement[field]
            entries = [entries] if isinstance(entries, str) else entries
            require(
                isinstance(entries, list)
                and all(isinstance(entry, str) and entry for entry in entries),
                "fixture_policy_invalid",
            )
            normalized[field] = sorted(entries)
        statements.append(normalized)
    statements.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return {"Version": value["Version"], "Statement": statements}


def account_fingerprints(owner, store):
    writer, reader = credentials(owner, store), reader_credentials(owner, store)
    result = {}
    for role, account, filename in (
        ("writer", writer, "policy.json"),
        ("reader", reader, "reader-policy.json"),
    ):
        policy = _canonical_policy(json.loads(_private_bytes(owner.private / filename)))
        result[role + "_identity_sha256"] = hashlib.sha256(
            account["access_key"].encode()
        ).hexdigest()
        result[role + "_policy_sha256"] = hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    return result


def verify_accounts(owner, store):
    root_access, root_secret = _root_credentials(owner)
    mc_env = {
        "PATH": os.environ["PATH"],
        "MC_CONFIG_DIR": str(owner.private / "mc"),
        "MC_HOST_fixture": (
            "http://"
            + urllib.parse.quote(root_access, safe="")
            + ":"
            + urllib.parse.quote(root_secret, safe="")
            + "@"
            + urllib.parse.urlsplit(store["admin_endpoint"]).netloc
        ),
    }
    for account, policy_name, filename in (
        (
            credentials(owner, store),
            "fixture-original-" + owner.data["nonce"],
            "policy.json",
        ),
        (
            reader_credentials(owner, store),
            "fixture-reader-" + owner.data["nonce"],
            "reader-policy.json",
        ),
    ):
        info = json.loads(
            command(
                [
                    "mc",
                    "--json",
                    "admin",
                    "user",
                    "info",
                    "fixture",
                    account["access_key"],
                ],
                env=mc_env,
            )
        )
        policy = json.loads(
            command(
                ["mc", "--json", "admin", "policy", "info", "fixture", policy_name],
                env=mc_env,
            )
        )
        require(
            info.get("status") == "success"
            and info.get("accessKey") == account["access_key"]
            and info.get("userStatus") == "enabled"
            and info.get("policyName") == policy_name
            and not info.get("memberOf")
            and policy.get("status") == "success"
            and _canonical_policy(policy["policyInfo"]["Policy"])
            == _canonical_policy(json.loads(_private_bytes(owner.private / filename))),
            "fixture_account_policy_changed",
        )


def _physical(owner, store):
    require(
        len(owner.data["resources"]) == 3
        and {(item["kind"], item["id"]) for item in owner.data["resources"]}
        == {
            ("container", store["container_id"]),
            ("volume", store["volume_id"]),
            ("network", store["network_id"]),
        },
        "fixture_inventory_changed",
    )
    container = next(
        item for item in owner.data["resources"] if item["id"] == store["container_id"]
    )
    volume = next(
        item for item in owner.data["resources"] if item["id"] == store["volume_id"]
    )
    network = next(
        item for item in owner.data["resources"] if item["id"] == store["network_id"]
    )
    observed_container = owner.verified(container)
    observed_volume = owner.verified(volume)
    observed_network = owner.verified(network)
    require(
        observed_container.get("Config", {}).get("Image") == MINIO_IMAGE
        and observed_container.get("State", {}).get("Running") is True
        and observed_volume.get("Name") == store["volume_id"]
        and any(
            mount.get("Type") == "volume"
            and mount.get("Name") == store["volume_id"]
            and mount.get("Destination") == "/data"
            for mount in observed_container.get("Mounts", [])
        )
        and observed_network.get("Name") == store["network_name"]
        and observed_network.get("Driver") == "bridge"
        and set(observed_network.get("Containers", {})) == {store["container_id"]}
        and observed_container["NetworkSettings"]["Networks"][store["network_name"]][
            "IPAddress"
        ]
        == store["bridge_address"]
        and store["endpoint"] == f"http://{store['bridge_address']}:9000"
        and store["admin_endpoint"]
        == f"http://127.0.0.1:{host_port(owner, container, 9000)}",
        "fixture_physical_identity_changed",
    )
    return observed_container, observed_network


def inspect_prepared_store(owner: Owner, database_identity: dict):
    """Validate the owned pre-start topology without publishing its credentials."""
    store = owner.data["fixture_store"]
    require(
        store["database"] == database_identity
        and owner.data["fixture_intent"]["database"] == database_identity
        and store["state"]
        in {"provisioned_unpublished", "observed_unpublished", "published"},
        "fixture_store_identity_changed",
    )
    _physical(owner, store)
    credentials(owner, store)
    return store["bridge_address"]


def verified_store(owner: Owner, database_identity: dict):
    store = owner.data["fixture_store"]
    require(
        store["database"] == database_identity
        and owner.data["fixture_intent"]["database"] == database_identity
        and store["state"] in {"observed_unpublished", "published"},
        "fixture_store_identity_changed",
    )
    receipt = _private_bytes(owner.private / "original-store-receipt.json")
    require(
        hashlib.sha256(receipt).hexdigest() == store["receipt_sha256"],
        "fixture_receipt_changed",
    )
    observation = json.loads(receipt)
    physical, network = _physical(owner, store)
    require(
        all(
            observation[key] == store[key]
            for key in (
                "database",
                "endpoint",
                "admin_endpoint",
                "bridge_address",
                "bucket",
                "container_id",
                "network_id",
                "network_name",
                "volume_id",
            )
        )
        and observation["image"] == MINIO_IMAGE
        and observation["image_id"] == physical["Image"]
        and observation["network_ipam"] == network["IPAM"]
        and observation["accounts"] == account_fingerprints(owner, store)
        and observation["initial_bucket_count"] == 0
        and observation["writer_canary_denied"] is True
        and observation["versioning"] == "Enabled"
        and observation["writer_probe"] == "passed"
        and observation["provisioning_run_id"]
        == owner.data["fixture_intent"]["provisioning_run_id"]
        and observation["reader_probe"]["head"] == "passed"
        and observation["reader_probe"]["get"] == "passed"
        and observation["reader_probe"]["range"] == "passed"
        and observation["reader_probe"]["put_denied"] is True
        and observation["reader_probe"]["delete_denied"] is True
        and observation["reader_probe"]["version_delete_denied"] is True
        and observation["reader_probe"]["admin_denied"] is True,
        "fixture_receipt_identity_changed",
    )
    verify_accounts(owner, store)
    account = credentials(owner, store)
    with closing(
        _client(store["endpoint"], account["access_key"], account["secret_key"])
    ) as limited:
        require(
            limited.get_bucket_versioning(Bucket=store["bucket"]).get("Status")
            == "Enabled",
            "fixture_versioning_unproven",
        )
    reader = reader_credentials(owner, store)
    with closing(
        _client(store["endpoint"], reader["access_key"], reader["secret_key"])
    ) as limited:
        require(
            limited.get_bucket_location(Bucket=store["bucket"])
            .get("ResponseMetadata", {})
            .get("HTTPStatusCode")
            == 200,
            "fixture_reader_credentials_unproven",
        )
    return store, account


_NATIVE_OUTPUT_SCHEMA = "aap-fixture-native-output-target/v1"
_NATIVE_OUTPUT_PREFIXES = (
    "image-assets/",
    "image-pyramids/",
    "media-assets/",
    "_aap_managed/media-cache/thumbnails/objects/",
    "_aap_managed/media-cache/image-pyramids/objects/",
    "_aap_managed/media-cache/video-derivatives/objects/",
)
_NATIVE_OUTPUT_OWNER_TAG = "aap-fixture-native-owner"
_NATIVE_OUTPUT_IAM_COMMANDS = {
    "policy_attach": ("policy", "attach"),
    "user_info": ("user", "info"),
    "policy_info": ("policy", "info"),
}
_NATIVE_OUTPUT_IAM_PROVIDER_FAILURES = {
    "AccessDenied": "access_denied",
    "InvalidAccessKeyId": "invalid_access_key",
    "SignatureDoesNotMatch": "signature_mismatch",
    "XMinioAdminNoSuchPolicy": "no_such_policy",
    "XMinioAdminNoSuchUser": "no_such_user",
    "XMinioIAMNotInitialized": "iam_not_initialized",
    "XMinioServerNotInitialized": "server_not_initialized",
}
_NATIVE_OUTPUT_IAM_OUTPUT_LIMIT = 1024 * 1024


def _native_output_policy(bucket, *, role):
    require(role in {"writer", "reader", "cleanup"}, "fixture_native_role_invalid")
    objects = [f"arn:aws:s3:::{bucket}/{prefix}*" for prefix in _NATIVE_OUTPUT_PREFIXES]
    statements = [
        {
            "Effect": "Allow",
            "Action": (
                ["s3:GetObjectVersion", "s3:DeleteObjectVersion"]
                if role == "cleanup"
                else ["s3:GetObject", "s3:GetObjectVersion"]
            ),
            "Resource": objects,
        },
        {
            "Effect": "Deny",
            "Action": ["s3:GetObject", "s3:GetObjectVersion"],
            "Resource": objects,
            "Condition": {"StringEquals": {"s3:authType": "REST-QUERY-STRING"}},
        },
    ]
    if role == "writer":
        statements.extend(
            [
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": [f"arn:aws:s3:::{bucket}"],
                    "Condition": {
                        "StringLike": {
                            "s3:prefix": [
                                f"{prefix}*" for prefix in _NATIVE_OUTPUT_PREFIXES
                            ]
                        }
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:PutObject",
                        "s3:ListMultipartUploadParts",
                        "s3:AbortMultipartUpload",
                    ],
                    "Resource": objects,
                },
            ]
        )
    elif role == "cleanup":
        statements.extend(
            [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetBucketVersioning"],
                    "Resource": [f"arn:aws:s3:::{bucket}"],
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucketVersions"],
                    "Resource": [f"arn:aws:s3:::{bucket}"],
                    "Condition": {
                        "StringLike": {
                            "s3:prefix": [
                                f"{prefix}*" for prefix in _NATIVE_OUTPUT_PREFIXES
                            ]
                        }
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:DeleteObject"],
                    "Resource": objects,
                    "Condition": {
                        "StringLike": {"s3:versionid": "*?*"},
                        "StringNotEquals": {"s3:versionid": "null"},
                    },
                },
            ]
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _canonical_native_output_policy(value):
    require(
        isinstance(value, dict)
        and set(value) == {"Version", "Statement"}
        and value["Version"] == "2012-10-17"
        and isinstance(value["Statement"], list),
        "fixture_native_policy_invalid",
    )
    statements = []
    for statement in value["Statement"]:
        require(
            isinstance(statement, dict)
            and set(statement)
            in (
                {"Effect", "Action", "Resource"},
                {"Effect", "Action", "Resource", "Condition"},
            )
            and statement["Effect"] in {"Allow", "Deny"},
            "fixture_native_policy_invalid",
        )
        normalized = {"Effect": statement["Effect"]}
        for field in ("Action", "Resource"):
            entries = statement[field]
            entries = [entries] if isinstance(entries, str) else entries
            require(
                isinstance(entries, list)
                and all(isinstance(entry, str) and entry for entry in entries),
                "fixture_native_policy_invalid",
            )
            normalized[field] = sorted(entries)
        if "Condition" in statement:
            condition = statement["Condition"]
            require(isinstance(condition, dict), "fixture_native_policy_invalid")
            normalized["Condition"] = {}
            for operator, fields in condition.items():
                require(
                    isinstance(operator, str) and isinstance(fields, dict),
                    "fixture_native_policy_invalid",
                )
                normalized["Condition"][operator] = {}
                for key, entries in fields.items():
                    entries = [entries] if isinstance(entries, str) else entries
                    require(
                        isinstance(key, str)
                        and isinstance(entries, list)
                        and all(isinstance(entry, str) and entry for entry in entries),
                        "fixture_native_policy_invalid",
                    )
                    normalized["Condition"][operator][key] = sorted(entries)
        statements.append(normalized)
    statements.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return {"Version": value["Version"], "Statement": statements}


def _native_output_paths(owner, run_id):
    require(isinstance(run_id, uuid.UUID), "fixture_native_run_invalid")
    info = owner.private.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o700,
        "fixture_native_private_directory",
    )
    stem = "native-output-" + run_id.hex
    return {
        "seed": owner.private / (stem + "-credentials.json"),
        "writer_policy": owner.private / (stem + "-writer-policy.json"),
        "reader_policy": owner.private / (stem + "-reader-policy.json"),
        "cleanup_policy": owner.private / (stem + "-cleanup-policy.json"),
        "receipt": owner.private / (stem + "-receipt.json"),
        "writer_file": owner.private / (stem + "-writer.json"),
        "reader_file": owner.private / (stem + "-reader.json"),
        "cleanup_file": owner.private / (stem + "-cleanup.json"),
    }


def _native_output_iam(mc_env, operation, *args):
    argv = [
        "mc",
        "--json",
        "admin",
        *_NATIVE_OUTPUT_IAM_COMMANDS[operation],
        "fixture",
        *args,
    ]
    failure = f"fixture_native_iam_{operation}"
    try:
        result = subprocess.run(argv, env=mc_env, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise ContractError(f"{failure}_timeout") from None
    except OSError:
        raise ContractError(f"{failure}_launch_failed") from None
    if result.returncode < 0:
        raise ContractError(f"{failure}_signal_{-result.returncode}")

    bounded = len(result.stdout) <= _NATIVE_OUTPUT_IAM_OUTPUT_LIMIT
    if result.returncode == 0:
        require(bounded, f"{failure}_output_budget")
        if operation == "policy_attach":
            return None

    payload = None
    if bounded and result.returncode in (0, 1):
        try:
            payload = json.loads(result.stdout)
        except (ValueError, RecursionError):
            pass
    if result.returncode:
        error = (
            payload.get("error")
            if isinstance(payload, dict) and payload.get("status") == "error"
            else None
        )
        cause = (
            error.get("cause")
            if isinstance(error, dict) and error.get("type") == "fatal"
            else None
        )
        detail = cause.get("error") if isinstance(cause, dict) else None
        code = detail.get("Code") if isinstance(detail, dict) else None
        reason = (
            _NATIVE_OUTPUT_IAM_PROVIDER_FAILURES.get(code)
            if isinstance(code, str)
            else None
        )
        raise ContractError(f"{failure}_{reason or 'nonzero'}")
    require(isinstance(payload, dict), f"{failure}_response_invalid")
    return payload


def _native_output_accounts(owner, store, entry, paths):
    accounts = json.loads(_private_bytes(paths["seed"]))
    require(
        set(accounts) == {"writer", "reader", "cleanup"}
        and all(
            isinstance(account, dict)
            and set(account) == {"access_key", "secret_key"}
            and all(isinstance(value, str) and value for value in account.values())
            for account in accounts.values()
        ),
        "fixture_native_credentials_invalid",
    )
    root_access, root_secret = _root_credentials(owner)
    original = credentials(owner, store)
    original_reader = reader_credentials(owner, store)
    require(
        len(
            {
                root_access,
                original["access_key"],
                original_reader["access_key"],
                *(
                    accounts[role]["access_key"]
                    for role in ("writer", "reader", "cleanup")
                ),
            }
        )
        == 6
        and len(
            {
                root_secret,
                original["secret_key"],
                original_reader["secret_key"],
                *(
                    accounts[role]["secret_key"]
                    for role in ("writer", "reader", "cleanup")
                ),
            }
        )
        == 6,
        "fixture_native_accounts_not_distinct",
    )
    mc_env = {
        "PATH": os.environ["PATH"],
        "MC_CONFIG_DIR": str(owner.private / "mc"),
        "MC_HOST_fixture": (
            "http://"
            + urllib.parse.quote(root_access, safe="")
            + ":"
            + urllib.parse.quote(root_secret, safe="")
            + "@"
            + urllib.parse.urlsplit(store["admin_endpoint"]).netloc
        ),
    }
    evidence = {}
    for role in ("writer", "reader", "cleanup"):
        account = accounts[role]
        expected = _canonical_native_output_policy(
            _native_output_policy(entry["bucket"], role=role)
        )
        policy = _canonical_native_output_policy(
            json.loads(_private_bytes(paths[role + "_policy"]))
        )
        name = entry[role + "_policy"]
        info = _native_output_iam(mc_env, "user_info", account["access_key"])
        observed = _native_output_iam(mc_env, "policy_info", name)
        require(
            policy == expected
            and info.get("status") == "success"
            and info.get("accessKey") == account["access_key"]
            and info.get("userStatus") == "enabled"
            and info.get("policyName") == name
            and not info.get("memberOf")
            and observed.get("status") == "success"
            and _canonical_native_output_policy(observed["policyInfo"]["Policy"])
            == expected,
            "fixture_native_account_policy_changed",
        )
        evidence[role] = {
            "identity_sha256": hashlib.sha256(
                account["access_key"].encode()
            ).hexdigest(),
            "policy_name": name,
            "policy_sha256": hashlib.sha256(
                json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    return accounts, evidence


def _native_output_bucket(owner, store, entry, *, require_empty=False):
    root_access, root_secret = _root_credentials(owner)
    with closing(_client(store["admin_endpoint"], root_access, root_secret)) as root:
        head = root.head_bucket(Bucket=entry["bucket"])
        require(
            head.get("ResponseMetadata", {}).get("HTTPStatusCode") == 200
            and root.get_bucket_versioning(Bucket=entry["bucket"]).get("Status")
            == "Enabled"
            and root.get_bucket_tagging(Bucket=entry["bucket"]).get("TagSet")
            == [{"Key": _NATIVE_OUTPUT_OWNER_TAG, "Value": entry["bucket_owner"]}],
            "fixture_native_bucket_changed",
        )
        if require_empty:
            _empty_inventory(root, entry["bucket"])


def _native_output_listings(store, entry, accounts, *, require_empty=False):
    for role in ("writer", "cleanup"):
        account = accounts[role]
        with closing(
            _client(store["endpoint"], account["access_key"], account["secret_key"])
        ) as client:
            if role == "cleanup":
                require(
                    client.get_bucket_versioning(Bucket=entry["bucket"]).get("Status")
                    == "Enabled",
                    "fixture_native_cleanup_versioning_unproven",
                )
            for prefix in _NATIVE_OUTPUT_PREFIXES:
                if role == "writer":
                    listing = client.list_objects_v2(
                        Bucket=entry["bucket"], Prefix=prefix, MaxKeys=1
                    )
                else:
                    listing = client.list_object_versions(
                        Bucket=entry["bucket"], Prefix=prefix, MaxKeys=1
                    )
                require(
                    listing.get("ResponseMetadata", {}).get("HTTPStatusCode") == 200
                    and (
                        not require_empty
                        or (
                            not listing.get("Contents")
                            and not listing.get("Versions")
                            and not listing.get("DeleteMarkers")
                            and not listing.get("IsTruncated")
                        )
                    ),
                    "fixture_native_prefix_listing_unproven",
                )


def _native_output_provider(owner, store):
    return {
        "owner_nonce": owner.data["nonce"],
        "container_id": store["container_id"],
        "network_id": store["network_id"],
        "volume_id": store["volume_id"],
    }


def verified_native_output(owner: Owner, database_identity: dict, run_id: uuid.UUID):
    """Reverify a completed target; the caller must hold the fixture store lock."""
    require(
        owner.data.get("cleaned") is False and "fixture_teardown" not in owner.data,
        "fixture_native_owner_unavailable",
    )
    store, _ = verified_store(owner, database_identity)
    paths = _native_output_paths(owner, run_id)
    entry = owner.data.get("fixture_native_outputs", {}).get(run_id.hex)
    require(
        isinstance(entry, dict)
        and entry.get("state") == "ready"
        and entry.get("run_id") == str(run_id)
        and entry.get("bucket") == "fixture-media-" + run_id.hex
        and entry.get("endpoint") == store["endpoint"]
        and entry.get("initial_bucket_absent") is True
        and entry.get("bucket_owner") == owner.data["nonce"] + ":" + run_id.hex
        and entry.get("writer_policy") == "fixture-media-w-" + run_id.hex
        and entry.get("reader_policy") == "fixture-media-r-" + run_id.hex
        and entry.get("cleanup_policy") == "fixture-media-c-" + run_id.hex,
        "fixture_native_target_unavailable",
    )
    identity = hashlib.sha256(
        json.dumps([store["endpoint"].rstrip("/"), entry["bucket"]]).encode()
    ).hexdigest()
    require(
        entry.get("storage_identity") == identity, "fixture_native_identity_changed"
    )
    accounts, evidence = _native_output_accounts(owner, store, entry, paths)
    _native_output_bucket(owner, store, entry)
    _native_output_listings(store, entry, accounts)
    receipt = _private_bytes(paths["receipt"])
    digest = hashlib.sha256(receipt).hexdigest()
    require(digest == entry.get("receipt_sha256"), "fixture_native_receipt_changed")
    require(
        json.loads(receipt)
        == {
            "schema": _NATIVE_OUTPUT_SCHEMA,
            "run_id": str(run_id),
            "endpoint": store["endpoint"],
            "bucket": entry["bucket"],
            "storage_identity": identity,
            "bucket_owner": entry["bucket_owner"],
            "versioning": "Enabled",
            "accounts": evidence,
            "provider": _native_output_provider(owner, store),
            "initial_bucket_absent": True,
            "initial_inventory_empty": True,
        },
        "fixture_native_receipt_identity_changed",
    )
    public = {
        "run_id": str(run_id),
        "endpoint": store["endpoint"],
        "bucket": entry["bucket"],
        "storage_identity": identity,
        "receipt_sha256": digest,
    }
    for role in ("writer", "reader", "cleanup"):
        value = json.loads(_private_bytes(paths[role + "_file"]))
        require(
            value == {"schema": _NATIVE_OUTPUT_SCHEMA, **public, **accounts[role]},
            "fixture_native_target_file_changed",
        )
    return {
        **public,
        "writer_file": str(paths["writer_file"]),
        "reader_file": str(paths["reader_file"]),
        "cleanup_file": str(paths["cleanup_file"]),
    }


def verified_native_output_birth(
    owner: Owner, database_identity: dict, run_id: uuid.UUID
) -> str:
    """Return exact private birth bytes after verification under fixture-store.lock."""
    verified_native_output(owner, database_identity, run_id)
    return _private_bytes(_native_output_paths(owner, run_id)["receipt"]).decode(
        "utf-8"
    )


def provision_native_output(owner: Owner, database_identity: dict, run_id: uuid.UUID):
    """Create a fresh per-run target under the caller-held fixture store lock."""
    require(
        owner.data.get("cleaned") is False and "fixture_teardown" not in owner.data,
        "fixture_native_owner_unavailable",
    )
    store, original = verified_store(owner, database_identity)
    paths = _native_output_paths(owner, run_id)
    targets = owner.data.setdefault("fixture_native_outputs", {})
    require(
        isinstance(targets, dict) and run_id.hex not in targets,
        "fixture_native_birth_already_recorded",
    )
    require(
        all(
            isinstance(item, dict) and item.get("state") in {"ready", "retired"}
            for item in targets.values()
        ),
        "fixture_native_birth_unsettled",
    )
    require(
        not any(path.exists() or path.is_symlink() for path in paths.values()),
        "fixture_native_birth_files_exist",
    )
    root_access, root_secret = _root_credentials(owner)
    original_reader = reader_credentials(owner, store)
    accounts = {
        role: {
            "access_key": "fixture" + secrets.token_hex(12),
            "secret_key": secrets.token_urlsafe(32),
        }
        for role in ("writer", "reader", "cleanup")
    }
    require(
        len(
            {
                root_access,
                original["access_key"],
                original_reader["access_key"],
                *(
                    accounts[role]["access_key"]
                    for role in ("writer", "reader", "cleanup")
                ),
            }
        )
        == 6
        and len(
            {
                root_secret,
                original["secret_key"],
                original_reader["secret_key"],
                *(
                    accounts[role]["secret_key"]
                    for role in ("writer", "reader", "cleanup")
                ),
            }
        )
        == 6,
        "fixture_native_accounts_not_distinct",
    )
    bucket = "fixture-media-" + run_id.hex
    entry = {
        "state": "creating",
        "run_id": str(run_id),
        "endpoint": store["endpoint"],
        "bucket": bucket,
        "bucket_owner": owner.data["nonce"] + ":" + run_id.hex,
        "storage_identity": hashlib.sha256(
            json.dumps([store["endpoint"].rstrip("/"), bucket]).encode()
        ).hexdigest(),
        "writer_policy": "fixture-media-w-" + run_id.hex,
        "reader_policy": "fixture-media-r-" + run_id.hex,
        "cleanup_policy": "fixture-media-c-" + run_id.hex,
    }
    targets[run_id.hex] = entry
    owner.save()
    private_write(paths["seed"], json.dumps(accounts).encode())
    for role in ("writer", "reader", "cleanup"):
        private_write(
            paths[role + "_policy"],
            json.dumps(_native_output_policy(bucket, role=role)).encode(),
        )
    mc_env = {
        "PATH": os.environ["PATH"],
        "MC_CONFIG_DIR": str(owner.private / "mc"),
        "MC_HOST_fixture": (
            "http://"
            + urllib.parse.quote(root_access, safe="")
            + ":"
            + urllib.parse.quote(root_secret, safe="")
            + "@"
            + urllib.parse.urlsplit(store["admin_endpoint"]).netloc
        ),
    }
    with closing(_client(store["admin_endpoint"], root_access, root_secret)) as root:
        require(
            bucket
            not in {item["Name"] for item in root.list_buckets().get("Buckets", [])},
            "fixture_native_bucket_already_exists",
        )
        entry["initial_bucket_absent"] = True
        owner.save()
        root.create_bucket(Bucket=bucket)
        root.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        root.put_bucket_tagging(
            Bucket=bucket,
            Tagging={
                "TagSet": [
                    {"Key": _NATIVE_OUTPUT_OWNER_TAG, "Value": entry["bucket_owner"]}
                ]
            },
        )
    _native_output_bucket(owner, store, entry, require_empty=True)
    for role in ("writer", "reader", "cleanup"):
        account = accounts[role]
        command(
            ["mc", "admin", "user", "add", "fixture"],
            input_data=f"{account['access_key']}\n{account['secret_key']}\n".encode(),
            env=mc_env,
        )
        command(
            [
                "mc",
                "admin",
                "policy",
                "create",
                "fixture",
                entry[role + "_policy"],
                str(paths[role + "_policy"]),
            ],
            env=mc_env,
        )
        _native_output_iam(
            mc_env,
            "policy_attach",
            entry[role + "_policy"],
            "--user",
            account["access_key"],
        )
    accounts, evidence = _native_output_accounts(owner, store, entry, paths)
    _native_output_bucket(owner, store, entry, require_empty=True)
    _native_output_listings(store, entry, accounts, require_empty=True)
    receipt = json.dumps(
        {
            "schema": _NATIVE_OUTPUT_SCHEMA,
            "run_id": str(run_id),
            "endpoint": store["endpoint"],
            "bucket": bucket,
            "storage_identity": entry["storage_identity"],
            "bucket_owner": entry["bucket_owner"],
            "versioning": "Enabled",
            "accounts": evidence,
            "provider": _native_output_provider(owner, store),
            "initial_bucket_absent": True,
            "initial_inventory_empty": True,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    private_write(paths["receipt"], receipt)
    entry["receipt_sha256"] = hashlib.sha256(receipt).hexdigest()
    public = {
        "run_id": str(run_id),
        "endpoint": store["endpoint"],
        "bucket": bucket,
        "storage_identity": entry["storage_identity"],
        "receipt_sha256": entry["receipt_sha256"],
    }
    for role in ("writer", "reader", "cleanup"):
        private_write(
            paths[role + "_file"],
            json.dumps(
                {"schema": _NATIVE_OUTPUT_SCHEMA, **public, **accounts[role]}
            ).encode(),
        )
    entry["state"] = "ready"
    owner.save()
    return verified_native_output(owner, database_identity, run_id)


def _native_retirement_receipt(entry, run_id, receipt):
    require(
        isinstance(receipt, dict)
        and set(receipt)
        == {
            "schema",
            "run_id",
            "storage_identity",
            "target_receipt_sha256",
            "retirement_id",
            "location_id",
            "binding_generation",
        }
        and receipt["schema"] == "aap-fixture-native-target-retirement/v1"
        and receipt["run_id"] == str(run_id)
        and receipt["storage_identity"] == entry["storage_identity"]
        and receipt["target_receipt_sha256"] == entry["receipt_sha256"]
        and type(receipt["binding_generation"]) is int
        and receipt["binding_generation"] > 0,
        "fixture_native_retirement_receipt_invalid",
    )
    try:
        canonical = all(
            str(uuid.UUID(receipt[key])) == receipt[key]
            for key in ("retirement_id", "location_id")
        )
    except (ValueError, TypeError, AttributeError):
        canonical = False
    require(canonical, "fixture_native_retirement_receipt_invalid")


def _native_iam_info(mc_env, kind, name):
    result = subprocess.run(
        ["mc", "--json", "admin", kind, "info", "fixture", name],
        env=mc_env,
        capture_output=True,
        timeout=30,
    )
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise ContractError("fixture_native_iam_response_invalid") from None
    require(isinstance(value, dict), "fixture_native_iam_response_invalid")
    if result.returncode == 0 and value.get("status") == "success":
        return value
    error = value.get("error")
    cause = error.get("cause") if isinstance(error, dict) else None
    detail = cause.get("error") if isinstance(cause, dict) else None
    require(
        result.returncode == 1
        and value.get("status") == "error"
        and isinstance(detail, dict)
        and detail.get("Code")
        == ("XMinioAdminNoSuchUser" if kind == "user" else "XMinioAdminNoSuchPolicy"),
        "fixture_native_iam_absence_unproven",
    )
    return None


def _native_physical_retirement_receipt(owner, run_id, proof):
    entry = owner.data["fixture_native_outputs"][run_id.hex]
    retirement = entry["retirement"]
    require(
        retirement["bucket"] == "absent"
        and retirement["iam"]
        == {
            role + ":" + kind: "absent"
            for role in ("writer", "reader", "cleanup")
            for kind in ("user", "policy")
        },
        "fixture_native_retirement_required",
    )
    raw = _private_bytes(_native_output_paths(owner, run_id)["receipt"])
    require(
        hashlib.sha256(raw).hexdigest() == entry["receipt_sha256"],
        "fixture_native_receipt_changed",
    )
    birth = json.loads(raw)
    require(
        birth["provider"] == _native_output_provider(owner, owner.data["fixture_store"])
        and birth["run_id"] == str(run_id)
        and birth["storage_identity"] == entry["storage_identity"]
        and birth["bucket"] == entry["bucket"],
        "fixture_native_receipt_identity_changed",
    )
    return {
        "schema": "aap-fixture-native-target-physical-retirement/v1",
        "native_receipt": dict(proof),
        "provider": birth["provider"],
        "bucket": birth["bucket"],
        "accounts": birth["accounts"],
        "bucket_absent": True,
        "iam_absent": dict.fromkeys(retirement["iam"], True),
    }


def _retire_native_output_physical(owner, database_identity, run_id, proof):
    store, _ = verified_store(owner, database_identity)
    paths = _native_output_paths(owner, run_id)
    entry = owner.data["fixture_native_outputs"][run_id.hex]
    _native_retirement_receipt(entry, run_id, proof)
    if entry["state"] == "ready":
        verified_native_output(owner, database_identity, run_id)
        _native_output_bucket(owner, store, entry, require_empty=True)
        entry["retirement"] = {
            "schema": "aap-fixture-native-target-physical-retirement/v1",
            "native_receipt": dict(proof),
            "files": {
                key: hashlib.sha256(_private_bytes(path)).hexdigest()
                for key, path in paths.items()
            },
            "iam": {
                role + ":" + kind: "present"
                for role in ("writer", "reader", "cleanup")
                for kind in ("user", "policy")
            },
            "bucket": "present",
        }
        entry["state"] = "retiring"
        owner.save()
    require(
        entry["state"] in {"retiring", "retired"}, "fixture_native_retirement_state"
    )
    retirement = entry["retirement"]
    require(
        retirement["schema"] == "aap-fixture-native-target-physical-retirement/v1"
        and retirement["native_receipt"] == proof
        and retirement["files"]
        == {
            key: hashlib.sha256(_private_bytes(path)).hexdigest()
            for key, path in paths.items()
        },
        "fixture_native_retirement_evidence_changed",
    )
    accounts = json.loads(_private_bytes(paths["seed"]))
    root_access, root_secret = _root_credentials(owner)
    mc_env = {
        "PATH": os.environ["PATH"],
        "MC_CONFIG_DIR": str(owner.private / "mc"),
        "MC_HOST_fixture": (
            "http://"
            + urllib.parse.quote(root_access, safe="")
            + ":"
            + urllib.parse.quote(root_secret, safe="")
            + "@"
            + urllib.parse.urlsplit(store["admin_endpoint"]).netloc
        ),
    }
    for role in ("writer", "reader", "cleanup"):
        policy_name = entry[role + "_policy"]
        for kind, name in (
            ("user", accounts[role]["access_key"]),
            ("policy", policy_name),
        ):
            key = role + ":" + kind
            state = retirement["iam"][key]
            require(
                state in {"present", "removing", "absent"}, "fixture_native_iam_state"
            )
            observed = _native_iam_info(mc_env, kind, name)
            if observed is None:
                require(state != "present", "fixture_native_iam_disappeared")
            else:
                require(state != "absent", "fixture_native_iam_reappeared")
                if kind == "user":
                    require(
                        observed.get("accessKey") == name
                        and observed.get("userStatus") == "enabled"
                        and observed.get("policyName") == policy_name
                        and not observed.get("memberOf"),
                        "fixture_native_account_policy_changed",
                    )
                else:
                    require(
                        _canonical_native_output_policy(
                            observed["policyInfo"]["Policy"]
                        )
                        == _canonical_native_output_policy(
                            json.loads(_private_bytes(paths[role + "_policy"]))
                        ),
                        "fixture_native_account_policy_changed",
                    )
                retirement["iam"][key] = "removing"
                owner.save()
                command(
                    ["mc", "--json", "admin", kind, "remove", "fixture", name],
                    env=mc_env,
                )
                require(
                    _native_iam_info(mc_env, kind, name) is None,
                    "fixture_native_iam_removal_unproven",
                )
            retirement["iam"][key] = "absent"
            owner.save()
    with closing(_client(store["admin_endpoint"], root_access, root_secret)) as root:
        buckets = {item["Name"] for item in root.list_buckets().get("Buckets", [])}
        require(store["bucket"] in buckets, "fixture_original_bucket_disappeared")
        require(
            retirement["bucket"] in {"present", "removing", "absent"},
            "fixture_native_bucket_retirement_state",
        )
        if entry["bucket"] in buckets:
            require(
                retirement["bucket"] != "absent", "fixture_native_bucket_reappeared"
            )
            _native_output_bucket(owner, store, entry, require_empty=True)
            retirement["bucket"] = "removing"
            owner.save()
            result = root.delete_bucket(Bucket=entry["bucket"])
            require(
                result.get("ResponseMetadata", {}).get("HTTPStatusCode") == 204,
                "fixture_native_bucket_removal_unproven",
            )
            require(
                entry["bucket"]
                not in {
                    item["Name"] for item in root.list_buckets().get("Buckets", [])
                },
                "fixture_native_bucket_absence_unproven",
            )
        else:
            require(
                retirement["bucket"] != "present", "fixture_native_bucket_disappeared"
            )
    retirement["bucket"] = "absent"
    physical_receipt = _native_physical_retirement_receipt(owner, run_id, proof)
    physical_sha256 = hashlib.sha256(
        json.dumps(physical_receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if "physical_receipt" in retirement:
        require(
            retirement["physical_receipt"] == physical_receipt
            and retirement.get("physical_receipt_sha256") == physical_sha256,
            "fixture_native_physical_receipt_changed",
        )
    else:
        require(entry["state"] == "retiring", "fixture_native_physical_receipt_missing")
        retirement["physical_receipt"] = physical_receipt
        retirement["physical_receipt_sha256"] = physical_sha256
    entry["state"] = "retired"
    owner.save()
    return dict(proof)


async def retire_native_output(
    owner, database_identity, run_id: uuid.UUID, *, control_url
):
    """Retire a Native-proven target while the caller holds fixture-store.lock."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from fixture_suite_coordinator import _settled_thread

    require(
        isinstance(run_id, uuid.UUID)
        and owner.data.get("cleaned") is False
        and "fixture_teardown" not in owner.data
        and owner.data["fixture_store"]["database"] == database_identity,
        "fixture_native_owner_unavailable",
    )
    entry = owner.data.get("fixture_native_outputs", {}).get(run_id.hex)
    require(
        isinstance(entry, dict)
        and entry.get("state") in {"ready", "retiring", "retired"}
        and entry.get("run_id") == str(run_id),
        "fixture_native_target_unavailable",
    )
    url = make_url(control_url)
    require(
        url.drivername == "postgresql+asyncpg"
        and url.host == database_identity["postgres_host"]
        and (url.port or 5432) == database_identity["postgres_port"]
        and url.database == database_identity["database"]
        and url.username == "aap_fixture_media_" + run_id.hex
        and not url.query,
        "fixture_native_control_database_changed",
    )
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT d.oid, d.datname, pg_catalog.pg_get_userbyid(d.datdba), "
                        "pg_catalog.shobj_description(d.oid,'pg_database'), "
                        "current_user, session_user, r.rolcanlogin, r.rolsuper, r.rolinherit, "
                        "r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolbypassrls, "
                        "EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members m "
                        "WHERE m.member=r.oid OR m.roleid=r.oid), "
                        "pg_catalog.shobj_description(r.oid,'pg_authid') "
                        "FROM pg_catalog.pg_database d, pg_catalog.pg_roles r "
                        "WHERE d.datname=current_database() AND r.rolname=session_user"
                    )
                )
            ).one()
            require(
                row[0] == database_identity["database_oid"]
                and row[1] == database_identity["database"]
                and row[2] == database_identity["database_owner"]
                and row[3] == database_identity["ownership"]
                and row[4] == row[5] == url.username
                and row[6] is True
                and not any(row[7:14])
                and row[14]
                == database_identity["ownership"] + ":fixture-media:" + str(run_id),
                "fixture_native_control_identity_changed",
            )
            proof = (
                await connection.execute(
                    text(
                        "SELECT yoao_assets_v1.retire_fixture_native_target(:run,:identity,:receipt)"
                    ),
                    {
                        "run": run_id,
                        "identity": entry["storage_identity"],
                        "receipt": entry["receipt_sha256"],
                    },
                )
            ).scalar_one()
            _native_retirement_receipt(entry, run_id, proof)
    finally:
        await engine.dispose()
    return await _settled_thread(
        _retire_native_output_physical, owner, database_identity, run_id, proof
    )


def probe_reader(owner, store):
    """Exercise the actual reader identity and settle the exact probe version."""
    writer = credentials(owner, store)
    reader = reader_credentials(owner, store)
    payload = secrets.token_bytes(32)
    key = "_aap_managed/datasets/.fixture-reader-" + uuid.uuid4().hex
    owner.data["fixture_reader_probe"] = {
        "key": key,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "state": "writing",
    }
    owner.save()
    with closing(
        _client(store["endpoint"], writer["access_key"], writer["secret_key"])
    ) as source:
        response = source.put_object(Bucket=store["bucket"], Key=key, Body=payload)
        version = response.get("VersionId")
        require(
            isinstance(version, str) and version not in {"", "null"},
            "fixture_probe_version_missing",
        )
        owner.data["fixture_reader_probe"]["version_id"] = version
        owner.data["fixture_reader_probe"]["state"] = "reading"
        owner.save()
        with closing(
            _client(store["endpoint"], reader["access_key"], reader["secret_key"])
        ) as client:
            params = {"Bucket": store["bucket"], "Key": key, "VersionId": version}
            metadata = client.head_object(**params)
            require(
                metadata.get("ContentLength") == len(payload),
                "fixture_reader_head_failed",
            )
            response = client.get_object(**params)
            with closing(response["Body"]) as body:
                require(body.read() == payload, "fixture_reader_get_failed")
            response = client.get_object(**params, Range="bytes=2-5")
            with closing(response["Body"]) as body:
                require(
                    response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 206
                    and response.get("ContentRange") == f"bytes 2-5/{len(payload)}"
                    and body.read() == payload[2:6],
                    "fixture_reader_range_failed",
                )
            for operation, values in (
                (
                    client.put_object,
                    {"Bucket": store["bucket"], "Key": key + "-denied", "Body": b"x"},
                ),
                (client.delete_object, {"Bucket": store["bucket"], "Key": key}),
                (client.delete_object, params),
            ):
                try:
                    operation(**values)
                except ClientError as exc:
                    require(
                        exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                        == 403,
                        "fixture_reader_write_denial_unproven",
                    )
                else:
                    raise ValueError("fixture_reader_write_allowed")
            response = client.get_object(**params)
            with closing(response["Body"]) as body:
                require(body.read() == payload, "fixture_reader_probe_changed")
        mc_env = {
            "PATH": os.environ["PATH"],
            "MC_CONFIG_DIR": str(owner.private / "mc-reader"),
            "MC_HOST_fixture": (
                "http://"
                + urllib.parse.quote(reader["access_key"], safe="")
                + ":"
                + urllib.parse.quote(reader["secret_key"], safe="")
                + "@"
                + urllib.parse.urlsplit(store["admin_endpoint"]).netloc
            ),
        }
        result = subprocess.run(
            ["mc", "--json", "admin", "info", "fixture"],
            env=mc_env,
            capture_output=True,
            timeout=30,
        )
        # mc admin info reports this denial in JSON even when its exit code is zero.
        try:
            denial = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            denial = None
        require(
            isinstance(denial, dict)
            and denial.get("status") == "error"
            and isinstance(denial.get("error"), str)
            and denial["error"].lower().replace(" ", "").rstrip(".") == "accessdenied",
            "fixture_reader_admin_denial_unproven",
        )
        deleted = source.delete_object(
            Bucket=store["bucket"], Key=key, VersionId=version
        )
        require(
            deleted.get("ResponseMetadata", {}).get("HTTPStatusCode") in {200, 204},
            "fixture_reader_probe_cleanup_failed",
        )
        _empty_inventory(source, store["bucket"])
        owner.data["fixture_reader_probe"]["state"] = "settled"
        owner.save()
    return {
        "version_id": version,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "head": "passed",
        "get": "passed",
        "range": "passed",
        "put_denied": True,
        "delete_denied": True,
        "version_delete_denied": True,
        "admin_denied": True,
    }


def observe_empty(owner: Owner, database_identity: dict, *, probe):
    """Probe the production writer, then durably record the pre-publication view."""
    store = owner.data["fixture_store"]
    require(store["database"] == database_identity, "fixture_database_changed")
    require(store["state"] == "provisioned_unpublished", "fixture_store_state")
    verify_accounts(owner, store)
    account = credentials(owner, store)
    phase = owner.data["fixture_intent"].get("writer_probe")
    if phase is None:
        owner.data["fixture_intent"]["writer_probe"] = "running"
        owner.save()
        probe(store["endpoint"], store["bucket"], account)
        owner.data["fixture_intent"]["writer_probe"] = "settled"
        owner.save()
    else:
        require(phase == "settled", "fixture_writer_probe_unsettled")
    saved_probe = owner.data.get("fixture_reader_probe")
    if saved_probe is None:
        reader_probe = probe_reader(owner, store)
    else:
        require(saved_probe["state"] == "settled", "fixture_reader_probe_unsettled")
        reader_probe = {
            "version_id": saved_probe["version_id"],
            "sha256": saved_probe["sha256"],
            "head": "passed",
            "get": "passed",
            "range": "passed",
            "put_denied": True,
            "delete_denied": True,
            "version_delete_denied": True,
            "admin_denied": True,
        }
    with closing(
        _client(store["endpoint"], account["access_key"], account["secret_key"])
    ) as limited:
        require(
            limited.get_bucket_versioning(Bucket=store["bucket"]).get("Status")
            == "Enabled",
            "fixture_versioning_unproven",
        )
        _empty_inventory(limited, store["bucket"])
    observed_container, observed_network = _physical(owner, store)
    observation = {
        "database": database_identity,
        "accounts": account_fingerprints(owner, store),
        "initial_bucket_count": owner.data["fixture_intent"]["initial_bucket_count"],
        "writer_canary_denied": owner.data["fixture_intent"]["writer_canary_denied"],
        "provisioning_run_id": owner.data["fixture_intent"]["provisioning_run_id"],
        "endpoint": store["endpoint"],
        "bucket": store["bucket"],
        "container_id": store["container_id"],
        "network_id": store["network_id"],
        "network_name": store["network_name"],
        "network_ipam": observed_network["IPAM"],
        "bridge_address": store["bridge_address"],
        "admin_endpoint": store["admin_endpoint"],
        "volume_id": store["volume_id"],
        "image": MINIO_IMAGE,
        "image_id": observed_container["Image"],
        "required_publication_order": [
            "fresh_owned_volume",
            "bucket_versioned",
            "scoped_credentials_created",
            "writer_probe_settled",
            "reader_probe_settled",
            "empty_inventory_observed",
            "root_and_binding_transaction",
            "ordinary_access",
        ],
        "versioning": "Enabled",
        "objects": [],
        "versions": [],
        "multipart_uploads": [],
        "writer_probe": "passed",
        "reader_probe": reader_probe,
    }
    receipt = json.dumps(observation, sort_keys=True, separators=(",", ":")).encode()
    private_write(owner.private / "original-store-receipt.json", receipt)
    store["receipt_sha256"] = hashlib.sha256(receipt).hexdigest()
    store["state"] = "observed_unpublished"
    owner.save()
    return store["receipt_sha256"]


def _root_credentials(owner):
    values = {}
    for line in (
        _private_bytes(owner.private / "minio.env").decode("ascii").splitlines()
    ):
        key, separator, value = line.partition("=")
        require(
            separator and key not in values and value,
            "fixture_root_credentials_invalid",
        )
        values[key] = value
    require(
        set(values) == {"MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"},
        "fixture_root_credentials_invalid",
    )
    return values["MINIO_ROOT_USER"], values["MINIO_ROOT_PASSWORD"]


def verify_empty_for_destroy(owner, database_identity):
    """Prove the exact owned bucket is settled and contains no remaining versions."""
    targets = owner.data.get("fixture_native_outputs", {})
    require(isinstance(targets, dict), "fixture_native_inventory_invalid")
    for run_hex, entry in targets.items():
        try:
            run_id = uuid.UUID(hex=run_hex)
        except (ValueError, TypeError, AttributeError):
            raise ContractError("fixture_native_inventory_invalid") from None
        require(
            run_id.hex == run_hex
            and isinstance(entry, dict)
            and entry.get("run_id") == str(run_id)
            and entry.get("state") == "retired",
            "fixture_native_retirement_required",
        )
        retirement = entry.get("retirement")
        require(
            isinstance(retirement, dict)
            and retirement.get("schema")
            == "aap-fixture-native-target-physical-retirement/v1"
            and retirement.get("bucket") == "absent"
            and retirement.get("iam")
            == {
                role + ":" + kind: "absent"
                for role in ("writer", "reader", "cleanup")
                for kind in ("user", "policy")
            },
            "fixture_native_retirement_required",
        )
        _native_retirement_receipt(entry, run_id, retirement.get("native_receipt"))
        physical_receipt = _native_physical_retirement_receipt(
            owner, run_id, retirement["native_receipt"]
        )
        require(
            retirement.get("physical_receipt") == physical_receipt
            and retirement.get("physical_receipt_sha256")
            == hashlib.sha256(
                json.dumps(
                    physical_receipt, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            "fixture_native_physical_receipt_changed",
        )
    business = owner.data.get("fixture_native_business", {})
    require(isinstance(business, dict), "fixture_native_business_inventory_invalid")
    for suite_hex, entry in business.items():
        try:
            suite_id = uuid.UUID(hex=suite_hex)
        except (ValueError, TypeError, AttributeError):
            raise ContractError("fixture_native_business_inventory_invalid") from None
        require(
            suite_id.hex == suite_hex
            and isinstance(entry, dict)
            and entry.get("suite_id") == str(suite_id),
            "fixture_native_business_inventory_invalid",
        )
        require(
            entry.get("state") == "retired",
            "fixture_native_business_retirement_required",
        )
    store = owner.data["fixture_store"]
    require(store["database"] == database_identity, "fixture_database_changed")
    _physical(owner, store)
    if store["state"] in {"observed_unpublished", "published"}:
        receipt = _private_bytes(owner.private / "original-store-receipt.json")
        require(
            hashlib.sha256(receipt).hexdigest() == store["receipt_sha256"],
            "fixture_receipt_changed",
        )
    else:
        require(store["state"] == "provisioned_unpublished", "fixture_store_state")
    root_access, root_secret = _root_credentials(owner)
    with closing(_client(store["admin_endpoint"], root_access, root_secret)) as root:
        buckets = root.list_buckets().get("Buckets", [])
        require(
            [item["Name"] for item in buckets] == [store["bucket"]],
            "fixture_bucket_inventory_changed",
        )
        require(
            root.get_bucket_versioning(Bucket=store["bucket"]).get("Status")
            == "Enabled",
            "fixture_versioning_unproven",
        )
        _empty_inventory(root, store["bucket"])


def fence_account_for_destroy(owner):
    """Revoke only the two store identities after the caller proves settlement."""
    store = owner.data["fixture_store"]
    accounts = (credentials(owner, store), reader_credentials(owner, store))
    root_access, root_secret = _root_credentials(owner)
    teardown = owner.data.setdefault("fixture_teardown", {"state": "fencing"})
    require(
        teardown["state"] in {"fencing", "fenced", "ready"}, "fixture_teardown_state"
    )
    owner.save()
    mc_env = {
        "PATH": os.environ["PATH"],
        "MC_CONFIG_DIR": str(owner.private / "mc"),
        "MC_HOST_fixture": (
            "http://"
            + urllib.parse.quote(root_access, safe="")
            + ":"
            + urllib.parse.quote(root_secret, safe="")
            + "@"
            + urllib.parse.urlsplit(store["admin_endpoint"]).netloc
        ),
    }
    for account in accounts:
        arguments = [
            "mc",
            "--json",
            "admin",
            "user",
            "info",
            "fixture",
            account["access_key"],
        ]
        status = json.loads(command(arguments, env=mc_env)).get("userStatus")
        if status == "enabled":
            require(teardown["state"] == "fencing", "fixture_account_reenabled")
            command(
                ["mc", "admin", "user", "disable", "fixture", account["access_key"]],
                env=mc_env,
            )
            status = json.loads(command(arguments, env=mc_env)).get("userStatus")
        require(status == "disabled", "fixture_account_fence_unproven")
    if teardown["state"] != "ready":
        teardown["state"] = "fenced"
    owner.save()


def retain_receipt(owner, destination):
    """Retain immutable non-secret evidence outside the private directory."""
    store = owner.data["fixture_store"]
    if store["state"] == "provisioned_unpublished":
        return
    receipt = _private_bytes(owner.private / "original-store-receipt.json")
    require(
        hashlib.sha256(receipt).hexdigest() == store["receipt_sha256"],
        "fixture_receipt_changed",
    )
    destination = Path(destination)
    if destination.exists():
        require(
            _private_bytes(destination) == receipt, "fixture_receipt_archive_conflict"
        )
    else:
        private_write(destination, receipt)


def _raster_destroy_items(owner, resources):
    """Bind the private Raster teardown to the published, fenced source owner."""
    from worktree_env import state_path

    store = owner.data["fixture_store"]
    teardown = owner.data.get("fixture_teardown") or {}
    require(
        isinstance(owner, Owner)
        and resources["mode"] == "e2e"
        and owner.path
        == state_path(Path(resources["root"]), "e2e", "fixture-store-owner.json")
        and owner.data["checkout"] == resources["root"]
        and owner.private == owner.path.parent / ("private-" + owner.data["nonce"])
        and owner.data["cleaned"] is False
        and store["state"] == "published"
        and owner.data["fixture_intent"]["database"] == store["database"]
        and store["database"]["database"] == resources["database"]
        and store["database"]["ownership"] == resources["owner"]
        and store["database"]["resource_root"] == resources["root"]
        and teardown.get("state") == "ready"
        and not teardown.get("partial")
        and all(session.get("closed") for session in owner.data["browser_sessions"]),
        "fixture_raster_destroy_identity_changed",
    )
    archived_receipt = state_path(
        Path(resources["root"]),
        "e2e",
        "fixture-original-receipt-" + owner.data["nonce"] + ".json",
    )
    require(
        hashlib.sha256(_private_bytes(archived_receipt)).hexdigest()
        == store["receipt_sha256"],
        "fixture_receipt_changed",
    )
    items = owner.data["resources"]
    require(
        len(items) == 3
        and {(item["kind"], item["id"]) for item in items}
        == {
            ("container", store["container_id"]),
            ("volume", store["volume_id"]),
            ("network", store["network_id"]),
        },
        "fixture_inventory_changed",
    )
    return {item["kind"]: item for item in items}


def _raster_volume_users(volume):
    return command(
        [
            "docker",
            "ps",
            "-a",
            "--no-trunc",
            "--filter",
            "volume=" + volume["id"],
            "--format",
            "{{.ID}}",
        ]
    ).splitlines()


def _raster_require_live_birth(owner, resources):
    items = _raster_destroy_items(owner, resources)
    birth = owner.data.get("fixture_process_birth") or {}
    observed = owner.verified(items["container"])
    state = observed.get("State") or {}
    require(
        state.get("Status") == "running"
        and state.get("Running") is True
        and type(state.get("Pid")) is int
        and state["Pid"] > 0
        and bool(state.get("StartedAt"))
        and observed.get("RestartCount") == 0
        and birth.get("container_id") == items["container"]["id"]
        and birth.get("pid") == state["Pid"]
        and birth.get("started_at") == state["StartedAt"],
        "fixture_raster_birth_changed",
    )


def _raster_stop_for_destroy(owner, resources):
    """Signal the exact already-fenced fixture source; retain uncertainty."""
    items = _raster_destroy_items(owner, resources)
    teardown = owner.data["fixture_teardown"]
    require(
        "raster_shutdown" not in teardown, "fixture_raster_shutdown_already_started"
    )
    container, network = _physical(owner, owner.data["fixture_store"])
    volume = owner.verified(items["volume"])
    state = container.get("State") or {}
    birth = owner.data.get("fixture_process_birth") or {}
    mounts = container.get("Mounts") or []
    require(
        state.get("Status") == "running"
        and state.get("Running") is True
        and isinstance(state.get("Pid"), int)
        and state["Pid"] > 0
        and bool(state.get("StartedAt"))
        and container.get("RestartCount") == 0
        and birth.get("container_id") == items["container"]["id"]
        and birth.get("pid") == state["Pid"]
        and birth.get("started_at") == state["StartedAt"]
        and (container.get("HostConfig", {}).get("RestartPolicy") or {}).get("Name")
        == "no"
        and container.get("HostConfig", {}).get("AutoRemove") is False
        and container.get("Config", {}).get("Cmd")
        == ["server", "/data", "--console-address", ":9001"]
        and len(mounts) == 1
        and mounts[0].get("Type") == "volume"
        and mounts[0].get("Name") == items["volume"]["id"]
        and mounts[0].get("Destination") == "/data"
        and mounts[0].get("RW") is True
        and volume.get("Driver") == "local"
        and set((container.get("NetworkSettings") or {}).get("Networks") or {})
        == {owner.data["fixture_store"]["network_name"]}
        and set(network.get("Containers") or {}) == {items["container"]["id"]}
        and _raster_volume_users(items["volume"]) == [items["container"]["id"]],
        "fixture_raster_shutdown_topology_changed",
    )
    receipt = {
        "state": "signal_intent",
        "container_id": items["container"]["id"],
        "volume_id": items["volume"]["id"],
        "network_id": items["network"]["id"],
        "pid": state["Pid"],
        "started_at": state["StartedAt"],
    }
    teardown["raster_shutdown"] = receipt
    owner.save()
    acknowledgement = command(
        ["docker", "kill", "--signal", "SIGTERM", items["container"]["id"]]
    )
    require(
        acknowledgement in {items["container"]["id"], items["container"]["name"]},
        "fixture_raster_signal_unconfirmed",
    )
    receipt.update(
        state="signal_acknowledged", signal="SIGTERM", signal_ack=acknowledgement
    )
    owner.save()
    result = command(["docker", "wait", items["container"]["id"]])
    require(result.isdecimal() and int(result) <= 255, "fixture_raster_wait_invalid")
    receipt.update(state="wait_acknowledged", wait_exit=int(result))
    owner.save()
    require(result == "0", "fixture_raster_exit_unsettled")
    observed = owner.verified(items["container"])
    state = observed.get("State") or {}
    require(
        state.get("Status") == "exited"
        and state.get("Running") is False
        and state.get("Pid") == 0
        and state.get("ExitCode") == 0
        and state.get("OOMKilled") is False
        and not state.get("Error")
        and state.get("StartedAt") == receipt["started_at"]
        and bool(state.get("FinishedAt"))
        and observed.get("RestartCount") == 0,
        "fixture_raster_exit_unsettled",
    )
    receipt.update(
        state="stopped",
        finished_at=state["FinishedAt"],
        exit_state={
            "status": state["Status"],
            "running": state["Running"],
            "pid": state["Pid"],
            "exit_code": state["ExitCode"],
            "oom_killed": state["OOMKilled"],
            "error_empty": not state.get("Error"),
            "restart_count": observed["RestartCount"],
        },
    )
    owner.save()
    _raster_require_stopped_for_destroy(owner, resources)


def _raster_require_stopped_for_destroy(owner, resources):
    """Recheck affirmative stop evidence and the original physical identities."""
    items = _raster_destroy_items(owner, resources)
    receipt = owner.data["fixture_teardown"].get("raster_shutdown") or {}
    birth = owner.data.get("fixture_process_birth") or {}
    container = owner.verified(items["container"])
    volume = owner.verified(items["volume"])
    network = owner.verified(items["network"])
    state = container.get("State") or {}
    mounts = container.get("Mounts") or []
    require(
        receipt.get("state") == "stopped"
        and receipt.get("container_id") == items["container"]["id"]
        and receipt.get("volume_id") == items["volume"]["id"]
        and receipt.get("network_id") == items["network"]["id"]
        and receipt.get("signal") == "SIGTERM"
        and receipt.get("signal_ack")
        in {items["container"]["id"], items["container"]["name"]}
        and receipt.get("wait_exit") == 0
        and isinstance(receipt.get("pid"), int)
        and receipt["pid"] > 0
        and bool(receipt.get("started_at"))
        and birth.get("container_id") == receipt["container_id"]
        and birth.get("pid") == receipt["pid"]
        and birth.get("started_at") == receipt["started_at"]
        and bool(receipt.get("finished_at"))
        and receipt.get("exit_state")
        == {
            "status": "exited",
            "running": False,
            "pid": 0,
            "exit_code": 0,
            "oom_killed": False,
            "error_empty": True,
            "restart_count": 0,
        }
        and state.get("Status") == "exited"
        and state.get("Running") is False
        and state.get("Pid") == 0
        and state.get("ExitCode") == 0
        and state.get("OOMKilled") is False
        and not state.get("Error")
        and state.get("StartedAt") == receipt["started_at"]
        and state.get("FinishedAt") == receipt["finished_at"]
        and container.get("RestartCount") == 0
        and container.get("Config", {}).get("Image") == MINIO_IMAGE
        and len(mounts) == 1
        and mounts[0].get("Type") == "volume"
        and mounts[0].get("Name") == items["volume"]["id"]
        and mounts[0].get("Destination") == "/data"
        and mounts[0].get("RW") is True
        and volume.get("Name") == items["volume"]["id"]
        and volume.get("Driver") == "local"
        and set((container.get("NetworkSettings") or {}).get("Networks") or {})
        == {owner.data["fixture_store"]["network_name"]}
        and network.get("Name") == owner.data["fixture_store"]["network_name"]
        and network.get("Driver") == "bridge"
        and set(network.get("Containers") or {}).issubset({items["container"]["id"]})
        and _raster_volume_users(items["volume"]) == [items["container"]["id"]],
        "fixture_raster_stop_unproven",
    )
    return items


def _raster_remove_stopped_for_destroy(owner, resources):
    """Remove only the stopped source container and its exact private topology."""
    items = _raster_require_stopped_for_destroy(owner, resources)
    receipt = owner.data["fixture_teardown"]["raster_shutdown"]
    container, volume, network = (
        items[kind] for kind in ("container", "volume", "network")
    )
    receipt["state"] = "container_remove_intent"
    owner.save()
    removed = command(["docker", "rm", container["id"]])
    require(
        removed in {container["id"], container["name"]},
        "fixture_raster_remove_unconfirmed",
    )
    receipt.update(state="container_remove_acknowledged", container_removed=removed)
    owner.save()
    require(owner.inspect(container) is None, "fixture_raster_container_present")
    container["state"] = "absent"
    receipt["state"] = "container_removed"
    owner.save()
    owner.verified(volume)
    require(not _raster_volume_users(volume), "fixture_raster_volume_shared")
    receipt["state"] = "volume_remove_intent"
    owner.save()
    removed = command(["docker", "volume", "rm", volume["id"]])
    require(removed == volume["id"], "fixture_raster_volume_remove_unconfirmed")
    receipt.update(state="volume_remove_acknowledged", volume_removed=removed)
    owner.save()
    require(owner.inspect(volume) is None, "fixture_raster_volume_present")
    volume["state"] = "absent"
    receipt["state"] = "volume_removed"
    owner.save()
    observed_network = owner.verified(network)
    require(not observed_network.get("Containers"), "fixture_raster_network_in_use")
    receipt["state"] = "network_remove_intent"
    owner.save()
    removed = command(["docker", "network", "rm", network["id"]])
    require(
        removed in {network["id"], network["name"]},
        "fixture_raster_network_remove_unconfirmed",
    )
    receipt.update(state="network_remove_acknowledged", network_removed=removed)
    owner.save()
    require(owner.inspect(network) is None, "fixture_raster_network_present")
    network["state"] = "absent"
    receipt["state"] = "retired"
    owner.save()


def _raster_finish_removed_for_destroy(owner, resources):
    """Finish this source owner without invoking generic forced container cleanup."""
    items = _raster_destroy_items(owner, resources)
    receipt = owner.data["fixture_teardown"].get("raster_shutdown") or {}
    birth = owner.data.get("fixture_process_birth") or {}
    require(
        receipt.get("state") == "retired"
        and receipt.get("signal") == "SIGTERM"
        and receipt.get("signal_ack")
        in {items["container"]["id"], items["container"]["name"]}
        and receipt.get("wait_exit") == 0
        and isinstance(receipt.get("pid"), int)
        and receipt["pid"] > 0
        and bool(receipt.get("started_at"))
        and bool(receipt.get("finished_at"))
        and birth.get("container_id") == receipt.get("container_id")
        and birth.get("pid") == receipt["pid"]
        and birth.get("started_at") == receipt["started_at"]
        and receipt.get("exit_state")
        == {
            "status": "exited",
            "running": False,
            "pid": 0,
            "exit_code": 0,
            "oom_killed": False,
            "error_empty": True,
            "restart_count": 0,
        }
        and receipt.get("container_id") == items["container"]["id"]
        and receipt.get("volume_id") == items["volume"]["id"]
        and receipt.get("network_id") == items["network"]["id"]
        and receipt.get("container_removed")
        in {items["container"]["id"], items["container"]["name"]}
        and receipt.get("volume_removed") == items["volume"]["id"]
        and receipt.get("network_removed")
        in {items["network"]["id"], items["network"]["name"]}
        and all(item.get("state") == "absent" for item in items.values()),
        "fixture_raster_retirement_unproven",
    )
    for item in items.values():
        require(
            owner.inspect(item) is None, "fixture_raster_retirement_identity_changed"
        )
    if owner.private.exists():
        require(
            owner.private.is_dir() and not owner.private.is_symlink(),
            "private_directory_type",
        )
        shutil.rmtree(owner.private)
    owner.data["cleaned"] = True
    owner.data["cleanup_verified_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    owner.data["cleanup_errors"] = []
    owner.save()
