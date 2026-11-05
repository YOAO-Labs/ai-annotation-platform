#!/usr/bin/env python3
"""Run the Mask product family against a fresh, owned pre-consumer fault store.

The outer process owns the worktree mode lock and the MinIO administration
identity.  The inner process prepares the exact fixtures before starting any
product listener, then passes only the scoped data-plane identity to Playwright.
An interrupted or failed run retains its owner journal and resources.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
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
from storage_s3_contract import ContractError, require  # noqa: E402
from verify_storage_candidates import Owner, command, host_port, private_write  # noqa: E402
from worktree_env import load_identity, state_path, topology  # noqa: E402
from worktree_runtime import (  # noqa: E402
    environment_lock,
    parse_arguments,
    reject_orphans,
    run_processes,
)


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
                "TagSet": [{"Key": "aap-worktree-owner", "Value": resources["owner"]}]
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
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        require(bool(line), "raster_seed_process_exited")
        reply = json.loads(line)
        if reply["status"] != 200:
            owner.data["raster"]["seed_failure"] = {
                "operation": payload.get("operation", payload.get("path")),
                "status": reply["status"],
            }
            owner.save()
            raise ContractError(f"raster_seed_http_{reply['status']}")
        return json.loads(reply["body"])

    cases, references = {"diagnosis": [], "retry": []}, []
    owner.data["raster"].update(
        {"state": "suite_creation_intent", "suite_id": suite, "cases": []}
    )
    owner.save()
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
    finally:
        process.stdin.close()
        status = process.wait()
        process.stdout.close()
        require(status == 0, "raster_seed_process_failed")
    return cases, references


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
    require(
        {(item["kind"], item["id"]) for item in owner.data["resources"]}
        == {
            ("container", store["container_id"]),
            ("network", store["network_id"]),
            ("volume", store["volume_id"]),
        },
        "raster_store_topology_changed",
    )
    container = next(
        item for item in owner.data["resources"] if item["id"] == store["container_id"]
    )
    network = next(
        item for item in owner.data["resources"] if item["id"] == store["network_id"]
    )
    volume = next(
        item for item in owner.data["resources"] if item["id"] == store["volume_id"]
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
        require(queue.dbsize() == 0, "raster_queue_not_empty")
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
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        require(pid != 0, "raster_consumer_descendants_still_running")


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


def run_product(command, environment, api, relay):
    child = subprocess.Popen(command, cwd=ROOT / "apps/web", env=environment)
    try:
        while True:
            try:
                result = child.wait(timeout=0.2)
                require(api.poll() is None, "raster_api_exited")
                require(
                    relay.seed.poll() is None and relay.failure is None,
                    "raster_fixture_relay_exited",
                )
                return result
            except subprocess.TimeoutExpired:
                require(api.poll() is None, "raster_api_exited")
                require(
                    relay.seed.poll() is None and relay.failure is None,
                    "raster_fixture_relay_exited",
                )
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait()


def inside(path, arguments):
    # Adopt every orphaned descendant so a successful exit proves the browser,
    # servers and fixture transports have actually stopped before destruction.
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.prctl(36, 1, 0, 0, 0) == 0, "raster_subreaper_unavailable")
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
    suite = str(uuid.uuid4())
    cases, references = request_fixtures(
        owner, restricted, suite, configured_attempts(arguments)
    )
    require_no_children()
    exercise_faults(owner, os.environ, references, suite)
    manifest = state_path(ROOT, "e2e", "data", f"raster-fault-{suite}.json")
    private_write(
        manifest,
        json.dumps(
            {"schema": "aap-e2e-raster-fault/v1", "suite_id": suite, "cases": cases}
        ).encode(),
    )
    from raster_fixture_relay import RasterFixtureRelay
    from worktree_env import application_environment, frontend_environment
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
            "AAP_WORKTREE_MODE": "e2e",
            "PLAYWRIGHT_FIXTURE_RUN_ID": suite,
            "PLAYWRIGHT_RASTER_FAULT_MANIFEST": str(manifest),
            "PLAYWRIGHT_RASTER_MASK_MATRIX": "native",
            "PLAYWRIGHT_RASTER_OFFLINE": "1",
        }
    )
    api_port = int(restricted["PLAYWRIGHT_ISOLATED_API_PORT"])
    require(1 <= api_port <= 65535, "raster_api_port_invalid")
    api_environment = application_environment(restricted)
    api_environment.update(
        {
            "RASTER_MASK_READ_ENABLED": "true",
            "RASTER_MASK_CREATE_ENABLED": "true",
        }
    )
    entries = [entry for group in cases.values() for entry in group]
    with tempfile.TemporaryDirectory(prefix="aap-raster-", dir="/tmp") as socket_dir:
        socket_path = str(Path(socket_dir) / "fixture.sock")
        browser_environment["PLAYWRIGHT_RASTER_FIXTURE_SOCKET"] = socket_path
        relay = RasterFixtureRelay(socket_path, suite, entries, restricted)
        api = None
        try:
            api = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                ],
                cwd=ROOT / "apps/api",
                env=api_environment,
            )
            deadline = time.monotonic() + 120
            while not http_ready(api_port, "/health/db"):
                require(
                    api.poll() is None and relay.seed.poll() is None,
                    "raster_api_start_failed",
                )
                require(time.monotonic() < deadline, "raster_api_start_timeout")
                time.sleep(0.1)
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
            )
        finally:
            try:
                relay.close()
            finally:
                if api is not None and api.poll() is None:
                    api.terminate()
                    api.wait(timeout=12)
    require_no_children()
    if result:
        return result
    browser_environment.pop("PLAYWRIGHT_RASTER_FIXTURE_SOCKET")
    result = subprocess.run(
        [
            "pnpm",
            "exec",
            "playwright",
            "test",
            "--config=playwright.browser-integration.config.ts",
            "--project=chromium",
            *arguments,
        ],
        cwd=ROOT / "apps/web",
        env=browser_environment,
    ).returncode
    require_no_children()
    if result == 0:
        owner = Owner(path)
        owner.data["raster"]["state"] = "consumers_stopped"
        owner.save()
    return result


def outer(arguments):
    require(
        not os.environ.get("AAP_WORKTREE_MODE"), "raster_launcher_must_wrap_worktree"
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
                }
            )
            from app.config import settings
            from worktree_resources import LocalResources

            require(
                settings.environment == "development", "raster_development_required"
            )
            backend = LocalResources(resources, settings, code_root=ROOT)
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
            result = run_processes(backend, options)
            owner = Owner(manifest)
            owner.data["raster"]["consumer_exit"] = result
            owner.save()
            if result != 0 or owner.data["raster"]["state"] != "consumers_stopped":
                return result or 1
            verify_store_physical(owner)
            backend.destroy(resources["confirmation"])
            owner.data["raster"]["state"] = "worktree_destroyed"
            owner.save()
            require(owner.cleanup(), "raster_store_cleanup_failed")
            return 0
        except BaseException as exc:
            owner = Owner(manifest)
            owner.data["raster_failure"] = type(exc).__name__
            owner.save()
            raise


if __name__ == "__main__":
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == "--inside":
            raise SystemExit(inside(sys.argv[2], sys.argv[3:]))
        raise SystemExit(outer(sys.argv[1:]))
    except Exception as error:
        detail = (
            str(error) if isinstance(error, ContractError) else type(error).__name__
        )
        if len(sys.argv) >= 3 and sys.argv[1] == "--inside":
            try:
                owner = Owner(sys.argv[2])
                owner.data["raster_failure"] = detail
                owner.save()
            except Exception:
                print(
                    "Raster fault failure journal could not be updated", file=sys.stderr
                )
        print(f"Raster fault launcher failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None
