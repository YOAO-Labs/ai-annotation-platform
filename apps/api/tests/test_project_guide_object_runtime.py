"""Exercise a Guide-only installed process against owned, restricted S3 data."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import httpx
import pytest
from sqlalchemy.engine import make_url

from aap_assets_source.guide_object_client import GuideObjectHttpClient
from aap_assets_source.guide_object_contract import (
    GUIDE_OBJECT_SCHEMA,
    GuideObjectFailure,
    GuideObjectRequest,
)
from app.config import settings
from tests.test_asset_storage_cleanup_runtime import (
    _local_s3_client,
    _required_env,
    _verified_resource_manifest,
    _verify_bucket_owner,
)
from tests.test_project_class_rename_legacy import _legacy_runtime_url
from tests.test_project_guide_assets_legacy import _cleanup_objects, _object_versions

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires explicitly owned test storage and installed Source artifact",
    ),
]


def _command(project_id, key, operation, content_type=None):
    return GuideObjectRequest(
        schema=GUIDE_OBJECT_SCHEMA,
        request_id=uuid4(),
        project_id=project_id,
        key=key,
        operation=operation,
        content_type=content_type,
    )


def _assert_denied(call, *, code=None):
    with pytest.raises(ClientError) as error:
        call()
    assert error.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403
    if code is not None:
        assert error.value.response["Error"]["Code"] == code


def _require_private_value(condition: bool, message: str) -> None:
    """Avoid assertion introspection exposing an actual signed URL on failure."""
    if not condition:
        raise AssertionError(message)


@asynccontextmanager
async def _installed_guide_process(
    tmp_path, *, endpoint, bucket, access_key, secret_key
):
    python = Path(_required_env("AAP_ASSETS_GUIDE_PYTHON"))
    assert python.is_absolute() and python.is_file()
    identity = secrets.token_hex(12)
    token = secrets.token_urlsafe(32)
    directory = tmp_path / identity
    directory.mkdir(mode=0o700)
    for name, value in (
        ("token", token),
        ("access", access_key),
        ("secret", secret_key),
    ):
        target = directory / name
        target.write_text(value)
        target.chmod(0o600)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {
        "PATH": str(python.parent) + ":/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "ASSETS_SOURCE_MODE": "guide-only",
        "ASSETS_SOURCE_TOKEN_FILE": str(directory / "token"),
        "ASSETS_SOURCE_HOST": "127.0.0.1",
        "ASSETS_SOURCE_PORT": str(port),
        "ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT": "4",
        "ASSETS_SOURCE_GUIDE_S3_ENDPOINT": endpoint,
        "ASSETS_SOURCE_GUIDE_S3_PUBLIC_URL": "/minio",
        "ASSETS_SOURCE_GUIDE_S3_BUCKET": bucket,
        "ASSETS_SOURCE_GUIDE_S3_ACCESS_KEY_FILE": str(directory / "access"),
        "ASSETS_SOURCE_GUIDE_S3_SECRET_KEY_FILE": str(directory / "secret"),
    }
    assert not any(
        "DATABASE" in key or key.startswith(("MINIO_", "AWS_", "PYTHONPATH"))
        for key in environment
    )
    log_path = directory / "service.log"
    with log_path.open("xb") as log:
        log_path.chmod(0o600)
        process = subprocess.Popen(
            [str(python), "-I", "-m", "aap_assets_source_runtime"],
            cwd=directory,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        origin = f"http://127.0.0.1:{port}"
        try:
            deadline = asyncio.get_running_loop().time() + 20
            async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                while asyncio.get_running_loop().time() < deadline:
                    assert process.poll() is None, "Guide-only process failed to start"
                    try:
                        if (await client.get(origin + "/ready")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("Guide-only process did not become ready")
            yield (
                origin,
                token,
                {"environment_keys": sorted(environment), "pid": process.pid},
            )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, 10)
            assert process.poll() is not None
            for name in ("token", "access", "secret"):
                (directory / name).unlink()
    # Logs must not retain the values which were passed only through secret files.
    log_bytes = log_path.read_bytes()
    assert all(
        value.encode() not in log_bytes for value in (token, access_key, secret_key)
    )


async def _exercise_guide_runtime(
    test_engine,
    tmp_path,
    *,
    launch=_installed_guide_process,
    observation_name="project-guide-object-runtime-observation.json",
    after_upload=None,
):
    resources = _verified_resource_manifest(
        test_engine, make_url(_legacy_runtime_url(test_engine))
    )
    bucket = resources["buckets"]["MINIO_BUCKET"]
    owner = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    _verify_bucket_owner(owner, bucket, resources["owner"])
    owner.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_module_data_boundary import (
        _cleanup_mc,
        _cleanup_storage_scope,
        _destroy_cleanup_storage_scope,
    )

    scope = _cleanup_storage_scope(
        SimpleNamespace(settings=settings, resources=resources, s3=owner)
    )
    project_id = uuid4()
    key = f"projects/{project_id}/guide/{uuid4()}-图像.png"
    foreign = f"outside-guide/{uuid4()}.png"
    keys = [key, foreign]
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    arn = f"arn:aws:s3:::{bucket}/projects/*/guide/*"
    observations = {}
    client = None
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
                    "Resource": [arn],
                },
                {
                    "Effect": "Deny",
                    "Action": ["s3:DeleteObjectVersion"],
                    "Resource": [arn],
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
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=scope["access_key"],
            aws_secret_access_key=scope["secret_key"],
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"total_max_attempts": 1},
            ),
        )
        async with launch(
            tmp_path,
            endpoint=endpoint,
            bucket=bucket,
            access_key=scope["access_key"],
            secret_key=scope["secret_key"],
        ) as (origin, token, process):
            port = GuideObjectHttpClient(origin, token)
            request = _command(project_id, key, "head")
            assert (await port.execute(request)).result.content_length is None
            async with httpx.AsyncClient(trust_env=False) as http:
                assert (
                    await http.post(
                        origin + "/v1/guide-objects/execute",
                        json=request.model_dump(mode="json", by_alias=True),
                    )
                ).status_code == 401
                malformed = request.model_dump(mode="json", by_alias=True)
                malformed["VersionId"] = "forbidden"
                denied = await http.post(
                    origin + "/v1/guide-objects/execute",
                    json=malformed,
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert denied.status_code == 400
                assert (
                    await http.post(origin + "/v1/source/upload-init", json={})
                ).status_code == 404
                signed = await port.execute(
                    _command(project_id, key, "presign_put", "image/png")
                )
                assert signed.result.expires_in == 900
                url = signed.result.url
                _require_private_value(
                    url.startswith("/minio/" + bucket + "/projects/"),
                    "Guide presign public prefix differs",
                )
                _require_private_value(
                    sorted(parse_qs(urlsplit(url).query))
                    == ["AWSAccessKeyId", "Expires", "Signature", "content-type"],
                    "Guide presign query field names differ",
                )
                response = await http.put(
                    endpoint + url[len("/minio") :],
                    content=b"guide-live-object",
                    headers={"Content-Type": "image/png"},
                )
                assert response.status_code == 200
                assert (
                    await port.execute(_command(project_id, key, "head"))
                ).result.content_length == len(b"guide-live-object")
                signed = await port.execute(_command(project_id, key, "presign_get"))
                assert signed.result.expires_in == 3600
                response = await http.get(endpoint + signed.result.url[len("/minio") :])
                assert (
                    response.status_code == 200
                    and response.content == b"guide-live-object"
                )
            if after_upload is not None:
                origin = await after_upload(origin, token, process)
                port = GuideObjectHttpClient(origin, token)
                assert (
                    await port.execute(_command(project_id, key, "head"))
                ).result.content_length == len(b"guide-live-object")
                started = asyncio.get_running_loop().time()

                async def profile_head():
                    try:
                        result = await port.execute(_command(project_id, key, "head"))
                        assert result.result.content_length == len(b"guide-live-object")
                        return "success"
                    except GuideObjectFailure as failure:
                        if failure.code != "busy":
                            raise
                        return "busy"

                results = await asyncio.gather(
                    *(profile_head() for _ in range(16)), return_exceptions=True
                )
                assert all(type(value) is str for value in results), (
                    "bounded guide HEAD profile failed"
                )
                assert results.count("success") >= 1
                process["head_profile"] = {
                    "requests": 16,
                    "success": results.count("success"),
                    "busy": results.count("busy"),
                    "seconds": round(asyncio.get_running_loop().time() - started, 3),
                    "production_capacity_or_slo": False,
                }
            version = owner.head_object(Bucket=bucket, Key=key)["VersionId"]
            owner.put_object(Bucket=bucket, Key=foreign, Body=b"owned-foreign")
            _assert_denied(lambda: client.head_object(Bucket=bucket, Key=foreign))
            _assert_denied(
                lambda: client.put_object(Bucket=bucket, Key=foreign, Body=b"forbidden")
            )
            _assert_denied(lambda: client.delete_object(Bucket=bucket, Key=foreign))
            _assert_denied(
                lambda: client.list_objects_v2(Bucket=bucket, MaxKeys=1),
                code="AccessDenied",
            )
            _assert_denied(
                lambda: client.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1),
                code="AccessDenied",
            )
            _assert_denied(
                lambda: client.delete_object(Bucket=bucket, Key=key, VersionId=version),
                code="AccessDenied",
            )
            assert (
                await port.execute(_command(project_id, key, "delete"))
            ).result.deleted is True
            assert (
                await port.execute(_command(project_id, key, "head"))
            ).result.content_length is None
            assert owner.head_object(Bucket=bucket, Key=key, VersionId=version)[
                "ContentLength"
            ] == len(b"guide-live-object")
            assert (
                len(
                    owner.list_object_versions(Bucket=bucket, Prefix=key).get(
                        "DeleteMarkers", []
                    )
                )
                == 1
            )
            observations["guide_process"] = process
            observations["ordinary_delete_preserves_history"] = True

        # Catalog's GET-only grants do not cover this Guide bucket. Reuse its
        # actual denial for both operations without broadening its authority.
        owner.put_object(Bucket=bucket, Key=key, Body=b"delete-denied")
        prior = _object_versions(owner, bucket, key)
        async with launch(
            tmp_path,
            endpoint=endpoint,
            bucket=bucket,
            access_key=_required_env("AAP_MODULE_DATA_ASSET_CATALOG_S3_ACCESS_KEY"),
            secret_key=_required_env("AAP_MODULE_DATA_ASSET_CATALOG_S3_SECRET_KEY"),
        ) as (origin, token, process):
            port = GuideObjectHttpClient(origin, token)
            with pytest.raises(GuideObjectFailure) as denied_head:
                await port.execute(_command(project_id, key, "head"))
            assert denied_head.value.code == "storage_error"
            with pytest.raises(GuideObjectFailure) as denied:
                await port.execute(_command(project_id, key, "delete"))
            assert denied.value.code == "storage_error"
            assert _object_versions(owner, bucket, key) == prior
            observations["existing_catalog_process"] = process
            observations["actual_guide_head_access_denied"] = True
            observations["actual_delete_access_denied_preserves_object"] = True
    finally:
        if client is not None:
            client.close()
        try:
            _destroy_cleanup_storage_scope(scope)
        finally:
            _cleanup_objects(owner, bucket, keys)
            owner.close()
    assert not scope["user_created"] and not scope["policy_created"]
    target = ROOT / ".worktree" / observation_name
    assert not target.exists()
    target.write_text(
        json.dumps(
            {
                "status": "passed",
                "observations": observations,
                "owned_processes_stopped": True,
                "probe_identity_removed": True,
                "exact_keys_and_versions_removed": True,
                "limitations": [
                    "Local Source process and MinIO",
                    "Prefix rewrite simulation; no browser/CORS",
                    "No public Guide workflow cutover or Native command qualification",
                ],
            },
            indent=2,
        )
        + "\n"
    )


async def test_guide_only_installed_process_scoped_s3_and_storage_failure(
    test_engine, tmp_path
):
    await _exercise_guide_runtime(test_engine, tmp_path)
