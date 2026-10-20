"""AUTH-only HTTP qualification for remote and dedicated offboarding owners."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from tests.factory import create_membership, create_project, create_user
from tests.test_identity_admission_live import (
    ROOT,
    _dsn,
    _start_api,
    _start_container_identity,
    _token,
    _world,
)
from tests.test_identity_offboarding_live import _cleanup_offboarding

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_OFFBOARDING_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled AUTH-only offboarding API verifier",
    ),
]


def _auth_dsn(test_engine):
    return (
        make_url(_dsn("AUTH", test_engine))
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )


def _node_public(mode, response, target_id, *, actor_id, expected_mode, reason):
    peer = ROOT / "apps/api/tests/fixtures/identity_offboarding_peer.mjs"
    value = {
        "mode": mode,
        "status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "body": response.text,
        "target_user_id": str(target_id),
    }
    if mode == "result":
        value.update(
            {
                "expected_mode": expected_mode,
                "expected_reason": reason,
                "actor_id": str(actor_id),
            }
        )
    result = subprocess.run(
        ["node", str(peer), "--validate-public"],
        input=json.dumps(value, ensure_ascii=False, allow_nan=False),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, "Public offboarding response failed Node validation"
    assert str(target_id) not in result.stderr
    return json.loads(result.stdout)


def _node_public_stale(response, expected_version):
    peer = ROOT / "apps/api/tests/fixtures/identity_offboarding_peer.mjs"
    result = subprocess.run(
        ["node", str(peer), "--validate-public-stale"],
        input=json.dumps(
            {
                "status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "body": response.text,
                "expected_preview_version": expected_version,
            },
            ensure_ascii=False,
            allow_nan=False,
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, "Stale offboarding response failed Node validation"
    assert expected_version not in result.stderr
    return json.loads(result.stdout)


async def _account_state(maker, user_id):
    async with maker() as db:
        row = await db.execute(
            text(
                "SELECT is_active, credential_generation, disabled_kind "
                "FROM public.users WHERE id=:id"
            ),
            {"id": user_id},
        )
        return dict(row.mappings().one())


async def _offboarding_receipts(maker, actor_id, target_id):
    async with maker() as db:
        rows = await db.execute(
            text(
                "SELECT operation_id, result, audit_id FROM "
                "public.identity_offboarding_operation_receipts "
                "WHERE actor_id=:actor AND target_user_id=:target "
                "ORDER BY operation_id"
            ),
            {"actor": actor_id, "target": target_id},
        )
        return [dict(row._mapping) for row in rows]


@pytest.mark.parametrize("binding", ["remote_container", "dedicated_dsn"])
async def test_auth_only_offboarding_http_bindings_preserve_public_contract(
    test_engine, tmp_path, binding
):
    maker, actor_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    identity = api = None
    user_ids = [actor_id]
    project_ids = []
    try:
        async with maker.begin() as db:
            target = await create_user(
                db,
                "employee",
                f"offboarding-api-target-{suffix}@example.test",
                "HTTP offboarding target",
            )
            owner_receiver = await create_user(
                db,
                "project_admin",
                f"offboarding-api-owner-{suffix}@example.test",
                "HTTP owner receiver",
            )
            annotator_receiver = await create_user(
                db,
                "employee",
                f"offboarding-api-annotator-{suffix}@example.test",
                "HTTP annotator receiver",
            )
            project = await create_project(
                db, owner_id=target.id, name="HTTP 离职交接项目"
            )
            project_ids.append(project.id)
            await create_membership(
                db,
                project_id=project.id,
                user_id=target.id,
                role="annotator",
                assigned_by=actor_id,
            )
            await create_membership(
                db,
                project_id=project.id,
                user_id=annotator_receiver.id,
                role="annotator",
                assigned_by=actor_id,
            )
        user_ids.extend([target.id, owner_receiver.id, annotator_receiver.id])

        configuration = {}
        if binding == "remote_container":
            identity = await _start_container_identity(
                test_engine,
                tmp_path,
                secret,
                control,
                enable_accounts=True,
                enable_groups=False,
                enable_invitations=False,
                enable_directory=False,
                enable_admin_password=False,
                enable_platform_role=False,
                enable_suspension=False,
                enable_deactivation=False,
                enable_deletions=False,
                enable_offboarding=True,
                enable_presence=False,
            )
            identity_origin = identity.origin
        else:
            identity_origin = ""
            configuration = {
                "IDENTITY_AUTH_DATABASE_URL": _dsn("AUTH", test_engine),
                "IDENTITY_ACCOUNT_DATABASE_URL": _dsn("ACCOUNTS", test_engine),
                "IDENTITY_OFFBOARDING_DATABASE_URL": _dsn("OFFBOARDING", test_engine),
            }

        api = _start_api(
            tmp_path,
            secret,
            control,
            identity_origin,
            database_url=_auth_dsn(test_engine),
            configuration=configuration,
        )
        manager_token = _token(actor_id, secret)
        headers = {"Authorization": f"Bearer {manager_token}"}
        async with httpx.AsyncClient(
            base_url=api.origin, trust_env=False, timeout=120
        ) as client:
            me = await client.get("/api/v1/auth/me", headers=headers)
            assert me.status_code == 200, (
                f"AUTH-only /auth/me: {me.status_code} {me.text}"
            )
            invalid_path = await client.get(
                "/api/v1/users/not-a-uuid/offboarding-preview", headers=headers
            )
            assert invalid_path.status_code == 422, invalid_path.text

            if binding == "dedicated_dsn":
                before = await _account_state(maker, target.id)
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "GRANT SELECT ON public.users TO identity_offboarding_client"
                        )
                    )
                try:
                    misprivileged = await client.get(
                        f"/api/v1/users/{target.id}/offboarding-preview",
                        headers=headers,
                    )
                    assert misprivileged.status_code == 503, misprivileged.text
                    assert secret not in misprivileged.text
                    assert control not in misprivileged.text
                    assert await _account_state(maker, target.id) == before
                    assert await _offboarding_receipts(maker, actor_id, target.id) == []
                finally:
                    async with maker.begin() as db:
                        await db.execute(
                            text(
                                "REVOKE SELECT ON public.users "
                                "FROM identity_offboarding_client"
                            )
                        )

            preview_response = await client.get(
                f"/api/v1/users/{target.id}/offboarding-preview", headers=headers
            )
            preview = _node_public(
                "preview",
                preview_response,
                target.id,
                actor_id=actor_id,
                expected_mode="handoff",
                reason="",
            )
            assert preview["can_commit"] is True
            assert len(preview["projects"]) == 1
            assert await _offboarding_receipts(maker, actor_id, target.id) == []

            huge_version = "\0" + "preview-version-" * 5000
            too_many_projects = [
                {
                    "project_id": str(uuid.uuid4()),
                    "owner_receiver_id": None,
                    "annotator_receiver_id": None,
                    "reviewer_receiver_id": None,
                }
                for _ in range(1025)
            ]
            stale_response = await client.post(
                f"/api/v1/users/{target.id}/offboarding",
                headers=headers,
                json={
                    "preview_version": huge_version,
                    "reason": "",
                    "projects": too_many_projects,
                },
            )
            assert stale_response.status_code == 409, stale_response.text
            stale_detail = _node_public_stale(stale_response, huge_version)
            assert stale_detail["code"] == "offboarding_preview_stale"
            assert stale_detail["expected_preview_version"] == huge_version
            assert await _account_state(maker, target.id) == {
                "is_active": True,
                "credential_generation": 0,
                "disabled_kind": None,
            }
            assert await _offboarding_receipts(maker, actor_id, target.id) == []

            expected_reason = ""
            commit_response = await client.post(
                f"/api/v1/users/{target.id}/offboarding",
                headers=headers,
                json={
                    "preview_version": preview["preview_version"],
                    "projects": [
                        {
                            "project_id": str(project.id),
                            "owner_receiver_id": str(owner_receiver.id),
                            "annotator_receiver_id": str(annotator_receiver.id),
                            "reviewer_receiver_id": None,
                        }
                    ],
                    "ignored_legacy_extra": "public request models ignore extras",
                },
            )
            public_result = _node_public(
                "result",
                commit_response,
                target.id,
                actor_id=actor_id,
                expected_mode="handoff",
                reason=expected_reason,
            )
            assert public_result["status"] == "suspended"
            assert public_result["user"]["disabled_kind"] == "suspended"
            assert len(public_result["transfers"]) == 2
            state = await _account_state(maker, target.id)
            assert state["is_active"] is False
            assert state["credential_generation"] == 1
            receipts = await _offboarding_receipts(maker, actor_id, target.id)
            assert len(receipts) == 1
            assert receipts[0]["result"]["user"]["id"] == str(target.id)
            assert receipts[0]["audit_id"] is not None

            consumed_preview = await client.post(
                f"/api/v1/users/{target.id}/offboarding",
                headers=headers,
                json={
                    "preview_version": preview["preview_version"],
                    "projects": [
                        {
                            "project_id": str(project.id),
                            "owner_receiver_id": str(owner_receiver.id),
                            "annotator_receiver_id": str(annotator_receiver.id),
                            "reviewer_receiver_id": None,
                        }
                    ],
                },
            )
            assert consumed_preview.status_code == 409, consumed_preview.text
            consumed_detail = _node_public_stale(
                consumed_preview, preview["preview_version"]
            )
            assert (
                consumed_detail["current_preview_version"] != preview["preview_version"]
            )
            assert len(await _offboarding_receipts(maker, actor_id, target.id)) == 1
            assert (await _account_state(maker, target.id)) == state
            async with maker() as db:
                audit_count = await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND target_id=:target "
                        "AND action='user.offboard'"
                    ),
                    {"actor": actor_id, "target": str(target.id)},
                )
            assert audit_count == 1
    finally:
        if api:
            api.stop()
        if identity:
            identity.stop()
        await _cleanup_offboarding(
            maker,
            user_ids=user_ids,
            project_ids=project_ids,
        )
