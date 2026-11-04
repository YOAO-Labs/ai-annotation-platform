"""Provision the private, physically isolated original store for fixture suites.

The caller owns the authority transaction and may publish the returned target only
with its immutable receipt and fixture root. An interrupted provision is retained
in the existing exact-ID owner manifest for explicit inspection and recovery.
"""

from __future__ import annotations
from contextlib import closing

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
        error = (result.stdout + result.stderr).decode(errors="replace").lower()
        require(
            result.returncode != 0
            and any(
                value in error
                for value in (
                    "accessdenied",
                    "access denied",
                    "not authorized",
                    "forbidden",
                )
            ),
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
