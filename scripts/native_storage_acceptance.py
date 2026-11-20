"""Owned Native-only storage for the exclusive lifecycle acceptance process.

The ordinary MinIO topology and executor credentials remain unchanged. Only the
isolated API/Source Native profile uses this endpoint as its builtin target.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

from botocore.exceptions import ClientError

from native_storage_identity import (
    _native_binding_probe_policy,
    _native_connection_storage_policy,
    _native_source_storage_policy,
    client_for,
    identity_state,
    policy_fingerprint,
    provision_identity,
)
from storage_s3_contract import Budget, load_lock, require, sha256
from verify_storage_candidates import (
    ROOT,
    Owner,
    command,
    owned_bridge_endpoint,
    private_write,
    start_candidate,
)

OWNER_SETTING = "AAP_MODULE_DATA_NATIVE_PROVIDER_OWNER_MANIFEST"
OWNER_DIGEST_SETTING = "AAP_MODULE_DATA_NATIVE_PROVIDER_OWNER_SHA256"
LEGACY_ENDPOINT_SETTING = "AAP_MODULE_DATA_NATIVE_LEGACY_S3_ENDPOINT"
BUCKET_FIELDS = (
    "MINIO_DATASETS_BUCKET",
    "MINIO_IMPORT_BUCKET",
    "MINIO_MEDIA_CACHE_BUCKET",
    "MINIO_BUCKET",
)


def qualify_native_provider(output: Path, evidence: dict) -> None:
    manifest = output.with_suffix(".native-contract-owner.json")
    report_path = output.with_suffix(".native-contract.json")
    log_path = output.with_suffix(".native-contract.log")
    require(
        not any(path.exists() for path in (manifest, report_path, log_path)),
        "native_contract_artifacts_exist",
    )
    evidence["qualification"] = {
        "owner_manifest": str(manifest),
        "report": str(report_path),
        "log": str(log_path),
        "passed": False,
        "retirement_requires_separate_approval": True,
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_native_storage_contract.py"),
            "--manifest",
            str(manifest),
            "--report",
            str(report_path),
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=300,
    )
    private_write(log_path, completed.stdout + b"\n" + completed.stderr)
    require(report_path.is_file(), "native_contract_report_missing")
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)
    evidence["qualification"].update(
        report_sha256=sha256(report_bytes),
        runtime_retired=report.get("runtime_retired") is True,
        retirement_requires_separate_approval=report.get("runtime_retired") is not True,
    )
    require(
        completed.returncode == 0
        and report.get("schema") == "aap.native-storage-contract/v1"
        and report.get("scope") == "native_saved_output_target_only"
        and report.get("native_target_contract_passed") is True
        and report.get("ordinary_binding_probe_passed") is True
        and report.get("runtime_retired") is True
        and report.get("general_storage_replacement_qualified") is False
        and report.get("native_lifecycle_qualified") is False
        and report.get("publication") == "owned_private_bridge"
        and report.get("policy_sha256") == policy_fingerprint(),
        "native_contract_prerequisite_failed",
    )
    evidence["qualification"]["passed"] = True
    evidence["qualified_image"] = report["image"]
    evidence["policy_sha256"] = report["policy_sha256"]


def start_native_provider(owner: Owner, evidence: dict) -> dict:
    lock = load_lock(ROOT / "scripts/storage_s3_candidates.lock.json", ROOT)
    candidate = next(item for item in lock["candidates"] if item["name"] == "seaweedfs")
    require(candidate["image"] == evidence["qualified_image"], "native_qualified_image")
    container, client, network, volume, _, credentials = start_candidate(
        owner,
        lock,
        candidate,
        Budget(),
        evidence,
        native_identity_management=True,
        publish_on_bridge=True,
    )
    endpoint = client.meta.endpoint_url
    client.close()
    record = {
        "schema": "aap.native-acceptance-provider/v1",
        "container": container["id"],
        "network": network["id"],
        "volume": volume["id"],
        "image": candidate["image"],
        "image_config_digest": candidate["config_digest"],
        "endpoint": endpoint,
        "policy_sha256": policy_fingerprint(),
        "filer_config_sha256": sha256((owner.private / "filer.toml").read_bytes()),
    }
    owner.data["native_acceptance"] = record
    owner.save()
    state = {**record, "credentials": list(credentials)}
    evidence.update(record)
    evidence["status"] = "started"
    evidence["owner_manifest"] = str(owner.path)
    return state


def provision_native_provider(owner, state, resources, legacy_identities, evidence):
    """Create finite Native, binding-probe and read-only original identities."""
    s3 = client_for("s3", state["endpoint"], state["credentials"], Budget())
    iam = client_for("iam", state["endpoint"], state["credentials"], Budget())
    buckets = {name: resources["buckets"][name] for name in BUCKET_FIELDS}
    require(len(set(buckets.values())) == len(BUCKET_FIELDS), "native_distinct_buckets")
    try:
        for bucket in buckets.values():
            s3.create_bucket(Bucket=bucket)
            s3.put_bucket_tagging(
                Bucket=bucket,
                Tagging={
                    "TagSet": [
                        {"Key": "aap-owner", "Value": owner.data["nonce"]},
                        {"Key": "yoao-worktree-owner", "Value": resources["owner"]},
                    ]
                },
            )
            s3.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
            )
        policies = {
            "native_source": _native_source_storage_policy(
                {"bucket": buckets["MINIO_MEDIA_CACHE_BUCKET"]}
            ),
            "native_connection": _native_connection_storage_policy(
                {"bucket": buckets["MINIO_BUCKET"]}
            ),
            "binding_probe": _native_binding_probe_policy(
                {"bucket": buckets["MINIO_MEDIA_CACHE_BUCKET"]}
            ),
            "original_reader": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                        "Resource": [
                            f"arn:aws:s3:::{buckets['MINIO_DATASETS_BUCKET']}/*"
                        ],
                    }
                ],
            },
        }
        policies.update(
            {
                name: policies["original_reader"]
                for name in ("image_control_original", "image_worker_original")
            }
        )
        identities = {
            name: provision_identity(
                iam, "acceptance-" + name.replace("_", "-"), policy
            )
            for name, policy in policies.items()
        }
        keys = [identity[1]["AccessKeyId"] for identity in identities.values()]
        require(
            len(set(keys)) == 6 and state["credentials"][0] not in keys,
            "native_distinct_identities",
        )
        bucket = buckets["MINIO_MEDIA_CACHE_BUCKET"]
        key = "image-pyramids/objects/authority-proof-" + owner.data["nonce"]
        receipt = s3.put_object(Bucket=bucket, Key=key, Body=b"native-authority-proof")
        version = receipt["VersionId"]
        ordinary_identities = {
            **legacy_identities,
            **{
                name: {
                    "access_key": identities[name][1]["AccessKeyId"],
                    "secret_key": identities[name][1]["SecretAccessKey"],
                }
                for name in (
                    "binding_probe",
                    "original_reader",
                    "image_control_original",
                    "image_worker_original",
                )
            },
        }
        for name, scope in ordinary_identities.items():
            credentials = (scope["access_key"], scope["secret_key"])
            require(
                credentials[0]
                not in {
                    identities["native_source"][1]["AccessKeyId"],
                    identities["native_connection"][1]["AccessKeyId"],
                    state["credentials"][0],
                },
                "native_executor_key",
            )
            client = client_for("s3", state["endpoint"], credentials, Budget())
            try:
                for operation in (client.get_object, client.delete_object):
                    try:
                        response = operation(Bucket=bucket, Key=key, VersionId=version)
                    except ClientError as exc:
                        require(
                            exc.response["ResponseMetadata"]["HTTPStatusCode"] == 403,
                            "native_executor_authority_denial",
                        )
                    else:
                        if response.get("Body"):
                            response["Body"].close()
                        raise RuntimeError(
                            "Ordinary executor gained Native storage authority"
                        )
                evidence.setdefault("ordinary_identity_native_denials", []).append(name)
            finally:
                client.close()
        require(
            s3.head_object(Bucket=bucket, Key=key, VersionId=version)["ETag"]
            == receipt["ETag"],
            "native_authority_probe_preserved",
        )
        s3.delete_object(Bucket=bucket, Key=key, VersionId=version)
        for identity in identities.values():
            identity_state(iam, identity)
        state.update(
            buckets=buckets, worktree_owner=resources["owner"], identities=identities
        )
        private_write(owner.private / "native-profile.json", json.dumps(state).encode())
        owner.data["native_acceptance"].update(
            buckets=buckets,
            worktree_owner=resources["owner"],
            profile_sha256=sha256((owner.private / "native-profile.json").read_bytes()),
        )
        owner.save()
        evidence.update(
            status="ready",
            buckets=buckets,
            owner_manifest_sha256=sha256(owner.path.read_bytes()),
            general_storage_replacement_qualified=False,
        )
        return {
            name: {
                "access_key": identity[1]["AccessKeyId"],
                "secret_key": identity[1]["SecretAccessKey"],
                "bucket": buckets[
                    {
                        "native_source": "MINIO_MEDIA_CACHE_BUCKET",
                        "native_connection": "MINIO_BUCKET",
                        "binding_probe": "MINIO_MEDIA_CACHE_BUCKET",
                        "original_reader": "MINIO_DATASETS_BUCKET",
                        "image_control_original": "MINIO_DATASETS_BUCKET",
                        "image_worker_original": "MINIO_DATASETS_BUCKET",
                    }[name]
                ],
            }
            for name, identity in identities.items()
        }
    finally:
        s3.close()
        iam.close()


def native_profile_environment(
    owner, state, scopes, legacy_endpoint, *, legacy_admin_access_key
):
    origin = urlsplit(state["endpoint"])
    require(origin.scheme == "http" and origin.netloc, "native_profile_endpoint")
    environment = {
        OWNER_SETTING: str(owner.path),
        OWNER_DIGEST_SETTING: sha256(owner.path.read_bytes()),
        LEGACY_ENDPOINT_SETTING: legacy_endpoint,
        "AAP_MODULE_DATA_NATIVE_LEGACY_ADMIN_ACCESS_KEY": legacy_admin_access_key,
        "MINIO_ENDPOINT": origin.netloc,
        "MINIO_PUBLIC_ENDPOINT": origin.netloc,
        "MINIO_USE_SSL": "false",
        "MINIO_ACCESS_KEY": scopes["binding_probe"]["access_key"],
        "MINIO_SECRET_KEY": scopes["binding_probe"]["secret_key"],
        "AAP_MODULE_DATA_ASSET_SOURCE_S3_ACCESS_KEY": scopes["original_reader"][
            "access_key"
        ],
        "AAP_MODULE_DATA_ASSET_SOURCE_S3_SECRET_KEY": scopes["original_reader"][
            "secret_key"
        ],
    }
    for identity, prefix in (
        ("image_control_original", "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3"),
        ("image_worker_original", "AAP_MODULE_DATA_ASSET_IMAGE_WORKER_S3"),
    ):
        environment[prefix + "_ACCESS_KEY"] = scopes[identity]["access_key"]
        environment[prefix + "_SECRET_KEY"] = scopes[identity]["secret_key"]
    return environment


def verified_native_provider(owner_path, owner_digest, resources):
    """Re-derive the live target from its exact owned container before admin I/O."""
    path = Path(owner_path)
    require(
        path.is_absolute() and path.resolve() == path and not path.is_symlink(),
        "native_owner_path",
    )
    require(sha256(path.read_bytes()) == owner_digest, "native_owner_digest")
    owner = Owner(path)
    require(owner.data.get("cleaned") is False, "native_owner_live")
    profile_path = owner.private / "native-profile.json"
    require(
        not owner.private.is_symlink()
        and owner.private.stat().st_uid == os.getuid()
        and owner.private.stat().st_mode & 0o077 == 0
        and not profile_path.is_symlink()
        and profile_path.is_file()
        and profile_path.stat().st_uid == os.getuid()
        and profile_path.stat().st_mode & 0o077 == 0,
        "native_profile_private",
    )
    record = owner.data["native_acceptance"]
    require(
        sha256(profile_path.read_bytes()) == record["profile_sha256"],
        "native_profile_digest",
    )
    state = json.loads(profile_path.read_bytes())
    require(
        all(
            state.get(name) == value
            for name, value in record.items()
            if name != "profile_sha256"
        )
        and state["policy_sha256"] == policy_fingerprint()
        and state["worktree_owner"] == resources["owner"]
        and state["buckets"]
        == {name: resources["buckets"][name] for name in BUCKET_FIELDS},
        "native_profile_identity",
    )
    items = {item["id"]: item for item in owner.data["resources"]}
    container, network, volume = (
        items[state[name]] for name in ("container", "network", "volume")
    )
    require(
        (container["kind"], network["kind"], volume["kind"])
        == ("container", "network", "volume")
        and container["image"] == state["image"],
        "native_owned_resource_types",
    )
    peer = owner.verified(container)
    owner.verified(volume)
    require(peer["Image"] == state["image_config_digest"], "native_live_image_config")
    require(
        owned_bridge_endpoint(owner, container, network) == state["endpoint"],
        "native_live_endpoint",
    )
    require(
        set(owner.verified(network).get("Containers") or {}) == {container["id"]},
        "native_network_members",
    )
    mounts = peer.get("Mounts") or []
    require(
        len(mounts) == 2
        and any(
            mount.get("Type") == "volume"
            and mount.get("Name") == volume["id"]
            and mount.get("Destination") == "/data"
            and mount.get("RW") is True
            for mount in mounts
        )
        and any(
            mount.get("Type") == "bind"
            and mount.get("Source") == str(owner.private / "filer.toml")
            and mount.get("Destination") == "/etc/seaweedfs/filer.toml"
            and mount.get("RW") is False
            for mount in mounts
        )
        and sha256((owner.private / "filer.toml").read_bytes())
        == state["filer_config_sha256"],
        "native_data_volume",
    )
    users = command(
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
    )
    # The stopped chown/config helpers are also exact records of this owner.
    allowed_users = {
        item["id"] for item in owner.data["resources"] if item["kind"] == "container"
    }
    require(
        container["id"] in users.splitlines()
        and set(users.splitlines()) <= allowed_users,
        "native_volume_users",
    )
    for user in users.splitlines():
        owner.verified(items[user])
    return state, client_for("s3", state["endpoint"], state["credentials"], Budget())
