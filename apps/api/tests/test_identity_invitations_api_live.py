"""AUTH-only API composition and bounded Identity invitation acceptance."""

from __future__ import annotations

import asyncio
import math
import os
import secrets
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from aap_identity.recovery_crypto import mail_key_from_secret
from aap_identity.security import create_access_token
from app.db.models.group import Group
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from tests.factory import create_project, create_user
from tests.identity_recovery_live_helpers import SMTPInbox
from tests.test_identity_admission_live import (
    _dsn,
    _start_api,
    _start_container_identity,
    _token,
    _world,
)
from tests.test_identity_invitations_live import (
    _cleanup_invitations,
    _command,
    _node,
    _start_mail_v2,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_INVITATIONS_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity invitation verifier",
    ),
]


def _auth_dsn(test_engine):
    return (
        make_url(_dsn("AUTH", test_engine))
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )


async def _invitation_id(maker, token):
    async with maker() as db:
        invitation = await db.scalar(
            select(UserInvitation).where(UserInvitation.token == token)
        )
    assert invitation is not None
    return invitation.id


@pytest.mark.parametrize("binding", ["remote_container", "dedicated_dsn"])
async def test_auth_only_api_exercises_all_thirteen_invitation_operations(
    test_engine, tmp_path, binding, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    actor_ids = [actor]
    project_ids = []
    invitation_ids = []
    group_ids = []
    user_operation_ids = []
    identity = api = mail_worker = None
    smtp = None
    try:
        async with maker.begin() as db:
            existing = await create_user(
                db,
                "employee",
                f"api-existing-{suffix}@example.test",
                "Existing invitation employee",
            )
            project = await create_project(
                db,
                owner_id=actor,
                name=f"API invitation project\n{suffix}",
            )
        actor_ids.append(existing.id)
        project_ids.append(project.id)

        configuration = {}
        if binding == "remote_container":
            identity = await _start_container_identity(
                test_engine,
                tmp_path,
                secret,
                control,
                enable_accounts=False,
                enable_groups=False,
                enable_presence=False,
                enable_invitations=True,
            )
            identity_origin = identity.origin
        else:
            identity_origin = ""
            configuration = {
                "IDENTITY_AUTH_DATABASE_URL": _dsn("AUTH", test_engine),
                "IDENTITY_INVITATION_DATABASE_URL": _dsn("INVITATION", test_engine),
            }
        api = _start_api(
            tmp_path,
            secret,
            control,
            identity_origin,
            database_url=_auth_dsn(test_engine),
            configuration=configuration,
        )

        manager_headers = {"Authorization": f"Bearer {_token(actor, secret)}"}
        existing_token = create_access_token(
            str(existing.id), "employee", secret=secret, expire_minutes=15
        )
        existing_headers = {"Authorization": f"Bearer {existing_token}"}
        async with (
            httpx.AsyncClient(
                base_url=api.origin,
                trust_env=False,
                timeout=90,
                headers=manager_headers,
            ) as manager,
            httpx.AsyncClient(
                base_url=api.origin, trust_env=False, timeout=30
            ) as public,
            httpx.AsyncClient(
                base_url=api.origin,
                trust_env=False,
                timeout=30,
                headers=existing_headers,
            ) as employee,
        ):
            # Resolve and accept an existing account without changing its role.
            existing_create = await manager.post(
                "/api/v1/users/invite",
                json={
                    "email": existing.email,
                    "role": "employee",
                    "group_name": None,
                    "project_id": str(project.id),
                    "project_member_role": "annotator",
                },
            )
            assert existing_create.status_code == 201, existing_create.text
            existing_proof = existing_create.json()["token"]
            invitation_ids.append(await _invitation_id(maker, existing_proof))
            resolved_existing = await public.get(
                f"/api/v1/auth/invitations/{existing_proof}"
            )
            assert resolved_existing.status_code == 200, resolved_existing.text
            assert resolved_existing.json()["email"] == existing.email
            assert resolved_existing.json()["project_id"] == str(project.id)
            accepted = await employee.post(
                "/api/v1/auth/invitations/accept", json={"token": existing_proof}
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["user"]["id"] == str(existing.id)
            assert accepted.json()["user"]["role"] == "employee"
            assert accepted.json()["acceptance"]["project_member_role"] == "annotator"

            # New-account registration creates the requested project/group rows
            # atomically, while the public API returns a usable owner-issued JWT.
            group_name = f"API-Invitation-Group-{suffix[:12]}"
            registered_email = f"api-registered-{suffix}@example.test"
            registration_invite = await manager.post(
                "/api/v1/users/invite",
                json={
                    "email": registered_email,
                    "role": "employee",
                    "group_name": group_name,
                    "project_id": str(project.id),
                    "project_member_role": "reviewer",
                },
            )
            assert registration_invite.status_code == 201, registration_invite.text
            registration_proof = registration_invite.json()["token"]
            invitation_ids.append(await _invitation_id(maker, registration_proof))
            resolved_registration = await public.get(
                f"/api/v1/auth/invitations/{registration_proof}"
            )
            assert resolved_registration.status_code == 200, resolved_registration.text
            assert resolved_registration.json()["group_name"] == group_name
            assert resolved_registration.json()["project_name"] == project.name
            password = "InvitationApiStrong123"
            registered = await public.post(
                "/api/v1/auth/register",
                json={
                    "token": registration_proof,
                    "name": "API registered employee",
                    "password": password,
                },
            )
            assert registered.status_code == 201, registered.text
            registered_body = registered.json()
            new_user_id = uuid.UUID(registered_body["user"]["id"])
            assert registered_body["access_token"]
            assert registered_body["user"]["role"] == "employee"
            assert registered_body["acceptance"]["project_member_role"] == "reviewer"
            actor_ids.append(new_user_id)
            user_operation_ids.append(new_user_id)
            async with maker() as db:
                user = await db.get(User, new_user_id)
                assert user is not None and user.group_name == group_name
                assert user.group_id is not None
                group = await db.get(Group, user.group_id)
                assert group is not None and group.name == group_name
                group_ids.append(group.id)
                membership = await db.scalar(
                    select(ProjectMember).where(
                        ProjectMember.project_id == project.id,
                        ProjectMember.user_id == new_user_id,
                    )
                )
                assert membership is not None and membership.role == "reviewer"

            # Preview is rolled back; apply keeps the two valid rows and reports
            # the invalid address independently.
            batch_items = [
                {
                    "email": f"api-bulk-one-{suffix}@example.test",
                    "role": "employee",
                    "group_name": None,
                    "project_id": None,
                    "project_member_role": None,
                },
                {
                    "email": "not-an-email",
                    "role": "employee",
                    "group_name": None,
                    "project_id": None,
                    "project_member_role": None,
                },
                {
                    "email": f"api-bulk-two-{suffix}@example.test",
                    "role": "employee",
                    "group_name": None,
                    "project_id": None,
                    "project_member_role": None,
                },
            ]
            async with maker() as db:
                before_preview = await db.scalar(
                    select(text("count(*)"))
                    .select_from(UserInvitation)
                    .where(UserInvitation.invited_by == actor)
                )
            preview = await manager.post(
                "/api/v1/users/bulk-invite/preview", json={"items": batch_items}
            )
            assert preview.status_code == 200, preview.text
            preview_body = preview.json()
            assert preview_body["preview"] is True
            assert (preview_body["succeeded"], preview_body["failed"]) == (2, 1)
            assert all(
                item["token"] is None and item["invite_url"] is None
                for item in preview_body["items"]
                if item["ok"]
            )
            async with maker() as db:
                assert (
                    await db.scalar(
                        select(text("count(*)"))
                        .select_from(UserInvitation)
                        .where(UserInvitation.invited_by == actor)
                    )
                    == before_preview
                )

            applied = await manager.post(
                "/api/v1/users/bulk-invite", json={"items": batch_items}
            )
            assert applied.status_code == 200, applied.text
            applied_body = applied.json()
            assert applied_body["preview"] is False
            assert (applied_body["succeeded"], applied_body["failed"]) == (2, 1)
            successful_batch = [item for item in applied_body["items"] if item["ok"]]
            assert len(successful_batch) == 2
            assert all(
                item["token"] and item["invite_url"] for item in successful_batch
            )
            for item in successful_batch:
                invitation_ids.append(await _invitation_id(maker, item["token"]))

            # Revoke and resend are distinct commands. Resend rotates the token
            # but must not enqueue mail; only the explicit send route does.
            mail_invite = await manager.post(
                "/api/v1/users/invite",
                json={
                    "email": f"api-mail-{suffix}@example.test",
                    "role": "employee",
                    "group_name": None,
                    "project_id": str(project.id),
                    "project_member_role": "viewer",
                },
            )
            assert mail_invite.status_code == 201, mail_invite.text
            old_mail_proof = mail_invite.json()["token"]
            mail_invitation_id = await _invitation_id(maker, old_mail_proof)
            invitation_ids.append(mail_invitation_id)
            smtp = SMTPInbox()
            mail_worker = _start_mail_v2(
                test_engine, tmp_path, mail_key_from_secret(secret), smtp
            )
            resent = await manager.post(
                f"/api/v1/invitations/{mail_invitation_id}/resend"
            )
            assert resent.status_code == 200, resent.text
            current_mail_proof = resent.json()["token"]
            assert current_mail_proof != old_mail_proof
            async with maker() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM public.identity_credential_mail_outbox "
                            "WHERE invitation_id=:id AND purpose='invitation'"
                        ),
                        {"id": mail_invitation_id},
                    )
                    == 0
                )
            assert smtp.messages == []
            sent = await manager.post(
                f"/api/v1/invitations/{mail_invitation_id}/send-email"
            )
            assert sent.status_code == 200, sent.text
            sent_body = sent.json()
            assert sent_body["ok"] is True
            assert sent_body["invitation_id"] == str(mail_invitation_id)
            assert parse_qs(urlsplit(sent_body["invite_url"]).query)["token"] == [
                current_mail_proof
            ]
            await smtp.wait(1)
            assert smtp.tokens() == [current_mail_proof]
            assert project.name in smtp.messages[0].get_content()
            async with maker() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT status FROM public.identity_credential_mail_outbox "
                            "WHERE invitation_id=:id AND purpose='invitation'"
                        ),
                        {"id": mail_invitation_id},
                    )
                    == "delivered"
                )

            revoked = await manager.post(
                "/api/v1/users/invite",
                json={
                    "email": f"api-revoked-{suffix}@example.test",
                    "role": "employee",
                    "group_name": None,
                    "project_id": None,
                    "project_member_role": None,
                },
            )
            assert revoked.status_code == 201, revoked.text
            revoked_invitation_id = await _invitation_id(maker, revoked.json()["token"])
            invitation_ids.append(revoked_invitation_id)
            deleted = await manager.delete(
                f"/api/v1/invitations/{revoked_invitation_id}"
            )
            assert deleted.status_code == 204, deleted.text

            # Exercise all manager read presentations after the writes.
            listed = await manager.get(
                "/api/v1/invitations", params={"status": "all", "scope": "me"}
            )
            assert listed.status_code == 200, listed.text
            listed_body = listed.json()
            assert any(item["id"] == str(revoked_invitation_id) for item in listed_body)
            queried = await manager.get(
                "/api/v1/invitations/query",
                params={"page": 1, "page_size": 100, "scope": "me"},
            )
            assert queried.status_code == 200, queried.text
            assert queried.json()["total"] >= len(invitation_ids)
            stats = await manager.get(
                "/api/v1/invitations/stats", params={"scope": "me"}
            )
            assert stats.status_code == 200, stats.text
            stats_body = stats.json()
            assert stats_body["total"] == sum(
                stats_body[key] for key in ("pending", "accepted", "expired", "revoked")
            )
            json_export = await manager.get(
                "/api/v1/invitations/export",
                params={"format": "json", "scope": "me"},
            )
            assert json_export.status_code == 200, json_export.text
            json_body = json_export.json()
            assert json_body["_export_meta"]["count"] == len(json_body["invitations"])
            assert any(
                item["id"] == str(mail_invitation_id)
                for item in json_body["invitations"]
            )
            csv_export = await manager.get(
                "/api/v1/invitations/export",
                params={"format": "csv", "scope": "me"},
            )
            assert csv_export.status_code == 200, csv_export.text
            assert csv_export.headers["content-type"].startswith("text/csv")
            csv_body = csv_export.content.decode("utf-8-sig")
            assert "email" in csv_body.splitlines()[3]
            assert f"api-mail-{suffix}@example.test" in csv_body

        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege("
                        "'identity_auth_client','public.users','SELECT')"
                    )
                )
                is False
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege("
                        "'identity_auth_client','public.user_invitations','SELECT')"
                    )
                )
                is False
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege("
                        "'identity_invitation_client','public.user_invitations','SELECT')"
                    )
                )
                is False
            )
        record_property(
            "invitation_api_boundary",
            binding + ",AUTH-only API DB,all 13 public operations,JSON+CSV export,"
            "registration+acceptance,external MailV2 with no API MAIL_V2 binding",
        )
    finally:
        if api is not None:
            api.stop()
        if mail_worker is not None:
            await asyncio.to_thread(mail_worker.stop)
        if smtp is not None:
            smtp.close()
        if identity is not None:
            identity.stop()
        await _cleanup_invitations(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            invitation_ids=invitation_ids,
            group_ids=group_ids,
            user_operation_ids=user_operation_ids,
        )


