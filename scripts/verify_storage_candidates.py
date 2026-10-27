#!/usr/bin/env python3
"""Run a digest-pinned, disposable local S3 comparison; never use deployment env.

Only the explicit ownership manifest authorizes cleanup. Images remain cached.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import functools
import http.server
import io
import json
import logging
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from storage_s3_contract import (
    CHECKOUT_LABEL,
    OWNER_LABEL,
    SCHEMA,
    Budget,
    ContractError,
    Recorder,
    collect_pages,
    load_lock,
    public_bytes,
    read_body,
    require,
    safe_error,
    sha256,
    summarize,
    validate_owner,
)

ROOT = Path(__file__).resolve().parents[1]
BUCKET_FIELDS = (
    "MINIO_BUCKET",
    "MINIO_DATASETS_BUCKET",
    "MINIO_BUG_REPORTS_BUCKET",
    "MINIO_MEDIA_CACHE_BUCKET",
    "MINIO_AUDIT_ARCHIVE_BUCKET",
    "MINIO_IMPORT_BUCKET",
    "MINIO_EXPORT_BUCKET",
    "MINIO_AVATARS_BUCKET",
    "MINIO_TRAINING_BUCKET",
)


def private_write(path, data):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def command(args, *, timeout=60, input_data=None, env=None):
    result = subprocess.run(
        args,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
    )
    require(result.returncode == 0, "subprocess_failed")
    require(len(result.stdout) <= 1024 * 1024, "subprocess_output_budget")
    return result.stdout.decode(errors="replace").strip()


class Owner:
    def __init__(self, path, *, create=False):
        self.path = Path(path).resolve()
        if create:
            require(not self.path.exists(), "manifest_exists")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.data = {
                "schema": "aap.storage-poc-owner/v1",
                "nonce": secrets.token_hex(16),
                "checkout": str(ROOT),
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "resources": [],
                "browser_sessions": [],
                "cleaned": False,
                "atomic_temporary": str(
                    self.path.with_suffix(".owner-" + secrets.token_hex(16) + ".tmp")
                ),
            }
            self.private = self.path.parent / ("private-" + self.data["nonce"])
            self.data["private_directory"] = str(self.private)
            private_write(self.path, json.dumps(self.data).encode())
            self.private.mkdir(mode=0o700)
        else:
            require(
                self.path.is_file() and self.path.stat().st_mode & 0o077 == 0,
                "manifest_private",
            )
            self.data = json.loads(self.path.read_bytes())
            require(self.data.get("checkout") == str(ROOT), "owner_checkout")
            require(
                self.data.get("schema") == "aap.storage-poc-owner/v1", "owner_schema"
            )
            nonce = self.data.get("nonce")
            require(
                isinstance(nonce, str)
                and re.fullmatch(r"[0-9a-f]{32}", nonce) is not None,
                "owner_nonce",
            )
            self.private = Path(self.data["private_directory"])
            require(
                self.private == self.path.parent / ("private-" + self.data["nonce"]),
                "owner_private_path",
            )
        self.counter = len(self.data["resources"])

    def save(self):
        temporary = Path(self.data["atomic_temporary"])
        require(
            temporary.parent == self.path.parent
            and temporary.name.startswith(self.path.stem + ".owner-"),
            "owner_atomic_path",
        )
        if temporary.exists():
            require(
                not temporary.is_symlink()
                and temporary.is_file()
                and temporary.stat().st_uid == os.getuid()
                and temporary.stat().st_mode & 0o077 == 0,
                "owner_atomic_file",
            )
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(self.data, indent=2).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def labels(self):
        return [
            "--label",
            f"{OWNER_LABEL}={self.data['nonce']}",
            "--label",
            f"{CHECKOUT_LABEL}={ROOT}",
        ]

    def create(self, kind, *, image=None, args=()):
        self.counter += 1
        name = f"aap-e07-{self.data['nonce']}-{self.counter}"
        item = {
            "kind": kind,
            "name": name,
            "id": None,
            "image": image,
            "state": "creation_intent",
        }
        self.data["resources"].append(item)
        self.save()  # durable intent precedes resource creation
        if kind == "container":
            cmd = ["docker", "create", "--name", name, *self.labels(), *args, image]
            # Container commands follow the image; callers supply them separately.
            if isinstance(image, tuple):
                raise ContractError("image_must_be_digest")
        else:
            cmd = ["docker", kind, "create", *self.labels(), name]
        item["id"] = command(cmd)
        item["state"] = "created"
        self.save()
        return item

    def container(self, image, args=(), cmd=()):
        self.counter += 1
        name = f"aap-e07-{self.data['nonce']}-{self.counter}"
        item = {
            "kind": "container",
            "name": name,
            "id": None,
            "image": image,
            "state": "creation_intent",
        }
        self.data["resources"].append(item)
        self.save()
        item["id"] = command(
            [
                "docker",
                "create",
                "--name",
                name,
                *self.labels(),
                "--platform",
                "linux/amd64",
                "--cpus",
                "4",
                "--memory",
                "8g",
                "--memory-swap",
                "8g",
                "--log-opt",
                "max-size=2m",
                "--log-opt",
                "max-file=1",
                *args,
                image,
                *cmd,
            ]
        )
        item["state"] = "created"
        self.save()
        return item

    def inspect(self, item):
        kind = item["kind"]
        args = (
            ["docker", "inspect", item["id"] or item["name"]]
            if kind == "container"
            else ["docker", kind, "inspect", item["id"] or item["name"]]
        )
        result = subprocess.run(args, capture_output=True, timeout=15)
        if result.returncode:
            require(
                b"no such" in result.stderr.lower()
                or b"not found" in result.stderr.lower(),
                "resource_inspect_failed",
            )
            return None
        return json.loads(result.stdout)[0]

    def verified(self, item):
        observed = self.inspect(item)
        require(observed is not None, "resource_missing")
        validate_owner(self.data, item, observed, ROOT)
        return observed

    def action(self, item, *args, timeout=60):
        self.verified(item)
        return command(["docker", *args, item["id"]], timeout=timeout)

    def start(self, item):
        self.action(item, "start")

    def helper(self, image, *, args=(), cmd=()):
        item = self.container(image, args=args, cmd=cmd)
        self.start(item)
        status = self.action(item, "wait", timeout=120)
        require(status == "0", "helper_failed")
        return self.action(item, "logs")

    def cleanup(self):
        failures = []
        for session in self.data["browser_sessions"]:
            if not session.get("closed"):
                try:
                    require(
                        session["name"] == "e07-" + self.data["nonce"], "browser_owner"
                    )
                    command(
                        ["agent-browser", "--session", session["name"], "close"],
                        timeout=30,
                    )
                    session["closed"] = True
                    self.save()
                except Exception as exc:
                    failures.append(safe_error(exc))
        ordered = sorted(
            self.data["resources"],
            key=lambda item: {"container": 0, "network": 1, "volume": 2}[item["kind"]],
        )
        for item in ordered:
            try:
                observed = self.inspect(item)
                if observed is None:
                    item["state"] = "absent"
                    self.save()
                    continue
                if item["id"] is None:  # recover only a durable exact creation intent
                    item["id"] = (
                        observed.get("Name")
                        if item["kind"] == "volume"
                        else observed.get("Id")
                    )
                    self.save()
                validate_owner(self.data, item, observed, ROOT)
                if item["kind"] == "container":
                    command(["docker", "rm", "-f", item["id"]])
                elif item["kind"] == "network":
                    require(not observed.get("Containers"), "network_in_use")
                    command(["docker", "network", "rm", item["id"]])
                else:
                    users = command(
                        [
                            "docker",
                            "ps",
                            "-a",
                            "--filter",
                            "volume=" + item["id"],
                            "--format",
                            "{{.ID}}",
                        ]
                    )
                    require(not users, "volume_in_use")
                    command(["docker", "volume", "rm", item["id"]])
                require(self.inspect(item) is None, "cleanup_absence")
                item["state"] = "absent"
                self.save()
            except Exception as exc:
                failures.append(safe_error(exc))
        if not failures:
            if self.private.exists():
                require(
                    self.private.is_dir() and not self.private.is_symlink(),
                    "private_directory_type",
                )
                shutil.rmtree(self.private)
            self.data["cleaned"] = True
            self.data["cleanup_verified_at"] = dt.datetime.now(
                dt.timezone.utc
            ).isoformat()
        self.data["cleanup_errors"] = failures
        self.save()
        return not failures


def host_port(owner, item, container_port):
    ports = owner.verified(item)["NetworkSettings"]["Ports"][f"{container_port}/tcp"]
    require(len(ports) == 1 and ports[0]["HostIp"] == "127.0.0.1", "loopback_only")
    return int(ports[0]["HostPort"])


def s3_config():
    from botocore.config import Config

    return Config(
        signature_version="s3v4",
        region_name="us-east-1",
        s3={"addressing_style": "path"},
        connect_timeout=3,
        read_timeout=15,
        retries={"total_max_attempts": 1},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )


def client_for(endpoint, credentials, budget):
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=credentials[0],
        aws_secret_access_key=credentials[1],
        config=s3_config(),
    )
    client.meta.events.register("before-parameter-build.s3", budget.hook)
    return client


def app_storage(endpoint, credentials, buckets, budget):
    """Use actual settings and storage with no inherited deployment sources."""
    from unittest.mock import patch

    storage_env = {
        "MINIO_ENDPOINT": endpoint.removeprefix("http://"),
        "MINIO_ACCESS_KEY": credentials[0],
        "MINIO_SECRET_KEY": credentials[1],
        "MINIO_USE_SSL": "false",
        "MINIO_PUBLIC_URL": "",
        "ML_BACKEND_STORAGE_HOST": "",
        "DATABASE_URL": "postgresql+asyncpg://poc:poc@127.0.0.1:1/poc",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "ENVIRONMENT": "development",
        "AWS_EC2_METADATA_DISABLED": "true",
        **dict(zip(BUCKET_FIELDS, buckets, strict=True)),
    }
    sys.path.insert(0, str(ROOT / "apps/api"))
    # Disable only external settings sources; no application service is replaced.
    with (
        patch.dict(os.environ, storage_env, clear=True),
        patch(
            "pydantic_settings.sources.DotEnvSettingsSource._read_env_files",
            return_value={},
        ),
    ):
        from app import config

        config.settings = config.Settings(_env_file=None)
        from app.services import storage, storage_presigning

        storage.settings = config.settings
        storage_presigning.settings = config.settings
        service = storage.StorageService(client_config=s3_config())
    service.client.meta.events.register("before-parameter-build.s3", budget.hook)
    return service, config.settings


def expect_error(action, *, statuses=None, codes=None):
    from botocore.exceptions import ClientError

    try:
        response = action()
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = exc.response.get("Error", {}).get("Code")
        require(
            (statuses is None or status in statuses)
            and (codes is None or code in codes),
            "wrong_error_response",
        )
        return {"http_status": status, "error_code": safe_error(exc)}
    else:
        if isinstance(response, dict) and "Body" in response:
            response["Body"].close()
        raise ContractError("expected_rejection")


def http_get(url, budget, *, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read(budget.byte_limit + 1)
            budget.consume("download", len(body))
            return (
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                body,
            )
    except urllib.error.HTTPError as exc:
        with exc:
            body = exc.read(65536)
            budget.consume("download", len(body))
            return exc.code, {}, b""


def fixtures(directory):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (31, 117, 199)).save(buffer, format="PNG")
    png = buffer.getvalue()
    mp4 = directory / "fixture.mp4"
    command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x48:r=5:d=0.6",
            "-an",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(mp4),
        ],
        timeout=30,
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        info = zipfile.ZipInfo("images/fixture.png", date_time=(2020, 1, 1, 0, 0, 0))
        archive.writestr(info, png)
    return {
        "image.png": (png, "image/png"),
        "clip.mp4": (mp4.read_bytes(), "video/mp4"),
        "training.zip": (buffer.getvalue(), "application/zip"),
    }


def candidate_args(name, private, env_file, volume, network):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    args = [
        "--network",
        network["id"],
        "--network-alias",
        "candidate",
        "--publish",
        f"127.0.0.1:{port}:9000",
        "--mount",
        f"type=volume,source={volume['id']},target=/data",
        "--env-file",
        str(env_file),
    ]
    if name == "seaweedfs":
        args += [
            "--mount",
            f"type=bind,source={private / 'filer.toml'},target=/etc/seaweedfs/filer.toml,readonly",
        ]
        cmd = [
            "mini",
            "-dir=/data",
            "-ip=127.0.0.1",
            "-ip.bind=0.0.0.0",
            "-s3.port=9000",
            "-s3.config=/data/poc-s3.json",
            "-webdav=false",
            "-admin.ui=false",
            "-s3.port.iceberg=0",
            "-s3.port.lance=0",
        ]
    else:
        cmd = ["rustfs", "/data"]
    return args, cmd


def ready(client, budget, *, seconds=120):
    deadline = min(time.monotonic() + seconds, budget.deadline)
    while time.monotonic() < deadline:
        try:
            client.list_buckets()
            return
        except Exception:
            time.sleep(0.5)
    raise ContractError("startup_timeout")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def proxy_setup(owner, lock, network, endpoint):
    site = owner.private / "site"
    site.mkdir(mode=0o755)
    private_write(
        site / "index.html",
        b"<!doctype html><meta charset=utf-8><title>S3 local contract</title><p>Isolated local S3 contract</p>",
    )
    # Binding a private test site needs no application port or runtime.
    handler = functools.partial(QuietHandler, directory=str(site))
    owner.data["local_origin"] = {
        "host": "127.0.0.1",
        "process_id": os.getpid(),
        "state": "creation_intent",
        "ephemeral": True,
    }
    owner.save()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    origin = "http://127.0.0.1:" + str(server.server_port)
    owner.data["local_origin"] = {
        "host": "127.0.0.1",
        "port": server.server_port,
        "process_id": os.getpid(),
        "ephemeral": True,
    }
    owner.save()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = owner.private / "nginx.conf"
    internal_host = urllib.parse.urlsplit(endpoint).netloc
    text = """events {}\nhttp {\n  log_format bounded '$request_method $status';\n  access_log /dev/stdout bounded;\n  error_log /dev/null;\n  server { listen 8080; client_max_body_size 80m;\n    location = / { root /site; try_files /index.html =404; }\n    location /relative/ { proxy_pass http://candidate:9000/; proxy_set_header Host INTERNAL; }\n    location /absolute/ { proxy_pass http://candidate:9000/; proxy_set_header Host $http_host; }\n    location / { proxy_pass http://candidate:9000; proxy_set_header Host $http_host; }\n  }\n}\n""".replace(
        "INTERNAL", internal_host
    )
    private_write(config, text.encode())
    # Image's nginx worker can read only this harmless static file.
    (site / "index.html").chmod(0o644)
    config.chmod(0o644)
    item = owner.container(
        lock["proxy"]["image"],
        args=[
            "--network",
            network["id"],
            "--publish",
            "127.0.0.1::8080",
            "--mount",
            f"type=bind,source={config},target=/etc/nginx/nginx.conf,readonly",
            "--mount",
            f"type=bind,source={site},target=/site,readonly",
        ],
    )
    owner.start(item)
    public = "http://127.0.0.1:" + str(host_port(owner, item, 8080))
    for _ in range(50):
        try:
            with urllib.request.urlopen(public, timeout=2):
                return item, public, origin, server
        except Exception:
            time.sleep(0.1)
    raise ContractError("proxy_timeout")


def basic_cases(recorder, client, bucket, budget, saved):
    data = b"s3-contract\x00\xff" * 193
    metadata = {"fixture": "deterministic", "owner": "local-poc"}
    with recorder.case("basic") as evidence:
        for key, body in [
            ("raw/中文 %25+字.png", data),
            ("raw/a%2Fb", b"percent"),
            ("raw/a%2fb", b"lower"),
            ("raw/empty", b""),
        ]:
            result = client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/octet-stream",
                CacheControl="private,max-age=17",
                Metadata=metadata,
            )
            head = client.head_object(Bucket=bucket, Key=key)
            require(
                head["ContentLength"] == len(body)
                and head["Metadata"] == metadata
                and head["CacheControl"] == "private,max-age=17",
                "head_metadata",
            )
            read_body(
                client.get_object(Bucket=bucket, Key=key),
                budget,
                expected=body,
                metadata=metadata,
            )
            saved.append((bucket, key, result.get("VersionId"), body, metadata))
        # Application path guards are asserted directly, without normalizing a key.
        require("raw/../literal".split("/")[1] == "..", "raw_key_normalized")
        evidence.update(
            objects=4,
            bytes_sha256=sha256(data),
            etag=head["ETag"],
            raw_key_handling="percent_case_and_unicode_preserved",
        )
    with recorder.case("prefix_pagination") as evidence:
        listed = collect_pages(
            client,
            "list_objects_v2",
            "Contents",
            Bucket=bucket,
            Prefix="raw/",
            MaxKeys=1,
        )
        require(
            {item["Key"] for item in listed} == {item[1] for item in saved},
            "prefix_contents",
        )
        client.put_object(Bucket=bucket, Key="delete/exact", Body=b"delete")
        client.delete_object(Bucket=bucket, Key="delete/exact")
        expect_error(
            lambda: client.head_object(Bucket=bucket, Key="delete/exact"),
            statuses={404},
        )
        evidence["listed_objects"] = len(listed)
    with recorder.case("conditional_reads") as evidence:
        key = saved[0][1]
        head = client.head_object(Bucket=bucket, Key=key)
        read_body(
            client.get_object(Bucket=bucket, Key=key, IfMatch=head["ETag"]),
            budget,
            expected=data,
        )
        evidence["get_mismatch"] = expect_error(
            lambda: client.get_object(Bucket=bucket, Key=key, IfMatch='"wrong"'),
            statuses={412},
        )
        evidence["head_mismatch"] = expect_error(
            lambda: client.head_object(Bucket=bucket, Key=key, IfMatch='"wrong"'),
            statuses={412},
        )
        evidence["not_modified"] = expect_error(
            lambda: client.get_object(Bucket=bucket, Key=key, IfNoneMatch=head["ETag"]),
            statuses={304},
        )
        response = client.get_object(Bucket=bucket, Key=key, Range="bytes=3-29")
        require(
            response["ResponseMetadata"]["HTTPStatusCode"] == 206
            and response["ContentRange"] == f"bytes 3-29/{len(data)}",
            "range_headers",
        )
        read_body(response, budget, expected=data[3:30])
        evidence["range_bytes"] = 27
    with recorder.case("conditional_create") as evidence:

        def create(_):
            try:
                client.put_object(
                    Bucket=bucket, Key="conditional/once", Body=data, IfNoneMatch="*"
                )
                return "success"
            except Exception as exc:
                return safe_error(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(create, range(4)))
        evidence["results"] = results
        require(
            results.count("success") == 1
            and all(
                result
                in {"success", "PreconditionFailed", "ConditionalRequestConflict"}
                for result in results
            ),
            "conditional_create_atomicity",
        )
        read_body(
            client.get_object(Bucket=bucket, Key="conditional/once"),
            budget,
            expected=data,
        )


def version_cases(recorder, client, bucket, budget, saved):
    with recorder.case("versions") as evidence:
        client.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        ids = []
        for body in (b"old-version", b"new-version"):
            result = client.put_object(
                Bucket=bucket,
                Key="versions/frozen",
                Body=body,
                Metadata={"version": body.decode()},
            )
            version = result.get("VersionId")
            require(
                version and version != "null" and version not in ids,
                "immutable_version_id",
            )
            ids.append(version)
            saved.append(
                (bucket, "versions/frozen", version, body, {"version": body.decode()})
            )
        for item in saved:
            if item[1] == "versions/frozen":
                read_body(
                    client.get_object(Bucket=bucket, Key=item[1], VersionId=item[2]),
                    budget,
                    expected=item[3],
                    metadata=item[4],
                )
        versions = collect_pages(
            client,
            "list_object_versions",
            "Versions",
            Bucket=bucket,
            Prefix="versions/",
            MaxKeys=1,
        )
        require({v["VersionId"] for v in versions} == set(ids), "version_pagination")
        marker = client.delete_object(Bucket=bucket, Key="versions/frozen")
        require(
            marker.get("DeleteMarker") is True and marker.get("VersionId"),
            "delete_marker_response",
        )
        expect_error(
            lambda: client.get_object(Bucket=bucket, Key="versions/frozen"),
            statuses={404},
        )
        page = client.list_object_versions(
            Bucket=bucket, Prefix="versions/", MaxKeys=100
        )
        require(
            any(
                v["VersionId"] == marker["VersionId"]
                for v in page.get("DeleteMarkers", [])
            ),
            "delete_marker_listing",
        )
        client.delete_object(
            Bucket=bucket, Key="versions/frozen", VersionId=marker["VersionId"]
        )
        read_body(
            client.get_object(Bucket=bucket, Key="versions/frozen"),
            budget,
            expected=b"new-version",
        )
        temporary = client.put_object(
            Bucket=bucket, Key="versions/delete-version", Body=b"exact"
        )
        client.delete_object(
            Bucket=bucket,
            Key="versions/delete-version",
            VersionId=temporary["VersionId"],
        )
        expect_error(
            lambda: client.get_object(
                Bucket=bucket,
                Key="versions/delete-version",
                VersionId=temporary["VersionId"],
            ),
            statuses={404},
        )
        evidence.update(
            saved_versions=len(ids), marker_removed=True, exact_version_deleted=True
        )
    with recorder.case("mutable_null") as evidence:
        client.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Suspended"}
        )
        for data in (b"null-one", b"null-two"):
            client.put_object(Bucket=bucket, Key="versions/null-slot", Body=data)
        read_body(
            client.get_object(
                Bucket=bucket, Key="versions/null-slot", VersionId="null"
            ),
            budget,
            expected=b"null-two",
        )
        versions = collect_pages(
            client,
            "list_object_versions",
            "Versions",
            Bucket=bucket,
            Prefix="versions/null-slot",
            MaxKeys=1,
        )
        require(
            len(versions) == 1 and versions[0]["VersionId"] == "null",
            "mutable_null_slot",
        )
        evidence["null_is_immutable"] = False
        client.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )


