"""Invitation receipts, signed registration and mail leases survive an owned restore."""

from __future__ import annotations

import json
import os
import secrets
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_identity.authenticator import Authenticator
from aap_identity.invitation_contract import (
    InvitationFailure,
    decode_invitation_request,
)
from aap_identity.invitation_manager import InvitationManager
from aap_identity.invitation_postgres import InvitationPostgresData
from aap_identity.mail_v2_postgres import MailV2PostgresData
from aap_identity.postgres import PostgresData
from aap_identity.recovery_crypto import decrypt_mail_token, mail_key_from_secret
from tests.test_identity_admission_live import _cleanup, _dsn, _token, _world
from tests.test_identity_recovery import (
    _alembic,
    _assert_exact_identity_functions,
    _postgres_tool,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_LIVE_ACCEPTANCE") != "1",
        reason="requires explicitly enabled owned PostgreSQL acceptance",
    ),
]


async def test_restored_invitation_receipts_jwt_and_durable_mail_lease(
    test_engine, tmp_path, record_property
):
    maker, issuer = await _world(test_engine)
    secret = secrets.token_urlsafe(32)
    key = mail_key_from_secret(secret)
    credential = _token(issuer, secret)
    clone = f"aap_identity_invitation_restore_{uuid.uuid4().hex[:12]}_test"
    ownership = f"identity-invitation-restore:{uuid.uuid4()}"
    owner_url = test_engine.url.set(database=clone)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    restored = create_async_engine(owner_url, hide_parameters=True)
    created = False
    snapshot = tmp_path / "invitation-authority.dump"
    operations = []
    registered_id = None

    def request(operation, payload, *, public=False):
        operation_id = uuid.uuid4()
        operations.append(operation_id)
        return decode_invitation_request(
            {
                "schema": "aap-identity-invitations/v1",
                "operation": operation,
                "operation_id": str(operation_id),
                "token": None if public else credential,
                "payload": payload,
                "audit_context": {
                    "method": "POST",
                    "path": "/restore/invitations",
                    "ip": "127.0.0.1",
                    "request_id": "restore-invitation",
                },
            }
        )

    def manager(
        dsn, auth_dsn, *, signing_secret=secret, frontend="https://restore.invite.test"
    ):
        return InvitationManager(
            Authenticator(PostgresData(auth_dsn), signing_secret),
            InvitationPostgresData(dsn),
            signing_secret,
            key,
            frontend_base_url=frontend,
        )

    source_manager = manager(_dsn("INVITATION", test_engine), _dsn("AUTH", test_engine))
    try:
        email = f"restore-invitation-{uuid.uuid4().hex}@example.test"
        create_request = request(
            "create_invitation", {"email": email, "role": "employee"}
        )
        first = await source_manager.execute(create_request)
        async with maker() as db:
            invitation_id = await db.scalar(
                text("SELECT id FROM public.user_invitations WHERE token=:token"),
                {"token": first.data.token},
            )
        resend_request = request(
            "resend_invitation", {"invitation_id": str(invitation_id)}
        )
        resent = await source_manager.execute(resend_request)
        assert resent.data.token != first.data.token
        send_request = request(
            "send_invitation_email", {"invitation_id": str(invitation_id)}
        )
        submitted = await source_manager.execute(send_request)
        assert submitted.data.mail_status == "pending"
        mail_data = MailV2PostgresData(_dsn("MAIL_V2", test_engine))
        lease = await mail_data.claim_mail(
            uuid.uuid4(), lease_seconds=300, delivery_id=send_request.operation_id
        )
        assert lease is not None and lease.invitation_id == invitation_id
        assert (
            decrypt_mail_token(lease.token_ciphertext, key, lease.encryption_id)
            == resent.data.token
        )

        registration_email = f"restore-registration-{uuid.uuid4().hex}@example.test"
        second_request = request(
            "create_invitation", {"email": registration_email, "role": "employee"}
        )
        second = await source_manager.execute(second_request)
        register_request = request(
            "register_invitation",
            {
                "invitation_token": second.data.token,
                "name": "Restored invitee",
                "password": "RestoreInvitation123",
            },
            public=True,
        )
        registered = await source_manager.execute(register_request)
        registered_id = registered.data.user.id
        async with maker() as db:
            audit_count = await db.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs WHERE actor_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [issuer, registered_id]},
            )
            receipts = await db.scalar(
                text(
                    "SELECT jsonb_agg(to_jsonb(receipt)) FROM public.identity_invitation_operation_receipts AS receipt WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": operations},
            )
        stored = json.dumps(receipts, ensure_ascii=False)
        for material in (
            first.data.token,
            resent.data.token,
            second.data.token,
            registered.data.access_token,
            register_request.payload.password,
        ):
            assert material not in stored

        snapshot.touch(mode=0o600)
        snapshot.write_bytes(
            _postgres_tool(
                test_engine, ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database]
            )
        )
        assert snapshot.read_bytes().startswith(b"PGDMP")
        async with admin.connect() as db:
            await db.execute(text(f'CREATE DATABASE "{clone}" TEMPLATE template0'))
            created = True
            await db.execute(text(f"COMMENT ON DATABASE \"{clone}\" IS '{ownership}'"))
            async with test_engine.connect() as source:
                acl = (
                    await source.execute(
                        text(
                            "SELECT COALESCE(role.rolname,'PUBLIC'), entry.privilege_type, entry.is_grantable "
                            "FROM pg_database AS database CROSS JOIN LATERAL aclexplode(COALESCE(database.datacl,acldefault('d',database.datdba))) AS entry "
                            "LEFT JOIN pg_roles AS role ON role.oid=entry.grantee WHERE database.datname=current_database()"
                        )
                    )
                ).all()
            quote = db.dialect.identifier_preparer.quote_identifier
            await db.execute(text(f'REVOKE ALL ON DATABASE "{clone}" FROM PUBLIC'))
            for grantee, privilege, grantable in acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote(grantee)
                suffix = " WITH GRANT OPTION" if grantable else ""
                await db.execute(
                    text(f'GRANT {privilege} ON DATABASE "{clone}" TO {role}{suffix}')
                )
        _postgres_tool(
            test_engine,
            ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", clone],
            body=snapshot.read_bytes(),
        )

        def dsn(kind):
            return (
                make_url(_dsn(kind, test_engine))
                .set(database=clone)
                .render_as_string(hide_password=False)
            )

        await _assert_exact_identity_functions(
            dsn("INVITATION"),
            {
                "aap_identity_invitations_v1.invitation_policy()",
                "aap_identity_invitations_v1.prepare_invitation_command(jsonb)",
                "aap_identity_invitations_v1.apply_invitation_command(jsonb)",
            },
        )
        await _assert_exact_identity_functions(
            dsn("MAIL_V2"),
            {
                "aap_identity_mail_v2.mail_policy()",
                "aap_identity_mail_v2.claim_mail(jsonb)",
                "aap_identity_mail_v2.renew_mail(jsonb)",
                "aap_identity_mail_v2.finish_mail(jsonb)",
            },
        )
        clone_manager = manager(
            dsn("INVITATION"),
            dsn("AUTH"),
            frontend="https://changed.after-restore.test",
        )
        assert (
            await InvitationPostgresData(dsn("INVITATION")).invitation_policy()
        ).policy == "aap-identity-invitations/v1"
        assert (
            await MailV2PostgresData(dsn("MAIL_V2")).mail_policy()
        ).policy == "aap-identity-mail/v2"
        for original_request, original_reply in (
            (create_request, first),
            (resend_request, resent),
            (second_request, second),
            (register_request, registered),
        ):
            replay = await clone_manager.execute(original_request)
            assert replay.data == original_reply.data
        async with restored.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE actor_id=ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [issuer, registered_id]},
                )
                == audit_count
            )
            assert (
                await db.scalar(
                    text("SELECT token FROM public.user_invitations WHERE id=:id"),
                    {"id": invitation_id},
                )
                == resent.data.token
            )
        clone_mail = MailV2PostgresData(dsn("MAIL_V2"))
        assert await clone_mail.renew_mail(lease)
        assert await clone_mail.finish_mail(
            lease, outcome="delivered", reason="sent", retry_delay_seconds=None
        )
        sent = await clone_manager.execute(send_request)
        assert (
            sent.data.mail_status == "sent"
            and sent.data.invite_url == submitted.data.invite_url
        )
        async with restored.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE action='invitation.send_email' AND target_id=:id"
                    ),
                    {"id": str(invitation_id)},
                )
                == 1
            )
        changed_secret = secrets.token_urlsafe(32)
        changed_request = create_request.model_copy(
            update={"token": _token(issuer, changed_secret)}
        )
        with pytest.raises(InvitationFailure, match="operation_conflict"):
            await manager(
                dsn("INVITATION"), dsn("AUTH"), signing_secret=changed_secret
            ).execute(changed_request)

        async with restored.connect() as db:
            before_downgrade = await db.scalar(
                text("SELECT version_num FROM public.alembic_version")
            )
        refused = _alembic(owner_url, "downgrade", "bo0241")
        assert refused.returncode != 0
        assert (
            b"Cannot remove Identity invitation ownership while receipts or durable mail state exists"
            in refused.stdout + refused.stderr
        )
        async with restored.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM public.alembic_version"))
                == before_downgrade
            )
            await db.execute(
                text("DELETE FROM public.identity_invitation_operation_receipts")
            )
            await db.execute(
                text(
                    "DELETE FROM public.identity_credential_mail_outbox WHERE purpose='invitation'"
                )
            )
        empty = _alembic(owner_url, "downgrade", "bo0241")
        assert empty.returncode == 0, "Owned empty invitation downgrade failed"
        async with restored.connect() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM public.alembic_version"))
                == "bo0241"
            )
        record_property(
            "invitation_restore",
            "same-token-and-JWT-replay,no-repeat-Audit,mail-lease-renew-and-finish,exact-3-and-4-functions,epoch-refusal,populated-downgrade-refusal",
        )
    finally:
        await restored.dispose()
        if created:
            async with admin.connect() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=:name"
                        ),
                        {"name": clone},
                    )
                    == ownership
                )
                assert not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=:name AND backend_type IS DISTINCT FROM 'autovacuum worker')"
                    ),
                    {"name": clone},
                )
                await db.execute(text(f'DROP DATABASE "{clone}"'))
        await admin.dispose()
        async with maker.begin() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(
                text(
                    "DELETE FROM public.audit_logs WHERE actor_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [issuer, registered_id] if registered_id else [issuer]},
            )
            await db.execute(
                text(
                    "DELETE FROM public.identity_credential_mail_outbox WHERE id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": operations},
            )
            await db.execute(
                text(
                    "DELETE FROM public.identity_invitation_operation_receipts WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": operations},
            )
            await db.execute(
                text("DELETE FROM public.user_invitations WHERE invited_by=:id"),
                {"id": issuer},
            )
            if registered_id:
                await db.execute(
                    text("DELETE FROM public.users WHERE id=:id"), {"id": registered_id}
                )
        await _cleanup(maker, issuer, [])
        snapshot.unlink(missing_ok=True)
