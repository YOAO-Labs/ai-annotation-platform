#!/usr/bin/env python3
"""Exercise the native source fence only against explicitly owned Docker fixtures.

This harness never loads application settings, provisions databases, or contacts
an existing store. The operator CLI and its durable journal remain unmodified.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from copy_storage_migration import client
from storage_migration_copy import Checkpoint, Limits, execute, plan
from storage_migration_inventory import InventoryLimits, PURPOSES, write_inventory
from storage_migration_references import (
    Budget,
    Limits as ReferenceLimits,
    canonical,
    write_catalog,
)
from storage_s3_contract import ContractError, require, safe_error
from verify_storage_candidates import Owner, command, host_port, private_write

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aap.storage-source-fence-verification/v1"
LABEL = "aap.source-fence."
MAX_OUTPUT = 1024 * 1024


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def private_parent(path):
    require(path.is_absolute() and not path.is_symlink(), "private_path")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    require(
        path.parent.is_dir()
        and not path.parent.is_symlink()
        and info.st_uid == os.getuid()
        and info.st_mode & 0o077 == 0,
        "private_parent",
    )


def replace_config(path, body):
    temporary = path.parent / (".owned-config-" + secrets.token_hex(8))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class FixtureOwner(Owner):
    """Keep the existing exact-ID cleanup, adding native roles and internal nets."""

    role = "fixture"

    def labels(self):
        return [
            *super().labels(),
            "--label",
            f"{LABEL}owner={self.data['nonce']}",
            "--label",
            f"{LABEL}checkout={ROOT}",
            "--label",
            f"{LABEL}role={self.role}",
        ]

    def resource(self, kind, role, *, args=()):
        # Owner.create deliberately does not forward non-container arguments.
        # Persist and recover this exact internal-network creation ourselves.
        self.role = role
        self.counter += 1
        item = {
            "kind": kind,
            "name": f"aap-e07-{self.data['nonce']}-{self.counter}",
            "id": None,
            "image": None,
            "state": "creation_intent",
        }
        self.data["resources"].append(item)
        self.save()
        item["id"] = command(
            ["docker", kind, "create", *self.labels(), *args, item["name"]]
        )
        item["state"] = "created"
        self.save()
        return item

    def service(self, role, image, *, args=(), cmd=()):
        self.role = role
        item = self.container(image, args=args, cmd=cmd)
        self.start(item)
        return item

    def absent(self):
        require(
            all(self.inspect(item) is None for item in self.data["resources"]),
            "independent_resource_absence",
        )
        for kind in ("container", "network", "volume"):
            invocation = (
                ["docker", "ps", "-aq"]
                if kind == "container"
                else ["docker", kind, "ls", "-q"]
            )
            require(
                not command(
                    [
                        *invocation,
                        "--filter",
                        f"label={LABEL}owner={self.data['nonce']}",
                    ]
                ),
                "independent_owner_absence",
            )


def request(url, *, method="GET", body=None, headers=None):
    try:
        response = urllib.request.urlopen(
            urllib.request.Request(
                url, data=body, headers=headers or {}, method=method
            ),
            timeout=15,
        )
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        data = response.read(MAX_OUTPUT + 1)
        require(len(data) <= MAX_OUTPUT, "http_output_budget")
        return response.status, dict(response.headers.items()), data


def header(headers, name):
    return next((value for key, value in headers.items() if key.lower() == name), None)


def body(s3, **params):
    response = s3.get_object(**params)
    with response["Body"] as stream:
        data = stream.read(65537)
    require(len(data) <= 65536, "fixture_object_budget")
    return data


def rules(s3, buckets):
    result = {}
    for bucket in sorted(buckets):
        try:
            result[bucket] = s3.get_bucket_lifecycle_configuration(Bucket=bucket)[
                "Rules"
            ]
        except Exception as exc:
            require(
                getattr(exc, "response", {}).get("Error", {}).get("Code")
                == "NoSuchLifecycleConfiguration",
                "lifecycle_observation_failed",
            )
            result[bucket] = []
    return result


def object_facts(s3, buckets):
    """Preserve every actual version and marker, never just the current body."""
    rows = []
    for bucket in sorted(buckets):
        response = s3.list_object_versions(Bucket=bucket, MaxKeys=100)
        require(not response.get("IsTruncated"), "fixture_version_budget")
        for kind in ("Versions", "DeleteMarkers"):
            for item in response.get(kind, []):
                data = None
                if kind == "Versions":
                    data = hashlib.sha256(
                        body(
                            s3,
                            Bucket=bucket,
                            Key=item["Key"],
                            VersionId=item["VersionId"],
                        )
                    ).hexdigest()
                rows.append(
                    {
                        "bucket": bucket,
                        "kind": kind,
                        "key": item["Key"],
                        "version_id": item["VersionId"],
                        "latest": item["IsLatest"],
                        "size": item.get("Size"),
                        "etag": item.get("ETag"),
                        "sha256": data,
                    }
                )
    return sorted(rows, key=canonical)


def uploads(s3, buckets):
    result = []
    for bucket in sorted(buckets):
        response = s3.list_multipart_uploads(Bucket=bucket, MaxUploads=100)
        require(not response.get("IsTruncated"), "fixture_multipart_budget")
        result.extend((bucket, item) for item in response.get("Uploads", []))
    return result


def matching_generation(owner, source, state):
    journal = json.loads(state.read_bytes())
    require(len(journal["barriers"]) >= 2, "barrier_receipts_missing")
    latest = journal["barriers"][-1]
    actual = owner.verified(source)["State"]
    require(
        latest["source_container"] == source["id"]
        and latest["started_at"] == actual["StartedAt"]
        and latest["pid"] == actual["Pid"]
        and actual["Running"] is True,
        "source_generation_mismatch",
    )


def until_ready(s3):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            s3.list_buckets()
            return
        except Exception:
            time.sleep(0.2)
    raise ContractError("fixture_start_timeout")


def ordinary_account(owner, endpoint, credentials, buckets):
    policy = owner.private / "ordinary-policy.json"
    private_write(
        policy,
        json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:PutObject", "s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{bucket}/*" for bucket in buckets],
                    }
                ],
            }
        ).encode(),
    )
    parsed = urllib.parse.urlsplit(endpoint)
    access = "aapf" + secrets.token_hex(8)
    environment = {
        **os.environ,
        "MC_CONFIG_DIR": str(owner.private / "mc"),
        "MC_HOST_fence": (
            "http://"
            + urllib.parse.quote(credentials[0], safe="")
            + ":"
            + urllib.parse.quote(credentials[1], safe="")
            + "@"
            + parsed.netloc
        ),
    }
    output = command(
        [
            "mc",
            "--json",
            "admin",
            "user",
            "svcacct",
            "add",
            "fence",
            credentials[0],
            "--access-key",
            access,
            "--policy",
            str(policy),
            "--name",
            "source-fence-fixture",
        ],
        env=environment,
        timeout=30,
    )
    value = json.loads(output)
    secret = value.get("secretKey")
    require(isinstance(secret, str) and secret, "service_account_response")
    return access, secret


def fence_cli(
    config,
    state,
    environment,
    action,
    *,
    revision=None,
    operation=None,
    expected=0,
    crash=False,
):
    arguments = [action, "--config", str(config), "--state", str(state)]
    if revision is not None:
        arguments += ["--expected-revision", str(revision)]
    if operation is not None:
        arguments += ["--operation-id", operation]
    invocation = [sys.executable, str(ROOT / "scripts/storage_source_fence.py")]
    if crash:
        # Only the fixture injects death, after a real process barrier and before
        # Controller can save its acknowledgement. There is no CLI fault switch.
        wrapper = """