def multipart_cases(recorder, client, bucket, budget):
    part = bytes(range(256)) * (5 * 1024 * 1024 // 256)
    with recorder.case("multipart") as evidence:
        key = "multipart/complete"
        upload = client.create_multipart_upload(
            Bucket=bucket, Key=key, Metadata={"fixture": "multipart"}
        )["UploadId"]
        parts = []
        try:
            for number, body in enumerate((part, part[::-1]), 1):
                response = client.upload_part(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload,
                    PartNumber=number,
                    Body=body,
                )
                parts.append({"PartNumber": number, "ETag": response["ETag"]})
            listed = collect_pages(
                client,
                "list_parts",
                "Parts",
                Bucket=bucket,
                Key=key,
                UploadId=upload,
                MaxParts=1,
            )
            require(
                [p["PartNumber"] for p in listed] == [1, 2]
                and all(
                    item["Size"] == len(part) and item["ETag"] == parts[index]["ETag"]
                    for index, item in enumerate(listed)
                ),
                "part_pagination",
            )
            evidence["invalid_completion"] = expect_error(
                lambda: client.complete_multipart_upload(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload,
                    MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": '"wrong"'}]},
                ),
                statuses={400},
                codes={"InvalidPart"},
            )
            response = client.complete_multipart_upload(
                Bucket=bucket,
                Key=key,
                UploadId=upload,
                MultipartUpload={"Parts": parts},
            )
            body = read_body(
                client.get_object(Bucket=bucket, Key=key),
                budget,
                expected=part + part[::-1],
                metadata={"fixture": "multipart"},
            )
            evidence.update(
                sha256=sha256(body),
                bytes=len(body),
                etag=response["ETag"],
                etag_is_digest_proof=False,
            )
        finally:
            try:
                client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload)
            except Exception:
                pass
    with recorder.case("multipart_abort") as evidence:
        key = "multipart/interrupted"
        upload = client.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
        client.upload_part(
            Bucket=bucket, Key=key, UploadId=upload, PartNumber=1, Body=part
        )
        expect_error(lambda: client.head_object(Bucket=bucket, Key=key), statuses={404})
        client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload)
        expect_error(
            lambda: client.list_parts(Bucket=bucket, Key=key, UploadId=upload),
            statuses={404},
            codes={"NoSuchUpload"},
        )
        response = client.list_multipart_uploads(Bucket=bucket, Prefix=key)
        require(not response.get("Uploads"), "abort_upload_present")
        expect_error(lambda: client.head_object(Bucket=bucket, Key=key), statuses={404})
        evidence["absent_after_abort"] = True


