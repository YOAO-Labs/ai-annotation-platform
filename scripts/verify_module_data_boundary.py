"""Run the module-data LOGIN-role acceptance against owned local resources.

Usage: apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
    --output /tmp/aap-module-data-acceptance.json

The JSON report is paired with a sibling JUnit XML file. Only this invocation's
PostgreSQL container and LocalResources test topology are created or removed.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4
import shutil
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

CONTAINER_LABEL = "org.aap.acceptance"
CONTAINER_LABEL_VALUE = "module-data-boundary"
CONTAINER_ID_LABEL = "org.aap.acceptance-id"
POSTGRES_IMAGE = "postgres:16-alpine"
MODULE_ROLES = (
    "identity_code",
    "workspace_code",
    "assets_code",
    "labeling_code",
    "jobs_code",
    "learning_code",
    "inference_code",
    "insights_code",
    "diagnostics_code",
    "export_code",
    "audit_code",
    "settings_code",
    "notifications_code",
    "append_client",
    "asset_cleanup_client",
    "asset_catalog_client",
    "asset_source_client",
    "asset_import_client",
)
SQL_ARTIFACTS = (
    "0190_workspace_data_contract.sql",
    "0191_dataset_task_append_contract.sql",
    "0192_creation_authority.sql",
    "0193_task_creation_jobs.sql",
    "0194_initial_task_creation.sql",
    "0195_task_append_timestamp.sql",
    "0196_project_deletion_retention.sql",
    "0197_project_labeling_delete.sql",
    "0198_project_owned_cleanup_contract.sql",
    "0199_project_deletion.sql",
    "0200_deleted_project_job_scope.sql",
    "0201_dataset_source_retention.sql",
    "0202_dataset_unlink_labeling.sql",
    "0203_dataset_unlink_assets.sql",
    "0204_dataset_unlink_jobs_audit.sql",
    "0205_dataset_unlink.sql",
    "0206_dataset_source_guards.sql",
    "0207_dataset_upload_intents.sql",
    "0208_dataset_source_support.sql",
    "0209_dataset_source_deletion.sql",
    "0210_asset_storage_cleanup.sql",
    "0211_dataset_catalog.sql",
    "0212_asset_association_reads.sql",
    "0213_assets_source_runtime.sql",
    "0214_dataset_import_delivery.sql",
    "0214_dataset_import_support.sql",
    "0214_dataset_import_core.sql",
    "0215_dataset_source_ingest.sql",
    "0215_dataset_source_ingest_delivery.sql",
)
SCHEMA_ARTIFACTS = (
    "0191_task_append.schema.json",
    "0192_creation_authority.schema.json",
    "0193_task_creation_jobs.schema.json",
    "0194_task_creation.schema.json",
    "0196_project_deletion_retention.schema.json",
    "0197_project_labeling_delete.schema.json",
    "0198_project_owned_cleanup_contract.schema.json",
    "0199_project_deletion.schema.json",
    "0201_dataset_source_retention.schema.json",
    "0202_dataset_unlink_labeling.schema.json",
    "0203_dataset_unlink_assets.schema.json",
    "0204_dataset_unlink_jobs_audit.schema.json",
    "0205_dataset_unlink.schema.json",
    "0206_dataset_source_guards.schema.json",
    "0207_dataset_upload_intents.schema.json",
    "0208_dataset_source_support.schema.json",
    "0209_dataset_source_deletion.schema.json",
    "0210_asset_storage_cleanup.schema.json",
    "0211_dataset_catalog.schema.json",
    "0212_asset_association_reads.schema.json",
    "0213_assets_source_runtime.schema.json",
    "0214_dataset_import.schema.json",
    "0214_dataset_import_delivery.schema.json",
    "0215_dataset_source_ingest.schema.json",
)
TEST_FILES = (
    "tests/test_workspace_data_contract.py",
    "tests/test_task_append_data_contract.py",
    "tests/test_task_append_consumer.py",
    "tests/test_creation_authority_contract.py",
    "tests/test_task_creation_job_contract.py",
    "tests/test_initial_task_creation_contract.py",
    "tests/test_task_creation_bindings.py",
    "tests/test_task_creation_runtime.py",
    "tests/test_task_creation_handoff.py",
    "tests/test_project_owned_cleanup_contract.py",
    "tests/test_project_labeling_delete_contract.py",
    "tests/test_project_deletion_retention.py",
    "tests/test_project_deletion_bindings.py",
    "tests/test_deleted_project_job_scope.py",
    "tests/test_project_deletion_data.py",
    "tests/test_model_lineage.py",
    "tests/test_project_task_datasets.py",
    "tests/test_sample_evaluations.py",
    "tests/test_async_jobs.py",
    "tests/test_async_job_actor_freshness.py",
    "tests/test_notifications.py",
    "tests/test_notification_delivery_scope.py",
    "tests/test_pointcloud_box3d_annotation.py",
    "tests/test_pointcloud_import.py",
    "tests/test_pointcloud_manifest.py",
    "tests/test_pointcloud_scene_e2e.py",
    "tests/test_module_data_roles.py",
    "tests/test_datasets_upload_zip.py",
    "tests/test_scene_inference.py",
    "tests/test_mask_formats_m6.py",
    "tests/test_assets_source_ingest.py",
    "tests/test_dataset_source_ingest_native.py",
    "tests/test_dataset_source_ingest_runtime.py",
    "tests/test_dataset_source_ingest_delivery.py",
    "tests/test_dataset_import.py",
    "tests/test_dataset_import_native.py",
    "tests/test_dataset_import_migration.py",
    "tests/test_assets_import_bindings.py",
    "tests/test_dataset_import_cancellation.py",
    "tests/test_dataset_import_runtime.py",
    "tests/test_dataset_import_delivery.py",
    "tests/test_dataset_import_delivery_native.py",
    "tests/test_dataset_link.py",
    "tests/test_dataset_link_kind_match.py",
    "tests/test_dataset_source_deletion_bindings.py",
    "tests/test_dataset_source_deletion.py",
    "tests/test_dataset_source_http.py",
    "tests/test_dataset_source_guards.py",
    "tests/test_dataset_source_support.py",
    "tests/test_dataset_upload_data.py",
    "tests/test_dataset_upload_native.py",
    "tests/test_dataset_upload_storage.py",
    "tests/test_dataset_upload_s3_runtime.py",
    "tests/test_dataset_source_retention.py",
    "tests/test_dataset_unlink_labeling.py",
    "tests/test_dataset_unlink_assets.py",
    "tests/test_dataset_unlink_jobs_audit.py",
    "tests/test_dataset_unlink_coordinator.py",
    "tests/test_dataset_unlink_bindings.py",
    "tests/test_dataset_unlink_http.py",
    "tests/test_asset_storage_cleanup_runtime.py",
    "tests/test_assets_source_native.py",
    "tests/test_assets_source_runtime.py",
    "tests/test_assets_source_gateway.py",
    "tests/test_assets_catalog_native.py",
    "tests/test_assets_catalog_runtime.py",
    "tests/test_asset_association_reads.py",
    "tests/test_asset_association_runtime.py",
    "tests/test_assets_associations_gateway.py",
    "tests/test_dataset_api_scopes.py",
    "tests/test_datasets_list_has_scenes.py",
    "tests/test_dataset_axis_convention.py",
    "tests/test_media_materialization.py",
    "tests/test_tool_bindings_helpers.py",
    "tests/test_worker_signals.py",
    "tests/test_security_headers.py",
    "tests/test_create_tasks_worker.py",
    "tests/test_batch_delete_force.py",
    "tests/test_batch_delete_reset.py",
    "tests/test_batch_split_by_scene.py",
    "tests/test_batch_split_semantics.py",
    "tests/test_project_scene_mode.py",
    "tests/test_operational_ingest_boundaries.py",
    "tests/test_storage_connections_api.py",
)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create one labeled disposable PostgreSQL instance, prepare the owned "
            "test-mode resources, and run the module-data role acceptance."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="evidence JSON path; a sibling .junit.xml report is written by default",
    )
    parser.add_argument(
        "--junit-xml",
        type=Path,
        help="explicit JUnit XML path (defaults beside --output)",
    )
    return parser.parse_args(argv)


def _write_evidence(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _cleanup_storage_scope(backend) -> dict:
    from worktree_resources import docker

    endpoint = urlsplit(f"http://{backend.settings.minio_endpoint}")
    matches = []
    for candidate in docker(
        "ps",
        "--filter",
        "label=com.docker.compose.service=minio",
        "--format",
        "{{.ID}}",
    ).splitlines():
        observed = json.loads(docker("inspect", candidate))[0]
        bindings = (observed.get("NetworkSettings", {}).get("Ports") or {}).get(
            "9000/tcp"
        ) or []
        if any(
            int(binding.get("HostPort", 0)) == (endpoint.port or 9000)
            for binding in bindings
        ):
            matches.append(observed)
    if len(matches) != 1:
        raise RuntimeError("The local MinIO admin container is not uniquely identified")
    bucket = backend.resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"]
    tags = backend.s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
    if not any(
        tag["Key"] == "aap-worktree-owner"
        and tag["Value"] == backend.resources["owner"]
        for tag in tags
    ):
        raise RuntimeError("The cleanup bucket is not owned by this acceptance")
    token = secrets.token_hex(8)
    scheme = "https" if backend.settings.minio_use_ssl else "http"
    return {
        "container": matches[0]["Id"],
        "bucket": bucket,
        "access_key": f"aapcln{token[:14]}",
        "secret_key": secrets.token_hex(24),
        "policy": f"aap-cleanup-{token}",
        "config": f"/tmp/aap-cleanup-{token}",
        "host": f"{scheme}://{quote(backend.settings.minio_access_key, safe='')}:{quote(backend.settings.minio_secret_key, safe='')}@127.0.0.1:9000",
        "user_created": False,
        "policy_created": False,
    }


def _cleanup_mc(
    scope: dict, arguments: list[str], *, payload: str | None = None
) -> None:
    from worktree_resources import docker

    observed = json.loads(docker("inspect", scope["container"]))[0]
    if not (
        observed.get("Id") == scope["container"]
        and observed.get("State", {}).get("Running") is True
        and (observed.get("Config", {}).get("Labels") or {}).get(
            "com.docker.compose.service"
        )
        == "minio"
    ):
        raise RuntimeError("The MinIO admin container identity changed")
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "-e",
            f"MC_HOST_scoped={scope['host']}",
            "-e",
            f"MC_CONFIG_DIR={scope['config']}",
            scope["container"],
            "/usr/bin/mc",
            "--json",
            *arguments,
        ],
        input=payload,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("MinIO cleanup identity administration failed")


def _create_cleanup_storage_scope(scope: dict) -> None:
    _cleanup_mc(
        scope,
        ["admin", "user", "add", "scoped", scope["access_key"], scope["secret_key"]],
    )
    scope["user_created"] = True
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{scope['bucket']}"],
                "Condition": {"StringLike": {"s3:prefix": ["image-pyramids/*"]}},
            },
            {
                "Effect": "Allow",
                "Action": ["s3:DeleteObject"],
                "Resource": [f"arn:aws:s3:::{scope['bucket']}/image-pyramids/*"],
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


def _destroy_cleanup_storage_scope(scope: dict) -> None:
    from worktree_resources import docker

    if scope["user_created"]:
        _cleanup_mc(scope, ["admin", "user", "remove", "scoped", scope["access_key"]])
        scope["user_created"] = False
    if scope["policy_created"]:
        _cleanup_mc(scope, ["admin", "policy", "remove", "scoped", scope["policy"]])
        scope["policy_created"] = False
    docker("exec", scope["container"], "rm", "-rf", "--", scope["config"])


def _create_catalog_storage_scope(scope: dict) -> None:
    _cleanup_mc(
        scope,
        ["admin", "user", "add", "scoped", scope["access_key"], scope["secret_key"]],
    )
    scope["user_created"] = True
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [
                    f"arn:aws:s3:::{scope['datasets_bucket']}/*",
                    *(
                        f"arn:aws:s3:::{scope['bucket']}/{prefix}/*"
                        for prefix in (
                            "thumbnails",
                            "videos",
                            "playback",
                            "image-pyramids",
                        )
                    ),
                ],
            }
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


def _create_source_storage_scope(scope: dict) -> None:
    _cleanup_mc(
        scope,
        ["admin", "user", "add", "scoped", scope["access_key"], scope["secret_key"]],
    )
    scope["user_created"] = True
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": [
                    f"arn:aws:s3:::{scope['import_bucket']}/dataset-uploads/*"
                ],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{scope['datasets_bucket']}/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{scope['datasets_bucket']}"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
                "Resource": [
                    f"arn:aws:s3:::{scope['datasets_bucket']}/dataset-sources/*"
                ],
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


def _create_import_storage_scope(scope: dict, *, source: bool) -> None:
    _cleanup_mc(
        scope,
        ["admin", "user", "add", "scoped", scope["access_key"], scope["secret_key"]],
    )
    scope["user_created"] = True
    if source:
        statements = [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{scope['source_bucket']}"],
                "Condition": {
                    "StringLike": {"s3:prefix": ["connector-import-source/*"]}
                },
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [
                    f"arn:aws:s3:::{scope['source_bucket']}/connector-import-source/*"
                ],
            },
        ]
    else:
        statements = [
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
                "Resource": [
                    f"arn:aws:s3:::{scope['datasets_bucket']}/dataset-sources/*"
                ],
            }
        ]
    _cleanup_mc(
        scope,
        ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
        payload=json.dumps({"Version": "2012-10-17", "Statement": statements}),
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


def _container_details(
    container_id: str, name: str, identity: str, *, require_running: bool
):
    from worktree_resources import docker

    observed = json.loads(docker("inspect", container_id))[0]
    labels = observed.get("Config", {}).get("Labels") or {}
    if (
        observed.get("Id") != container_id
        or observed.get("Name") != f"/{name}"
        or observed.get("Config", {}).get("Image") != POSTGRES_IMAGE
        or labels.get(CONTAINER_LABEL) != CONTAINER_LABEL_VALUE
        or labels.get(CONTAINER_ID_LABEL) != identity
    ):
        raise RuntimeError("PostgreSQL container ownership verification failed")
    if require_running and not observed.get("State", {}).get("Running"):
        raise RuntimeError("owned PostgreSQL container is not running")
    bindings = (observed.get("NetworkSettings", {}).get("Ports") or {}).get(
        "5432/tcp"
    ) or []
    if require_running and (
        len(bindings) != 1 or bindings[0].get("HostIp") != "127.0.0.1"
    ):
        raise RuntimeError("PostgreSQL must have one loopback-only published port")
    port = int(bindings[0]["HostPort"]) if bindings else None
    return port


def _psql(
    container_id: str,
    container_name: str,
    container_identity: str,
    *,
    username: str,
    password: str,
    database: str,
    sql: str,
    variables: dict[str, str] | None = None,
) -> str:
    _container_details(
        container_id, container_name, container_identity, require_running=True
    )
    command = [
        "docker",
        "exec",
        "-i",
        "-e",
        f"PGPASSWORD={password}",
        container_id,
        "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-h",
        "127.0.0.1",
        "-p",
        "5432",
        "-U",
        username,
        "-d",
        database,
        "-v",
        "ON_ERROR_STOP=1",
    ]
    for name, value in (variables or {}).items():
        command.extend(("-v", f"{name}={value}"))
    command.extend(("-f", "-"))
    result = subprocess.run(
        command,
        input=sql,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"psql acceptance phase failed with exit {result.returncode}"
        )
    return result.stdout


def _wait_for_postgres(container_id: str, name: str, identity: str) -> None:
    _container_details(container_id, name, identity, require_running=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "docker",
                "exec",
                container_id,
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
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError("disposable PostgreSQL did not become ready")


def _create_runtime_role(
    container_id: str,
    container_name: str,
    container_identity: str,
    password: str,
    owner_password: str,
) -> None:
    _psql(
        container_id,
        container_name,
        container_identity,
        username="owner",
        password=owner_password,
        database="postgres",
        sql=(
            "CREATE ROLE legacy_runtime LOGIN "
            f"PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
        ),
    )


def _create_module_roles(
    container_id: str,
    container_name: str,
    container_identity: str,
    database: str,
    owner_password: str,
    append_password: str,
    cleanup_password: str,
    catalog_password: str,
    source_password: str,
    import_password: str,
) -> None:
    _psql(
        container_id,
        container_name,
        container_identity,
        username="owner",
        password=owner_password,
        database=database,
        sql=(
            "CREATE ROLE identity_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE workspace_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE assets_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE labeling_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE jobs_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE learning_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE inference_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE insights_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE diagnostics_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE export_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE audit_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE settings_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE notifications_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE append_client LOGIN "
            f"PASSWORD '{append_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE asset_cleanup_client LOGIN "
            f"PASSWORD '{cleanup_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE asset_catalog_client LOGIN "
            f"PASSWORD '{catalog_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE asset_source_client LOGIN "
            f"PASSWORD '{source_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE asset_import_client LOGIN "
            f"PASSWORD '{import_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
        ),
    )


def _run_alembic(backend, action: str, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", action, revision],
        cwd=ROOT / "apps/api",
        env=backend.environment(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"disposable Alembic {action} failed with exit {result.returncode}"
        )


def _install_and_rollback_frozen_sql(
    container_id: str,
    container_name: str,
    container_identity: str,
    database: str,
    owner_password: str,
    artifacts: tuple[Path, ...],
) -> None:
    install_sql = "\n".join(path.read_text(encoding="utf-8") for path in artifacts)
    sql = (
        "\\set ON_ERROR_STOP on\n"
        "BEGIN;\n"
        "SELECT 1 / (current_database() = :'expected_database')::integer;\n"
        f"{install_sql}\n"
        "SELECT aap_identity_v1.lock_active_actor(NULL::uuid);\n"
        "SELECT aap_workspace_v1.lock_project_manager(NULL::uuid, NULL::uuid);\n"
        "SELECT aap_assets_v1.associate_project(NULL::uuid, NULL::uuid, NULL::uuid);\n"
        "SELECT aap_assets_v1.lock_task_creation_items(NULL::uuid, NULL::uuid[]);\n"
        "SELECT aap_jobs_v1.create_task_creation_job(NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid);\n"
        "SELECT aap_jobs_v1.find_task_creation_job(NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid);\n"
        "SELECT aap_jobs_v1.claim_task_creation_job(NULL::uuid, NULL::text);\n"
        "SELECT aap_jobs_v1.lock_task_creation_job(NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid);\n"
        "SELECT aap_jobs_v1.finish_task_creation_job(NULL::uuid, NULL::uuid);\n"
        "SELECT aap_jobs_v1.fail_task_creation_job(NULL::uuid, NULL::uuid, NULL::text);\n"
        "SELECT aap_jobs_v1.cancel_task_creation_job(NULL::uuid, NULL::uuid);\n"
        "SELECT aap_labeling_v1.accept_dataset_task_creation(NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::text, NULL::integer);\n"
        "SELECT aap_labeling_v1.prepare_dataset_task_creation(NULL::uuid, NULL::jsonb);\n"
        "SELECT aap_labeling_v1.read_dataset_task_creation(NULL::uuid);\n"
        "SELECT aap_labeling_v1.advance_dataset_task_creation(NULL::uuid, NULL::integer);\n"
        "SELECT aap_identity_v1.lock_audit_actor(NULL::uuid);\n"
        "SELECT aap_learning_v1.guard_project_deletion(NULL::uuid);\n"
        "SELECT aap_workspace_v1.delete_project(NULL::uuid, NULL::uuid, NULL::uuid, NULL::jsonb);\n"
        "SELECT aap_learning_v1.guard_dataset_item_deletion(NULL::uuid, NULL::uuid);\n"
        "SELECT aap_learning_v1.guard_dataset_unlink(NULL::uuid, NULL::uuid);\n"
        "SELECT aap_workspace_v1.preview_dataset_unlink(NULL::uuid, NULL::uuid, NULL::uuid);\n"
        "SELECT aap_workspace_v1.unlink_dataset(NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::text, NULL::jsonb);\n"
        "SELECT aap_assets_v1.reserve_dataset_upload(NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,NULL::text);\n"
        "SELECT aap_assets_v1.delete_dataset(NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,NULL::jsonb);\n"
        "SELECT count(*) FROM aap_assets_v1.pending_storage_deletions_v2('cache-bucket',NULL::timestamptz,NULL::uuid,NULL::uuid,100);\n"
        "SELECT aap_assets_v1.list_catalog_datasets(NULL::uuid,NULL::text,NULL::text,NULL::boolean,50,0::bigint);\n"
        "SELECT aap_assets_v1.get_catalog_dataset(NULL::uuid,NULL::uuid);\n"
        "SELECT aap_assets_v1.list_catalog_items(NULL::uuid,NULL::uuid,50,0::bigint);\n"
        "SELECT aap_assets_v1.create_catalog_dataset(NULL::uuid,NULL::uuid,NULL::jsonb);\n"
        "SELECT aap_assets_v1.update_catalog_dataset(NULL::uuid,NULL::uuid,NULL::uuid,NULL::jsonb);\n"
        "SELECT aap_assets_v1.list_dataset_projects(NULL::uuid,NULL::uuid);\n"
        "SELECT aap_assets_v1.list_project_datasets(NULL::uuid,NULL::uuid);\n"
        "SELECT aap_assets_v1.lock_legacy_dataset_upload(NULL::uuid,NULL::uuid,NULL::uuid,NULL::uuid);\n"
        "SELECT aap_assets_v1.complete_legacy_dataset_upload(NULL::uuid,NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,NULL::bigint,NULL::text,NULL::integer,NULL::integer);\n"
        "SELECT aap_assets_v1.begin_dataset_ingest(NULL::uuid,NULL::uuid,NULL::uuid,NULL::jsonb);\n"
        "SELECT aap_assets_v1.claim_dataset_source_ingest_delivery(NULL::uuid,NULL::integer);\n"
        "SELECT aap_learning_v1.lock_project_deletion(:'project_id'::uuid);\n"
        "SELECT aap_learning_v1.guard_project_deletion(:'project_id'::uuid);\n"
        "SELECT aap_assets_v1.delete_project_owned_records(:'project_id'::uuid);\n"
        "SELECT aap_inference_v1.clear_project_ml_backend_pool(:'project_id'::uuid);\n"
        "SELECT aap_inference_v1.clear_project_pipelines(:'project_id'::uuid);\n"
        "SELECT aap_insights_v1.clear_project_task_views(:'project_id'::uuid);\n"
        "SELECT aap_diagnostics_v1.detach_project_references(:'project_id'::uuid);\n"
        "SELECT aap_export_v1.clear_project_export_artifacts(:'project_id'::uuid);\n"
        "SELECT aap_jobs_v1.detach_project_references(:'project_id'::uuid);\n"
        "SELECT aap_learning_v1.delete_project_owned_records(:'project_id'::uuid);\n"
        "SELECT aap_labeling_v1.delete_project_owned_records(:'project_id'::uuid);\n"
        "SELECT aap_labeling_v1.append_dataset_items("
        ":'dataset_id'::uuid, ARRAY[]::uuid[], :'operation_id'::uuid);\n"
        "ROLLBACK;\n"
    )
    output = _psql(
        container_id,
        container_name,
        container_identity,
        username="owner",
        password=owner_password,
        database=database,
        sql=sql,
        variables={
            "expected_database": database,
            "dataset_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "project_id": str(uuid4()),
        },
    )
    responses: dict[str, list[dict]] = {}
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("schema"), str):
            responses.setdefault(value["schema"], []).append(value)
    append_receipts = responses.get("aap-task-append/v1", [])
    if len(append_receipts) != 1 or append_receipts[0].get("created") != 0:
        raise RuntimeError(
            "native frozen-SQL append probe returned an unexpected receipt"
        )

    expected_errors = {
        "aap-active-actor/v1": "account_not_active",
        "aap-audit-actor/v1": "account_not_active",
        "aap-project-manager/v1": "account_not_active",
        "aap-dataset-association/v1": "account_not_active",
        "aap-task-creation-items/v1": "invalid_request",
        "aap-task-creation-job-command/v1": "invalid_request",
        "aap-task-creation-job-discovery/v1": "invalid_request",
        "aap-task-creation-job-claim/v1": "invalid_request",
        "aap-task-creation-job/v1": "invalid_request",
        "aap-task-creation-job-finish/v1": "invalid_request",
        "aap-task-creation/v1": "invalid_request",
        "aap-project-delete-retention/v1": "invalid_request",
        "aap-project-delete/v1": "invalid_request",
        "aap-source-retention/v1": "invalid_request",
        "aap-dataset-unlink-preview/v1": "invalid_request",
        "aap-dataset-unlink/v1": "invalid_request",
        "aap-dataset-source-delete/v1": "invalid_request",
        "aap-dataset-upload/v1": "invalid_request",
        "aap-assets-catalog/v1": "invalid_request",
        "aap-assets-associations/v1": "invalid_request",
        "aap-legacy-dataset-upload/v1": "invalid_request",
        "aap-dataset-source-ingest/v1": "invalid_request",
        "aap-dataset-source-ingest-delivery/v1": "invalid_request",
    }
    if any(
        not any(
            response.get("ok") is False
            and response.get("error", {}).get("code") == code
            for response in responses.get(schema, [])
        )
        for schema, code in expected_errors.items()
    ):
        raise RuntimeError(
            "native frozen-SQL contract probes returned unexpected errors"
        )
    if not any(
        response.get("ok") is True and response.get("data") == {}
        for response in responses.get("aap-project-delete-retention/v1", [])
    ):
        raise RuntimeError("native Learning retention guard probe did not succeed")
    if not any(
        response.get("ok") is True
        and response.get("data") == {"deleted_tasks": 0, "deleted_annotations": 0}
        for response in responses.get("aap-project-labeling-delete/v1", [])
    ):
        raise RuntimeError("native Labeling project-delete probe did not succeed")

    rollback_state = _psql(
        container_id,
        container_name,
        container_identity,
        username="owner",
        password=owner_password,
        database=database,
        sql=(
            "SELECT current_database() = :'expected_database' "
            "AND to_regnamespace('aap_identity_v1') IS NULL "
            "AND to_regnamespace('aap_workspace_v1') IS NULL "
            "AND to_regnamespace('aap_assets_v1') IS NULL "
            "AND to_regnamespace('aap_labeling_v1') IS NULL "
            "AND to_regnamespace('aap_jobs_v1') IS NULL "
            "AND to_regnamespace('aap_learning_v1') IS NULL "
            "AND to_regnamespace('aap_inference_v1') IS NULL "
            "AND to_regnamespace('aap_insights_v1') IS NULL "
            "AND to_regnamespace('aap_diagnostics_v1') IS NULL "
            "AND to_regnamespace('aap_export_v1') IS NULL "
            "AND to_regnamespace('aap_audit_v1') IS NULL "
            "AND to_regnamespace('aap_settings_v1') IS NULL "
            "AND to_regnamespace('aap_notifications_v1') IS NULL "
            "AND to_regclass('public.task_append_operations') IS NULL "
            "AND to_regclass('public.task_creation_operations') IS NULL "
            "AND to_regclass('public.task_creation_chunks') IS NULL "
            "AND to_regclass('public.project_deletion_operations') IS NULL "
            "AND to_regclass('public.dataset_unlink_operations') IS NULL "
            "AND to_regclass('public.asset_storage_deletions') IS NULL "
            "AND to_regclass('public.dataset_upload_intents') IS NULL "
            "AND to_regclass('public.dataset_source_deletion_operations') IS NULL "
            "AND to_regclass('public.dataset_catalog_operations') IS NULL "
            "AND to_regclass('public.dataset_legacy_upload_operations') IS NULL "
            "AND to_regclass('public.dataset_import_operations') IS NULL "
            "AND to_regclass('public.dataset_import_entries') IS NULL "
            "AND to_regclass('public.dataset_import_deliveries') IS NULL "
            "AND to_regclass('public.dataset_source_ingest_operations') IS NULL "
            "AND to_regclass('public.dataset_source_ingest_entries') IS NULL "
            "AND to_regclass('public.dataset_source_ingest_deliveries') IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute "
            "WHERE attrelid = 'public.async_jobs'::pg_catalog.regclass "
            "AND attname = 'retained_project_id' AND NOT attisdropped) "
            "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger "
            "WHERE tgrelid = 'public.async_jobs'::pg_catalog.regclass "
            "AND tgname = 'async_jobs_retain_project_scope' AND NOT tgisinternal);\n"
        ),
        variables={"expected_database": database},
    )
    if rollback_state.strip() != "t":
        raise RuntimeError("native frozen-SQL transaction did not roll back cleanly")


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    output = args.output.resolve()
    junit = (
        args.junit_xml.resolve()
        if args.junit_xml
        else output.with_name(f"{output.stem}.junit.xml")
    )
    if output == junit:
        raise SystemExit("--output and --junit-xml must be different paths")
    output.parent.mkdir(parents=True, exist_ok=True)
    junit.parent.mkdir(parents=True, exist_ok=True)

    evidence = {
        "schema": 1,
        "acceptance": "module-data-boundary",
        "status": "failed",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "code_root": str(ROOT),
        "container_image": POSTGRES_IMAGE,
        "container_labels": {
            CONTAINER_LABEL: CONTAINER_LABEL_VALUE,
        },
        "contract_artifacts": {
            "sql": list(SQL_ARTIFACTS),
            "schemas": list(SCHEMA_ARTIFACTS),
        },
        "database": None,
        "roles": list(MODULE_ROLES),
        "test_files": list(TEST_FILES),
        "junit_xml": str(junit),
        "test_exit_code": None,
        "migrations": {
            "head_before_downgrade": None,
            "downgraded_to_0189": False,
            "frozen_sql_installed_and_rolled_back": False,
            "upgraded_to_head": False,
            "head_after_upgrade": None,
            "runtime_grants_reapplied": False,
        },
        "cleanup": {
            "storage_identity_removed": False,
            "catalog_storage_identity_removed": False,
            "source_storage_identity_removed": False,
            "import_storage_identity_removed": False,
            "import_source_storage_identity_removed": False,
            "local_resources_destroyed": False,
            "postgres_container_removed": False,
            "resource_root_retained": None,
            "errors": [],
        },
    }
    phase = "preflight"
    exit_code = 1
    container_id = None
    container_name = f"aap-module-data-{secrets.token_hex(6)}"
    container_identity = secrets.token_hex(12)
    backend = None
    cleanup_storage = None
    catalog_storage = None
    source_storage = None
    import_storage = None
    import_source_storage = None
    resource_root = Path(
        tempfile.mkdtemp(prefix="aap-module-data-acceptance-")
    ).resolve()

    try:
        grants_path = ROOT / "infra/docker/module-data-grants.sql"
        catalog_grants_path = ROOT / "infra/docker/assets-catalog-grants.sql"
        source_grants_path = ROOT / "infra/docker/assets-source-grants.sql"
        import_grants_path = ROOT / "infra/docker/assets-import-grants.sql"
        artifacts = tuple(
            ROOT / "apps/api/alembic/sql" / name for name in SQL_ARTIFACTS
        )
        schema_paths = tuple(
            ROOT / "apps/api/alembic/sql" / name for name in SCHEMA_ARTIFACTS
        )
        if (
            not grants_path.is_file()
            or not catalog_grants_path.is_file()
            or not source_grants_path.is_file()
            or not import_grants_path.is_file()
            or any(not path.is_file() for path in artifacts)
            or any(not path.is_file() for path in schema_paths)
        ):
            raise RuntimeError(
                "module-data SQL/schema artifacts are missing from this checkout"
            )
        for path in schema_paths:
            contract = json.loads(path.read_text(encoding="utf-8"))
            if (
                contract.get("$schema")
                != "https://json-schema.org/draft/2020-12/schema"
                or not isinstance(contract.get("$id"), str)
                or not isinstance(contract.get("$defs"), dict)
            ):
                raise RuntimeError(f"invalid frozen module-data schema: {path.name}")
        grant_sql = grants_path.read_text(encoding="utf-8")
        catalog_grant_sql = catalog_grants_path.read_text(encoding="utf-8")
        source_grant_sql = source_grants_path.read_text(encoding="utf-8")
        import_grant_sql = import_grants_path.read_text(encoding="utf-8")

        from sqlalchemy.engine import URL

        from app.config import settings
        from worktree_env import load_identity, topology
        from worktree_resources import LocalResources, docker, migration_graph
        from worktree_runtime import parse_arguments, run_processes

        if settings.environment != "development":
            raise RuntimeError("module-data acceptance requires development settings")
        owner_password = f"owner-{secrets.token_hex(16)}"
        runtime_password = f"runtime-{secrets.token_hex(16)}"
        append_password = f"append-{secrets.token_hex(16)}"
        cleanup_password = f"cleanup-{secrets.token_hex(16)}"
        catalog_password = f"catalog-{secrets.token_hex(16)}"
        source_password = f"source-{secrets.token_hex(16)}"
        import_password = f"import-{secrets.token_hex(16)}"
        container_id = docker(
            "run",
            "-d",
            "--name",
            container_name,
            "--label",
            f"{CONTAINER_LABEL}={CONTAINER_LABEL_VALUE}",
            "--label",
            f"{CONTAINER_ID_LABEL}={container_identity}",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_USER=owner",
            "--env",
            f"POSTGRES_PASSWORD={owner_password}",
            "--env",
            "POSTGRES_DB=postgres",
            POSTGRES_IMAGE,
        )
        evidence["container_labels"][CONTAINER_ID_LABEL] = container_identity
        port = _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        evidence["postgres_container"] = {
            "id": container_id,
            "name": container_name,
            "host": "127.0.0.1",
            "port": port,
        }
        _wait_for_postgres(container_id, container_name, container_identity)
        owner_url = URL.create(
            "postgresql+asyncpg",
            username="owner",
            password=owner_password,
            host="127.0.0.1",
            port=port,
            database="postgres",
        ).render_as_string(hide_password=False)
        legacy_url = URL.create(
            "postgresql+asyncpg",
            username="legacy_runtime",
            password=runtime_password,
            host="127.0.0.1",
            port=port,
            database="postgres",
        ).render_as_string(hide_password=False)
        phase = "legacy-runtime-role"
        _create_runtime_role(
            container_id,
            container_name,
            container_identity,
            runtime_password,
            owner_password,
        )

        resources = topology(
            resource_root, load_identity(resource_root, create=True), "test"
        )
        backend = LocalResources(
            resources,
            settings.model_copy(
                update={
                    "database_url": legacy_url,
                    "migration_database_url": owner_url,
                }
            ),
            code_root=ROOT,
        )
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        phase = "prepare-owned-test-resources"
        backend.prepare()
        database = resources["database"]
        evidence["database"] = database
        _, heads = migration_graph(ROOT)
        if heads != ["0215"]:
            raise RuntimeError("module-data acceptance requires migration head 0215")
        evidence["migrations"]["head_before_downgrade"] = heads[0]
        phase = "downgrade-to-0189"
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        _run_alembic(backend, "downgrade", "0189")
        if asyncio.run(backend.revisions()) != ["0189"]:
            raise RuntimeError("disposable database did not reach revision 0189")
        evidence["migrations"]["downgraded_to_0189"] = True
        phase = "native-frozen-sql-roundtrip"
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        _install_and_rollback_frozen_sql(
            container_id,
            container_name,
            container_identity,
            database,
            owner_password,
            artifacts,
        )
        evidence["migrations"]["frozen_sql_installed_and_rolled_back"] = True
        phase = "upgrade-to-head"
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        _run_alembic(backend, "upgrade", "head")
        if set(asyncio.run(backend.revisions())) != set(heads):
            raise RuntimeError("disposable database did not return to migration head")
        evidence["migrations"]["upgraded_to_head"] = True
        evidence["migrations"]["head_after_upgrade"] = heads[0]
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        asyncio.run(backend.grant_runtime())
        evidence["migrations"]["runtime_grants_reapplied"] = True
        append_url = URL.create(
            "postgresql+asyncpg",
            username="append_client",
            password=append_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        cleanup_url = URL.create(
            "postgresql+asyncpg",
            username="asset_cleanup_client",
            password=cleanup_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        catalog_url = URL.create(
            "postgresql+asyncpg",
            username="asset_catalog_client",
            password=catalog_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        source_url = URL.create(
            "postgresql+asyncpg",
            username="asset_source_client",
            password=source_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        import_url = URL.create(
            "postgresql+asyncpg",
            username="asset_import_client",
            password=import_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        role_create_password = append_password.replace("'", "''")
        phase = "create-scoped-roles"
        _create_module_roles(
            container_id,
            container_name,
            container_identity,
            database,
            owner_password,
            role_create_password,
            cleanup_password,
            catalog_password,
            source_password,
            import_password,
        )
        phase = "install-module-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "assets_owner": "assets_code",
                "labeling_owner": "labeling_code",
                "jobs_owner": "jobs_code",
                "learning_owner": "learning_code",
                "inference_owner": "inference_code",
                "insights_owner": "insights_code",
                "diagnostics_owner": "diagnostics_code",
                "export_owner": "export_code",
                "audit_owner": "audit_code",
                "settings_owner": "settings_code",
                "notifications_owner": "notifications_code",
                "workflow_role": "append_client",
                "asset_cleanup_role": "asset_cleanup_client",
            },
        )

        resource_environment = backend.environment()
        phase = "install-catalog-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=catalog_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "assets_owner": "assets_code",
                "asset_catalog_role": "asset_catalog_client",
            },
        )
        phase = "install-source-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=source_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "assets_owner": "assets_code",
                "asset_source_role": "asset_source_client",
            },
        )
        phase = "install-import-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=import_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "assets_owner": "assets_code",
                "asset_import_role": "asset_import_client",
            },
        )
        phase = "create-scoped-storage-identity"
        cleanup_storage = _cleanup_storage_scope(backend)
        _create_cleanup_storage_scope(cleanup_storage)
        catalog_storage = _cleanup_storage_scope(backend)
        catalog_storage["datasets_bucket"] = resources["buckets"][
            "MINIO_DATASETS_BUCKET"
        ]
        _create_catalog_storage_scope(catalog_storage)
        source_storage = _cleanup_storage_scope(backend)
        source_storage["datasets_bucket"] = resources["buckets"][
            "MINIO_DATASETS_BUCKET"
        ]
        source_storage["import_bucket"] = resources["buckets"]["MINIO_IMPORT_BUCKET"]
        _create_source_storage_scope(source_storage)
        import_storage = _cleanup_storage_scope(backend)
        import_storage["datasets_bucket"] = resources["buckets"][
            "MINIO_DATASETS_BUCKET"
        ]
        _create_import_storage_scope(import_storage, source=False)
        import_source_storage = _cleanup_storage_scope(backend)
        import_source_storage["source_bucket"] = resources["buckets"][
            "MINIO_IMPORT_BUCKET"
        ]
        _create_import_storage_scope(import_source_storage, source=True)
        from cryptography.fernet import Fernet

        resource_environment.update(
            {
                "AAP_MODULE_DATA_ROLE_ACCEPTANCE": "1",
                "RUN_NATIVE_IMPORT_DELIVERY_LEASE_TEST": "1",
                "AAP_MODULE_DATA_APPEND_URL": append_url,
                "AAP_MODULE_DATA_ASSET_CLEANUP_URL": cleanup_url,
                "AAP_MODULE_DATA_ASSET_CATALOG_URL": catalog_url,
                "AAP_MODULE_DATA_ASSET_SOURCE_URL": source_url,
                "AAP_MODULE_DATA_ASSET_IMPORT_URL": import_url,
                "AAP_MODULE_DATA_ASSET_IMPORT_S3_ACCESS_KEY": import_storage[
                    "access_key"
                ],
                "AAP_MODULE_DATA_ASSET_IMPORT_S3_SECRET_KEY": import_storage[
                    "secret_key"
                ],
                "AAP_MODULE_DATA_IMPORT_SOURCE_S3_ACCESS_KEY": import_source_storage[
                    "access_key"
                ],
                "AAP_MODULE_DATA_IMPORT_SOURCE_S3_SECRET_KEY": import_source_storage[
                    "secret_key"
                ],
                "CONNECTOR_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "AAP_MODULE_DATA_ASSET_SOURCE_S3_ACCESS_KEY": source_storage[
                    "access_key"
                ],
                "AAP_MODULE_DATA_ASSET_SOURCE_S3_SECRET_KEY": source_storage[
                    "secret_key"
                ],
                "AAP_MODULE_DATA_ASSET_CATALOG_S3_ACCESS_KEY": catalog_storage[
                    "access_key"
                ],
                "AAP_MODULE_DATA_ASSET_CATALOG_S3_SECRET_KEY": catalog_storage[
                    "secret_key"
                ],
                "AAP_MODULE_DATA_ASSET_CLEANUP_S3_ACCESS_KEY": cleanup_storage[
                    "access_key"
                ],
                "AAP_MODULE_DATA_ASSET_CLEANUP_S3_SECRET_KEY": cleanup_storage[
                    "secret_key"
                ],
                "AAP_MODULE_DATA_LEGACY_RUNTIME_URL": URL.create(
                    "postgresql+asyncpg",
                    username="legacy_runtime",
                    password=runtime_password,
                    host="127.0.0.1",
                    port=port,
                    database=database,
                ).render_as_string(hide_password=False),
                "AAP_MODULE_DATA_PG_CONTAINER_ID": container_id,
                "AAP_MODULE_DATA_PG_CONTAINER_NAME": container_name,
                "AAP_MODULE_DATA_PG_CONTAINER_IDENTITY": container_identity,
            }
        )
        command = [
            sys.executable,
            "-c",
            "import os,sys; args=sys.argv[1:]; os.chdir(args.pop(0)); "
            "sys.path.insert(0, os.getcwd()); sys.argv=['pytest', *args]; "
            "from pytest import console_main; raise SystemExit(console_main())",
            str(ROOT / "apps/api"),
            "-q",
            "--tb=short",
            "--maxfail=3",
            f"--junitxml={junit}",
            *TEST_FILES,
        ]
        options = parse_arguments(["exec", "--mode", "test", "--", *command])
        phase = "pytest"
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        with patch.dict(os.environ, resource_environment, clear=False):
            exit_code = run_processes(backend, options)
        evidence["test_exit_code"] = exit_code
        evidence["status"] = "passed" if exit_code == 0 else "failed"
    except Exception as error:
        evidence["status"] = "failed"
        evidence["failure"] = {"phase": phase, "error_type": type(error).__name__}
        exit_code = 1
    finally:
        for label, scope in (
            ("import", import_storage),
            ("import_source", import_source_storage),
        ):
            if scope is not None:
                try:
                    _destroy_cleanup_storage_scope(scope)
                    evidence["cleanup"][label + "_storage_identity_removed"] = True
                except Exception as error:
                    evidence["cleanup"]["errors"].append(
                        {label + "_storage_identity": type(error).__name__}
                    )
                    exit_code = 1
        if source_storage is not None:
            try:
                _destroy_cleanup_storage_scope(source_storage)
                evidence["cleanup"]["source_storage_identity_removed"] = True
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"source_storage_identity": type(error).__name__}
                )
                exit_code = 1
        if catalog_storage is not None:
            try:
                _destroy_cleanup_storage_scope(catalog_storage)
                evidence["cleanup"]["catalog_storage_identity_removed"] = True
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"catalog_storage_identity": type(error).__name__}
                )
                exit_code = 1
        if cleanup_storage is not None:
            try:
                _destroy_cleanup_storage_scope(cleanup_storage)
                evidence["cleanup"]["storage_identity_removed"] = True
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"storage_identity": type(error).__name__}
                )
                exit_code = 1
        local_resources_destroyed = backend is None or not backend.touched
        if backend is not None and backend.touched:
            try:
                if container_id:
                    _container_details(
                        container_id,
                        container_name,
                        container_identity,
                        require_running=True,
                    )
                backend.destroy(backend.resources["confirmation"])
                evidence["cleanup"]["local_resources_destroyed"] = True
                local_resources_destroyed = True
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"resource": "local-resources", "error_type": type(error).__name__}
                )
                exit_code = 1
        if container_id:
            try:
                _container_details(
                    container_id,
                    container_name,
                    container_identity,
                    require_running=False,
                )
                from worktree_resources import docker

                docker("rm", "-f", "-v", container_id)
                evidence["cleanup"]["postgres_container_removed"] = True
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {
                        "resource": "postgres-container",
                        "error_type": type(error).__name__,
                    }
                )
                exit_code = 1
        if local_resources_destroyed:
            try:
                shutil.rmtree(resource_root)
            except FileNotFoundError:
                pass
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"resource": "resource-root", "error_type": type(error).__name__}
                )
                evidence["cleanup"]["resource_root_retained"] = str(resource_root)
                exit_code = 1
        else:
            evidence["cleanup"]["resource_root_retained"] = str(resource_root)
        if exit_code != 0:
            evidence["status"] = "failed"
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_evidence(output, evidence)
        print(
            f"{'PASS' if exit_code == 0 else 'FAIL'}: module-data acceptance; "
            f"evidence={output} junit={junit}",
            flush=True,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
