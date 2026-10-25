"""Replay the immutable legacy Guide HTTP contract through three owner bindings."""

from __future__ import annotations

from contextlib import AsyncExitStack
import os
import uuid

import httpx
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.project import Project
from app.services import workspace_configuration as configuration_service
from app.services.storage import storage_service
from tests.factory import create_membership, create_project, create_user
from tests.labeling_class_query_helpers import api_db_binding, clear_local_identity
from tests.project_guide_asset_owner_helpers import (
    GUIDE_FILE,
    _cleanup_objects,
    _normalise,
    _object_state,
    _put_through_presigned_url,
    _version_summary,
    assert_no_configuration_receipts_or_states,
    assert_workspace_identity,
    cleanup_configuration_receipts_or_states,
    compare_sample_state,
    create_guide_storage_scope,
    endpoint_url,
    fixture_data,
    port_client,
    request_runtime,
    required_env,
    selected_guide_binding,
    snapshot_state,
)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the explicitly owned PostgreSQL and MinIO verifier",
    ),
]


async def _request(
    client,
    *,
    method,
    path,
    actor_alias,
    token,
    aliases,
    body,
    params,
    sample,
    response_key_alias=None,
):
    request_value = {
        "method": method,
        "path": _normalise(path, aliases),
        "actor_alias": actor_alias,
    }
    if body is not None:
        request_value["body"] = _normalise(body, aliases)
    if params is not None:
        request_value["query"] = _normalise(params, aliases)
    assert request_value == sample["request"], f"{sample['label']}: request"

    response = await client.request(
        method,
        path,
        headers={"Authorization": f"Bearer {token}"},
        json=body,
        params=params,
    )
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
    expected = sample["response"]
    actual_headers = {}
    if "content-type" in response.headers:
        actual_headers["content-type"] = response.headers["content-type"]
    actual = {
        "status": response.status_code,
        "headers": actual_headers,
        "body": _normalise(payload, aliases),
    }
    assert actual == expected, sample["label"]
    return payload