def media_cases(recorder, service, settings, public, media, budget, directory):
    bucket = service.datasets_bucket
    with recorder.case("media") as evidence:
        from PIL import Image

        for key, (body, content_type) in media.items():
            service.put_bytes(
                "media/" + key, body, content_type=content_type, bucket=bucket
            )
            actual = read_body(
                service.client.get_object(Bucket=bucket, Key="media/" + key),
                budget,
                expected=body,
            )
            if key.endswith("png"):
                with Image.open(io.BytesIO(actual)) as image:
                    image.load()
                    require(image.size == (64, 48), "image_dimensions")
            elif key.endswith("mp4"):
                path = directory / "download.mp4"
                private_write(path, actual)
                command(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(path),
                        "-f",
                        "null",
                        "-",
                    ],
                    timeout=30,
                )
            else:
                with zipfile.ZipFile(io.BytesIO(actual)) as archive:
                    require(
                        archive.testzip() is None
                        and archive.read("images/fixture.png") == media["image.png"][0],
                        "zip_crc",
                    )
            evidence[key] = {"bytes": len(actual), "sha256": sha256(actual)}
    with recorder.case("signatures") as evidence:
        for audience in ("internal", "ml", "browser_absolute", "browser_relative"):
            settings.ml_backend_storage_host = urllib.parse.urlsplit(public).netloc
            settings.minio_public_url = (
                "/relative" if audience == "browser_relative" else public + "/absolute"
            )
            for key, (body, _) in media.items():
                signed = service.generate_download_url(
                    "media/" + key,
                    bucket=bucket,
                    align=False,
                    audience=audience if audience in {"internal", "ml"} else "browser",
                )
                url = public + signed if signed.startswith("/") else signed
                status, headers, actual = http_get(url, budget)
                require(status == 200 and sha256(actual) == sha256(body), "signed_body")
                require(
                    int(headers.get("content-length", -1)) == len(body), "signed_length"
                )
                status, headers, actual = http_get(
                    url, budget, headers={"Range": "bytes=0-15"}
                )
                require(
                    status == 206
                    and actual == body[:16]
                    and headers.get("content-range") == f"bytes 0-15/{len(body)}",
                    "signed_range",
                )
            evidence[audience] = {"media_count": 3, "full_digest_and_range": True}
        settings.minio_public_url = public + "/absolute"
    with recorder.case("signature_rejections") as evidence:
        url = service.generate_download_url(
            "media/image.png",
            bucket=bucket,
            expires_in=1,
            align=False,
            audience="internal",
        )
        status, _, _ = http_get(url, budget)
        require(status == 200, "expiry_initial_validity")
        split = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(split.query)
        changed = [(k, "0" * 64 if k == "X-Amz-Signature" else v) for k, v in query]
        tampered = urllib.parse.urlunsplit(
            split._replace(query=urllib.parse.urlencode(changed))
        )
        status, _, _ = http_get(tampered, budget)
        require(status in {400, 403}, "tampered_signature_accepted")
        evidence["tamper_http"] = status
        wrong = client_for(
            service.client.meta.endpoint_url,
            ("invalid-local-key", "invalid-local-secret"),
            budget,
        )
        evidence["wrong_credentials"] = expect_error(
            lambda: wrong.head_object(Bucket=bucket, Key="media/image.png"),
            statuses={403},
        )
        time.sleep(2.2)
        status, _, _ = http_get(url, budget)
        require(status in {400, 403}, "expired_signature_accepted")
        evidence.update(expired_http=status, originally_valid=True, align=False)


