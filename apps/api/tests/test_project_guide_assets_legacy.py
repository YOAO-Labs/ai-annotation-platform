"""Capture real pre-extraction Project guide-asset HTTP, SQL and S3 phases."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import warnings
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import delete, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from app.config import settings
from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services.storage import storage_service
from tests.factory import create_membership, create_project, create_user
from tests.labeling_class_query_helpers import (
    api_db_binding,
    clear_local_identity,
    make_request_runtime,
)
from tests.test_labeling_class_usage_reads_legacy import _legacy_runtime_url
from tests.test_asset_storage_cleanup_runtime import (
    _local_s3_client,
    _verified_resource_manifest,
    _verify_bucket_owner,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_PROJECT_GUIDE_ASSETS_LEGACY_CAPTURE") != "1",
        reason="requires an explicit owned legacy Project guide-assets capture",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]
_BASE_COMMIT = "9c2c4c7c05ec8e4bf5e79ddcb2fa4c9f70e0118b"
_SOURCE_FILES = """
apps/api/app/api/v1/guide_assets.py
apps/api/app/application/project_guide_assets.py
apps/api/app/application/project_workspace.py
apps/api/app/core/security.py
apps/api/app/db/models/project.py
apps/api/app/db/models/project_member.py
apps/api/app/db/models/user.py
apps/api/app/deps.py
apps/api/app/schemas/guide_asset.py
apps/api/app/services/guide_asset_contracts.py
apps/api/app/services/identity_account_lock_data.py
apps/api/app/services/identity_contracts.py
apps/api/app/services/identity_gateway.py
apps/api/app/services/project_access.py
apps/api/app/services/project_workspace.py
apps/api/app/services/storage.py
apps/api/app/config.py
apps/api/tests/factory.py
apps/workspace/src/aap_workspace/access_contract.py
""".split()
_HEADERS = ("content-type",)
_ACTIVE_CAPTURE: ContextVar[dict | None] = ContextVar(
    "project_guide_assets_legacy_capture", default=None
)
_GUIDE_FILE = b"\x89PNG\r\n\x1a\n" + b"owned-guide-fixture\n"


def _capture_path() -> Path:
    raw = _required_env("AAP_PROJECT_GUIDE_ASSETS_LEGACY_OUTPUT")
    requested = Path(raw)
    assert requested.is_absolute(), "capture output must be an absolute path"
    worktree = (_ROOT / ".worktree").resolve()
    output = requested.resolve()
    assert worktree in output.parents, "capture output must be inside Root .worktree"
    assert output.parent.is_dir(), "capture output parent must already exist"
    assert not output.exists() and not output.is_symlink(), (
        f"refusing to overwrite {output}"
    )
    return output


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"the owned guide-assets verifier did not provide {name}"
    return value


def _source_metadata() -> dict:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    hashes = {}
    for relative in _SOURCE_FILES:
        baseline = subprocess.run(
            ["git", "show", f"{_BASE_COMMIT}:{relative}"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
        ).stdout
        current = (_ROOT / relative).read_bytes()
        assert current == baseline, relative
        hashes[relative] = hashlib.sha256(current).hexdigest()
    return {
        "captured_head": head,
        "pre_extraction_base": _BASE_COMMIT,
        "capture_driver_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "runtime_source_sha256": hashes,
    }


def _normalise_text(value: str, aliases: dict[str, str]) -> str:
    for actual, alias in sorted(
        aliases.items(), key=lambda item: len(item[0]), reverse=True
    ):
        value = value.replace(actual, alias)
    return value


def _key_alias(key: str, aliases: dict[str, str]) -> str:
    if key in aliases:
        return aliases[key]
    for project_id, alias in aliases.items():
        if alias.startswith("$project_") and key.startswith(
            f"projects/{project_id}/guide/"
        ):
            return alias + "_guide_key"
    return "$owned_guide_key"


def _safe_url(value: str, aliases: dict[str, str]) -> dict:
    parsed = urlsplit(value)
    return {
        "scheme": parsed.scheme or None,
        "authority": _normalise_text(parsed.netloc, aliases) or None,
        "path": _normalise_text(parsed.path, aliases),
        "query_redacted": bool(parsed.query),
    }


def _normalise(value, aliases: dict[str, str], *, field: str | None = None):
    if field in {"upload_url", "url"} and isinstance(value, str):
        return _safe_url(value, aliases)
    if field in {"uploaded_at", "created_at", "updated_at"} and isinstance(value, str):
        return "<dynamic-timestamp>"
    if isinstance(value, uuid.UUID):
        value = str(value)
    if isinstance(value, str):
        return _normalise_text(value, aliases)
    if isinstance(value, (list, tuple)):
        return [_normalise(item, aliases) for item in value]
    if isinstance(value, dict):
        return {
            name: _normalise(item, aliases, field=name) for name, item in value.items()
        }
    return value


def _record(event_value: dict) -> None:
    capture = _ACTIVE_CAPTURE.get()
    if capture is None:
        return
    event_value["sequence"] = len(capture["events"]) + 1
    capture["events"].append(event_value)


def _sql_phase(statement: str) -> str:
    sql = statement.casefold()
    if "aap_identity_v1.read_actor_facts" in sql:
        return "authorization.actor_status"
    if "aap_identity_v1.lock_accounts_" in sql:
        return "authorization.account_lock"
    if "for key share" in sql or "for share" in sql:
        return "authorization.project_key_share"
    if "project_members" in sql:
        return "authorization.project_membership"
    if "update projects" in sql:
        return "project.write"
    if "projects" in sql and "for update" in sql:
        return "project.lock"
    if "from projects" in sql or "join projects" in sql:
        return "project.read"
    if "users" in sql:
        return "authorization.account_read"
    return "database.query"


def _sql_before_execute(
    _connection, _cursor, statement, _parameters, _context, _executemany
) -> None:
    capture = _ACTIVE_CAPTURE.get()
    if capture is not None:
        aliases = capture["aliases"]
        sql = _normalise_text(" ".join(statement.split()), aliases)
        _record(
            {
                "kind": "database",
                "phase": _sql_phase(sql),
                "operation": "statement",
                "sql": sql,
            }
        )


def _after_commit(_session) -> None:
    _record({"kind": "database", "operation": "commit"})


def _after_rollback(_session) -> None:
    _record({"kind": "database", "operation": "rollback"})


@contextmanager
def _observe_database(engine):
    event.listen(engine.sync_engine, "before_cursor_execute", _sql_before_execute)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_rollback", _after_rollback)
    try:
        yield
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _sql_before_execute)
        event.remove(Session, "after_commit", _after_commit)
        event.remove(Session, "after_rollback", _after_rollback)


def _observe_storage(monkeypatch, aliases: dict[str, str]) -> None:
    """Log storage calls while delegating every operation to the real client."""
    methods = (
        "generate_upload_url",
        "verify_upload",
        "delete_object",
        "generate_download_url",
    )
    for name in methods:
        original = getattr(storage_service, name)

        def observed(*args, _name=name, _original=original, **kwargs):
            key = kwargs.get("key") or (args[0] if args else "")
            try:
                result = _original(*args, **kwargs)
            except Exception as error:
                detail = {
                    "kind": "storage",
                    "operation": _name,
                    "key": _key_alias(str(key), aliases),
                    "bucket": "$owned-guide-assets-bucket",
                    "outcome": "error",
                    "error_type": type(error).__name__,
                }
                if isinstance(error, ClientError):
                    detail["error_code"] = error.response.get("Error", {}).get(
                        "Code", "unknown"
                    )
                _record(detail)
                raise
            detail = {
                "kind": "storage",
                "operation": _name,
                "key": _key_alias(str(key), aliases),
                "bucket": "$owned-guide-assets-bucket",
                "outcome": "returned",
            }
            if _name == "verify_upload":
                detail["outcome"] = "missing" if result is None else "found"
                if result is not None:
                    detail["content_length"] = int(result.get("ContentLength") or 0)
                    detail["content_type"] = result.get("ContentType")
            elif _name in {"generate_upload_url", "generate_download_url"}:
                detail["expires_in"] = kwargs.get("expires_in") or (
                    args[2]
                    if _name == "generate_upload_url" and len(args) > 2
                    else args[1]
                    if len(args) > 1
                    else None
                )
            _record(detail)
            return result

        monkeypatch.setattr(storage_service, name, observed)


async def _capture_http(
    client: httpx.AsyncClient,
    *,
    label: str,
    method: str,
    path: str,
    actor_alias: str,
    token: str,
    aliases: dict[str, str],
    body: dict | None = None,
    params: dict | None = None,
    response_key_alias: str | None = None,
) -> tuple[dict, object]:
    state = {"events": [], "aliases": aliases}
    active = _ACTIVE_CAPTURE.set(state)
    try:
        response = await client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            params=params,
        )
    finally:
        _ACTIVE_CAPTURE.reset(active)
    try:
        payload = response.json() if response.content else None
    except ValueError:
        payload = response.text
    if (
        response_key_alias
        and isinstance(payload, dict)
        and isinstance(payload.get("key"), str)
    ):
        aliases[payload["key"]] = response_key_alias
    request = {
        "method": method,
        "path": _normalise_text(path, aliases),
        "actor_alias": actor_alias,
    }
    if body is not None:
        request["body"] = _normalise(body, aliases)
    if params is not None:
        request["query"] = _normalise(params, aliases)
    return (
        {
            "label": label,
            "request": request,
            "response": {
                "status": response.status_code,
                "headers": {
                    name: response.headers[name]
                    for name in _HEADERS
                    if name in response.headers
                },
                "body": _normalise(payload, aliases),
            },
            "ordered_effects": state["events"],
        },
        payload,
    )


def _storage_operations(operation: dict) -> list[str]:
    return [
        item["operation"]
        for item in operation["ordered_effects"]
        if item["kind"] == "storage"
    ]


def _phase_sequences(operation: dict, phase: str) -> list[int]:
    if phase == "database.commit":
        return [
            item["sequence"]
            for item in operation["ordered_effects"]
            if item.get("kind") == "database" and item.get("operation") == "commit"
        ]
    return [
        item["sequence"]
        for item in operation["ordered_effects"]
        if item.get("phase") == phase
    ]


def _storage_sequence(operation: dict, method: str) -> list[int]:
    return [
        item["sequence"]
        for item in operation["ordered_effects"]
        if item.get("kind") == "storage" and item.get("operation") == method
    ]


async def _project_assets(maker, project_id: uuid.UUID, aliases: dict[str, str]):
    async with maker() as db:
        value = await db.scalar(
            select(Project.guide_assets).where(Project.id == project_id)
        )
    return _normalise(value, aliases)


def _object_versions(client, bucket: str, key: str) -> list[dict]:
    result = client.list_object_versions(Bucket=bucket, Prefix=key, MaxKeys=100)
    assert not result.get("IsTruncated"), (
        "random guide-object key has too many versions"
    )
    entries = []
    for group, marker in (("Versions", False), ("DeleteMarkers", True)):
        entries.extend(
            {"version_id": item["VersionId"], "delete_marker": marker}
            for item in result.get(group, [])
            if item.get("Key") == key
        )
    return entries


def _object_state(client, bucket: str, key: str) -> dict:
    try:
        metadata = client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return {"exists": False}
        raise
    return {
        "exists": True,
        "content_length": int(metadata.get("ContentLength") or 0),
        "content_type": metadata.get("ContentType"),
    }


def _version_summary(client, bucket: str, key: str) -> dict:
    versions = _object_versions(client, bucket, key)
    return {
        "count": len(versions),
        "delete_markers": sum(item["delete_marker"] for item in versions),
    }


def _assert_key_unused(client, bucket: str, key: str) -> None:
    assert not _object_state(client, bucket, key)["exists"]
    assert not _object_versions(client, bucket, key)


def _public_origin_for_upload(upload_url: str) -> str:
    parsed = urlsplit(upload_url)
    endpoint = urlsplit(storage_service.client.meta.endpoint_url)
    assert endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme and parsed.netloc:
        hostname = parsed.hostname or ""
        assert hostname in {"localhost", "127.0.0.1", "::1"} or hostname.endswith(
            ".localhost"
        )
        return upload_url
    rewrite = settings.minio_public_url.rstrip("/")
    assert rewrite.startswith("/"), (
        "the verifier must use the owned relative MinIO proxy path"
    )
    assert parsed.path.startswith(rewrite + "/")
    internal_path = parsed.path[len(rewrite) :]
    return urlunsplit(
        (endpoint.scheme, endpoint.netloc, internal_path, parsed.query, "")
    )


async def _put_through_presigned_url(upload_url: str, body: bytes) -> int:
    # The returned query is used only in memory; capture output always removes it.
    target = _public_origin_for_upload(upload_url)
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            response = await client.put(
                target,
                content=body,
                headers={"Content-Type": "image/png"},
            )
    except httpx.HTTPError as error:
        # Keep the signed query out of pytest's failure text.
        raise AssertionError(
            f"presigned MinIO PUT failed: {type(error).__name__}"
        ) from None
    assert response.status_code in {200, 204}
    return response.status_code


async def _cleanup_rows(test_engine, *, actor_ids, project_ids) -> None:
    if not actor_ids and not project_ids:
        return
    async with test_engine.begin() as db:
        if project_ids:
            await db.execute(
                delete(ProjectMember).where(ProjectMember.project_id.in_(project_ids))
            )
            await db.execute(delete(Project).where(Project.id.in_(project_ids)))
        if actor_ids:
            await db.execute(delete(User).where(User.id.in_(actor_ids)))
    async with test_engine.connect() as db:
        if project_ids:
            assert (
                await db.scalar(
                    select(Project.id).where(Project.id.in_(project_ids)).limit(1)
                )
                is None
            )
        if actor_ids:
            assert (
                await db.scalar(select(User.id).where(User.id.in_(actor_ids)).limit(1))
                is None
            )


def _cleanup_objects(client, bucket: str, keys: list[str]) -> None:
    failures = []
    for key in dict.fromkeys(keys):
        try:
            versions = _object_versions(client, bucket, key)
            if versions:
                for version in versions:
                    client.delete_object(
                        Bucket=bucket, Key=key, VersionId=version["version_id"]
                    )
            else:
                client.delete_object(Bucket=bucket, Key=key)
            assert not _object_versions(client, bucket, key)
            assert not _object_state(client, bucket, key)["exists"]
        except Exception as error:  # cleanup continues for the remaining exact keys
            failures.append((key, error))
    if failures:
        raise RuntimeError(
            f"could not clean {len(failures)} exact guide-object keys"
        ) from failures[0][1]


async def test_capture_legacy_project_guide_asset_http_and_io(
    test_engine, app_module, monkeypatch
):
    capture_path = _capture_path()
    source = _source_metadata()
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    runtime_url = _legacy_runtime_url(test_engine)
    resources = _verified_resource_manifest(test_engine, make_url(runtime_url))
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    bucket = resources["buckets"]["MINIO_BUCKET"]
    assert (
        settings.minio_bucket == bucket == f"aap-wt-{resources['id']}-test-annotations"
    )
    assert storage_service.bucket == bucket
    owner_s3 = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    _verify_bucket_owner(owner_s3, bucket, resources["owner"])
    versioning = owner_s3.get_bucket_versioning(Bucket=bucket).get("Status")

    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    owned_keys: list[str] = []
    aliases: dict[str, str] = {bucket: "$owned-guide-assets-bucket"}
    operations: list[dict] = []
    capture = None
    runtime_identity = {}
    active_error = None
    runtime_engine = None
    clear_local_identity(monkeypatch)

    try:
        runtime_engine, runtime_maker = await make_request_runtime(
            test_engine, application_name="project-guide-assets-legacy-capture"
        )
        async with runtime_engine.connect() as connection:
            role, database = (
                await connection.execute(
                    text("SELECT current_user, current_database()")
                )
            ).one()
        assert role == "legacy_runtime" and database == test_engine.url.database

        async with owner_maker.begin() as db:
            owner = await create_user(
                db,
                "project_admin",
                f"guide-assets-owner-{uuid.uuid4().hex}@example.test",
                "Guide asset owner",
            )
            reader = await create_user(
                db,
                "employee",
                f"guide-assets-reader-{uuid.uuid4().hex}@example.test",
                "Guide asset member",
            )
            writer = await create_user(
                db,
                "project_admin",
                f"guide-assets-writer-{uuid.uuid4().hex}@example.test",
                "Guide asset non-owner manager",
            )
            actor_ids.extend([owner.id, reader.id, writer.id])
            project = await create_project(
                db, owner_id=owner.id, name="Guide asset owned project"
            )
            other_project = await create_project(
                db, owner_id=owner.id, name="Guide asset second project"
            )
            project.guide_assets = []
            other_project.guide_assets = []
            project_ids.extend([project.id, other_project.id])
            await create_membership(
                db,
                project_id=project.id,
                user_id=reader.id,
                role="annotator",
                assigned_by=owner.id,
            )
            await create_membership(
                db,
                project_id=project.id,
                user_id=writer.id,
                role="annotator",
                assigned_by=owner.id,
            )
            await db.flush()

        aliases.update(
            {
                str(owner.id): "$actor_owner",
                str(reader.id): "$actor_reader",
                str(writer.id): "$actor_nonowner_manager",
                str(project.id): "$project_main",
                str(other_project.id): "$project_other",
            }
        )
        tokens = {
            "owner": create_access_token(str(owner.id), owner.role),
            "reader": create_access_token(str(reader.id), reader.role),
            "writer": create_access_token(str(writer.id), writer.role),
        }
        runtime_identity = {
            "database_role": "legacy_runtime",
            "database": "$owned_test_database",
            "postgres_container_id": _required_env("AAP_MODULE_DATA_PG_CONTAINER_ID"),
            "postgres_container_identity": _required_env(
                "AAP_MODULE_DATA_PG_CONTAINER_IDENTITY"
            ),
        }
        _observe_storage(monkeypatch, aliases)

        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
        with _observe_database(runtime_engine):
            async with api_db_binding(app_module, runtime_maker):
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test", timeout=30
                ) as client:
                    base_path = f"/api/v1/projects/{project.id}/guide-assets"
                    other_path = f"/api/v1/projects/{other_project.id}/guide-assets"

                    before = await _project_assets(owner_maker, project.id, aliases)
                    init, init_raw = await _capture_http(
                        client,
                        label="upload_init_presign",
                        method="POST",
                        path=f"{base_path}/upload-init",
                        actor_alias="owner",
                        token=tokens["owner"],
                        aliases=aliases,
                        body={
                            "filename": "guide.png",
                            "content_type": "image/png",
                            "size": len(_GUIDE_FILE),
                        },
                        response_key_alias="$project_main_guide_key",
                    )
                    assert init["response"]["status"] == 200
                    assert init["response"]["body"]["upload_url"]["query_redacted"]
                    assert _storage_operations(init) == ["generate_upload_url"]
                    key = init_raw["key"]
                    assert key.startswith(f"projects/{project.id}/guide/")
                    _assert_key_unused(owner_s3, bucket, key)
                    aliases[key] = "$project_main_guide_key"
                    owned_keys.append(key)
                    operations.append(
                        {
                            **init,
                            "project_assets_before": before,
                            "project_assets_after": await _project_assets(
                                owner_maker, project.id, aliases
                            ),
                            "object_after_presign": _object_state(
                                owner_s3, bucket, key
                            ),
                            "object_versions_after_presign": _version_summary(
                                owner_s3, bucket, key
                            ),
                        }
                    )

                    presigned_put_status = await _put_through_presigned_url(
                        init_raw["upload_url"], _GUIDE_FILE
                    )
                    uploaded_object = _object_state(owner_s3, bucket, key)
                    assert uploaded_object["exists"]
                    assert uploaded_object["content_length"] == len(_GUIDE_FILE)
                    assert uploaded_object["content_type"] == "image/png"
                    before = await _project_assets(owner_maker, project.id, aliases)
                    completed, complete_raw = await _capture_http(
                        client,
                        label="upload_complete_head_then_manager_lock_write",
                        method="POST",
                        path=f"{base_path}/upload-complete",
                        actor_alias="owner",
                        token=tokens["owner"],
                        aliases=aliases,
                        body={
                            "key": key,
                            "original_name": "guide.png",
                            "content_type": "image/png",
                        },
                    )
                    assert completed["response"]["status"] == 200
                    assert complete_raw["key"] == key
                    assert complete_raw["size"] == len(_GUIDE_FILE)
                    assert complete_raw["content_type"] == "image/png"
                    assert _storage_operations(completed) == ["verify_upload"]
                    head_sequence = _storage_sequence(completed, "verify_upload")[0]
                    lock_sequence = _phase_sequences(completed, "project.lock")[0]
                    write_sequence = _phase_sequences(completed, "project.write")[0]
                    commit_sequence = _phase_sequences(completed, "database.commit")[0]
                    assert (
                        head_sequence < lock_sequence < write_sequence < commit_sequence
                    )
                    operations.append(
                        {
                            **completed,
                            "presigned_put_status": presigned_put_status,
                            "object_before": uploaded_object,
                            "object_after": _object_state(owner_s3, bucket, key),
                            "object_versions_after": _version_summary(
                                owner_s3, bucket, key
                            ),
                            "project_assets_before": before,
                            "project_assets_after": await _project_assets(
                                owner_maker, project.id, aliases
                            ),
                        }
                    )

                    before = await _project_assets(owner_maker, project.id, aliases)
                    signed, signed_raw = await _capture_http(
                        client,
                        label="visible_member_signs_registered_asset",
                        method="GET",
                        path=f"{base_path}/sign-url",
                        actor_alias="reader",
                        token=tokens["reader"],
                        aliases=aliases,
                        params={"key": key},
                    )
                    assert signed["response"]["status"] == 200
                    assert signed_raw["expires_in"] == 3600
                    assert signed["response"]["body"]["url"]["query_redacted"]
                    assert _storage_operations(signed) == ["generate_download_url"]
                    operations.append(
                        {
                            **signed,
                            "project_assets_before": before,
                            "project_assets_after": await _project_assets(
                                owner_maker, project.id, aliases
                            ),
                            "object_after": _object_state(owner_s3, bucket, key),
                        }
                    )

                    before = await _project_assets(owner_maker, project.id, aliases)
                    denied, _ = await _capture_http(
                        client,
                        label="member_delete_denied_before_storage",
                        method="DELETE",
                        path=base_path,
                        actor_alias="nonowner_manager",
                        token=tokens["writer"],
                        aliases=aliases,
                        params={"key": key},
                    )
                    assert denied["response"]["status"] == 403
                    assert not _storage_operations(denied)
                    operations.append(
                        {
                            **denied,
                            "project_assets_before": before,
                            "project_assets_after": await _project_assets(
                                owner_maker, project.id, aliases
                            ),
                            "object_after": _object_state(owner_s3, bucket, key),
                        }
                    )

                    before_main = await _project_assets(
                        owner_maker, project.id, aliases
                    )
                    before_other = await _project_assets(
                        owner_maker, other_project.id, aliases
                    )
                    wrong_upload, _ = await _capture_http(
                        client,
                        label="wrong_project_key_upload_complete_before_head",
                        method="POST",
                        path=f"{other_path}/upload-complete",
                        actor_alias="owner",
                        token=tokens["owner"],
                        aliases=aliases,
                        body={
                            "key": key,
                            "original_name": "guide.png",
                            "content_type": "image/png",
                        },
                    )
                    assert wrong_upload["response"]["status"] == 404
                    assert not _storage_operations(wrong_upload)
                    operations.append(
                        {
                            **wrong_upload,
                            "main_project_assets_before": before_main,
                            "main_project_assets_after": await _project_assets(
                                owner_maker, project.id, aliases
                            ),
                            "other_project_assets_before": before_other,
                            "other_project_assets_after": await _project_assets(
                                owner_maker, other_project.id, aliases
                            ),
                            "object_after": _object_state(owner_s3, bucket, key),
                        }
                    )

                    before_main = await _project_assets(
                        owner_maker, project.id, aliases
                    )
                    before_other = await _project_assets(
                        owner_maker, other_project.id, aliases
                    )
                    wrong_project, _ = await _capture_http(
                        client,
                        label="wrong_project_key_rejected_before_storage",
                        method="DELETE",
                        path=other_path,
                        actor_alias="owner",
                        token=tokens["owner"],
                        aliases=aliases,
                        params={"key": key},
                    )
                    assert wrong_project["response"]["status"] == 404
                    assert not _storage_operations(wrong_project)
                    operations.append(
                        {
                            **wrong_project,
                            "main_project_assets_before": before_main,
                            "main_project_assets_after": await _project_assets(
                                owner_maker, project.id, aliases
                            ),
                            "other_project_assets_before": before_other,
                            "other_project_assets_after": await _project_assets(
                                owner_maker, other_project.id, aliases
                            ),
                            "object_after": _object_state(owner_s3, bucket, key),
                        }
                    )

                    unregistered = (
                        f"projects/{project.id}/guide/{uuid.uuid4()}-unregistered.png"
                    )
                    aliases[unregistered] = "$guide_unregistered"
                    _assert_key_unused(owner_s3, bucket, unregistered)
                    owned_keys.append(unregistered)
                    owner_s3.put_object(
                        Bucket=bucket,
                        Key=unregistered,
                        Body=b"unregistered-guide-object",
                        ContentType="image/png",
                    )
                    unregistered_before = _object_state(owner_s3, bucket, unregistered)
                    before = await _project_assets(owner_maker, project.id, aliases)
                    absent_reference, _ = await _capture_http(
                        client,
                        label="unregistered_reference_rejected_before_storage",
                        method="DELETE",
                        path=base_path,
                        actor_alias="owner",
                        token=tokens["owner"],
                        aliases=aliases,
                        params={"key": unregistered},
                    )
                    assert absent_reference["response"]["status"] == 404
                    assert not _storage_operations(absent_reference)
                    operations.append(
                        {
                            **absent_reference,
                            "project_assets_before": before,
                            "project_assets_after": await _project_assets(
                                owner_maker, project.id, aliases
                            ),
                            "object_before": unregistered_before,
                            "object_after": _object_state(
                                owner_s3, bucket, unregistered
                            ),
                        }
                    )

                    missing = f"projects/{project.id}/guide/{uuid.uuid4()}-missing.png"
                    aliases[missing] = "$guide_missing"
                    _assert_key_unused(owner_s3, bucket, missing)
                    owned_keys.append(missing)
                    before = await _project_assets(owner_maker, project.id, aliases)
                    missing_upload, _ = await _capture_http(
                        client,
                        label="missing_object_head_rejected_before_write_lock",
                        method="POST",
                        path=f"{base_path}/upload-complete",
                        actor_alias="owner",
                        token=tokens["owner"],
                        aliases=aliases,
                        body={
                            "key": missing,
                            "original_name": "missing.png",
                            "content_type": "image/png",
                        },
                    )
                    assert missing_upload["response"]["status"] == 400
                    assert _storage_operations(missing_upload) == ["verify_upload"]
                    assert not _phase_sequences(missing_upload, "project.lock")
                    assert not _phase_sequences(missing_upload, "project.write")
                    operations.append(
                        {
                            **missing_upload,
                            "project_assets_before": before,
                            "project_assets_after": await _project_assets(
                                owner_maker, project.id, aliases
                            ),
                            "object_after": _object_state(owner_s3, bucket, missing),
                        }
                    )

                    before = await _project_assets(owner_maker, project.id, aliases)
                    unsupported, _ = await _capture_http(
                        client,
                        label="unsupported_upload_type_rejected_without_storage",
                        method="POST",
                        path=f"{base_path}/upload-init",
                        actor_alias="owner",
                        token=tokens["owner"],
                        aliases=aliases,
                        body={
                            "filename": "unsafe.exe",
                            "content_type": "application/x-msdownload",
                            "size": 128,
                        },
                    )
                    assert unsupported["response"]["status"] == 400
                    assert not _storage_operations(unsupported)
                    operations.append(
                        {
                            **unsupported,
                            "project_assets_before": before,
                            "project_assets_after": await _project_assets(
                                owner_maker, project.id, aliases
                            ),
                        }
                    )

                    denied_key = (
                        f"projects/{project.id}/guide/{uuid.uuid4()}-delete-denied.png"
                    )
                    aliases[denied_key] = "$guide_delete_denied"
                    _assert_key_unused(owner_s3, bucket, denied_key)
                    owned_keys.append(denied_key)
                    owner_s3.put_object(
                        Bucket=bucket,
                        Key=denied_key,
                        Body=_GUIDE_FILE,
                        ContentType="image/png",
                    )
                    async with owner_maker.begin() as fixture_db:
                        fixture_project = await fixture_db.get(Project, project.id)
                        fixture_project.guide_assets = [
                            *(fixture_project.guide_assets or []),
                            {
                                "key": denied_key,
                                "original_name": "delete-denied.png",
                                "content_type": "image/png",
                                "size": len(_GUIDE_FILE),
                                "uploaded_at": "2026-01-15T12:00:00+00:00",
                            },
                        ]
                    deny_delete_client = _local_s3_client(
                        settings,
                        _required_env("AAP_MODULE_DATA_ASSET_CATALOG_S3_ACCESS_KEY"),
                        _required_env("AAP_MODULE_DATA_ASSET_CATALOG_S3_SECRET_KEY"),
                    )
                    # This already-owned identity only has GET permissions. Its
                    # real MinIO denial exercises the failure path without an
                    # additional identity, policy mutation or fabricated error.
                    before = await _project_assets(owner_maker, project.id, aliases)
                    denied_versions = _version_summary(owner_s3, bucket, denied_key)
                    with monkeypatch.context() as denied_storage:
                        denied_storage.setattr(
                            storage_service, "client", deny_delete_client
                        )
                        failed_delete, failed_delete_raw = await _capture_http(
                            client,
                            label="real_delete_denial_still_removes_metadata",
                            method="DELETE",
                            path=base_path,
                            actor_alias="owner",
                            token=tokens["owner"],
                            aliases=aliases,
                            params={"key": denied_key},
                        )
                    assert failed_delete["response"]["status"] == 200
                    assert failed_delete_raw == {"deleted": denied_key}
                    denied_events = [
                        item
                        for item in failed_delete["ordered_effects"]
                        if item.get("kind") == "storage"
                    ]
                    assert len(denied_events) == 1
                    assert denied_events[0]["operation"] == "delete_object"
                    assert denied_events[0]["outcome"] == "error"
                    assert denied_events[0]["error_code"] in {"AccessDenied", "403"}
                    after = await _project_assets(owner_maker, project.id, aliases)
                    assert not any(
                        entry["key"] == "$guide_delete_denied" for entry in after
                    )
                    assert _object_state(owner_s3, bucket, denied_key)["exists"]
                    assert (
                        _version_summary(owner_s3, bucket, denied_key)
                        == denied_versions
                    )
                    assert (
                        _phase_sequences(failed_delete, "project.lock")[0]
                        < denied_events[0]["sequence"]
                        < _phase_sequences(failed_delete, "project.write")[0]
                        < _phase_sequences(failed_delete, "database.commit")[0]
                    )
                    operations.append(
                        {
                            **failed_delete,
                            "project_assets_before": before,
                            "project_assets_after": after,
                            "object_after": _object_state(owner_s3, bucket, denied_key),
                            "object_versions_before": denied_versions,
                            "object_versions_after": _version_summary(
                                owner_s3, bucket, denied_key
                            ),
                            "storage_identity": "existing-verifier-catalog-readonly",
                            "storage_policy_changed": False,
                        }
                    )

                    before = await _project_assets(owner_maker, project.id, aliases)
                    object_before_delete = _object_state(owner_s3, bucket, key)
                    deleted, deleted_raw = await _capture_http(
                        client,
                        label="registered_reference_removed_then_exact_object_deleted",
                        method="DELETE",
                        path=base_path,
                        actor_alias="owner",
                        token=tokens["owner"],
                        aliases=aliases,
                        params={"key": key},
                    )
                    assert deleted["response"]["status"] == 200
                    assert deleted_raw == {"deleted": key}
                    object_after_delete = _object_state(owner_s3, bucket, key)
                    assert (
                        object_before_delete["exists"]
                        and not object_after_delete["exists"]
                    )
                    assert _storage_operations(deleted) == ["delete_object"]
                    lock_sequence = _phase_sequences(deleted, "project.lock")[0]
                    delete_sequence = _storage_sequence(deleted, "delete_object")[0]
                    write_sequence = _phase_sequences(deleted, "project.write")[0]
                    commit_sequence = _phase_sequences(deleted, "database.commit")[0]
                    assert (
                        lock_sequence
                        < delete_sequence
                        < write_sequence
                        < commit_sequence
                    )
                    operations.append(
                        {
                            **deleted,
                            "project_assets_before": before,
                            "project_assets_after": await _project_assets(
                                owner_maker, project.id, aliases
                            ),
                            "object_before": object_before_delete,
                            "object_after": object_after_delete,
                            "object_versions_after": _version_summary(
                                owner_s3, bucket, key
                            ),
                        }
                    )

                    assert len(operations) == 11
                    capture = {
                        "capture_kind": "actual-legacy-project-guide-assets-http-and-effects",
                        "source": source,
                        "runtime_identity": runtime_identity,
                        "object_store": {
                            "bucket": "$owned-guide-assets-bucket",
                            "ownership_tag_verified": True,
                            "versioning": versioning or "Disabled",
                            "api_bucket_matches_owned_bucket": True,
                            "uploads": "presigned HTTP PUT to verifier MinIO",
                        },
                        "observations": {
                            "fixture_object_put_is_real": True,
                            "api_head_and_delete_are_unmocked": True,
                            "cleanup_scope": "exact generated object keys and versions only",
                            "delete_storage_failure": "actual MinIO AccessDenied with existing readonly catalog credential; metadata still commits, object remains",
                        },
                        "samples": operations,
                    }

    except BaseException:
        active_error = sys.exc_info()[1]
        raise
    finally:
        cleanup_errors = []
        try:
            _cleanup_objects(owner_s3, bucket, owned_keys)
        except Exception as error:
            cleanup_errors.append(error)
        try:
            await _cleanup_rows(
                test_engine, actor_ids=actor_ids, project_ids=project_ids
            )
        except Exception as error:
            cleanup_errors.append(error)
        if runtime_engine is not None:
            try:
                await runtime_engine.dispose()
            except Exception as error:
                cleanup_errors.append(error)
        if active_error is not None:
            for error in cleanup_errors:
                warnings.warn(
                    f"legacy guide-assets capture cleanup failed: {error!r}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        elif cleanup_errors:
            raise cleanup_errors[0]

    assert capture is not None
    serialized = json.dumps(capture, ensure_ascii=False, sort_keys=True)
    for marker in (
        "x-amz-credential",
        "x-amz-signature",
        "x-amz-algorithm",
        "x-amz-date",
        "x-amz-expires",
        "x-amz-security-token",
        "awsaccesskeyid=",
        "signature=",
    ):
        assert marker not in serialized.casefold(), marker
    assert runtime_url not in serialized
    assert all(token not in serialized for token in tokens.values())
    assert settings.minio_access_key not in serialized
    assert settings.minio_secret_key not in serialized
    with capture_path.open("x", encoding="utf-8") as stream:
        json.dump(capture, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