import json, os, sys
sys.path.insert(0, sys.argv.pop(1))
trace_path = sys.argv.pop(1)
import storage_source_fence as cli
from storage_source_fence_docker import DockerAdapter
original = DockerAdapter.barrier
def cached_uploads(self):
    with self._client() as client:
        return sum(len(client.list_multipart_uploads(Bucket=bucket, MaxUploads=1000).get('Uploads', []))
                   for bucket in set(self.config['purpose_buckets'].values()))
def barrier(self):
    with self._client() as client:
        bucket = self.config['purpose_buckets']['annotations']
        key = 'probes/late-create'
        upload = client.create_multipart_upload(Bucket=bucket, Key=key)['UploadId']
        client.upload_part(Bucket=bucket, Key=key, UploadId=upload, PartNumber=1, Body=b'late-owned-part')
    late_path = os.path.join(os.path.dirname(trace_path), 'late-upload.json')
    fd = os.open(late_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'Bucket': bucket, 'Key': key, 'UploadId': upload}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    before = cached_uploads(self)
    original(self)
    after = cached_uploads(self)
    fd = os.open(trace_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'cached_before_restart': before, 'cached_after_restart': after}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os._exit(73)
DockerAdapter.barrier = barrier
raise SystemExit(cli.main())
"""
        invocation = [
            sys.executable,
            "-c",
            wrapper,
            str(ROOT / "scripts"),
            str(config.parent / "barrier-counts.json"),
        ]
    result = subprocess.run(
        [*invocation, *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        timeout=180,
    )
    require(result.returncode == expected, "fence_cli_exit")
    require(len(result.stdout) + len(result.stderr) <= MAX_OUTPUT, "cli_output_budget")
    if crash:
        require(not result.stdout and not result.stderr, "crash_output")
        return None
    value = json.loads(result.stdout if expected == 0 else result.stderr)
    require(isinstance(value, dict), "fence_cli_response")
    if expected == 0:
        require(not result.stderr, "fence_cli_stderr")
        fields = (
            {"schema", "counts"}
            if action == "preview"
            else {"schema", "revision", "phase", "held", "observed_at", "counts"}
        )
        require(
            set(value) == fields,
            "fence_summary_fields",
        )
    return value


def partial_put(s3, private_endpoint, bucket):
    key = "probes/accepted-slow-put"
    signed = s3.generate_presigned_url(
        "put_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=600
    )
    parsed = urllib.parse.urlsplit(signed)
    connection = socket.create_connection(("127.0.0.1", parsed.port), timeout=5)
    try:
        connection.sendall(
            (
                f"PUT {parsed.path}?{parsed.query} HTTP/1.1\r\n"
                f"Host: {parsed.netloc}\r\nContent-Length: {32 * 1024**2}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            + b"x" * (256 * 1024)
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for route in ("cluster", "node"):
                status, _, data = request(
                    private_endpoint + "/minio/v2/metrics/" + route
                )
                if status != 200:
                    continue
                for line in data.decode("utf-8").splitlines():
                    if not line or line.startswith("#"):
                        continue
                    metric, _, value = line.rpartition(" ")
                    if (
                        metric.split("{", 1)[0]
                        in {
                            "minio_s3_requests_inflight_total",
                            "minio_s3_requests_inflight",
                        }
                        and re.search(r'api="putobject"', metric, re.I)
                        and float(value) > 0
                    ):
                        return connection, key
            time.sleep(0.2)
        raise ContractError("source_put_acceptance_unproven")
    except BaseException:
        connection.close()
        raise


def put_http(url, data):
    status, headers, _ = request(url, method="PUT", body=data)
    require(status == 200, "fixture_put_not_writable")
    version = header(headers, "x-amz-version-id")
    require(isinstance(version, str) and version not in {"", "null"}, "probe_version")
    return version


def complete_xml(etag):
    return (
        "<CompleteMultipartUpload><Part><PartNumber>1</PartNumber><ETag>"
        + etag
        + "</ETag></Part></CompleteMultipartUpload>"
    ).encode("ascii")


def multipart(s3, bucket, key, *, part=False):
    upload = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    etag = None
    if part:
        etag = s3.upload_part(
            Bucket=bucket, Key=key, UploadId=upload, PartNumber=1, Body=b"owned-part"
        )["ETag"]
    return upload, etag


def writable_probes(s3, ordinary, endpoint, bucket):
    key = "probes/old-signed-put"
    signed_put = s3.generate_presigned_url(
        "put_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=600
    )
    version = put_http(signed_put, b"signed-before-fence")
    s3.delete_object(Bucket=bucket, Key=key, VersionId=version)
    for writer, probe in ((s3, "root"), (ordinary, "ordinary")):
        name = "probes/" + probe
        version = writer.put_object(Bucket=bucket, Key=name, Body=b"writable")[
            "VersionId"
        ]
        s3.delete_object(Bucket=bucket, Key=name, VersionId=version)
    anonymous = endpoint + "/" + bucket + "/anonymous-probe"
    s3.put_bucket_policy(
        Bucket=bucket,
        Policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": ["*"]},
                        "Action": ["s3:PutObject"],
                        "Resource": [f"arn:aws:s3:::{bucket}/anonymous-probe"],
                    }
                ],
            }
        ),
    )
    version = put_http(anonymous, b"anonymous-before-fence")
    s3.delete_object(Bucket=bucket, Key="anonymous-probe", VersionId=version)
    # A companion completion proves this same signing capability was writable;
    # the old pending UploadId below must remain unconsumed until sealing.
    completed_key = "probes/completed-before-fence"
    upload, etag = multipart(s3, bucket, completed_key, part=True)
    signed = s3.generate_presigned_url(
        "complete_multipart_upload",
        Params={
            "Bucket": bucket,
            "Key": completed_key,
            "UploadId": upload,
        },
        ExpiresIn=600,
    )
    status, headers, data = request(signed, method="POST", body=complete_xml(etag))
    require(status == 200 and b"<Error>" not in data, "signed_complete_not_writable")
    version = header(headers, "x-amz-version-id")
    require(version not in {None, "", "null"}, "complete_probe_version")
    s3.delete_object(Bucket=bucket, Key=completed_key, VersionId=version)
    pending_key = "probes/pending-with-part"
    pending, etag = multipart(s3, bucket, pending_key, part=True)
    multipart(s3, bucket, "probes/pending-empty")
    signed_complete = s3.generate_presigned_url(
        "complete_multipart_upload",
        Params={
            "Bucket": bucket,
            "Key": pending_key,
            "UploadId": pending,
        },
        ExpiresIn=600,
    )
    return signed_put, signed_complete, complete_xml(etag), anonymous


def rejected_writes(s3, ordinary, bucket, signed_put, signed_complete, xml, anonymous):
    from botocore.exceptions import ClientError

    observed = {}
    for name, signed, method, payload in (
        ("signed_put", signed_put, "PUT", b"denied"),
        ("signed_complete", signed_complete, "POST", xml),
        ("anonymous_put", anonymous, "PUT", b"denied"),
    ):
        observed[name] = request(signed, method=method, body=payload)[0]
        require(observed[name] == 405, "signed_write_not_fenced")
    for name, writer in (("root_put", s3), ("ordinary_put", ordinary)):
        try:
            writer.put_object(Bucket=bucket, Key="probes/denied", Body=b"denied")
        except ClientError as exc:
            observed[name] = exc.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            )
            require(observed[name] == 405, "write_rejection_status")
        else:
            raise ContractError("credential_write_not_fenced")
    return observed


def copy_while_held(
    owner, s3, target, target_endpoint, target_credentials, buckets, facts
):
    directory = owner.private
    inventory = directory / "final-source.jsonl"
    report = write_inventory(
        inventory,
        s3,
        buckets,
        source={"endpoint": s3.meta.endpoint_url},
        handling=dict.fromkeys(PURPOSES, "migrate"),
        limits=InventoryLimits(
            max_entries=100,
            max_pages=100,
            max_object_bytes=65536,
            max_total_bytes=1024**2,
        ),
    )
    require(report["complete"], "fenced_inventory_incomplete")
    spool = directory / "references-spool.jsonl"
    private_write(
        spool,
        b"".join(
            canonical(
                {
                    "type": "reference",
                    "family": "fixture",
                    "owner_id": [str(index)],
                    "role": "saved_owner",
                    "endpoint_url": s3.meta.endpoint_url,
                    "bucket": row["bucket"],
                    "key": row["key"],
                    "version_id": row["version_id"],
                    "sha256": row["sha256"],
                }
            )
            for index, row in enumerate(facts)
            if row["kind"] == "Versions"
        ),
    )
    catalog = directory / "references.jsonl"
    write_catalog(
        catalog,
        spool,
        {"enumeration_complete": True, "families": ["fixture"]},
        inventory,
        directory,
        Budget(ReferenceLimits(max_seconds=60)),
    )
    bucket = "fence-target-" + owner.data["nonce"]
    target.create_bucket(Bucket=bucket)
    target.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    config = {
        "endpoint_url": target_endpoint,
        "bucket": bucket,
        "key_prefix": "fenced/",
        "access_key_id": target_credentials[0],
        "secret_access_key": target_credentials[1],
    }
    checkpoint = directory / "copy.sqlite"
    limits = Limits(
        max_objects=100,
        max_object_bytes=65536,
        max_source_bytes=1024**2,
        max_upload_bytes=1024**2,
        max_verify_bytes=1024**2,
        max_seconds=120,
    )
    plan(
        checkpoint,
        inventory,
        catalog,
        config,
        handling=dict.fromkeys(PURPOSES, "migrate"),
        limits=limits,
    )
    with Checkpoint(checkpoint) as state:
        result = execute(state, s3, target, limits=limits)
        expected = sum(row["kind"] == "Versions" for row in facts)
        require(
            result["copy_subset_complete"]
            and result["counts"].get("verified") == expected,
            "fenced_copy_incomplete",
        )
        require(result["migration_ready"] is False, "unexpected_cutover_qualification")
        for row in state.rows():
            if row["state"] != "verified":
                continue
            receipt = row["receipt"]
            require(
                receipt["version_id"] not in {None, "", "null"},
                "target_version_unfixed",
            )
            actual = body(
                target,
                Bucket=bucket,
                Key=row["target_key"],
                VersionId=receipt["version_id"],
            )
            require(
                hashlib.sha256(actual).hexdigest() == row["sha256"],
                "target_content_changed",
            )
    return {
        "copied_versions": expected,
        "source_inventory_sha256": hashlib.sha256(inventory.read_bytes()).hexdigest(),
    }


def run_fixture(owner, summary):
    from storage_source_fence_docker import MINIO_IMAGE, NGINX_IMAGE, gateway_config

    summary["stage"] = "provision"
    for image in (MINIO_IMAGE, NGINX_IMAGE):
        inspected = json.loads(command(["docker", "image", "inspect", image]))
        require(
            len(inspected) == 1
            and inspected[0].get("Os") == "linux"
            and inspected[0].get("Architecture") == "amd64"
            and image in inspected[0].get("RepoDigests", []),
            "pinned_image_not_cached",
        )
    credentials = ("aap" + secrets.token_hex(12), secrets.token_urlsafe(32))
    target_credentials = ("aap" + secrets.token_hex(12), secrets.token_urlsafe(32))
    source_env, target_env = owner.private / "source.env", owner.private / "target.env"
    for path, pair in ((source_env, credentials), (target_env, target_credentials)):
        private_write(
            path,
            (
                f"MINIO_ROOT_USER={pair[0]}\nMINIO_ROOT_PASSWORD={pair[1]}\n"
                "MINIO_PROMETHEUS_AUTH_TYPE=public\n"
            ).encode(),
        )
    network = owner.resource("network", "network", args=("--internal",))
    frontend = owner.resource("network", "frontend", args=("--driver", "bridge"))
    network_facts = owner.verified(network)
    subnet = ipaddress.ip_network(network_facts["IPAM"]["Config"][0]["Subnet"])
    require(subnet.version == 4 and subnet.num_addresses > 16, "fixture_ipv4_network")
    source_ip, gateway_ip = str(subnet[10]), str(subnet[11])
    volume = owner.resource("volume", "data")
    source = owner.service(
        "source",
        MINIO_IMAGE,
        args=(
            "--pull=never",
            "--network",
            network["id"],
            "--ip",
            source_ip,
            "--volume",
            volume["id"] + ":/data",
            "--env-file",
            str(source_env),
        ),
        cmd=("server", "/data", "--console-address", ":9001"),
    )
    gateway_directory = owner.private / "gateway"
    gateway_directory.mkdir(mode=0o700)
    nginx = gateway_directory / "nginx.conf"
    replace_config(nginx, gateway_config(source_ip, False))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        public_port = reservation.getsockname()[1]
    owner.role = "gateway"
    gateway = owner.container(
        NGINX_IMAGE,
        args=(
            "--pull=never",
            "--network",
            frontend["id"],
            "--sysctl",
            "net.ipv4.ip_forward=0",
            "--publish",
            f"127.0.0.1:{public_port}:80",
            "--entrypoint",
            "nginx",
            "--volume",
            str(gateway_directory) + ":/etc/nginx:ro",
        ),
        cmd=("-g", "daemon off;"),
    )
    owner.action(gateway, "network", "connect", "--ip", gateway_ip, network["id"])
    owner.start(gateway)
    endpoint = f"http://127.0.0.1:{host_port(owner, gateway, 80)}"
    private_endpoint = f"http://{source_ip}:9000"
    target_volume = owner.resource("volume", "target-data")
    target_container = owner.service(
        "target",
        MINIO_IMAGE,
        args=(
            "--pull=never",
            "--publish",
            "127.0.0.1::9000",
            "--env-file",
            str(target_env),
            "--volume",
            target_volume["id"] + ":/data",
        ),
        cmd=("server", "/data", "--console-address", ":9001"),
    )
    target_endpoint = f"http://127.0.0.1:{host_port(owner, target_container, 9000)}"
    limits = Limits(connect_timeout_seconds=3, read_timeout_seconds=15)
    with ExitStack() as stack:
        s3 = stack.enter_context(closing(client(endpoint, *credentials, limits)))
        target = stack.enter_context(
            closing(client(target_endpoint, *target_credentials, limits))
        )
        until_ready(s3)
        until_ready(target)
        summary["stage"] = "seed_source"
        buckets = {
            purpose: "fence-" + purpose + "-" + owner.data["nonce"][:12]
            for purpose in sorted(PURPOSES)
        }
        for purpose, bucket in buckets.items():
            s3.create_bucket(Bucket=bucket)
            s3.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
            )
            s3.put_object(
                Bucket=bucket, Key="objects/" + purpose + "-中文", Body=purpose.encode()
            )
        versioned = buckets["datasets"]
        for data in (b"old frozen body", b"new frozen body"):
            s3.put_object(Bucket=versioned, Key="objects/history", Body=data)
        s3.delete_object(Bucket=versioned, Key="objects/history")
        s3.put_bucket_lifecycle_configuration(
            Bucket=versioned,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "owned-long-retention",
                        "Status": "Enabled",
                        "Filter": {"Prefix": ""},
                        "Expiration": {"Days": 365},
                        "NoncurrentVersionExpiration": {"NoncurrentDays": 365},
                    }
                ]
            },
        )
        summary["stage"] = "writability_probes"
        ordinary_credentials = ordinary_account(
            owner, private_endpoint, credentials, buckets.values()
        )
        ordinary = stack.enter_context(
            closing(client(endpoint, *ordinary_credentials, limits))
        )
        probe_bucket = buckets["annotations"]
        signed_put, signed_complete, xml, anonymous = writable_probes(
            s3, ordinary, endpoint, probe_bucket
        )
        before = object_facts(s3, buckets.values())
        original_rules = rules(s3, buckets.values())
        require(len(uploads(s3, buckets.values())) == 2, "fixture_multipart_count")
        private_write(
            owner.private / "source-facts.json",
            canonical({"objects": before, "rules": original_rules}),
        )
        config = owner.private / "fence.json"
        private_write(
            config,
            canonical(
                {
                    "schema": "aap.storage-source-fence-config/v1",
                    "owner": owner.data["nonce"],
                    "checkout": str(ROOT),
                    "source_container": source["id"],
                    "gateway_container": gateway["id"],
                    "network_id": network["id"],
                    "gateway_frontend_network_id": frontend["id"],
                    "source_volume_name": volume["id"],
                    "gateway_config_directory": str(gateway_directory),
                    "public_endpoint": endpoint,
                    "purpose_buckets": buckets,
                }
            ),
        )
        state = owner.private / "fence-state.json"
        environment = {
            **os.environ,
            "AAP_SOURCE_FENCE_ACCESS_KEY": credentials[0],
            "AAP_SOURCE_FENCE_SECRET_KEY": credentials[1],
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        summary["stage"] = "initialize_fence"
        status = fence_cli(config, state, environment, "init")
        fence_cli(config, state, environment, "preview")
        generation_before = owner.verified(source)["State"]["StartedAt"]
        summary["stage"] = "accepted_slow_put"
        slow, slow_key = partial_put(s3, private_endpoint, probe_bucket)
        try:
            summary["stage"] = "hard_exit_resume"
            operation = str(uuid.uuid4())
            fence_cli(
                config,
                state,
                environment,
                "seal",
                revision=status["revision"],
                operation=operation,
                crash=True,
                expected=73,
            )
            cached = json.loads((config.parent / "barrier-counts.json").read_bytes())
            require(
                set(cached) == {"cached_before_restart", "cached_after_restart"}
                and all(type(value) is int and value >= 0 for value in cached.values()),
                "cached_upload_observation_invalid",
            )
            summary["multipart_cache_observation"] = cached
            interrupted = fence_cli(config, state, environment, "status")
            require(
                interrupted["phase"] == "sealing" and interrupted["held"] is False,
                "hard_exit_responsibility_lost",
            )
            unacknowledged = json.loads(state.read_bytes())
            require(
                unacknowledged["operation_id"] == operation
                and unacknowledged["snapshot"] is not None
                and unacknowledged["barriers"] == []
                and unacknowledged["intent"]["step"] == "barrier_before_abort",
                "hard_exit_intent_lost",
            )
            require(
                owner.verified(source)["State"]["StartedAt"] != generation_before,
                "real_barrier_not_exercised",
            )
            owner.action(source, "stop", "--time", "1")
            stopped = owner.verified(source)["State"]
            require(
                stopped["Running"] is False and stopped["Pid"] == 0,
                "stopped_source_boundary_not_exercised",
            )
            denied = fence_cli(
                config,
                state,
                environment,
                "seal",
                revision=interrupted["revision"],
                operation=operation,
                expected=2,
            )
            require(
                denied.get("code") == "source_multipart_metadata_remaining",
                "hidden_multipart_not_rejected",
            )
            unknown = json.loads(state.read_bytes())
            require(
                unknown["phase"] == "uncertain"
                and unknown["intent"]["step"] == "verify",
                "hidden_multipart_responsibility_lost",
            )
            late = json.loads((config.parent / "late-upload.json").read_bytes())
            require(set(late) == {"Bucket", "Key", "UploadId"}, "late_upload_invalid")
            with closing(client(private_endpoint, *credentials, limits)) as operator:
                require(
                    len(operator.list_parts(**late).get("Parts", [])) == 1,
                    "hidden_upload_not_readable",
                )
                operator.abort_multipart_upload(**late)
                try:
                    operator.list_parts(**late)
                except Exception as exc:
                    require(
                        getattr(exc, "response", {}).get("Error", {}).get("Code")
                        == "NoSuchUpload",
                        "late_upload_unconfirmed",
                    )
                else:
                    raise ContractError("late_upload_still_present")
            status = fence_cli(
                config,
                state,
                environment,
                "seal",
                revision=unknown["revision"],
                operation=operation,
            )
            summary["hidden_multipart_rejected_and_resolved"] = True
            summary["stopped_source_resumed"] = True
        finally:
            slow.close()
        require(status["held"] is True, "source_not_held")
        summary["sealed_counts"] = status["counts"]
        matching_generation(owner, source, state)
        aborted = status["counts"].get("aborted_uploads")
        require(
            type(aborted) is int and aborted >= 2, "multipart_abort_receipt_missing"
        )
        summary["stage"] = "fenced_observations"
        summary["write_rejections"] = rejected_writes(
            s3, ordinary, probe_bucket, signed_put, signed_complete, xml, anonymous
        )
        require(object_facts(s3, buckets.values()) == before, "source_versions_changed")
        require(not uploads(s3, buckets.values()), "multipart_not_aborted")
        require(
            not any(
                row["key"] == slow_key for row in object_facts(s3, buckets.values())
            ),
            "partial_put_became_object",
        )
        disabled = rules(s3, buckets.values())
        require(
            disabled
            == {
                bucket: [{**rule, "Status": "Disabled"} for rule in value]
                for bucket, value in original_rules.items()
            },
            "rules_not_disabled",
        )
        first = next(row for row in before if row["kind"] == "Versions")
        signed_get = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": first["bucket"],
                "Key": first["key"],
                "VersionId": first["version_id"],
            },
            ExpiresIn=600,
        )
        full = request(signed_get)
        ranged = request(signed_get, headers={"Range": "bytes=0-1"})
        require(
            full[0] == 200 and hashlib.sha256(full[2]).hexdigest() == first["sha256"],
            "fenced_get_failed",
        )
        require(ranged[0] == 206 and ranged[2] == full[2][:2], "fenced_range_failed")
        before_copy = fence_cli(config, state, environment, "status")
        require(before_copy["held"] is True, "fence_lost_before_copy")
        summary["stage"] = "fenced_inventory_copy"
        summary.update(
            copy_while_held(
                owner, s3, target, target_endpoint, target_credentials, buckets, before
            )
        )
        after_copy = fence_cli(config, state, environment, "status")
        matching_generation(owner, source, state)
        require(
            after_copy["held"] is True
            and rules(s3, buckets.values()) == disabled
            and object_facts(s3, buckets.values()) == before,
            "fence_lost_during_copy",
        )
        wrong_cas = fence_cli(
            config,
            state,
            environment,
            "seal",
            revision=after_copy["revision"] - 1,
            operation=operation,
            expected=2,
        )
        require(wrong_cas.get("code") == "revision_changed", "wrong_cas_reason")
        status = fence_cli(config, state, environment, "status")
        summary["stage"] = "release"
        status = fence_cli(
            config,
            state,
            environment,
            "release",
            revision=status["revision"],
            operation=str(uuid.uuid4()),
        )
        require(
            status["held"] is False and rules(s3, buckets.values()) == original_rules,
            "release_did_not_restore_rules",
        )
        put_http(signed_put, b"new independent post-release probe")
        post_release = object_facts(s3, buckets.values())
        require(
            all(row in post_release for row in before),
            "release_changed_original_versions",
        )
        summary["stage"] = "configuration_drift"
        operation = str(uuid.uuid4())
        status = fence_cli(
            config,
            state,
            environment,
            "seal",
            revision=status["revision"],
            operation=operation,
        )
        require(status["held"] is True, "second_seal_failed")
        readonly = nginx.read_bytes()
        replace_config(nginx, readonly + b"\n# owned drift probe\n")
        try:
            fence_cli(config, state, environment, "status", expected=2)
        finally:
            replace_config(nginx, readonly)
        status = fence_cli(config, state, environment, "status")
        require(status["held"] is False, "drift_proof_not_invalidated")
        status = fence_cli(
            config,
            state,
            environment,
            "seal",
            revision=status["revision"],
            operation=operation,
        )
        require(
            status["held"] is True
            and object_facts(s3, buckets.values()) == post_release,
            "drift_resume_changed_source",
        )
        status = fence_cli(
            config,
            state,
            environment,
            "release",
            revision=status["revision"],
            operation=str(uuid.uuid4()),
        )
        require(
            status["held"] is False and rules(s3, buckets.values()) == original_rules,
            "final_release_failed",
        )
        summary.update(
            {
                "purpose_count": len(buckets),
                "source_versions": sum(row["kind"] == "Versions" for row in before),
                "delete_markers": sum(row["kind"] == "DeleteMarkers" for row in before),
                "aborted_multipart": aborted,
                "denied_write_paths": 5,
                "accepted_slow_put": True,
                "hard_exit_resumed": True,
                "source_generation_matched": True,
                "wrong_cas_rejected": True,
                "configuration_drift_rejected": True,
                "rules_restored": True,
                "source_facts_sha256": digest(before),
                "migration_ready": False,
                "stage": "complete",
            }
        )


def interrupted(_signum, _frame):
    raise KeyboardInterrupt


def failure_site(exc):
    lines = []
    frame = exc.__traceback__
    while frame is not None and len(lines) < 64:
        if (
            Path(frame.tb_frame.f_code.co_filename).resolve()
            == Path(__file__).resolve()
        ):
            lines.append(frame.tb_lineno)
        frame = frame.tb_next
    return {"type": type(exc).__name__, "lines": lines}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args(argv)
    owner = None
    summary = {
        "schema": SCHEMA,
        "completed": False,
        "cleanup_verified": False,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    exit_code = 1
    previous = {}
    try:
        private_parent(args.manifest)
        private_parent(args.summary)
        require(
            not args.summary.exists() and args.summary != args.manifest,
            "summary_exists",
        )
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, interrupted)
        owner = FixtureOwner(args.manifest, create=not args.cleanup)
        if not args.cleanup:
            run_fixture(owner, summary)
        summary["completed"] = True
        exit_code = 0
    except BaseException as exc:
        summary["error_code"] = safe_error(exc)
        response = getattr(exc, "response", {})
        provider_code = response.get("Error", {}).get("Code")
        if provider_code in {
            "NoSuchKey",
            "NoSuchVersion",
            "NoSuchBucket",
            "SignatureDoesNotMatch",
            "AccessDenied",
            "MethodNotAllowed",
            "InvalidAccessKeyId",
            "InternalError",
            "ServiceUnavailable",
            "InvalidArgument",
            "InvalidRequest",
            "400",
            "403",
            "404",
            "405",
            "500",
            "502",
            "503",
        }:
            summary["provider_error_code"] = provider_code
        http_status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if type(http_status) is int and 100 <= http_status <= 599:
            summary["provider_http_status"] = http_status
        summary["failure_site"] = failure_site(exc)
        exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        if owner is not None:
            try:
                summary["cleanup_verified"] = owner.cleanup()
                if summary["cleanup_verified"]:
                    owner.absent()
            except BaseException:
                summary["cleanup_verified"] = False
                summary["cleanup_error_code"] = "fixture_cleanup_failed"
            if not summary["cleanup_verified"]:
                exit_code = exit_code or 1
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        summary["exit_code"] = exit_code
        try:
            private_write(args.summary, canonical(summary))
        except Exception:
            summary["summary_saved"] = False
            exit_code = exit_code or 1
        print(json.dumps(summary, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