def browser_cases(
    recorder, owner, service, settings, public, origin, proxy, media, budget
):
    session = {"name": "e07-" + owner.data["nonce"], "closed": False}
    owner.data["browser_sessions"].append(session)
    owner.save()  # persist session ownership before launching Chromium
    bucket = service.datasets_bucket
    data = media["image.png"][0]
    try:
        service.client.put_bucket_cors(
            Bucket=bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": [origin],
                        "AllowedMethods": ["GET", "HEAD", "PUT"],
                        "AllowedHeaders": ["content-type", "range"],
                        "ExposeHeaders": [
                            "ETag",
                            "Content-Range",
                            "Content-Length",
                            "Content-Type",
                        ],
                        "MaxAgeSeconds": 0,
                    }
                ]
            },
        )
    except Exception as exc:
        # Browser attempts remain required even if configuration is unsupported.
        recorder.cases["browser_cross_origin"]["cors_setup_error"] = safe_error(exc)
    for same in (True, False):
        name = "browser_same_origin" if same else "browser_cross_origin"
        with recorder.case(name) as evidence:
            settings.minio_public_url = "/relative" if same else public + "/absolute"
            key = "browser/same.png" if same else "browser/cross.png"
            upload = service.generate_upload_url(
                key, content_type="image/png", bucket=bucket
            )
            download = service.generate_download_url(key, bucket=bucket, align=False)
            # Never place signed URLs in CLI argv, rendered page text or a report.
            request = {
                "put": upload,
                "get": download,
                "bytes": list(data),
                "sha": sha256(data),
            }
            javascript = """(async () => {
              const p = PAYLOAD;
              const errors = [];
              window.addEventListener('error', () => errors.push('error'));
              window.addEventListener('unhandledrejection', () => errors.push('unhandled'));
              const body = new Uint8Array(p.bytes);
              const put = await fetch(p.put, {method:'PUT',headers:{'Content-Type':'image/png'},body});
              const get = await fetch(p.get);
              const bytes = new Uint8Array(await get.arrayBuffer());
              const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(v=>v.toString(16).padStart(2,'0')).join('');
              const range = await fetch(p.get,{headers:{Range:'bytes=0-15'}});
              const part = new Uint8Array(await range.arrayBuffer());
              const bad = new URL(p.get,location.href); bad.searchParams.set('X-Amz-Signature','0'.repeat(64));
              const rejected = await fetch(bad);
              const retry = await fetch(p.get);
              const retried = new Uint8Array(await retry.arrayBuffer());
              return {put:put.status,get:get.status,digest,range:range.status,rangeLength:part.length,
                rangeHeader:range.headers.get('Content-Range'),length:get.headers.get('Content-Length'),
                contentType:get.headers.get('Content-Type'),etag:!!get.headers.get('ETag'),
                rejection:rejected.status,retry:retry.status,retryLength:retried.length,errors};
            })()""".replace("PAYLOAD", json.dumps(request))
            command(
                [
                    "agent-browser",
                    "--session",
                    session["name"],
                    "open",
                    public + "/" if same else origin + "/",
                ],
                timeout=45,
            )
            output = command(
                [
                    "agent-browser",
                    "--session",
                    session["name"],
                    "--json",
                    "eval",
                    "--stdin",
                ],
                input_data=javascript.encode(),
                timeout=45,
            )
            parsed = json.loads(output)
            result = parsed.get("data", {}).get("result", parsed.get("result"))
            require(isinstance(result, dict), "browser_result_shape")
            evidence.update(result)
            # Reserve successful object payloads; the bounded PNG response is known.
            budget.consume("upload", len(data), (bucket, key))
            budget.consume(
                "download",
                int(result["length"]) + result["rangeLength"] + result["retryLength"],
            )
            require(
                result["put"] == 200
                and result["get"] == 200
                and result["digest"] == sha256(data),
                "browser_object",
            )
            require(
                result["range"] == 206
                and result["rangeLength"] == 16
                and result["rangeHeader"] == f"bytes 0-15/{len(data)}",
                "browser_range_headers",
            )
            require(
                result["contentType"] == "image/png"
                and int(result["length"]) == len(data)
                and result["etag"],
                "browser_exposed_headers",
            )
            require(
                result["rejection"] in {400, 403}
                and result["retry"] == 200
                and not result["errors"],
                "browser_rejection_retry",
            )
            # A newly signed retry uses the current application signer.
            fresh = service.generate_download_url(
                key, bucket=bucket, expires_in=121, align=False
            )
            retry_js = (
                "(async()=>{const r=await fetch("
                + json.dumps(fresh)
                + ");const b=await r.arrayBuffer();return {status:r.status,bytes:b.byteLength}})()"
            )
            output = json.loads(
                command(
                    [
                        "agent-browser",
                        "--session",
                        session["name"],
                        "--json",
                        "eval",
                        "--stdin",
                    ],
                    input_data=retry_js.encode(),
                    timeout=30,
                )
            )
            retried = output.get("data", {}).get("result", output.get("result"))
            require(
                retried == {"status": 200, "bytes": len(data)},
                "browser_new_signature_retry",
            )
            budget.consume("download", len(data))
            evidence["new_signature_retry"] = True
            if not same:
                preflights = [
                    line
                    for line in owner.action(proxy, "logs").splitlines()
                    if line.startswith("OPTIONS ")
                ]
                require(
                    any(line in {"OPTIONS 200", "OPTIONS 204"} for line in preflights),
                    "browser_preflight_absent",
                )
                evidence["successful_preflights"] = sum(
                    line in {"OPTIONS 200", "OPTIONS 204"} for line in preflights
                )
    command(["agent-browser", "--session", session["name"], "close"], timeout=30)
    session["closed"] = True
    owner.save()


