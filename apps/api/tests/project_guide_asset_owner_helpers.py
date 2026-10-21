"""Owned resources and service bindings for the frozen Guide HTTP replay."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from sqlalchemy.engine import make_url

from app.config import settings
from tests.test_asset_storage_cleanup_runtime import (
    _local_s3_client,
    _verified_resource_manifest,
    _verify_bucket_owner,
)
from tests.test_identity_admission_live import _port, _start
from tests.test_labeling_class_usage_reads_legacy import _legacy_runtime_url
from tests.test_project_guide_assets_legacy import (
    _cleanup_objects,
    _normalise,
    _object_state,
    _object_versions,
    _put_through_presigned_url,
    _version_summary,
)
from tests.workspace_configuration_helpers import ROOT, configuration_dsn


GUIDE_FILE = b"\x89PNG\r\n\x1a\n" + b"owned-guide-fixture\n"
SAMPLES_SHA256 = "88c7a43398c5562d64ba081cf541dd36cb296135486737375c1100aa9039bb0d"
FIXTURE_PATH = (
    Path(__file__).parent / "fixtures/project_guide_assets_legacy/samples.json"
)


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise AssertionError(f"the owned Guide verifier did not provide {name}")
    return value


def fixture_data() -> dict:
    import hashlib

    raw = FIXTURE_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SAMPLES_SHA256
    value = json.loads(raw)
    assert (
        value["capture_kind"] == "actual-legacy-project-guide-assets-http-and-effects"
    )
    assert len(value["samples"]) == 11
    return value


def port_client(access_key: str, secret_key: str):
    endpoint = urlsplit(
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
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


def endpoint_url() -> str:
    return (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
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


@asynccontextmanager
async def installed_workspace_guide_owner(
    test_engine,
    tmp_path,
    *,
    token: str,
    guide_url: str,
    guide_token: str,
):
    """Run the installed Workspace owner with only Configuration and Guide ports."""
    interpreter = Path(required_env("AAP_WORKSPACE_QUERY_PYTHON"))
    assert interpreter.is_absolute() and interpreter.is_file()
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    dsn = configuration_dsn(test_engine)
    parsed = make_url(dsn)
    assert parsed.username == "workspace_configuration_client"
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )

    directory = Path(
        tempfile.mkdtemp(
            prefix=f"guide-workspace-{secrets.token_hex(5)}-", dir=tmp_path
        )
    )
    directory.chmod(0o700)
    values = (
        ("configuration", dsn),
        ("control-token", token),
        ("guide-token", guide_token),
    )
    secret_paths = []
    for name, value in values:
        path = directory / name
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)
        secret_paths.append(path)

    port = _port()
    environment = {
        "PATH": f"{interpreter.parent}:/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "WORKSPACE_HOST": "127.0.0.1",
        "WORKSPACE_PORT": str(port),
        "WORKSPACE_TIMEOUT_SECONDS": "20",
        "WORKSPACE_CONFIGURATION_SETUP_TIMEOUT_SECONDS": "10",
        "WORKSPACE_CONFIGURATION_DATABASE_URL_FILE": str(secret_paths[0]),
        "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE": str(secret_paths[1]),
        "WORKSPACE_GUIDE_OBJECTS_URL": guide_url,
        "WORKSPACE_GUIDE_OBJECTS_TOKEN_FILE": str(secret_paths[2]),
    }
    assert not any(
        key.startswith(("PYTHONPATH", "WORKSPACE_MEMBERSHIP_DATABASE_URL"))
        for key in environment
    )
    log_path = directory / "workspace.log"
    log_path.touch(mode=0o600)
    owner = None
    try:
        owner = await asyncio.to_thread(
            _start,
            [str(interpreter), "-I", "-m", "aap_workspace"],
            environment,
            directory,
            log_path,
            port,
            "/ready",
        )
        yield owner.origin
    finally:
        if owner is not None:
            owner.stop()
        log_bytes = log_path.read_bytes()
        assert all(value.encode() not in log_bytes for _, value in values), (
            "Workspace log retained a private Guide or database credential"
        )
        for path in secret_paths:
            path.unlink(missing_ok=True)
        log_path.unlink(missing_ok=True)
        directory.rmdir()


@asynccontextmanager
async def selected_guide_binding(
    monkeypatch,
    test_engine,
    tmp_path,
    *,
    mode: str,
    guide_url: str | None = None,
    guide_token: str | None = None,
):
    """Select one Configuration deployment without an implicit fallback."""
    if mode not in {"borrowed", "dedicated", "http"}:
        raise ValueError("unknown Guide owner binding")
    token = secrets.token_urlsafe(32)
    dsn = configuration_dsn(test_engine) if mode == "dedicated" else ""
    url = ""
    async with AsyncExitStack() as stack:
        if mode == "http":
            if not guide_url or not guide_token:
                raise ValueError("HTTP Workspace binding requires the Guide byte port")
            url = await stack.enter_async_context(
                installed_workspace_guide_owner(
                    test_engine,
                    tmp_path,
                    token=token,
                    guide_url=guide_url,
                    guide_token=guide_token,
                )
            )
        changes = {
            "workspace_configuration_url": url,
            "workspace_configuration_database_url": dsn,
            "workspace_configuration_database_url_file": "",
            "workspace_configuration_timeout_seconds": 20,
            "workspace_membership_control_token": token if url else "",
            "workspace_membership_control_token_file": "",
            "workspace_guide_objects_url": "",
            "workspace_guide_objects_token": "",
            "workspace_guide_objects_token_file": "",
            "workspace_project_query_url": "",
            "workspace_project_query_database_url": "",
            "workspace_project_query_database_url_file": "",
            "workspace_project_query_timeout_seconds": 20,
        }
        for name, value in changes.items():
            monkeypatch.setattr(settings, name, value)
        yield


async def request_runtime(test_engine):
    from tests.labeling_class_query_helpers import make_request_runtime

    return await make_request_runtime(
        test_engine, application_name="project-guide-owner-replay"
    )


async def asset_list(maker, project_id, aliases):
    from sqlalchemy import select

    from app.db.models.project import Project

    async with maker() as db:
        value = await db.scalar(
            select(Project.guide_assets).where(Project.id == project_id)
        )
    return _normalise(value, aliases)


async def snapshot_state(
    maker, main_id, other_id, aliases, *, owner_s3, bucket, object_key=None
):
    state = {
        "project_assets": await asset_list(maker, main_id, aliases),
        "main_project_assets": await asset_list(maker, main_id, aliases),
        "other_project_assets": await asset_list(maker, other_id, aliases),
    }
    if object_key is not None:
        state["object"] = _object_state(owner_s3, bucket, object_key)
        state["object_versions"] = _version_summary(owner_s3, bucket, object_key)
    return state


def compare_sample_state(sample: dict, before: dict, after: dict) -> None:
    fields = {
        "project_assets_before": before.get("project_assets"),
        "project_assets_after": after.get("project_assets"),
        "main_project_assets_before": before.get("main_project_assets"),
        "main_project_assets_after": after.get("main_project_assets"),
        "other_project_assets_before": before.get("other_project_assets"),
        "other_project_assets_after": after.get("other_project_assets"),
        "object_before": before.get("object"),
        "object_after": after.get("object"),
        "object_after_presign": after.get("object"),
        "object_versions_after": after.get("object_versions"),
        "object_versions_after_presign": after.get("object_versions"),
    }
    for name, actual in fields.items():
        expected = sample.get(name)
        if expected is not None:
            assert actual == expected, f"{sample['label']}: {name}"


async def assert_no_configuration_receipts_or_states(
    test_engine, actor_ids, operation_ids
) -> None:
    from sqlalchemy import text

    actor_values = [str(actor_id) for actor_id in actor_ids]
    async with test_engine.connect() as db:
        receipts = await db.scalar(
            text(
                "SELECT count(*) FROM public.workspace_configuration_operation_receipts "
                "WHERE actor_id = ANY(CAST(:actor_ids AS uuid[])) "
                "OR operation_id = ANY(CAST(:operation_ids AS uuid[]))"
            ),
            {"actor_ids": actor_ids, "operation_ids": operation_ids},
        )
        states = await db.scalar(
            text(
                "SELECT count(*) FROM public.workspace_configuration_operation_states "
                "WHERE request->>'actor_id' = ANY(CAST(:actor_values AS text[])) "
                "OR operation_id = ANY(CAST(:operation_ids AS uuid[]))"
            ),
            {"actor_values": actor_values, "operation_ids": operation_ids},
        )
        attempts = await db.scalar(
            text(
                "SELECT count(*) FROM public.workspace_configuration_operation_attempts "
                "WHERE operation_id = ANY(CAST(:operation_ids AS uuid[]))"
            ),
            {"operation_ids": operation_ids},
        )
    assert receipts == 0, "Guide commands persisted a configuration receipt"
    assert states == 0, "Guide commands persisted configuration operation state"
    assert attempts == 0, "Guide commands persisted a configuration write attempt"


async def cleanup_configuration_receipts_or_states(
    test_engine, actor_ids, operation_ids
) -> None:
    """Failure cleanup only; successful replay asserts these tables stayed empty."""
    from sqlalchemy import text

    actor_values = [str(actor_id) for actor_id in actor_ids]
    async with test_engine.begin() as db:
        await db.execute(
            text(
                "DELETE FROM public.workspace_configuration_operation_attempts AS attempt "
                "USING public.workspace_configuration_operation_states AS state "
                "WHERE attempt.transaction_id = state.transaction_id "
                "AND attempt.operation_id = state.operation_id "
                "AND (state.request->>'actor_id' = ANY(CAST(:actor_values AS text[])) "
                "OR state.operation_id = ANY(CAST(:operation_ids AS uuid[])))"
            ),
            {"actor_values": actor_values, "operation_ids": operation_ids},
        )
        await db.execute(
            text(
                "DELETE FROM public.workspace_configuration_operation_attempts "
                "WHERE operation_id = ANY(CAST(:operation_ids AS uuid[]))"
            ),
            {"operation_ids": operation_ids},
        )
        await db.execute(
            text(
                "DELETE FROM public.workspace_configuration_operation_states "
                "WHERE request->>'actor_id' = ANY(CAST(:actor_values AS text[])) "
                "OR operation_id = ANY(CAST(:operation_ids AS uuid[]))"
            ),
            {"actor_values": actor_values, "operation_ids": operation_ids},
        )
        await db.execute(
            text(
                "DELETE FROM public.workspace_configuration_operation_receipts "
                "WHERE actor_id = ANY(CAST(:actor_ids AS uuid[])) "
                "OR operation_id = ANY(CAST(:operation_ids AS uuid[]))"
            ),
            {"actor_ids": actor_ids, "operation_ids": operation_ids},
        )


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
    "FIXTURE_PATH",
    "SAMPLES_SHA256",
    "_cleanup_objects",
    "_normalise",
    "_object_state",
    "_object_versions",
    "_put_through_presigned_url",
    "_version_summary",
    "asset_list",
    "assert_workspace_identity",
    "assert_no_configuration_receipts_or_states",
    "compare_sample_state",
    "cleanup_configuration_receipts_or_states",
    "create_guide_storage_scope",
    "endpoint_url",
    "fixture_data",
    "installed_workspace_guide_owner",
    "port_client",
    "request_runtime",
    "selected_guide_binding",
    "snapshot_state",
]
