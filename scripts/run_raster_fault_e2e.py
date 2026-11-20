#!/usr/bin/env python3
"""Run the Mask product family against a fresh, owned pre-consumer fault store.

The outer process owns the worktree mode lock and the MinIO administration
identity.  The inner process prepares the exact fixtures before starting any
product listener, then passes only the scoped data-plane identity to Playwright.
An interrupted or failed run retains its owner journal and resources.
"""

from __future__ import annotations

import asyncio
import builtins
from contextlib import closing
import ctypes
import hmac
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import stat
import subprocess
import tempfile
import time
import threading
import sys
import urllib.parse
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "apps/api"))

from fixture_store import (  # noqa: E402
    MINIO_IMAGE,
    _client,
    _empty_inventory,
    until_ready,
    verify_prerequisites,
)
from raster_consumer_process import run_product  # noqa: E402
from linux_pidfd import open_pidfd, pidfd_alive  # noqa: E402
from raster_web_process import RasterWebServer  # noqa: E402
from raster_images_process import (  # noqa: E402
    RasterImagesProcess,
    image_environment,
    image_role,
    make_credentials,
)
from raster_exclusive_provider import (  # noqa: E402
    _items,
    retire_postgres,
    retire_provider,
    verified_postgres,
    verify_exclusive_provider,
)
from storage_s3_contract import ContractError, require  # noqa: E402
from verify_storage_candidates import Owner, command, host_port, private_write  # noqa: E402
from worktree_env import load_identity, state_path, topology  # noqa: E402
from worktree_runtime import (  # noqa: E402
    environment_lock,
    parse_arguments,
    reject_orphans,
    run_processes,
)

_RETAINED_SEED_CLIENTS = []


class NativeApiObserver:
    """Observe the API actually owned and reaped by the fixture authority."""

    def __init__(self, record, port):
        require(
            isinstance(record, dict)
            and set(record) == {"pid", "start", "port"}
            and type(record["pid"]) is int
            and type(record["start"]) is int
            and record["port"] == port,
            "raster_api_identity_changed",
        )
        self.pid = record["pid"]
        self.descriptor = open_pidfd(self.pid)
        try:
            value = Path(f"/proc/{self.pid}/stat").read_text()
            require(
                pidfd_alive(self.descriptor)
                and int(value[value.rfind(")") + 2 :].split()[19]) == record["start"],
                "raster_api_identity_changed",
            )
        except BaseException:
            os.close(self.descriptor)
            raise

    def poll(self):
        return None if pidfd_alive(self.descriptor) else 1

    def close(self):
        os.close(self.descriptor)


def seed_operation(process, owner, payload):
    """Keep the operation identity durable until its actual result is received."""
    operation_id = str(uuid.uuid4())
    owner.data["raster"]["seed_operation"] = {
        "operation_id": operation_id,
        "operation": payload.get("operation", payload.get("path")),
        "state": "pending",
    }
    owner.save()
    reply = None
    acknowledged = False
    try:
        process.stdin.write(
            json.dumps(
                {
                    "action": "execute",
                    "operation_id": operation_id,
                    "payload": payload,
                }
            )
            + "\n"
        )
        process.stdin.flush()
        line = process.stdout.readline()
        require(bool(line), "raster_seed_process_exited")
        reply = json.loads(line)
        require(
            reply.get("operation_id") == operation_id, "raster_seed_response_changed"
        )
        require(reply.get("state") != "pending", "raster_seed_operation_pending")
        require(
            (
                isinstance(reply.get("owner_error"), str)
                and reply["owner_error"].isidentifier()
                if "owner_error" in reply
                else (
                    type(reply.get("status")) is int
                    and isinstance(reply.get("body"), str)
                )
            )
            and (
                "journal_error" not in reply
                or (
                    isinstance(reply["journal_error"], str)
                    and reply["journal_error"].isidentifier()
                )
            )
            and (
                "disposal_error" not in reply
                or (
                    isinstance(reply["disposal_error"], str)
                    and reply["disposal_error"].isidentifier()
                )
            )
            and (
                "operation_response" not in reply
                or (
                    isinstance(reply["operation_response"], dict)
                    and type(reply["operation_response"].get("status")) is int
                    and isinstance(reply["operation_response"].get("body"), str)
                )
            ),
            "raster_seed_response_invalid",
        )
        if reply.get("owner_error") in {
            "OperationOutcomeUnresolved",
            "OperationIdentityConflict",
        }:
            raise ContractError(f"raster_seed_{reply['owner_error']}")
        known = reply.get("operation_response", reply)
        if known.get("status") is not None and (
            known["status"] != 200 or reply.get("disposal_error")
        ):
            owner.data["raster"].setdefault("seed_failures", []).append(
                {
                    "operation_id": operation_id,
                    "operation": payload.get("operation", payload.get("path")),
                    "status": known["status"],
                    "body": known["body"],
                    "disposal_error": reply.get("disposal_error"),
                }
            )
            try:
                owner.save()
            except BaseException as error:
                causes = [ContractError(f"raster_seed_http_{known['status']}")]
                if reply.get("disposal_error"):
                    causes.append(
                        ContractError(f"raster_seed_disposal_{reply['disposal_error']}")
                    )
                causes.append(error)
                raise builtins.BaseExceptionGroup(
                    "raster seed and evidence failed", causes
                ) from None
        process.stdin.write(
            json.dumps({"action": "ack", "operation_id": operation_id}) + "\n"
        )
        process.stdin.flush()
        receipt_line = process.stdout.readline()
        require(bool(receipt_line), "raster_seed_receipt_missing")
        receipt = json.loads(receipt_line)
        require(
            receipt == {"operation_id": operation_id, "state": "acknowledged"},
            "raster_seed_receipt_unconfirmed",
        )
        acknowledged = True
        owner.data["raster"]["seed_operation"]["state"] = "acknowledged"
        owner.save()
        errors = []
        if reply.get("operation_response"):
            errors.append(ContractError(f"raster_seed_http_{known['status']}"))
        if reply.get("owner_error") and reply["owner_error"] != reply.get(
            "disposal_error"
        ):
            errors.append(ContractError(f"raster_seed_{reply['owner_error']}"))
        if reply.get("disposal_error"):
            errors.append(
                ContractError(f"raster_seed_disposal_{reply['disposal_error']}")
            )
        if not errors and reply.get("status") != 200:
            errors.append(ContractError(f"raster_seed_http_{reply['status']}"))
        if reply.get("journal_error"):
            errors.append(
                ContractError(f"raster_seed_journal_{reply['journal_error']}")
            )
        if len(errors) > 1:
            raise builtins.BaseExceptionGroup(
                "raster seed and authority evidence failed", errors
            )
        if errors:
            raise errors[0]
        return json.loads(reply["body"])
    except BaseException as error:
        if reply is not None:
            error.fixture_response = reply
            error.add_note(
                "Fixture response was received"
                + (
                    " before acknowledgement became uncertain"
                    if not acknowledged
                    else ""
                )
            )
        error.fixture_operation_id = operation_id
        error.add_note(f"Fixture operation ID: {operation_id}")
        raise