def lifecycle_case(recorder, service):
    with recorder.case("lifecycle_configuration") as evidence:
        # Seed the two legacy application-owned rules to exercise actual removal.
        for bucket, rule in (
            (service.bucket, "comment-attachments-90d"),
            (service.import_bucket, "import-artifacts-7d"),
        ):
            service.client.put_bucket_lifecycle_configuration(
                Bucket=bucket,
                LifecycleConfiguration={
                    "Rules": [
                        {
                            "ID": rule,
                            "Status": "Enabled",
                            "Filter": {"Prefix": ""},
                            "Expiration": {"Days": 7},
                        }
                    ]
                },
            )
        service._ensure_lifecycle()
        media = service.client.get_bucket_lifecycle_configuration(
            Bucket=service.media_cache_bucket
        )["Rules"]
        export = service.client.get_bucket_lifecycle_configuration(
            Bucket=service.export_bucket
        )["Rules"]
        require(
            len(media) == 1
            and media[0]["ID"] == "media-cache-frames-chunks-30d"
            and media[0]["Filter"] == {"Prefix": "videos/"}
            and media[0]["Expiration"] == {"Days": 30},
            "media_lifecycle_roundtrip",
        )
        require(
            len(export) == 1
            and export[0]["ID"] == "export-artifacts-7d"
            and export[0]["Filter"] == {"Prefix": ""}
            and export[0]["Expiration"] == {"Days": 7},
            "export_lifecycle_roundtrip",
        )
        absent = []
        for bucket in (
            service.bucket,
            service.import_bucket,
            service.bug_reports_bucket,
            service.audit_archive_bucket,
            service.avatars_bucket,
        ):
            expect_error(
                lambda bucket=bucket: service.client.get_bucket_lifecycle_configuration(
                    Bucket=bucket
                ),
                statuses={404},
                codes={"NoSuchLifecycleConfiguration"},
            )
            absent.append(bucket.rsplit("-", 1)[-1])
        require(
            all(
                not "playback/".startswith(rule["Filter"]["Prefix"])
                and not "thumbnails/".startswith(rule["Filter"]["Prefix"])
                for rule in media
            ),
            "persistent_preview_scope",
        )
        evidence.update(
            media_rules=media,
            export_rules=export,
            persistent_buckets_without_lifecycle=len(absent),
            native_expiry_verified=False,
        )
    with recorder.case("encryption_probe") as evidence:
        evidence["platform_requests_sse"] = False
        evidence["probe"] = "SSE-S3 AES256 without external KMS"
        body = b"explicit-encryption-probe"
        result = service.client.put_object(
            Bucket=service.datasets_bucket,
            Key="encryption/probe",
            Body=body,
            ServerSideEncryption="AES256",
        )
        response = service.client.get_object(
            Bucket=service.datasets_bucket, Key="encryption/probe"
        )
        if (
            result.get("ServerSideEncryption") != "AES256"
            or response.get("ServerSideEncryption") != "AES256"
        ):
            response["Body"].close()
        require(
            result.get("ServerSideEncryption") == "AES256"
            and response.get("ServerSideEncryption") == "AES256",
            "sse_response_missing",
        )
        read_body(response, service._poc_budget, expected=body)
        evidence["sha256"] = sha256(body)