async def test_invitation_container_bounded_create_capacity_with_replay(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = None
    suffix = uuid.uuid4().hex
    commands = [
        _command(
            "create_invitation",
            _token(actor, secret),
            {
                "email": f"capacity-{index}-{suffix}@example.test",
                "role": "employee",
                "group_name": None,
                "project_id": None,
                "project_member_role": None,
            },
        )
        for index in range(12)
    ]
    try:
        identity = await _start_container_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_invitations=True,
        )
        elapsed = []
        retry_count = 0
        slots = asyncio.Semaphore(4)

        async def create_with_retry(command):
            nonlocal retry_count
            async with slots:
                started = time.monotonic()
                for attempt in range(8):
                    result = await asyncio.to_thread(_node, identity, control, command)
                    if result["ok"]:
                        assert result["data"]["token"]
                        elapsed.append(time.monotonic() - started)
                        return result
                    assert result["error"]["code"] == "current_scope_busy"
                    assert result["error"]["status_code"] == 409
                    retry_count += 1
                    if attempt < 7:
                        await asyncio.sleep(min(0.2, 0.02 * (2**attempt)))
                pytest.fail("Invitation create contention exceeded bounded retries")

        duration_started = time.monotonic()
        results = await asyncio.gather(
            *(create_with_retry(command) for command in commands)
        )
        duration = time.monotonic() - duration_started
        assert len(results) == 12 and all(result["ok"] for result in results)
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_invitation_operation_receipts "
                        "WHERE actor_id=:actor AND operation='create_invitation'"
                    ),
                    {"actor": actor},
                )
                == 12
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.user_invitations "
                        "WHERE invited_by=:actor AND email LIKE :pattern"
                    ),
                    {"actor": actor, "pattern": f"capacity-%-{suffix}@example.test"},
                )
                == 12
            )
        ordered = sorted(elapsed)
        p50 = ordered[math.ceil(0.50 * len(ordered)) - 1]
        p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
        record_property(
            "invitation_local_capacity",
            f"12 creates, concurrency=4, contention retries={retry_count}, "
            f"duration={duration:.3f}s, observed p50={p50:.3f}s, p95={p95:.3f}s; "
            "bounded local container evidence, not a production capacity claim",
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup_invitations(maker, actor_ids=[actor])