def seed_once(owner, environment, payload):
    process = subprocess.Popen(
        [
            str(ROOT / "apps/api/.venv/bin/python"),
            str(ROOT / "apps/web/e2e/fixtures/seed-request.py"),
        ],
        cwd=ROOT / "apps/api",
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        result = seed_operation(process, owner, payload)
    except BaseException:
        _RETAINED_SEED_CLIENTS.append(process)
        raise
    process.stdin.close()
    require(process.wait() == 0, "raster_seed_process_failed")
    process.stdout.close()
    return result


class NativePhaseControl:
    """The outer owner exposes only Raster's authenticated phase transitions."""

    def __init__(self, inference, resources):
        self.inference = inference
        self.resources = resources
        self.failure = None
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.directory = Path(tempfile.mkdtemp(prefix="aap-raster-native-"))
        self.socket_path = self.directory / "phase.sock"
        self.directory_identity = self.directory.stat()
        control = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    credentials = self.request.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, 12
                    )
                    import struct

                    _, uid, _ = struct.unpack("3i", credentials)
                    request = json.loads(self.rfile.readline())
                    require(uid == os.getuid(), "raster_phase_peer_changed")
                    require(
                        set(request) == {"token", "action"}
                        and hmac.compare_digest(request["token"], control.token),
                        "raster_phase_authentication_failed",
                    )
                    with control.lock:
                        action = request["action"]
                        if action == "pause":
                            control.inference.pause()
                            require(
                                control.inference.process_identity_inventory() == [],
                                "raster_native_pause_unconfirmed",
                            )
                            result = {}
                        elif action == "start":
                            control.inference.start()
                            result = control.inference.verified_selection(
                                control.resources, code_root=ROOT
                            )
                        elif action == "inventory":
                            result = control.inference.process_identity_inventory()
                        else:
                            raise ContractError("raster_phase_action_invalid")
                    response = {"result": result}
                except BaseException as error:
                    control.failure = error
                    response = {"error": type(error).__name__}
                self.wfile.write(json.dumps(response).encode() + b"\n")
                self.wfile.flush()

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            block_on_close = False

        self.server = Server(str(self.socket_path), Handler)
        os.chmod(self.socket_path, 0o600)
        self.socket_identity = self.socket_path.stat()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        text = Path(f"/proc/{os.getpid()}/stat").read_text()
        self.connection = {
            "socket": str(self.socket_path),
            "token": self.token,
            "pid": os.getpid(),
            "start": int(text[text.rfind(")") + 2 :].split()[19]),
            "socket_identity": [
                self.socket_identity.st_dev,
                self.socket_identity.st_ino,
            ],
        }

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        socket_info = self.socket_path.lstat()
        directory_info = self.directory.lstat()
        require(
            stat.S_ISSOCK(socket_info.st_mode)
            and (socket_info.st_dev, socket_info.st_ino)
            == (self.socket_identity.st_dev, self.socket_identity.st_ino)
            and (directory_info.st_dev, directory_info.st_ino)
            == (self.directory_identity.st_dev, self.directory_identity.st_ino),
            "raster_phase_socket_changed",
        )
        self.socket_path.unlink()
        self.directory.rmdir()


class NativePhaseClient:
    def __init__(self, record):
        require(
            isinstance(record, dict)
            and set(record) == {"socket", "token", "pid", "start", "socket_identity"},
            "raster_phase_record_invalid",
        )
        self.record = record

    def request(self, action):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(self.record["socket"])
            import struct

            pid, uid, _ = struct.unpack(
                "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            info = Path(self.record["socket"]).lstat()
            text = Path(f"/proc/{pid}/stat").read_text()
            require(
                pid == self.record["pid"]
                and uid == os.getuid()
                and int(text[text.rfind(")") + 2 :].split()[19]) == self.record["start"]
                and stat.S_ISSOCK(info.st_mode)
                and info.st_uid == uid
                and stat.S_IMODE(info.st_mode) == 0o600
                and [info.st_dev, info.st_ino] == self.record["socket_identity"],
                "raster_phase_owner_changed",
            )
            with connection.makefile("rwb") as stream:
                stream.write(
                    json.dumps(
                        {"token": self.record["token"], "action": action}
                    ).encode()
                    + b"\n"
                )
                stream.flush()
                line = stream.readline()
                require(bool(line), "raster_phase_owner_disconnected")
                result = json.loads(line)
                require("error" not in result, "raster_native_phase_failed")
                return result["result"]

    def pause(self):
        return self.request("pause")

    def start(self):
        return self.request("start")


def provision(resources, owner):
    """Create an empty server and the versioned annotations bucket at birth."""
    verify_prerequisites()
    root_access, root_secret = (
        "raster" + secrets.token_hex(12),
        secrets.token_urlsafe(32),
    )
    access, secret = "runtime" + secrets.token_hex(12), secrets.token_urlsafe(32)
    env_file = owner.private / "minio.env"
    private_write(
        env_file,
        f"MINIO_ROOT_USER={root_access}\nMINIO_ROOT_PASSWORD={root_secret}\n".encode(),
    )
    private_write(
        owner.private / "runtime.json",
        json.dumps({"access_key": access, "secret_key": secret}).encode(),
    )
    owner.data["raster"] = {"state": "creating", "resources": resources}
    images = make_credentials(owner)
    owner.data["raster"]["images"] = {
        "state": "credentials_created",
        "database_role": images["database_role"],
        "s3_user": images["access_key"],
        "s3_policy": "raster-images-" + owner.data["nonce"],
    }
    owner.save()
    volume = owner.create("volume")
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
        "raster_provider_birth_unconfirmed",
    )
    owner.data["raster"]["provider_birth"] = {
        "container_id": container["id"],
        "pid": process["Pid"],
        "started_at": process["StartedAt"],
    }
    owner.save()
    endpoint = f"127.0.0.1:{host_port(owner, container, 9000)}"
    bucket = resources["buckets"]["MINIO_BUCKET"]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetBucketLocation",
                    "s3:ListBucket",
                    "s3:ListBucketMultipartUploads",
                    "s3:GetBucketVersioning",
                ],
                "Resource": [
                    f"arn:aws:s3:::{name}" for name in resources["buckets"].values()
                ],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:AbortMultipartUpload",
                    "s3:ListMultipartUploadParts",
                ],
                "Resource": [
                    f"arn:aws:s3:::{name}/*" for name in resources["buckets"].values()
                ],
            },
            {
                "Effect": "Deny",
                "Action": ["s3:DeleteObjectVersion"],
                "Resource": [
                    f"arn:aws:s3:::{name}/*" for name in resources["buckets"].values()
                ],
            },
        ],
    }
    policy_file = owner.private / "runtime-policy.json"
    private_write(policy_file, json.dumps(policy).encode())
    mc_env = {
        "PATH": os.environ["PATH"],
        "MC_CONFIG_DIR": str(owner.private / "mc"),
        "MC_HOST_raster": "http://"
        + urllib.parse.quote(root_access, safe="")
        + ":"
        + urllib.parse.quote(root_secret, safe="")
        + "@"
        + endpoint,
    }
    with closing(_client("http://" + endpoint, root_access, root_secret)) as client:
        until_ready(client)
        require(
            not client.list_buckets().get("Buckets"), "raster_store_not_empty_at_birth"
        )
        owner.data["raster"].update(
            {
                "endpoint": endpoint,
                "bucket": bucket,
                "container_id": container["id"],
                "network_id": network["id"],
                "network_name": network["name"],
                "volume_id": volume["id"],
                "initial_bucket_count": 0,
                "state": "bucket_creation_intent",
            }
        )
        owner.save()
        client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        client.put_bucket_tagging(
            Bucket=bucket,
            Tagging={
                "TagSet": [{"Key": "yoao-worktree-owner", "Value": resources["owner"]}]
            },
        )
        require(
            client.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled",
            "raster_versioning_missing",
        )
        _empty_inventory(client, bucket)
        command(
            ["mc", "admin", "user", "add", "raster"],
            input_data=f"{access}\n{secret}\n".encode(),
            env=mc_env,
        )
        policy_name = "raster-runtime-" + owner.data["nonce"]
        command(
            [
                "mc",
                "admin",
                "policy",
                "create",
                "raster",
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
                "raster",
                policy_name,
                "--user",
                access,
            ],
            env=mc_env,
        )
        from verify_module_data_boundary import _image_worker_storage_policy

        buckets = resources["buckets"]
        image_policy = _image_worker_storage_policy(
            {
                "bucket": buckets["MINIO_MEDIA_CACHE_BUCKET"],
                "datasets_bucket": buckets["MINIO_DATASETS_BUCKET"],
                "legacy_bucket": buckets["MINIO_BUCKET"],
                "inputs_bucket": buckets["MINIO_INPUTS_BUCKET"],
            }
        )
        image_policy_file = owner.private / "images-policy.json"
        private_write(image_policy_file, json.dumps(image_policy).encode())
        owner.data["raster"]["images"]["state"] = "s3_creation_intent"
        owner.save()
        command(
            ["mc", "admin", "user", "add", "raster"],
            input_data=f"{images['access_key']}\n{images['secret_key']}\n".encode(),
            env=mc_env,
        )
        command(
            [
                "mc",
                "admin",
                "policy",
                "create",
                "raster",
                owner.data["raster"]["images"]["s3_policy"],
                str(image_policy_file),
            ],
            env=mc_env,
        )
        command(
            [
                "mc",
                "admin",
                "policy",
                "attach",
                "raster",
                owner.data["raster"]["images"]["s3_policy"],
                "--user",
                images["access_key"],
            ],
            env=mc_env,
        )
        owner.data["raster"]["images"]["state"] = "s3_ready"
        owner.save()
        from botocore.exceptions import ClientError

        canary = "raster-canary-" + owner.data["nonce"]
        client.create_bucket(Bucket=canary)
        with closing(_client("http://" + endpoint, access, secret)) as limited:
            try:
                limited.head_bucket(Bucket=canary)
            except ClientError as exc:
                require(
                    exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    == 403,
                    "raster_runtime_scope_unproven",
                )
            else:
                raise ValueError("raster_runtime_scope_unproven")
        client.delete_bucket(Bucket=canary)
        owner.data["raster"]["runtime_canary_denied"] = True
        owner.save()
    owner.data["raster"].update(
        {"state": "born", "versioning": "Enabled", "runtime_policy": policy_name}
    )
    owner.save()
    return endpoint, root_access, root_secret