def verify_saved(client, budget, saved):
    require(bool(saved), "confirmed_fixture_empty")
    require(
        sum(item[2] not in {None, "null"} for item in saved) >= 2,
        "confirmed_versions_missing",
    )
    for bucket, key, version, body, metadata in saved:
        params = {"Bucket": bucket, "Key": key}
        if version is not None:
            params["VersionId"] = version
        read_body(client.get_object(**params), budget, expected=body, metadata=metadata)
        head = client.head_object(**params)
        require(
            head["ContentLength"] == len(body) and head.get("Metadata") == metadata,
            "recovery_metadata",
        )
    return len(saved)


def kill_case(recorder, owner, container, client, service, budget, saved):
    with recorder.case("kill_during_write") as evidence:
        expected = b"\x5a" * (32 * 1024 * 1024)
        key = "fault/unconfirmed"
        url = client.generate_presigned_url(
            "put_object",
            Params={"Bucket": service.datasets_bucket, "Key": key},
            ExpiresIn=120,
        )
        split = urllib.parse.urlsplit(url)
        sock = socket.create_connection((split.hostname, split.port), timeout=3)
        sent = 0
        try:
            request = f"PUT {split.path}?{split.query} HTTP/1.1\r\nHost: {split.netloc}\r\nContent-Length: {len(expected)}\r\nConnection: close\r\n\r\n"
            sock.sendall(request.encode())
            chunk = expected[: 256 * 1024]
            sock.sendall(chunk)
            sent += len(chunk)
            budget.consume("upload", len(chunk), (service.datasets_bucket, key))
            time.sleep(0.2)
            owner.action(container, "kill")
            evidence.update(
                bytes_sent=sent,
                declared_bytes=len(expected),
                success_acknowledged=False,
            )
        finally:
            sock.close()
        owner.start(container)
        ready(client, budget, seconds=60)
        evidence["confirmed_versions_retained"] = verify_saved(client, budget, saved)
        try:
            response = client.get_object(Bucket=service.datasets_bucket, Key=key)
        except Exception as exc:
            require(
                getattr(exc, "response", {})
                .get("ResponseMetadata", {})
                .get("HTTPStatusCode")
                == 404,
                "unconfirmed_read_error",
            )
            evidence["unconfirmed_outcome"] = "absent"
        else:
            read_body(response, budget, expected=expected)
            evidence["unconfirmed_outcome"] = "complete"


def volume_restore(
    recorder,
    owner,
    lock,
    candidate,
    container,
    volume,
    network,
    env_file,
    credentials,
    budget,
    saved,
):
    with recorder.case("volume_restore") as evidence:
        owner.action(container, "stop", "--time", "20", timeout=40)
        require(
            not owner.verified(container)["State"]["Running"], "backup_source_running"
        )
        archive = owner.private / "backup"
        archive.mkdir(mode=0o700)
        backup_mounts = [
            "--network",
            "none",
            "--entrypoint",
            "/bin/sh",
            "--mount",
            f"type=volume,source={volume['id']},target=/source,readonly",
            "--mount",
            f"type=bind,source={archive},target=/backup",
        ]
        owner.helper(
            lock["proxy"]["image"],
            args=backup_mounts,
            cmd=[
                "-c",
                "ulimit -f 4194304; tar -C /source -cf /backup/data.tar . && chmod 600 /backup/data.tar",
            ],
        )
        target = owner.create("volume")
        restore_mounts = [
            "--network",
            "none",
            "--entrypoint",
            "/bin/sh",
            "--mount",
            f"type=volume,source={target['id']},target=/restore",
            "--mount",
            f"type=bind,source={archive},target=/backup,readonly",
        ]
        owner.helper(
            lock["proxy"]["image"],
            args=restore_mounts,
            cmd=["-c", "tar -C /restore -xf /backup/data.tar"],
        )
        args, cmd = candidate_args(
            candidate["name"], owner.private, env_file, target, network
        )
        # Source is stopped before alias reuse; restore gets its own published port.
        restored = owner.container(candidate["image"], args=args, cmd=cmd)
        owner.start(restored)
        endpoint = "http://127.0.0.1:" + str(host_port(owner, restored, 9000))
        restored_client = client_for(endpoint, credentials, budget)
        ready(restored_client, budget)
        count = verify_saved(restored_client, budget, saved)
        evidence.update(
            confirmed_versions_read=count,
            archive_bytes=(archive / "data.tar").stat().st_size,
            max_archive_bytes=2 * 1024**3,
            fresh_volume=True,
            source_stopped=True,
            same_provider_same_build=True,
            cross_provider_disk_migration=False,
        )
        owner.action(restored, "stop", "--time", "20", timeout=40)