async def _replay_mode(test_engine, app_module, monkeypatch, tmp_path, mode: str):
    frozen = fixture_data()
    resources, bucket, owner_s3 = await assert_workspace_identity(test_engine)
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime_engine = None
    scoped_s3 = None
    scope = None
    destroy_scope = None
    actor_ids = []
    project_ids = []
    operation_ids = []
    owned_keys: list[str] = []
    aliases = {bucket: "$owned-guide-assets-bucket"}
    try:
        clear_local_identity(monkeypatch)
        async with owner_maker.begin() as db:
            owner = await create_user(
                db,
                "project_admin",
                f"guide-owner-{uuid.uuid4().hex}@example.test",
                "Guide asset owner",
            )
            actor_ids.append(owner.id)
            reader = await create_user(
                db,
                "employee",
                f"guide-reader-{uuid.uuid4().hex}@example.test",
                "Guide asset reader",
            )
            actor_ids.append(reader.id)
            nonowner_manager = await create_user(
                db,
                "project_admin",
                f"guide-manager-{uuid.uuid4().hex}@example.test",
                "Guide asset non-owner manager",
            )
            actor_ids.append(nonowner_manager.id)
            main = await create_project(
                db, owner_id=owner.id, name="Guide asset owned project"
            )
            project_ids.append(main.id)
            other = await create_project(
                db, owner_id=owner.id, name="Guide asset second project"
            )
            project_ids.append(other.id)
            main.guide_assets = []
            other.guide_assets = []
            await create_membership(
                db,
                project_id=main.id,
                user_id=reader.id,
                role="annotator",
                assigned_by=owner.id,
            )
            await create_membership(
                db,
                project_id=main.id,
                user_id=nonowner_manager.id,
                role="annotator",
                assigned_by=owner.id,
            )

        aliases.update(
            {
                str(owner.id): "$actor_owner",
                str(reader.id): "$actor_reader",
                str(nonowner_manager.id): "$actor_nonowner_manager",
                str(main.id): "$project_main",
                str(other.id): "$project_other",
            }
        )
        tokens = {
            "owner": create_access_token(str(owner.id), owner.role),
            "reader": create_access_token(str(reader.id), reader.role),
            "nonowner_manager": create_access_token(
                str(nonowner_manager.id), nonowner_manager.role
            ),
        }
        denied_key = f"projects/{main.id}/guide/{uuid.uuid4()}-delete-denied.png"
        aliases[denied_key] = "$guide_delete_denied"
        scope, destroy_scope = create_guide_storage_scope(
            resources, owner_s3, bucket, denied_key
        )
        scoped_s3 = port_client(scope["access_key"], scope["secret_key"])
        monkeypatch.setattr(storage_service, "client", scoped_s3)
        monkeypatch.setattr(storage_service, "bucket", bucket)

        runtime_engine, runtime_maker = await request_runtime(test_engine)
        async with runtime_engine.connect() as connection:
            role, database = (
                await connection.execute(
                    text("SELECT current_user, current_database()")
                )
            ).one()
        assert role == "legacy_runtime"
        assert database == test_engine.url.database

        make_configuration_request = configuration_service.configuration_request

        def record_configuration_request(*args, **kwargs):
            request = make_configuration_request(*args, **kwargs)
            operation_ids.append(request.operation_id)
            return request

        monkeypatch.setattr(
            configuration_service,
            "configuration_request",
            record_configuration_request,
        )

        source_origin = None
        source_token = None
        keys: dict[str, str] = {}
        used: list[str] = []
        base = f"/api/v1/projects/{main.id}/guide-assets"
        other_base = f"/api/v1/projects/{other.id}/guide-assets"
        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)

        async with AsyncExitStack() as stack:
            if mode == "http":
                required_env("AAP_ASSETS_GUIDE_PYTHON")
                from tests.test_project_guide_object_runtime import (
                    _installed_guide_process,
                )

                source_origin, source_token, _ = await stack.enter_async_context(
                    _installed_guide_process(
                        tmp_path,
                        endpoint=endpoint_url(),
                        bucket=bucket,
                        access_key=scope["access_key"],
                        secret_key=scope["secret_key"],
                    )
                )
            async with selected_guide_binding(
                monkeypatch,
                test_engine,
                tmp_path,
                mode=mode,
                guide_url=source_origin,
                guide_token=source_token,
            ):
                async with api_db_binding(app_module, runtime_maker):
                    async with httpx.AsyncClient(
                        transport=transport, base_url="http://test", timeout=30
                    ) as client:

                        async def replay(
                            label,
                            *,
                            method,
                            path,
                            actor_alias,
                            object_alias=None,
                            body=None,
                            params=None,
                            response_key_alias=None,
                        ):
                            sample = next(
                                row
                                for row in frozen["samples"]
                                if row["label"] == label
                            )
                            key_before = (
                                keys.get(object_alias) if object_alias else None
                            )
                            before = await snapshot_state(
                                owner_maker,
                                main.id,
                                other.id,
                                aliases,
                                owner_s3=owner_s3,
                                bucket=bucket,
                                object_key=key_before,
                            )
                            payload = await _request(
                                client,
                                method=method,
                                path=path,
                                actor_alias=actor_alias,
                                token=tokens[actor_alias],
                                aliases=aliases,
                                body=body,
                                params=params,
                                sample=sample,
                                response_key_alias=response_key_alias,
                            )
                            if response_key_alias:
                                key_after = payload["key"]
                                aliases[key_after] = response_key_alias
                                owned_keys.append(key_after)
                                if object_alias:
                                    keys[object_alias] = key_after
                            after = await snapshot_state(
                                owner_maker,
                                main.id,
                                other.id,
                                aliases,
                                owner_s3=owner_s3,
                                bucket=bucket,
                                object_key=keys.get(object_alias)
                                if object_alias
                                else None,
                            )
                            compare_sample_state(sample, before, after)
                            used.append(label)
                            return payload

                        # 1. Init returns a presigned URL; the real PUT follows it.
                        upload = await replay(
                            "upload_init_presign",
                            method="POST",
                            path=f"{base}/upload-init",
                            actor_alias="owner",
                            object_alias="main",
                            body={
                                "filename": "guide.png",
                                "content_type": "image/png",
                                "size": len(GUIDE_FILE),
                            },
                            response_key_alias="$project_main_guide_key",
                        )
                        key = keys["main"]
                        assert key.startswith(f"projects/{main.id}/guide/")
                        assert not _object_state(owner_s3, bucket, key)["exists"]
                        assert not _version_summary(owner_s3, bucket, key)["count"]
                        assert await _put_through_presigned_url(
                            upload["upload_url"], GUIDE_FILE
                        ) in {200, 204}
                        uploaded = _object_state(owner_s3, bucket, key)
                        assert uploaded == {
                            "exists": True,
                            "content_length": len(GUIDE_FILE),
                            "content_type": "image/png",
                        }

                        # 2. HEAD verifies the actual object before metadata is saved.
                        complete = await replay(
                            "upload_complete_head_then_manager_lock_write",
                            method="POST",
                            path=f"{base}/upload-complete",
                            actor_alias="owner",
                            object_alias="main",
                            body={
                                "key": key,
                                "original_name": "guide.png",
                                "content_type": "image/png",
                            },
                        )
                        assert complete["size"] == len(GUIDE_FILE)

                        # 3. A visible member signs and retrieves the real object.
                        signed = await replay(
                            "visible_member_signs_registered_asset",
                            method="GET",
                            path=f"{base}/sign-url",
                            actor_alias="reader",
                            object_alias="main",
                            params={"key": key},
                        )
                        from tests.test_project_guide_assets_legacy import (
                            _public_origin_for_upload,
                        )

                        try:
                            async with httpx.AsyncClient(
                                trust_env=False, timeout=15
                            ) as signed_client:
                                downloaded = await signed_client.get(
                                    _public_origin_for_upload(signed["url"])
                                )
                        except httpx.HTTPError as error:
                            raise AssertionError(
                                f"signed Guide download failed: {type(error).__name__}"
                            ) from None
                        assert downloaded.status_code == 200
                        assert downloaded.content == GUIDE_FILE

                        # 4. A member without ownership cannot delete.
                        await replay(
                            "member_delete_denied_before_storage",
                            method="DELETE",
                            path=base,
                            actor_alias="nonowner_manager",
                            object_alias="main",
                            params={"key": key},
                        )

                        # 5-6. A key belonging to the other project stays invisible.
                        await replay(
                            "wrong_project_key_upload_complete_before_head",
                            method="POST",
                            path=f"{other_base}/upload-complete",
                            actor_alias="owner",
                            object_alias="main",
                            body={
                                "key": key,
                                "original_name": "guide.png",
                                "content_type": "image/png",
                            },
                        )
                        await replay(
                            "wrong_project_key_rejected_before_storage",
                            method="DELETE",
                            path=other_base,
                            actor_alias="owner",
                            object_alias="main",
                            params={"key": key},
                        )

                        # 7. Existing unregistered bytes are not a deletable reference.
                        unregistered = (
                            f"projects/{main.id}/guide/{uuid.uuid4()}-unregistered.png"
                        )
                        aliases[unregistered] = "$guide_unregistered"
                        keys["unregistered"] = unregistered
                        owned_keys.append(unregistered)
                        owner_s3.put_object(
                            Bucket=bucket,
                            Key=unregistered,
                            Body=b"unregistered-guide-object",
                            ContentType="image/png",
                        )
                        await replay(
                            "unregistered_reference_rejected_before_storage",
                            method="DELETE",
                            path=base,
                            actor_alias="owner",
                            object_alias="unregistered",
                            params={"key": unregistered},
                        )

                        # 8. Missing bytes fail at HEAD without metadata changes.
                        missing = f"projects/{main.id}/guide/{uuid.uuid4()}-missing.png"
                        aliases[missing] = "$guide_missing"
                        keys["missing"] = missing
                        owned_keys.append(missing)
                        await replay(
                            "missing_object_head_rejected_before_write_lock",
                            method="POST",
                            path=f"{base}/upload-complete",
                            actor_alias="owner",
                            object_alias="missing",
                            body={
                                "key": missing,
                                "original_name": "missing.png",
                                "content_type": "image/png",
                            },
                        )

                        # 9. Invalid media type produces no object operation.
                        await replay(
                            "unsupported_upload_type_rejected_without_storage",
                            method="POST",
                            path=f"{base}/upload-init",
                            actor_alias="owner",
                            body={
                                "filename": "unsafe.exe",
                                "content_type": "application/x-msdownload",
                                "size": 128,
                            },
                        )

                        # 10. A real exact-key AccessDenied still removes metadata.
                        owner_s3.put_object(
                            Bucket=bucket,
                            Key=denied_key,
                            Body=GUIDE_FILE,
                            ContentType="image/png",
                        )
                        async with owner_maker.begin() as fixture_db:
                            project = await fixture_db.get(Project, main.id)
                            project.guide_assets = [
                                *(project.guide_assets or []),
                                {
                                    "key": denied_key,
                                    "original_name": "delete-denied.png",
                                    "content_type": "image/png",
                                    "size": len(GUIDE_FILE),
                                    "uploaded_at": "2026-01-15T12:00:00+00:00",
                                },
                            ]
                        keys["denied"] = denied_key
                        denied_before = _object_state(owner_s3, bucket, denied_key)
                        versions_before = _version_summary(owner_s3, bucket, denied_key)
                        try:
                            scoped_s3.delete_object(Bucket=bucket, Key=denied_key)
                        except ClientError as error:
                            denied_code = error.response.get("Error", {}).get("Code")
                            assert denied_code in {"AccessDenied", "403", "Forbidden"}
                        else:
                            raise AssertionError(
                                "the exact guide object policy did not deny deletion"
                            )
                        await replay(
                            "real_delete_denial_still_removes_metadata",
                            method="DELETE",
                            path=base,
                            actor_alias="owner",
                            object_alias="denied",
                            params={"key": denied_key},
                        )
                        assert (
                            _object_state(owner_s3, bucket, denied_key) == denied_before
                        )
                        assert (
                            _version_summary(owner_s3, bucket, denied_key)
                            == versions_before
                        )

                        # 11. Registered reference deletion removes only its object.
                        await replay(
                            "registered_reference_removed_then_exact_object_deleted",
                            method="DELETE",
                            path=base,
                            actor_alias="owner",
                            object_alias="main",
                            params={"key": key},
                        )
                        assert operation_ids, (
                            "Guide requests bypassed Workspace Configuration"
                        )
                        assert used == [row["label"] for row in frozen["samples"]]
                        await assert_no_configuration_receipts_or_states(
                            test_engine, actor_ids, operation_ids
                        )
    finally:
        cleanup_errors = []
        try:
            _cleanup_objects(owner_s3, bucket, owned_keys)
        except Exception as error:
            cleanup_errors.append(error)
        if scope is not None and destroy_scope is not None:
            try:
                destroy_scope(scope)
            except Exception as error:
                cleanup_errors.append(error)
        if actor_ids or project_ids:
            from tests.test_project_guide_assets_legacy import _cleanup_rows

            try:
                if actor_ids:
                    await cleanup_configuration_receipts_or_states(
                        test_engine, actor_ids, operation_ids
                    )
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
        if scoped_s3 is not None:
            scoped_s3.close()
        owner_s3.close()
        if cleanup_errors:
            raise RuntimeError(
                f"Guide replay cleanup failed for {len(cleanup_errors)} owned resource(s)"
            ) from cleanup_errors[0]


@pytest.mark.parametrize("mode", ["borrowed", "dedicated", "http"])
async def test_project_guide_asset_owner_replays_frozen_http_and_s3(
    mode, test_engine, app_module, monkeypatch, tmp_path
):
    if mode == "http":
        required_env("AAP_WORKSPACE_QUERY_PYTHON")
    await _replay_mode(test_engine, app_module, monkeypatch, tmp_path, mode)