def provision_postgres(resources, owner):
    """Create this run's PostgreSQL under the existing durable Docker owner."""
    from sqlalchemy.engine import URL
    from verify_module_data_boundary import POSTGRES_IMAGE

    password = secrets.token_urlsafe(32)
    env_file = owner.private / "postgres.env"
    private_write(
        env_file,
        f"POSTGRES_USER=owner\nPOSTGRES_PASSWORD={password}\nPOSTGRES_DB=postgres\n".encode(),
    )
    volume = owner.create("volume")
    container = owner.container(
        POSTGRES_IMAGE,
        args=(
            "--pull=never",
            "--publish",
            "127.0.0.1::5432",
            "--env-file",
            str(env_file),
            "--volume",
            volume["id"] + ":/var/lib/postgresql/data",
        ),
    )
    owner.start(container)
    observed = owner.verified(container)
    state = observed.get("State") or {}
    port = host_port(owner, container, 5432)
    owner.data["raster"]["postgres"] = {
        "container_id": container["id"],
        "volume_id": volume["id"],
        "pid": state.get("Pid"),
        "image_id": observed.get("Image"),
        "started_at": state.get("StartedAt"),
        "port": port,
    }
    owner.save()
    verified_postgres(owner, resources, port=port)
    deadline = time.monotonic() + 30
    while True:
        verified_postgres(owner, resources, port=port)
        probe = subprocess.run(
            [
                "docker",
                "exec",
                container["id"],
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-p",
                "5432",
                "-U",
                "owner",
                "-d",
                "postgres",
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if probe.returncode == 0:
            break
        require(time.monotonic() < deadline, "raster_postgres_not_ready")
        time.sleep(0.2)
    return URL.create(
        "postgresql+asyncpg",
        username="owner",
        password=password,
        host="127.0.0.1",
        port=port,
        database="postgres",
    ).render_as_string(hide_password=False)


def request_fixtures(owner, environment, suite, attempts):
    """Use the real in-process fixture router, never a listening API."""
    process = subprocess.Popen(
        [
            str(ROOT / "apps/api/.venv/bin/python"),
            str(ROOT / "apps/web/e2e/fixtures/seed-request.py"),
        ],
        cwd=ROOT / "apps/api",
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    def post(payload):
        return seed_operation(process, owner, payload)

    cases, references = {"diagnosis": [], "retry": []}, []
    owner.data["raster"].update(
        {"state": "suite_creation_intent", "suite_id": suite, "cases": []}
    )
    owner.save()
    failure = None
    try:
        post(
            {
                "operation": "prepare_fixture_suite",
                "data": {"suite_id": suite, "family": "owned"},
            }
        )
        for case in cases:
            for _ in range(attempts):
                namespace, run_id = secrets.token_hex(6), str(uuid.uuid4())
                prepared = {
                    "case": case,
                    "namespace": namespace,
                    "run_id": run_id,
                    "state": "seed_intent",
                }
                owner.data["raster"]["cases"].append(prepared)
                owner.save()
                seed = post(
                    {
                        "path": "/api/v1/__test/seed/owned",
                        "data": {
                            "namespace": namespace,
                            "suite_id": suite,
                            "run_id": run_id,
                            "image_canvas": "default",
                        },
                    }
                )
                prepared.update({"state": "seeded", "seed": seed})
                owner.save()
                task_id = seed["task_ids"][0]
                masks = []
                for variant in ("single", "fault_source"):
                    prepared["state"] = variant + "_intent"
                    owner.save()
                    masks.append(
                        post(
                            {
                                "path": "/api/v1/__test/seed/inject-raster-mask",
                                "data": {
                                    "run_id": run_id,
                                    "task_id": task_id,
                                    "user_email": seed["annotator_email"],
                                    "variant": variant,
                                    "canvas": "default",
                                },
                            }
                        )
                    )
                    prepared[variant] = masks[-1]
                    prepared["state"] = variant + "_published"
                    owner.save()
                require(
                    masks[0]["mask"]["object_key"] != masks[1]["mask"]["object_key"],
                    "raster_fault_shares_healthy_content",
                )
                cases[case].append(
                    {
                        "namespace": namespace,
                        "run_id": run_id,
                        "seed": seed,
                        "task_id": task_id,
                        "healthy_annotation_id": masks[0]["annotation_id"],
                        "corrupt_annotation_id": masks[1]["annotation_id"],
                    }
                )
                references.append((masks[0]["mask"], masks[1]["mask"]))
        fenced = post(
            {"operation": "owner_offline_seal", "require_fixture_inference": True}
        )
    except BaseException as error:
        failure = error
        raise
    finally:
        if failure is not None:
            _RETAINED_SEED_CLIENTS.append(process)
        else:
            process.stdin.close()
            status = process.wait()
            process.stdout.close()
            require(status == 0, "raster_seed_process_failed")
    return cases, references, fenced


async def inspect_records(environment, references, suite, prepared_cases):
    """Resolve the durable physical owners, and reject any unsettled work."""
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from app.db.models.raster_mask_object import RasterMaskObject
    from app.services import raster_object_storage as physical
    from app.services.raster_mask_references import (
        _MASK_REFERENCE_QUERIES,
        referenced_keys,
    )
    from fixture_suite_coordinator import _verified_database

    owner_url, resources, identity = await _verified_database(environment)
    engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
    keys = {ref["object_key"] for pair in references for ref in pair}
    healthy_keys = {pair[0]["object_key"] for pair in references}
    fault_keys = {pair[1]["object_key"] for pair in references}
    require(
        len(fault_keys) == len(references)
        and not healthy_keys.intersection(fault_keys),
        "raster_fault_key_shared",
    )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            for query in (
                "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend')",
                "SELECT EXISTS(SELECT 1 FROM public.storage_operations)",
                "SELECT EXISTS(SELECT 1 FROM public.storage_usage_mutations WHERE state <> 'confirmed')",
                "SELECT EXISTS(SELECT 1 FROM public.raster_mask_publications)",
                "SELECT EXISTS(SELECT 1 FROM public.raster_mask_objects WHERE state <> 'ready')",
                "SELECT EXISTS(SELECT 1 FROM yoao_fixture_v1.fixture_run_admissions WHERE state <> 'settled')",
            ):
                require(
                    not await db.scalar(text(query)), "raster_preparation_not_settled"
                )
            runs = (
                await db.execute(
                    text(
                        "SELECT id, parent_run_id, state FROM yoao_fixture_v1.fixture_runs WHERE state <> 'retired'"
                    )
                )
            ).all()
            require(
                len(runs) == len(references) + 1
                and sum(
                    str(row.id) == suite
                    and row.parent_run_id is None
                    and row.state == "open"
                    for row in runs
                )
                == 1
                and all(
                    str(row.id) == suite
                    or (str(row.parent_run_id) == suite and row.state == "open")
                    for row in runs
                ),
                "raster_suite_not_exclusive",
            )
            require(
                await referenced_keys(db) == keys, "raster_reference_inventory_changed"
            )
            expected_faults = {
                item["fault_source"]["annotation_id"]: item["fault_source"]["mask"][
                    "object_key"
                ]
                for item in prepared_cases
            }
            fault_rows = (
                await db.execute(
                    text(
                        "SELECT id, geometry->'mask'->>'object_key' FROM annotations "
                        "WHERE is_active IS TRUE AND geometry->'mask'->>'object_key' = ANY(:keys)"
                    ),
                    {"keys": list(fault_keys)},
                )
            ).all()
            require(
                {str(row[0]): row[1] for row in fault_rows} == expected_faults
                and len(fault_rows) == len(expected_faults),
                "raster_fault_annotation_not_exclusive",
            )
            for index, query in enumerate(_MASK_REFERENCE_QUERIES):
                if index != 2:
                    other_keys = set((await db.execute(text(query))).scalars())
                    require(
                        not fault_keys.intersection(other_keys),
                        "raster_fault_other_reference",
                    )
            rows = list(await db.scalars(select(RasterMaskObject)))
            require(
                len(rows) == len(keys) and {row.logical_key for row in rows} == keys,
                "raster_object_inventory_changed",
            )
            records = {}
            for row in rows:
                target = physical.resolve(row)
                require(
                    target.bucket == resources["buckets"]["MINIO_BUCKET"]
                    and target.ref["endpoint_url"]
                    == "http://" + environment["MINIO_ENDPOINT"],
                    "raster_target_changed",
                )
                records[row.logical_key] = {
                    "id": str(row.id),
                    "key": target.object_key,
                    "ref": target.ref,
                    "stored_sha256": row.stored_sha256,
                    "stored_size_bytes": row.stored_size_bytes,
                    "reference": physical.reference(row),
                }
            return resources, identity, records
    finally:
        await engine.dispose()


def verify_store_physical(owner):
    store = owner.data["raster"]
    items = _items(owner, store["resources"])
    container, network, volume = (
        items[name] for name in ("container", "network", "volume")
    )
    observed = owner.verified(container)
    observed_network = owner.verified(network)
    owner.verified(volume)
    require(
        observed.get("Config", {}).get("Image") == MINIO_IMAGE
        and observed.get("State", {}).get("Running") is True
        and any(
            mount.get("Type") == "volume"
            and mount.get("Name") == store["volume_id"]
            and mount.get("Destination") == "/data"
            for mount in observed.get("Mounts", [])
        )
        and observed_network.get("Driver") == "bridge"
        and observed_network.get("Name") == store["network_name"]
        and set(observed_network.get("Containers", {})) == {store["container_id"]}
        and store["endpoint"] == f"127.0.0.1:{host_port(owner, container, 9000)}",
        "raster_store_physical_identity_changed",
    )
    return observed_network


def fixture_provider_host(owner):
    """Advertise the exact verified private bridge, not a machine hostname."""
    network = verify_store_physical(owner)
    configs = network.get("IPAM", {}).get("Config")
    require(
        isinstance(configs, list)
        and len(configs) == 1
        and isinstance(configs[0], dict),
        "raster_provider_bridge_invalid",
    )
    gateway = ipaddress.ip_address(configs[0]["Gateway"])
    subnet = ipaddress.ip_network(configs[0]["Subnet"])
    require(
        gateway.version == 4
        and gateway in subnet
        and not gateway.is_loopback
        and not gateway.is_unspecified,
        "raster_provider_bridge_invalid",
    )
    return str(gateway)


def require_broker_idle(queue, paused_run_ids):
    """Keep passive bindings and sealed workers' expiring heartbeat metadata."""
    heartbeats = [
        f"celery:hb:aap-fixture-{uuid.UUID(run_id).hex}-{kind}@127.0.0.1"
        for run_id in paused_run_ids
        for kind in ("media", "pyramid")
    ]
    blocked = queue.eval_ro(
        """
        local bindings = '_kombu.binding.'
        local heartbeats = {}
        for _, key in ipairs(ARGV) do heartbeats[key] = true end
        for _, key in ipairs(redis.call('KEYS', '*')) do
            local kind = redis.call('TYPE', key).ok
            local passive = kind == 'set'
                and string.sub(key, 1, string.len(bindings)) == bindings
            if not passive and heartbeats[key] and kind == 'string' then
                passive = redis.call('PTTL', key) > 0
                    and string.match(redis.call('GET', key), '^%d+$') ~= nil
            end
            if not passive then
                local size = -1
                if kind == 'list' then size = redis.call('LLEN', key)
                elseif kind == 'hash' then size = redis.call('HLEN', key)
                elseif kind == 'zset' then size = redis.call('ZCARD', key)
                elseif kind == 'string' then size = redis.call('STRLEN', key)
                elseif kind == 'set' then size = redis.call('SCARD', key)
                elseif kind == 'stream' then size = redis.call('XLEN', key)
                end
                return {key, kind, size, redis.call('PTTL', key)}
            end
        end
        return {}
        """,
        0,
        *heartbeats,
    )
    if blocked:
        error = ContractError("raster_queue_not_empty")
        error.broker_observation = {
            "key_hex": blocked[0].hex(),
            "type": blocked[1].decode("ascii"),
            "size": blocked[2],
            "remaining_ttl_ms": blocked[3],
        }
        raise error


def exercise_faults(owner, environment, references, suite):
    from botocore.exceptions import ClientError
    from app.services import raster_object_storage as physical
    import redis

    resources, identity, records = asyncio.run(
        inspect_records(environment, references, suite, owner.data["raster"]["cases"])
    )
    store = owner.data["raster"]
    require(
        resources == store["resources"]
        and environment["MINIO_ENDPOINT"] == store["endpoint"],
        "raster_store_identity_changed",
    )
    verify_store_physical(owner)
    with redis.Redis.from_url(environment["REDIS_URL"], socket_timeout=2) as queue:
        require_broker_idle(
            queue, (case["run_id"] for case in owner.data["raster"]["cases"])
        )
    with closing(
        _client(
            "http://" + store["endpoint"],
            environment["MINIO_ACCESS_KEY"],
            environment["MINIO_SECRET_KEY"],
        )
    ) as client:
        require(
            client.get_bucket_versioning(Bucket=store["bucket"]).get("Status")
            == "Enabled",
            "raster_versioning_changed",
        )
        versions = []
        for page in client.get_paginator("list_object_versions").paginate(
            Bucket=store["bucket"]
        ):
            require(not page.get("DeleteMarkers"), "raster_delete_marker_present")
            versions.extend(page.get("Versions", []))
        require(
            len(versions) == len(records)
            and {v["Key"] for v in versions} == {v["key"] for v in records.values()}
            and all(
                v["IsLatest"] and v["VersionId"] not in ("", "null") for v in versions
            ),
            "raster_version_inventory_changed",
        )
        require(
            not client.list_multipart_uploads(Bucket=store["bucket"]).get("Uploads"),
            "raster_upload_pending",
        )
        by_key = {v["Key"]: v for v in versions}
        for logical, record in records.items():
            version = by_key[record["key"]]["VersionId"]
            response = client.get_object(
                Bucket=store["bucket"], Key=record["key"], VersionId=version
            )
            with closing(response["Body"]) as body:
                data = body.read(record["stored_size_bytes"] + 1)
            require(
                len(data) == record["stored_size_bytes"]
                and hashlib.sha256(data).hexdigest() == record["stored_sha256"],
                "raster_object_bytes_changed",
            )
            require(
                physical.content(record["reference"], data)["logical_key"] == logical,
                "raster_object_content_invalid",
            )
            record["version_id"] = version
            record["etag"] = response["ETag"]
        owner.data["raster"].update(
            {
                "state": "fault_intent",
                "database": identity,
                "suite_id": suite,
                "objects": records,
                "faults": [],
            }
        )
        owner.save()
        for healthy, corrupt in references:
            bad, good = records[corrupt["object_key"]], records[healthy["object_key"]]
            fault = {
                "object_id": bad["id"],
                "key": bad["key"],
                "version_id": bad["version_id"],
                "state": "deletion_intent",
            }
            owner.data["raster"]["faults"].append(fault)
            owner.save()
            result = client.delete_object(
                Bucket=store["bucket"], Key=bad["key"], VersionId=bad["version_id"]
            )
            fault.update(
                {
                    "state": "delete_result",
                    "response_version_id": result.get("VersionId"),
                    "response_status": result.get("ResponseMetadata", {}).get(
                        "HTTPStatusCode"
                    ),
                }
            )
            owner.save()
            require(
                fault["response_version_id"] == bad["version_id"]
                and fault["response_status"] in (200, 204),
                "raster_delete_unconfirmed",
            )
            fault["state"] = "deleted"
            owner.save()
            try:
                client.head_object(Bucket=store["bucket"], Key=bad["key"])
            except ClientError as exc:
                require(
                    exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    == 404,
                    "raster_fault_not_missing",
                )
            else:
                raise ValueError("raster_fault_not_missing")
            response = client.get_object(
                Bucket=store["bucket"], Key=good["key"], VersionId=good["version_id"]
            )
            with closing(response["Body"]) as body:
                data = body.read(good["stored_size_bytes"] + 1)
            require(
                len(data) == good["stored_size_bytes"]
                and hashlib.sha256(data).hexdigest() == good["stored_sha256"]
                and response["ETag"] == good["etag"],
                "raster_healthy_neighbor_changed",
            )
            fault["state"] = "verified"
            owner.save()
    owner.data["raster"]["state"] = "faults_verified"
    owner.save()


def require_no_children():
    # Linux subreaper adoption exposes detached Playwright descendants too.
    outcomes = []
    live = False
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            live = True
            break
        outcomes.append({"pid": pid, "status": status})
    if live or any(
        not os.WIFEXITED(item["status"]) or os.WEXITSTATUS(item["status"]) != 0
        for item in outcomes
    ):
        error = ContractError("raster_consumer_descendants_unsettled")
        error.adopted_outcomes = outcomes
        error.children_live = live
        raise error


def archive_closed_native_authority(environment, resources, suite):
    """Move only the closed Raster owner's exact Native evidence into its suite."""
    from fixture_native_business import _backend, _private_bytes, _private_json
    from fixture_native_playwright import _verify_run_receipts

    identifier = uuid.UUID(suite)
    require(str(identifier) == suite, "raster_native_suite_changed")
    root = state_path(ROOT, "e2e", "fixture-authority")
    backend = _backend(environment, resources)
    try:
        with backend.fixture_authority_fence():
            require(
                not os.path.lexists(root / "owner.json"),
                "raster_fixture_owner_still_present",
            )
            generation_path = root / "generation.json"
            generation = _private_json(generation_path)
            require(
                set(generation) == {"resources", "nonce", "state"}
                and generation["resources"] == resources
                and generation["state"] == "closed",
                "raster_fixture_generation_changed",
            )
            generation_bytes = _private_bytes(generation_path)
            supervisor = _private_json(root / "native-supervisor.json")
            require(
                supervisor.get("suite_id") == suite
                and supervisor.get("resources") == resources
                and supervisor.get("state") == "retired"
                and isinstance(supervisor.get("native_run_ids"), list),
                "raster_native_supervisor_unsettled",
            )
            _verify_run_receipts(root, identifier, supervisor["native_run_ids"])
            evidence = sorted(
                (path for path in root.iterdir() if path.name.startswith("native-")),
                key=lambda path: path.name,
            )
            require(
                any(path.name == "native-supervisor.json" for path in evidence),
                "raster_native_supervisor_unsettled",
            )
            for path in evidence:
                info = path.lstat()
                require(
                    info.st_uid == os.geteuid()
                    and (
                        (
                            stat.S_ISREG(info.st_mode)
                            and stat.S_IMODE(info.st_mode) == 0o600
                            and info.st_nlink == 1
                        )
                        or (
                            stat.S_ISDIR(info.st_mode)
                            and stat.S_IMODE(info.st_mode) == 0o700
                        )
                    ),
                    "raster_native_evidence_changed",
                )
            rename = ctypes.CDLL(None, use_errno=True).renameat2
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            rename.restype = ctypes.c_int
            destination = root / ("suite-" + identifier.hex)
            destination.mkdir(mode=0o700)
            private_write(destination / "generation.json", generation_bytes)
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            destination_fd = os.open(
                destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.fsync(destination_fd)
                os.fsync(root_fd)
                ordered = [
                    path for path in evidence if path.name != "native-supervisor.json"
                ]
                ordered.append(root / "native-supervisor.json")
                for source in ordered:
                    if (
                        rename(
                            root_fd,
                            os.fsencode(source.name),
                            destination_fd,
                            os.fsencode(source.name),
                            1,
                        )
                        != 0
                    ):
                        error = ctypes.get_errno()
                        raise OSError(error, os.strerror(error))
                    os.fsync(destination_fd)
                    os.fsync(root_fd)
            finally:
                os.close(destination_fd)
                os.close(root_fd)
    finally:
        backend.s3.close()


def require_only_fixture_owner(fenced):
    """Prove the only surviving descendants are the sealed original owners."""
    record_path = state_path(ROOT, "e2e", "fixture-authority", "owner.json")
    descriptor = os.open(record_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        current = record_path.lstat()
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
            "raster_fixture_owner_record_invalid",
        )
        with os.fdopen(descriptor) as stream:
            descriptor = -1
            record = json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    def start_time(pid):
        text = Path(f"/proc/{pid}/stat").read_text()
        return int(text[text.rfind(")") + 2 :].split()[19])

    def children(pid):
        result = set()
        for task in Path(f"/proc/{pid}/task").iterdir():
            result.update(
                int(value) for value in (task / "children").read_text().split()
            )
        return result

    owner_pid = record["pid"]
    providers = fenced["providers"]
    require(
        record["resources"] == topology(ROOT, load_identity(ROOT), "e2e")
        and record["code_root"] == str(ROOT)
        and owner_pid == fenced["pid"]
        and record["start"] == fenced["start"]
        and record["nonce"] == fenced["nonce"]
        and record.get("seal_secret") == fenced["seal_secret"]
        and start_time(owner_pid) == record["start"],
        "raster_fixture_owner_identity_changed",
    )
    if fenced["seal_secret"] is not None:
        offline = record.get("offline_seal")
        require(
            isinstance(offline, dict)
            and offline.get("phase") == "sealed"
            and offline.get("engine_disposed") is True
            and offline.get("requires_native") is True
            and offline.get("native_runs_paused") is True
            and offline.get("native_control_paused") is True
            and offline.get("native_runs_resumed") is False
            and offline.get("native_control_resumed") is False
            and len(offline.get("providers", [])) == len(providers)
            and [
                {"pid": item["pid"], "start": item["start"]}
                for item in offline.get("providers", [])
                if item.get("sealed") and not item.get("resumed")
            ]
            == providers,
            "raster_fixture_seal_incomplete",
        )
    expected = {entry["pid"] for entry in providers}
    require(len(expected) == len(providers), "raster_fixture_provider_identity_changed")
    require(children(os.getpid()) == {owner_pid}, "raster_unknown_descendants")
    require(children(owner_pid) == expected, "raster_unknown_fixture_descendants")
    for entry in providers:
        require(
            start_time(entry["pid"]) == entry["start"] and not children(entry["pid"]),
            "raster_fixture_provider_identity_changed",
        )


def configured_attempts(arguments):
    retries = 1 if os.environ.get("CI") else 0
    for index, argument in enumerate(arguments):
        if argument == "--retries":
            require(index + 1 < len(arguments), "raster_retry_count_missing")
            retries = int(arguments[index + 1])
        elif argument.startswith("--retries="):
            retries = int(argument.split("=", 1)[1])
    require(retries >= 0, "raster_retry_count_invalid")
    return retries + 1


def inside(path, arguments):
    # Adopt every orphaned descendant before launching any owned process.
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.prctl(36, 1, 0, 0, 0) == 0, "raster_subreaper_unavailable")

    identity = load_identity(ROOT, create=False)
    resources = topology(ROOT, identity, "e2e")
    require(
        os.environ.get("YOAO_WORKTREE_RESOURCE_ROOT") == str(ROOT)
        and os.environ.get("YOAO_WORKTREE_MODE") == "e2e"
        and os.environ.get("AAP_PRIVATE_FIXTURE_RASTER_POSTGRES") == "1"
        and os.environ.get("AAP_PRIVATE_FIXTURE_RASTER_OWNER") == str(Path(path))
        and Owner(path).data["raster"]["resources"] == resources,
        "raster_worktree_identity_changed",
    )
    raw = sys.stdin.buffer.read(65537)
    require(len(raw) <= 65536, "raster_phase_record_invalid")
    phase_record = json.loads(raw)
    devnull = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(devnull, sys.stdin.fileno())
    finally:
        os.close(devnull)
    inference = NativePhaseClient(phase_record)
    return inside_with_inference(path, arguments, inference)


def inside_with_inference(path, arguments, inference):
    owner = Owner(path)
    require(owner.data["raster"]["state"] == "born", "raster_owner_state_changed")
    credentials = json.loads((owner.private / "runtime.json").read_text())
    restricted = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        not in {
            "MINIO_ACCESS_KEY",
            "MINIO_SECRET_KEY",
            "MINIO_ROOT_USER",
            "MINIO_ROOT_PASSWORD",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
        }
    }
    restricted.update(
        {
            "MINIO_ACCESS_KEY": credentials["access_key"],
            "MINIO_SECRET_KEY": credentials["secret_key"],
        }
    )
    restricted["PLAYWRIGHT_ML_FIXTURE_HOST"] = fixture_provider_host(owner)
    suite = str(uuid.uuid4())
    restricted.update(
        {
            "AAP_PRIVATE_FIXTURE_RASTER_OWNER": str(Path(path)),
            "AAP_PRIVATE_FIXTURE_SUPERVISED_SUITE": suite,
            "PLAYWRIGHT_FIXTURE_RUN_ID": suite,
        }
    )
    cases, references, fenced = request_fixtures(
        owner, restricted, suite, configured_attempts(arguments)
    )
    inference.pause()
    require_only_fixture_owner(fenced)
    exercise_faults(owner, os.environ, references, suite)
    selection = inference.start()
    os.environ.update(selection)
    restricted.update(selection)
    seed_once(
        owner,
        restricted,
        {
            "operation": "owner_offline_resume",
            "seal_secret": fenced["seal_secret"],
        },
    )
    manifest = state_path(ROOT, "e2e", "data", f"raster-fault-{suite}.json")
    private_write(
        manifest,
        json.dumps(
            {"schema": "aap-e2e-raster-fault/v1", "suite_id": suite, "cases": cases}
        ).encode(),
    )
    from raster_fixture_relay import RasterFixtureRelay
    from worktree_env import frontend_environment
    from worktree_runtime import http_ready

    browser_environment = frontend_environment(restricted)
    for key in (
        "PLAYWRIGHT_ISOLATED_API_PORT",
        "PLAYWRIGHT_ISOLATED_WEB_PORT",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PLAYWRIGHT_CHROMIUM_CHANNEL",
        "GITHUB_ACTIONS",
        "GITHUB_STEP_SUMMARY",
        "DISPLAY",
    ):
        if key in restricted:
            browser_environment[key] = restricted[key]
    browser_environment.update(
        {
            "YOAO_WORKTREE_MODE": "e2e",
            "PLAYWRIGHT_FIXTURE_RUN_ID": suite,
            "PLAYWRIGHT_RASTER_FAULT_MANIFEST": str(manifest),
            "PLAYWRIGHT_RASTER_MASK_MATRIX": "native",
            "PLAYWRIGHT_RASTER_OFFLINE": "1",
        }
    )
    api_port = int(restricted["PLAYWRIGHT_ISOLATED_API_PORT"])
    require(1 <= api_port <= 65535, "raster_api_port_invalid")
    entries = [entry for group in cases.values() for entry in group]
    socket_dir = Path(tempfile.mkdtemp(prefix="aap-raster-", dir="/tmp"))
    directory_identity = socket_dir.stat()
    socket_path = socket_dir / "fixture.sock"
    owner.data["raster"]["relay_socket"] = str(socket_path)
    owner.save()
    browser_environment["PLAYWRIGHT_RASTER_FIXTURE_SOCKET"] = str(socket_path)

    def record_seed_failure(request, response):
        recorded = Owner(path)
        recorded.data["raster"].setdefault("seed_failures", []).append(
            {
                "operation_id": response.get("operation_id"),
                "operation": request.get("operation", request.get("path")),
                "status": response.get("status"),
                "body": response.get("body"),
                "owner_error": response.get("owner_error"),
                "journal_error": response.get("journal_error"),
                "operation_response": response.get("operation_response"),
                "disposal_error": response.get("disposal_error"),
            }
        )
        recorded.save()

    def record_seed_intent(operation_id):
        recorded = Owner(path)
        recorded.data["raster"]["relay_prepare_operation_id"] = operation_id
        recorded.save()

    relay = RasterFixtureRelay(
        str(socket_path),
        suite,
        entries,
        restricted,
        record_seed_failure,
        record_seed_intent,
    )
    images = None
    api = None
    result = None
    failure = None
    shutdown_failures = []

    def record_unsettled(reason, snapshot=None):
        recorded = Owner(path)
        shutdown = recorded.data.setdefault("raster_shutdown", {})
        shutdown.update(state="unsettled", reason=reason)
        if snapshot is not None:
            shutdown["relay_snapshot"] = snapshot
        recorded.save()

    def retain_unsettled(reason):
        try:
            record_unsettled(reason)
        except BaseException as error:
            shutdown_failures.append(error)

    def record_images(state, outcome):
        recorded = Owner(path)
        recorded.data["raster"]["images"].update({"state": state, **outcome})
        recorded.save()

    try:
        images = RasterImagesProcess(
            image_environment(owner, restricted), root=ROOT, journal=record_images
        )
        images.start()
        api_record = seed_once(
            owner, restricted, {"operation": "owner_native_api_start", "port": api_port}
        )
        api = NativeApiObserver(api_record, api_port)
        deadline = time.monotonic() + 120
        while not http_ready(api_port, "/health/db"):
            images.observe()
            require(
                api.poll() is None and relay.seed.poll() is None,
                "raster_api_start_failed",
            )
            require(time.monotonic() < deadline, "raster_api_start_timeout")
            time.sleep(0.1)
        with RasterWebServer(
            browser_environment, cwd=ROOT / "apps/web", preview=True
        ) as web:
            result = run_product(
                [
                    "pnpm",
                    "exec",
                    "playwright",
                    "test",
                    "--config=playwright.mask-product.config.ts",
                    "e2e/tests/raster-mask-native.spec.ts",
                    "e2e/tests/mask-advanced-operations.spec.ts",
                    "e2e/tests/mask-editor.spec.ts",
                    "--project=chromium",
                    *arguments,
                ],
                browser_environment,
                api,
                relay,
                cwd=ROOT / "apps/web",
                web=web,
                images=images,
            )
    except BaseException as error:
        failure = error
        retain_unsettled("raster_consumer_shutdown_failed")
    if api is not None:
        try:
            disposition = seed_once(
                owner, restricted, {"operation": "owner_native_suite_retire"}
            )
            require(
                disposition.get("state") == "retired" and api.poll() is not None,
                "raster_api_exit_unsettled",
            )
        except BaseException as error:
            shutdown_failures.append(error)
            retain_unsettled("raster_api_shutdown_failed")
        finally:
            api.close()
    try:
        relay.close(on_unsettled=record_unsettled)
    except BaseException as error:
        shutdown_failures.append(error)
        if not relay.unsettled:
            retain_unsettled("raster_fixture_shutdown_failed")
    if images is not None and images.child is not None:
        try:
            images.close()
        except BaseException as error:
            shutdown_failures.append(error)
            retain_unsettled("raster_images_shutdown_failed")
    if failure is not None:
        shutdown_failures.insert(0, failure)
    if result and shutdown_failures:
        shutdown_failures.insert(0, RuntimeError(f"raster_consumer_exit_{result}"))
    if shutdown_failures:
        if len(shutdown_failures) == 1:
            raise shutdown_failures[0]
        raise builtins.BaseExceptionGroup(
            "raster consumer and shutdown failures", shutdown_failures
        )
    try:
        require_only_fixture_owner(
            {
                **fenced,
                "providers": [],
                "seal_secret": None,
            }
        )
        native_inventory = inference.request("inventory")
        require(
            isinstance(native_inventory, list) and native_inventory,
            "raster_native_identity_unconfirmed",
        )
        owner = Owner(path)
        owner.data["raster"]["pre_retirement_native_identities"] = native_inventory
        owner.save()
        shutdown = seed_once(
            owner, restricted, {"operation": "owner_shutdown", "suite_id": suite}
        )
        require(
            shutdown.get("state") == "stopped", "raster_fixture_shutdown_unconfirmed"
        )
        inference.pause()
        archive_closed_native_authority(
            restricted, owner.data["raster"]["resources"], suite
        )
    except BaseException as error:
        retain_unsettled("raster_owner_shutdown_unsettled")
        errors = [error, *shutdown_failures]
        if result:
            errors.insert(0, RuntimeError(f"raster_consumer_exit_{result}"))
        if len(errors) > 1:
            raise builtins.BaseExceptionGroup(
                "raster consumer and owner shutdown failed", errors
            ) from None
        raise
    try:
        require_no_children()
    except BaseException as error:
        retain_unsettled("raster_consumer_descendants_still_running")
        errors = [error, *shutdown_failures]
        if result:
            errors.insert(0, RuntimeError(f"raster_consumer_exit_{result}"))
        if len(errors) > 1:
            raise builtins.BaseExceptionGroup(
                "raster descendant and journal failures", errors
            ) from None
        raise
    if result:
        return result
    observed = socket_dir.lstat()
    require(
        stat.S_ISDIR(observed.st_mode)
        and (observed.st_dev, observed.st_ino)
        == (directory_identity.st_dev, directory_identity.st_ino)
        and stat.S_ISSOCK(socket_path.lstat().st_mode),
        "raster_relay_socket_identity_changed",
    )
    socket_path.unlink()
    socket_dir.rmdir()
    browser_environment.pop("PLAYWRIGHT_RASTER_FIXTURE_SOCKET")
    try:
        with RasterWebServer(
            browser_environment, cwd=ROOT / "apps/web", preview=False
        ) as web:
            result = run_product(
                [
                    "pnpm",
                    "exec",
                    "playwright",
                    "test",
                    "--config=playwright.browser-integration.config.ts",
                    "--project=chromium",
                    *arguments,
                ],
                browser_environment,
                None,
                None,
                cwd=ROOT / "apps/web",
                web=web,
            )
        require_no_children()
    except BaseException as error:
        retain_unsettled("raster_final_browser_shutdown_failed")
        errors = [error, *shutdown_failures]
        if result:
            errors.insert(0, RuntimeError(f"raster_consumer_exit_{result}"))
        if len(errors) > 1:
            raise builtins.BaseExceptionGroup(
                "raster final browser and descendant shutdown failed", errors
            ) from None
        raise
    if result == 0:
        owner = Owner(path)
        owner.data["raster"]["state"] = "consumers_stopped"
        owner.save()
    return result


def failure_evidence(error):
    """Retain causal identities without exposing arbitrary exception text."""
    value = {"type": type(error).__name__}
    locations = []
    trace = error.__traceback__
    while trace is not None:
        source = Path(trace.tb_frame.f_code.co_filename).resolve()
        if source.parent == ROOT / "scripts":
            locations.append({"file": source.name, "line": trace.tb_lineno})
        trace = trace.tb_next
    if locations:
        value["locations"] = locations
    if isinstance(error, builtins.BaseExceptionGroup):
        value["errors"] = [failure_evidence(item) for item in error.exceptions]
    elif isinstance(error, (ContractError, InterruptedError)):
        value["code"] = str(error)
    elif isinstance(error, RuntimeError):
        text = str(error)
        if (
            text.startswith("raster_consumer_exit_")
            and text.removeprefix("raster_consumer_exit_").lstrip("-").isdigit()
        ):
            value["code"] = text
    if hasattr(error, "descendant_outcomes"):
        value["descendant_outcomes"] = error.descendant_outcomes
        value["leader_status"] = error.leader_status
    if hasattr(error, "adopted_outcomes"):
        value["adopted_outcomes"] = error.adopted_outcomes
        value["children_live"] = error.children_live
    if hasattr(error, "broker_observation"):
        value["broker_observation"] = error.broker_observation
    if hasattr(error, "api_outcome"):
        value["api_outcome"] = error.api_outcome
    if hasattr(error, "web_outcome"):
        value["web_outcome"] = error.web_outcome
    if hasattr(error, "images_outcome"):
        value["images_outcome"] = error.images_outcome
    if hasattr(error, "image_retirement_obligations"):
        value["image_retirement_obligations"] = error.image_retirement_obligations
    if isinstance(error, OSError) and error.errno is not None:
        value["errno"] = error.errno
    if error.__cause__ is not None:
        value["cause"] = failure_evidence(error.__cause__)
    return value


def destroy_with_images_role(backend, owner, confirmation):
    """Retire the exact private provider after all existing destruction guards."""
    with backend.fixture_authority_fence():
        verify_exclusive_provider(owner, backend)
        asyncio.run(image_role(backend, owner, "check"))
        with backend._prepare_owned_destruction(confirmation) as prepared:
            backend._bind_raster_destroy_identity(prepared, owner)
            retire_provider(owner, backend)
            backend._accept_retired_raster_provider(prepared, owner)
            backend._retire_prepared_raster_redis(prepared, owner)
            backend._stop_prepared_raster_fixture_source(prepared, owner)
            owner.data["raster"]["images"]["state"] = "database_drop_intent"
            owner.save()
            backend._destroy_prepared_database(prepared)
            backend._destroy_prepared_services(prepared)
            owner.data["raster"]["images"]["state"] = "database_role_drop_intent"
            owner.save()
            asyncio.run(image_role(backend, owner, "drop"))
            owner.data["raster"]["images"]["state"] = "database_role_removed"
            owner.save()
            if "postgres" in owner.data["raster"]:
                retire_postgres(owner, backend, prepared)
            backend._finish_prepared_destruction(prepared)


def outer(arguments):
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.prctl(36, 1, 0, 0, 0) == 0, "raster_subreaper_unavailable")
    require(
        not os.environ.get("YOAO_WORKTREE_MODE"), "raster_launcher_must_wrap_worktree"
    )
    identity = load_identity(ROOT, create=True)
    resources = topology(ROOT, identity, "e2e")
    journal_dir = state_path(ROOT, "e2e", "raster-fault-runs")
    with environment_lock(resources):
        for name in (
            "resources.json",
            "run.json",
            "runtime-credentials.json",
            "fixture-store-owner.json",
            "fixture-connector-key.json",
            "inference-paused.json",
            "data",
            "tmp",
            "redis",
        ):
            require(not state_path(ROOT, "e2e", name).exists(), "raster_mode_not_fresh")
        subprocess.run(
            ["bash", str(ROOT / "scripts/ensure-api-vendor.sh")], cwd=ROOT, check=True
        )
        if journal_dir.exists():
            require(
                journal_dir.is_dir() and not journal_dir.is_symlink(),
                "raster_journal_directory_invalid",
            )
            for entry in journal_dir.iterdir():
                require(
                    entry.is_dir() and not entry.is_symlink(), "raster_journal_invalid"
                )
                require(
                    Owner(entry / "owner.json").data["cleaned"] is True,
                    "raster_previous_run_unsettled",
                )
        manifest = journal_dir / uuid.uuid4().hex / "owner.json"
        owner = Owner(manifest, create=True)
        try:
            endpoint, access, secret = provision(resources, owner)
            postgres_url = provision_postgres(resources, owner)
            for key in tuple(os.environ):
                if key.upper() in {
                    "MINIO_ENDPOINT",
                    "MINIO_ACCESS_KEY",
                    "MINIO_SECRET_KEY",
                    "MINIO_USE_SSL",
                }:
                    del os.environ[key]
            os.environ.update(
                {
                    "MINIO_ENDPOINT": endpoint,
                    "MINIO_ACCESS_KEY": access,
                    "MINIO_SECRET_KEY": secret,
                    "MINIO_USE_SSL": "false",
                    "AAP_PRIVATE_FIXTURE_RASTER_OWNER": str(manifest),
                    "AAP_PRIVATE_FIXTURE_RASTER_POSTGRES": "1",
                }
            )
            from app.config import settings
            from worktree_resources import LocalResources

            require(
                settings.environment == "development", "raster_development_required"
            )
            backend = LocalResources(
                resources,
                settings.model_copy(
                    update={
                        "database_url": postgres_url,
                        "migration_database_url": postgres_url,
                        "minio_endpoint": endpoint,
                        "minio_access_key": access,
                        "minio_secret_key": secret,
                        "minio_use_ssl": False,
                    }
                ),
                code_root=ROOT,
            )
            reject_orphans(backend)
            inventory = backend.inspect()
            require(
                inventory["database"] is None and inventory["redis"] is None,
                "raster_mode_resources_exist",
            )
            require(
                all(
                    value is None
                    for name, value in inventory["buckets"].items()
                    if name != resources["buckets"]["MINIO_BUCKET"]
                ),
                "raster_unexpected_bucket",
            )
            options = parse_arguments(
                [
                    "exec",
                    "--mode",
                    "e2e",
                    "--",
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--inside",
                    str(manifest),
                    *arguments,
                ]
            )
            from fixture_inference_services import FixtureInferenceServices

            backend._arm_raster_redis_birth(owner)
            backend.prepare(skip_migrations=options.skip_migrations)
            asyncio.run(backend.prepare_fixture_inference_database())
            owner.data["raster"]["images"]["state"] = "database_role_creation_intent"
            owner.save()
            owner.data["raster"]["images"]["database_role_oid"] = asyncio.run(
                image_role(backend, owner, "create")
            )
            owner.data["raster"]["images"]["state"] = "database_role_ready"
            owner.save()
            inference = FixtureInferenceServices(backend, code_root=ROOT)
            inference.start()
            phase = NativePhaseControl(inference, resources)
            result = run_processes(
                backend,
                options,
                fixture_inference=inference,
                coordinator_input=phase.connection,
            )
            owner = Owner(manifest)
            owner.data["raster"]["consumer_exit"] = result
            owner.save()
            if result != 0 or owner.data["raster"]["state"] != "consumers_stopped":
                return result or 1
            require(phase.failure is None, "raster_native_phase_unsettled")
            require(
                inference.process_identity_inventory() == [],
                "raster_native_pause_unconfirmed",
            )
            require_no_children()
            inference.close()
            phase.close()
            require_no_children()
            verify_store_physical(owner)
            destroy_with_images_role(backend, owner, resources["confirmation"])
            owner.data["raster"]["state"] = "worktree_destroyed"
            owner.save()
            require(owner.cleanup(), "raster_store_cleanup_failed")
            return 0
        except BaseException as exc:
            owner = Owner(manifest)
            owner.data["raster_failure"] = failure_evidence(exc)
            owner.save()
            raise


if __name__ == "__main__":
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == "--inside":
            raise SystemExit(inside(sys.argv[2], sys.argv[3:]))
        raise SystemExit(outer(sys.argv[1:]))
    except Exception as error:
        detail = failure_evidence(error)
        if len(sys.argv) >= 3 and sys.argv[1] == "--inside":
            try:
                owner = Owner(sys.argv[2])
                owner.data["raster_failure"] = detail
                owner.save()
            except Exception:
                print(
                    "Raster fault failure journal could not be updated", file=sys.stderr
                )
        print(f"Raster fault launcher failed: {json.dumps(detail)}", file=sys.stderr)
        raise SystemExit(1) from None