def performance_case(recorder, owner, container, client, bucket, media, budget):
    with recorder.case("performance") as evidence:
        fixtures_ = [
            media["image.png"][0],
            b"p" * 65536,
            bytes(range(256)) * 4096,
            b"m" * 262144,
        ] * 2
        samples = []
        stop = threading.Event()
        pid = owner.verified(container)["State"]["Pid"]

        def sample():
            while not stop.is_set():
                try:
                    status = Path(f"/proc/{pid}/status").read_text()
                    rss = next(
                        int(line.split()[1]) * 1024
                        for line in status.splitlines()
                        if line.startswith("VmRSS:")
                    )
                    stat = (
                        Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()
                    )
                    samples.append(
                        (
                            time.monotonic(),
                            (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK"),
                            rss,
                        )
                    )
                except (OSError, StopIteration):
                    pass
                stop.wait(0.005)

        thread = threading.Thread(target=sample, daemon=True)
        thread.start()

        def transfer(item, operation, concurrency):
            index, body = item
            key = f"performance/{concurrency}/{index}"
            start = time.monotonic()
            try:
                if operation == "put":
                    client.put_object(Bucket=bucket, Key=key, Body=body)
                else:
                    read_body(
                        client.get_object(Bucket=bucket, Key=key), budget, expected=body
                    )
                return (time.monotonic() - start) * 1000, None, len(body)
            except Exception as exc:
                return (time.monotonic() - start) * 1000, safe_error(exc), 0

        try:
            for concurrency in (1, 4):
                for operation in ("put", "get"):
                    start = time.monotonic()
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=concurrency
                    ) as pool:
                        results = list(
                            pool.map(
                                lambda item: transfer(item, operation, concurrency),
                                enumerate(fixtures_),
                            )
                        )
                    latency = sorted(result[0] for result in results)
                    evidence[f"{operation}_{concurrency}"] = {
                        "objects": len(results),
                        "body_bytes_completed": sum(result[2] for result in results),
                        "elapsed_ms": round((time.monotonic() - start) * 1000, 3),
                        "latency_ms_min": round(min(latency), 3),
                        "latency_ms_median": round(latency[len(latency) // 2], 3),
                        "latency_ms_p95": round(latency[-1], 3),
                        "http_failures": [result[1] for result in results if result[1]],
                    }
        finally:
            stop.set()
            thread.join(timeout=2)
        require(len(samples) >= 2, "resource_samples_missing")
        elapsed = samples[-1][0] - samples[0][0]
        evidence["process_samples"] = {
            "count": len(samples),
            "window_seconds": round(elapsed, 3),
            "rss_bytes_min": min(sample[2] for sample in samples),
            "rss_bytes_sample_peak": max(sample[2] for sample in samples),
            "cpu_seconds_delta": round(samples[-1][1] - samples[0][1], 3),
            "average_cpu_cores": round((samples[-1][1] - samples[0][1]) / elapsed, 3),
            "scope": "candidate main process; sampled peak, not cgroup or exact maximum",
        }
        require(
            not any(
                item.get("http_failures")
                for item in evidence.values()
                if isinstance(item, dict)
            ),
            "performance_http_failures",
        )


def candidate_run(owner, lock, candidate):
    import boto3

    recorder = Recorder()
    budget = Budget()
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    result = {
        "candidate": candidate,
        "started_at": started,
        "cases": recorder.cases,
        "execution_completed": False,
        "limits": {
            "cpus": 4,
            "memory_bytes": 8 * 1024**3,
            "seconds": 900,
            "sdk_attempts": 1,
            "connect_seconds": 3,
            "read_seconds": 15,
        },
    }
    server = None
    stage = "image_inspection"
    try:
        owner.private.mkdir(mode=0o700, exist_ok=True)
        image = json.loads(command(["docker", "image", "inspect", candidate["image"]]))[
            0
        ]
        require(image["Id"] == candidate["config_digest"], "actual_image_config")
        require(
            image["Architecture"] == "amd64" and image["Os"] == "linux",
            "actual_image_platform",
        )
        stage = "binary_probe"
        binary = "/usr/bin/weed" if candidate["name"] == "seaweedfs" else "rustfs"
        version = owner.helper(
            candidate["image"],
            args=["--network", "none", "--entrypoint", "/bin/sh"],
            cmd=[
                "-c",
                f"id -u; id -g; {binary} {'version' if candidate['name'] == 'seaweedfs' else '--version'}",
            ],
        )
        lines = version.splitlines()
        require(
            len(lines) >= 3 and lines[0].isdigit() and lines[1].isdigit(),
            "binary_version_shape",
        )
        require(len(version) < 2048, "binary_version_budget")
        result["observed_binary"] = "\n".join(lines[2:])
        result["observed_image_config"] = {
            "id": image["Id"],
            "entrypoint": image["Config"]["Entrypoint"],
            "cmd": image["Config"].get("Cmd"),
            "user": image["Config"]["User"],
            "source_revision_label": (image["Config"].get("Labels") or {}).get(
                "org.opencontainers.image.revision"
            ),
        }
        stage = "owned_resource_setup"
        network = owner.create("network")
        volume = owner.create("volume")
        owner.helper(
            lock["proxy"]["image"],
            args=[
                "--network",
                "none",
                "--entrypoint",
                "/bin/sh",
                "--mount",
                f"type=volume,source={volume['id']},target=/owned",
            ],
            cmd=["-c", f"chown {int(lines[0])}:{int(lines[1])} /owned"],
        )
        credentials = ("poc" + secrets.token_hex(10), secrets.token_hex(24))
        env_file = owner.private / "candidate.env"
        private_write(
            env_file,
            f"RUSTFS_ACCESS_KEY={credentials[0]}\nRUSTFS_SECRET_KEY={credentials[1]}\nRUSTFS_ADDRESS=0.0.0.0:9000\nRUSTFS_CONSOLE_ENABLE=false\nRUSTFS_OBS_LOGGER_LEVEL=warn\n".encode(),
        )
        seaweed_config = owner.private / "seaweed.json"
        private_write(
            seaweed_config,
            json.dumps(
                {
                    "identities": [
                        {
                            "name": "poc",
                            "credentials": [
                                {
                                    "accessKey": credentials[0],
                                    "secretKey": credentials[1],
                                }
                            ],
                            "actions": ["Admin", "Read", "Write", "List", "Tagging"],
                        }
                    ]
                }
            ).encode(),
        )
        if candidate["name"] == "seaweedfs":
            filer = b'[leveldb2]\nenabled = true\ndir = "/data/filerldb"\n[foundationdb]\nenabled = false\n'
            private_write(owner.private / "filer.toml", filer)
            (owner.private / "filer.toml").chmod(0o644)
            result["local_metadata_store"] = {
                "engine": "leveldb2",
                "directory": "/data/filerldb",
                "configuration_sha256": sha256(filer),
                "foundationdb_instance": False,
            }
            # Secret stays 0600; copy via a root helper into this owned volume.
            owner.helper(
                lock["proxy"]["image"],
                args=[
                    "--network",
                    "none",
                    "--entrypoint",
                    "/bin/sh",
                    "--mount",
                    f"type=volume,source={volume['id']},target=/owned",
                    "--mount",
                    f"type=bind,source={seaweed_config},target=/secret,readonly",
                ],
                cmd=[
                    "-c",
                    f"cp /secret /owned/poc-s3.json && chmod 600 /owned/poc-s3.json && chown {int(lines[0])}:{int(lines[1])} /owned/poc-s3.json",
                ],
            )
        args, cmd = candidate_args(
            candidate["name"], owner.private, env_file, volume, network
        )
        container = owner.container(candidate["image"], args=args, cmd=cmd)
        owner.start(container)
        endpoint = "http://127.0.0.1:" + str(host_port(owner, container, 9000))
        client = client_for(endpoint, credentials, budget)
        stage = "candidate_readiness"
        ready(client, budget)
        buckets = ["poc-" + owner.data["nonce"][:12] + "-" + str(i) for i in range(9)]
        stage = "actual_application_storage"
        service, settings = app_storage(endpoint, credentials, buckets, budget)
        service._poc_budget = budget
        logging.getLogger("app.services.storage").disabled = True
        for bucket in buckets:
            service.ensure_bucket(bucket)
        stage = "local_proxy"
        proxy, public, origin, server = proxy_setup(owner, lock, network, endpoint)
        media = fixtures(owner.private)
        result["fixture_sha256"] = {
            key: sha256(value[0]) for key, value in media.items()
        }
        stage = "case_execution"
        saved = []
        basic_cases(recorder, client, service.bucket, budget, saved)
        version_cases(recorder, client, service.bucket, budget, saved)
        multipart_cases(recorder, client, service.datasets_bucket, budget)
        media_cases(recorder, service, settings, public, media, budget, owner.private)
        browser_cases(
            recorder, owner, service, settings, public, origin, proxy, media, budget
        )
        lifecycle_case(recorder, service)
        performance_case(
            recorder, owner, container, client, service.datasets_bucket, media, budget
        )
        with recorder.case("restart") as evidence:
            owner.action(container, "restart", "--time", "20", timeout=60)
            ready(client, budget, seconds=60)
            evidence["confirmed_versions_retained"] = verify_saved(
                client, budget, saved
            )
        kill_case(recorder, owner, container, client, service, budget, saved)
        volume_restore(
            recorder,
            owner,
            lock,
            candidate,
            container,
            volume,
            network,
            env_file,
            credentials,
            budget,
            saved,
        )
        result["execution_completed"] = True
        result["sdk"] = {"boto3": boto3.__version__}
    except Exception as exc:
        result["setup_or_runner_error"] = safe_error(exc)
        result["runner_stage"] = stage
        for case in recorder.cases.values():
            if case["stage"] == "pending":
                case["stage"] = "dependency_setup_failed"
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        result["budget"] = budget.summary()
        result["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return summarize(result)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("--manifest", required=True, type=Path)
    run.add_argument("--report", required=True, type=Path)
    run.add_argument(
        "--concurrent-load-note",
        default="Shared host; concurrent workloads not isolated or exhaustively inventoried",
    )
    clean = sub.add_parser("cleanup")
    clean.add_argument("--manifest", required=True, type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    owner = None
    try:
        if args.action == "cleanup":
            owner = Owner(args.manifest)
            good = owner.cleanup()
            print(
                json.dumps(
                    {
                        "cleanup_verified": good,
                        "resources": len(owner.data["resources"]),
                    }
                )
            )
            return 0 if good else 2
        require(len(args.concurrent_load_note) <= 256, "load_note_budget")
        require(not args.report.exists(), "report_exists")
        lock = load_lock(ROOT / "scripts/storage_s3_candidates.lock.json", ROOT)
        owner = Owner(args.manifest, create=True)
        # The command's complete input is the lock; inherited deployment secrets
        # are discarded before any application module can be imported.
        for key in list(os.environ):
            if key.startswith(("MINIO_", "ML_BACKEND_STORAGE_", "AWS_")):
                del os.environ[key]
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        report = {
            "schema": SCHEMA,
            "scope": "isolated local single-node comparison",
            "matrix_origin": "E07 section 5",
            "category_names": {
                "basic_objects": "基础对象",
                "browser_upload": "浏览器上传",
                "large_objects": "大对象",
                "media_reads": "媒体读取",
                "checksums": "校验和",
                "lifecycle": "生命周期",
                "frozen_versions": "冻结版本",
                "operations": "运维",
            },
            "selection_ready": False,
            "production_ready": False,
            "host": {
                "os": platform.platform(),
                "logical_cpus": os.cpu_count(),
                "memory_bytes": os.sysconf("SC_PAGE_SIZE")
                * os.sysconf("SC_PHYS_PAGES"),
                "docker": command(
                    ["docker", "version", "--format", "{{.Server.Version}}"]
                ),
                "python": platform.python_version(),
                "concurrent_load": args.concurrent_load_note,
            },
            "source_fingerprints": {
                str(path.relative_to(ROOT)): sha256(path.read_bytes())
                for path in (
                    ROOT / "apps/api/app/services/storage.py",
                    ROOT / "apps/api/app/services/storage_presigning.py",
                    ROOT / "scripts/storage_s3_candidates.lock.json",
                    ROOT / "scripts/storage_s3_contract.py",
                    ROOT / "scripts/verify_storage_candidates.py",
                )
            },
            "candidates": [],
        }
        for candidate in lock["candidates"]:
            result = candidate_run(owner, lock, candidate)
            report["candidates"].append(result)
            # Separate providers reuse no volumes, ports or credentials.
            good = owner.cleanup()
            result["cleanup_verified"] = good
            if not good:
                break
            owner.data["cleaned"] = False
            owner.private.mkdir(mode=0o700)
            owner.save()
            print(
                json.dumps(
                    {
                        "candidate": candidate["name"],
                        "counts": result["counts"],
                        "execution_completed": result["execution_completed"],
                        "local_baseline_passed": result["local_baseline_passed"],
                    }
                ),
                flush=True,
            )
        report["execution_completed"] = len(report["candidates"]) == 2 and all(
            item["execution_completed"] for item in report["candidates"]
        )
        report["local_baseline_passed"] = all(
            item["local_baseline_passed"] for item in report["candidates"]
        )
        report["cleanup_verified"] = owner.cleanup()
        private_write(args.report, public_bytes(report))
        return 0 if report["execution_completed"] and report["cleanup_verified"] else 1
    except KeyboardInterrupt:
        print('{"error_code":"interrupted"}')
        return 130
    except Exception as exc:
        print(json.dumps({"error_code": safe_error(exc)}))
        return 2
    finally:
        if owner is not None:
            owner.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
