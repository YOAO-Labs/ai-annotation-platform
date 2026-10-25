"""Installed recovery owner, independent mail process and table-free real API."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
import uuid

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import text

from aap_identity.authenticator import Authenticator
from aap_identity.key_contract import decode_key_request
from aap_identity.key_manager import KeyManager
from aap_identity.key_postgres import KeyPostgresData
from aap_identity.postgres import PostgresData
from aap_identity.recovery_contract import decode_recovery_request
from aap_identity.recovery_manager import RecoveryManager
from aap_identity.recovery_postgres import RecoveryPostgresData
from aap_identity.security import verify_password
from tests.identity_recovery_live_helpers import (
    SMTPInbox,
    cleanup,
    command,
    node,
    outbox,
    start_mail,
    start_mail_container,
    wait_status,
)
from tests.test_identity_admission_live import (
    INSTALLATION_PROBE,
    ROOT,
    _dsn,
    _start_api,
    _start_container_identity,
    _start_identity,
    _token,
    _verdict,
    _verify_installation,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_RECOVERY_LIVE_ACCEPTANCE") != "1",
        reason="requires explicitly enabled owned recovery process acceptance",
    ),
]


def _identity(test_engine, directory, secret, control, key, *, port=None, verify=True):
    return _start_identity(
        test_engine,
        directory,
        secret,
        control,
        port=port,
        enable_keys=False,
        enable_sessions=False,
        enable_accounts=False,
        enable_presence=False,
        recovery_mail_key=key,
        session_settings={
            "IDENTITY_ALLOW_OPEN_REGISTRATION": "true",
            "IDENTITY_EMAIL_VERIFICATION_REQUIRED": str(verify).lower(),
            "IDENTITY_FRONTEND_BASE_URL": "https://app.example.test",
        },
    )


async def _call(client, identity, control, request):
    response = await client.post(
        identity.origin + "/v1/identity/recovery/execute",
        headers={"Authorization": f"Bearer {control}"},
        json=request,
    )
    assert response.status_code == 200, "Recovery owner returned a transport failure"
    return response.json()


async def test_installed_owner_node_all_operations_and_separate_mail_process(
    test_engine, tmp_path, record_property
):
    probe = subprocess.run(
        [os.environ["AAP_IDENTITY_RUNTIME_PYTHON"], "-I", "-c", INSTALLATION_PROBE],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert probe.returncode == 0, "Installed recovery package probe failed"
    _verify_installation(probe.stdout)
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    key = Fernet.generate_key().decode()
    register_id, resend_id, reset_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    operations = [register_id, resend_id, reset_id]
    actors = [actor, register_id]
    identity = mail = None
    inbox = SMTPInbox(pause_ack=True)
    register = command(
        "register_open",
        {
            "email": f"registered-{register_id}@example.test",
            "name": "Independent registration",
            "password": "OriginalStrong123",
            "captcha_verified": True,
        },
        operation_id=register_id,
    )
    try:
        identity = _identity(test_engine, tmp_path, secret, control, key)
        issued = await asyncio.to_thread(node, identity, control, register)
        assert issued["ok"] and issued["account"]["role"] == "viewer"
        assert issued["email_verification_required"] is True
        assert issued["access_token_sha256"] is None
        assert (
            await outbox(maker, register_id)
        ).status == "pending" and not inbox.messages
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM audit_logs WHERE actor_id=:id AND action='user.register'"
                    ),
                    {"id": register_id},
                )
                == 1
            )

        mail = start_mail(test_engine, tmp_path, key, inbox)
        await inbox.wait(1)
        # DATA is paused in a different process. User, outbox lease and Audit
        # are already committed and no User lock remains around SMTP.
        async with maker.begin() as db:
            await db.execute(text("SET LOCAL lock_timeout='200ms'"))
            assert (
                await db.scalar(
                    text("SELECT id FROM users WHERE id=:id FOR UPDATE"),
                    {"id": register_id},
                )
                == register_id
            )
            assert (await outbox(maker, register_id)).status == "leased"
        inbox.release.set()
        await wait_status(maker, register_id, "delivered")
        verify = command("consume_email_verification", {"token": inbox.tokens()[0]})
        operations.append(uuid.UUID(verify["operation_id"]))
        assert (await asyncio.to_thread(node, identity, control, verify))[
            "completed"
        ] is True

        resend = command(
            "resend_email_verification",
            {"email": f"identity-live-{actor}@example.test"},
            operation_id=resend_id,
        )
        assert (await asyncio.to_thread(node, identity, control, resend))["ok"]
        await inbox.wait(2)
        await wait_status(maker, resend_id, "delivered")
        reset = command(
            "request_password_reset",
            {"email": register["payload"]["email"], "captcha_verified": True},
            operation_id=reset_id,
        )
        assert (await asyncio.to_thread(node, identity, control, reset))["ok"]
        await inbox.wait(3)
        await wait_status(maker, reset_id, "delivered")
        old_jwt = _token(register_id, secret)
        key_operation = uuid.uuid4()
        retained_key = await KeyManager(
            Authenticator(PostgresData(_dsn("AUTH", test_engine)), secret),
            KeyPostgresData(_dsn("KEYS", test_engine)),
            secret,
        ).execute(
            decode_key_request(
                {
                    "schema": "aap-identity-keys/v1",
                    "operation": "create_api_key",
                    "operation_id": str(key_operation),
                    "token": old_jwt,
                    "payload": {
                        "name": "Retained through reset",
                        "scopes": ["datasets:read"],
                    },
                }
            )
        )
        plaintext = retained_key.data.plaintext
        consumers = [
            command(
                "consume_password_reset",
                {"token": inbox.tokens()[2], "new_password": "ReplacementStrong123"},
            )
            for _ in range(2)
        ]
        operations.extend(uuid.UUID(value["operation_id"]) for value in consumers)
        replies = await asyncio.gather(
            *(asyncio.to_thread(node, identity, control, value) for value in consumers)
        )
        assert sorted(value["ok"] for value in replies) == [False, True]
        winner = consumers[
            next(index for index, value in enumerate(replies) if value["ok"])
        ]
        async with maker() as db:
            user = (
                await db.execute(
                    text(
                        "SELECT password_hash,credential_generation FROM users WHERE id=:id"
                    ),
                    {"id": register_id},
                )
            ).one()
            assert (
                verify_password("ReplacementStrong123", user.password_hash)
                and user.credential_generation == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM audit_logs WHERE actor_id=:id AND action='user.password_change'"
                    ),
                    {"id": register_id},
                )
                == 1
            )
        origin = identity.origin
        identity.stop()
        identity = _identity(
            test_engine,
            tmp_path,
            secret,
            control,
            key,
            port=int(origin.rsplit(":", 1)[1]),
        )
        assert (await asyncio.to_thread(node, identity, control, winner))[
            "completed"
        ] is True
        assert (await asyncio.to_thread(node, identity, control, verify))[
            "completed"
        ] is True
        async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
            assert (await _verdict(client, identity, control, old_jwt))["error"][
                "code"
            ] == "generation_outdated"
            assert (await _verdict(client, identity, control, plaintext))["ok"]

        # Frozen registration replay stays intact after verification and reset.
        assert (await asyncio.to_thread(node, identity, control, register))[
            "account"
        ] == issued["account"]
        mail.stop()
        mail = None
        identity.stop()
        identity = _identity(
            test_engine,
            tmp_path,
            secret,
            control,
            key,
            port=int(origin.rsplit(":", 1)[1]),
            verify=False,
        )
        immediate_id = uuid.uuid4()
        actors.append(immediate_id)
        operations.append(immediate_id)
        immediate = command(
            "register_open",
            {
                "email": f"immediate-{immediate_id}@example.test",
                "name": "Immediate",
                "password": "ImmediateStrong123",
                "captcha_verified": True,
            },
            operation_id=immediate_id,
        )
        signed = await asyncio.to_thread(node, identity, control, immediate)
        assert signed["ok"] and signed["issued"]["role"] == "viewer"
        assert signed["issued"]["generation_decimal"] == "0"
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE users SET role='employee' WHERE id=:id"),
                {"id": immediate_id},
            )
        promoted = await asyncio.to_thread(node, identity, control, immediate)
        assert promoted["account"]["role"] == "viewer"
        assert promoted["issued"]["role"] == "employee"
        assert promoted["access_token_sha256"] == signed["access_token_sha256"]
        async with maker.begin() as db:
            await db.execute(
                text("SELECT aap_identity_v1.advance_credential_generation(:id)"),
                {"id": immediate_id},
            )
        historical = await asyncio.to_thread(node, identity, control, immediate)
        assert historical["access_token_sha256"] == signed["access_token_sha256"]
        assert historical["issued"] == {
            "ok": False,
            "error": {"code": "generation_outdated"},
        }
        for path in tmp_path.glob("*.log"):
            logs = path.read_text(errors="replace")
            assert all(
                value not in logs
                for value in [secret, control, key, plaintext, *inbox.tokens()]
            )
        record_property(
            "recovery_process_group",
            "five-operations,node,installed-artifact,SMTP-after-commit,one-time-race,restart-replay,current-role,frozen-claims,generation-fence,API-key-retained",
        )
    finally:
        inbox.release.set()
        if mail is not None:
            mail.stop()
        if identity is not None:
            identity.stop()
        inbox.close()
        async with maker.begin() as db:
            await db.execute(
                text(
                    "DELETE FROM identity_key_operation_receipts WHERE actor_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": actors},
            )
        await cleanup(maker, actors, operations)


async def test_real_api_recovers_without_identity_table_access_and_never_falls_back(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control, key = (
        secrets.token_urlsafe(32),
        secrets.token_urlsafe(32),
        Fernet.generate_key().decode(),
    )
    inbox = SMTPInbox()
    identity = mail = api = None
    actors, operations = [actor], []
    email = f"public-{uuid.uuid4()}@example.test"
    async with maker() as db:
        initial_receipts = set(
            (
                await db.execute(
                    text(
                        "SELECT operation_id FROM identity_recovery_operation_receipts"
                    )
                )
            ).scalars()
        )
    try:
        identity = _identity(test_engine, tmp_path, secret, control, key)
        mail = start_mail(test_engine, tmp_path, key, inbox)
        api = _start_api(
            tmp_path,
            secret,
            control,
            identity.origin,
            database_url=os.environ["AAP_MODULE_DATA_IDENTITY_AUTH_URL"],
            configuration={
                "TURNSTILE_ENABLED": "false",
                "IDENTITY_TIMEOUT_SECONDS": "20",
            },
        )
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            response = await client.post(
                api.origin + "/api/v1/auth/register-open",
                json={
                    "email": email,
                    "name": "Public recovery",
                    "password": "PublicStrong123",
                },
            )
            assert response.status_code == 201, (
                "Table-free API could not register through owner"
            )
            user_id = uuid.UUID(response.json()["user"]["id"])
            actors.append(user_id)
            await inbox.wait(1)
            verified = await client.post(
                api.origin + "/api/v1/auth/verify-email",
                json={"token": inbox.tokens()[0]},
            )
            assert verified.status_code == 200
            messages = []
            for address in (email, f"missing-{uuid.uuid4()}@example.test"):
                result = await client.post(
                    api.origin + "/api/v1/auth/forgot-password", json={"email": address}
                )
                assert result.status_code == 202
                messages.append(result.json())
            assert messages[0] == messages[1]
            await inbox.wait(2)
            reset = await client.post(
                api.origin + "/api/v1/auth/reset-password",
                json={
                    "token": inbox.tokens()[1],
                    "new_password": "PublicReplacement123",
                },
            )
            assert reset.status_code == 200
            reused = await client.post(
                api.origin + "/api/v1/auth/reset-password",
                json={
                    "token": inbox.tokens()[1],
                    "new_password": "OtherReplacement123",
                },
            )
            assert reused.status_code == 400
            resend = await client.post(
                api.origin + "/api/v1/auth/send-verification-email",
                json={"email": email},
            )
            assert resend.status_code == 202
            origin = identity.origin
            identity.stop()
            identity = _start_identity(
                test_engine,
                tmp_path,
                secret,
                control,
                port=int(origin.rsplit(":", 1)[1]),
                enable_keys=False,
                enable_sessions=False,
                enable_accounts=False,
                enable_presence=False,
            )
            missing = await client.post(
                api.origin + "/api/v1/auth/forgot-password", json={"email": email}
            )
            assert missing.status_code == 503, (
                "Selected owner's missing capability must not fall back"
            )
        async with maker() as db:
            assert not await db.scalar(
                text(
                    "SELECT has_table_privilege('identity_auth_client','users','SELECT')"
                )
            )
        record_property(
            "recovery_real_api",
            "register,verify,uniform-forgot,reset,reuse,resend,table-free,missing-capability503",
        )
    finally:
        for process in (api, mail, identity):
            if process is not None:
                process.stop()
        inbox.close()
        async with maker() as db:
            operations = (
                set(
                    (
                        await db.execute(
                            text(
                                "SELECT operation_id FROM identity_recovery_operation_receipts"
                            )
                        )
                    ).scalars()
                )
                - initial_receipts
            )
        await cleanup(maker, actors, operations)


async def test_recovery_container_bounded_concurrent_registration(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control, key = (
        secrets.token_urlsafe(32),
        secrets.token_urlsafe(32),
        Fernet.generate_key().decode(),
    )
    identity = None
    operations = [uuid.uuid4() for _ in range(8)]
    try:
        identity = await _start_container_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_accounts=False,
            enable_presence=False,
            recovery_mail_key=key,
        )
        semaphore = asyncio.Semaphore(4)
        timings = []
        async with httpx.AsyncClient(trust_env=False, timeout=45) as client:

            async def register(operation_id):
                async with semaphore:
                    started = time.monotonic()
                    result = await _call(
                        client,
                        identity,
                        control,
                        command(
                            "register_open",
                            {
                                "email": f"capacity-{operation_id}@example.test",
                                "name": "Bounded registration",
                                "password": "CapacityStrong123",
                                "captcha_verified": True,
                            },
                            operation_id=operation_id,
                        ),
                    )
                    timings.append(time.monotonic() - started)
                    assert (
                        result["ok"] and result["data"]["account"]["role"] == "viewer"
                    )

            started = time.monotonic()
            await asyncio.gather(*(register(value) for value in operations))
            elapsed = time.monotonic() - started
        from tests.test_asset_storage_cleanup_runtime import _inspect_docker

        details = _inspect_docker(identity.container_id)
        assert not details["State"]["OOMKilled"]
        record_property(
            "recovery_local_capacity",
            json.dumps(
                {
                    "registrations": 8,
                    "concurrency": 4,
                    "elapsed_seconds": round(elapsed, 3),
                    "p95_seconds": round(max(timings), 3),
                    "memory_limit_bytes": details["HostConfig"]["Memory"],
                }
            ),
        )
    finally:
        if identity is not None:
            identity.stop()
        await cleanup(maker, [actor, *operations], operations)


async def test_readonly_mail_container_takes_over_after_sender_process_dies(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    operation_id, key = uuid.uuid4(), Fernet.generate_key().decode()
    inbox = SMTPInbox(pause_ack=True)
    first = second = None
    try:
        await RecoveryManager(
            RecoveryPostgresData(_dsn("RECOVERY", test_engine)),
            secrets.token_urlsafe(32),
            key,
        ).execute(
            decode_recovery_request(
                command(
                    "request_password_reset",
                    {
                        "email": f"identity-live-{actor}@example.test",
                        "captcha_verified": True,
                    },
                    operation_id=operation_id,
                )
            )
        )
        first = start_mail(test_engine, tmp_path, key, inbox)
        await inbox.wait(1)
        before = await outbox(maker, operation_id)
        assert before.status == "leased" and before.attempt == 1
        # Kill only this test's sender while DATA acknowledgement is withheld.
        first.process.kill()
        first.process.wait(timeout=5)
        first.log.close()
        first = None
        inbox.release.set()
        second = await start_mail_container(test_engine, tmp_path, key, inbox)
        await inbox.wait(2)
        after = await wait_status(maker, operation_id, "delivered")
        assert after.attempt == 2
        assert (
            after.token_hash == before.token_hash
            and after.token_ciphertext == before.token_ciphertext
        )
        assert len(inbox.tokens()) == 2 and secrets.compare_digest(*inbox.tokens())
        assert inbox.messages[0]["Message-ID"] == inbox.messages[1]["Message-ID"]
        from tests.test_asset_storage_cleanup_runtime import _inspect_docker

        details = _inspect_docker(second.container_id)
        assert not details["State"]["OOMKilled"]
        record_property(
            "mail_process_handoff",
            "killed-during-DATA,expired-lease-takeover,same-link,readonly-nonroot256MiB,MAIL-and-settings-only",
        )
    finally:
        inbox.release.set()
        if first is not None:
            first.stop()
        if second is not None:
            second.stop()
        inbox.close()
        await cleanup(maker, [actor], [operation_id])


async def test_existing_celery_scheduler_delivers_across_separate_task_loops(
    test_engine, tmp_path, record_property
):
    from celery import Celery
    from tests.test_identity_sessions_live import _verify_redis

    _verify_redis()
    maker, actor = await _world(test_engine)
    key = Fernet.generate_key().decode()
    operations = [uuid.uuid4(), uuid.uuid4()]
    inbox = SMTPInbox()
    queue = f"identity_mail_{uuid.uuid4().hex}"
    hostname = queue + "@owned"
    broker = os.environ["REDIS_URL"]
    sender = Celery("identity-mail-acceptance", broker=broker, backend=broker)
    worker = None
    log = (tmp_path / "celery-mail.log").open("wb")
    deliveries = []
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "ENVIRONMENT": "development",
        "AAP_WORKTREE_MODE": "test",
        "DATABASE_URL": os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"],
        "MIGRATION_DATABASE_URL": "",
        "TEST_DATABASE_URL": "",
        "PLAYWRIGHT_E2E_DATABASE_URL": "",
        "REDIS_URL": broker,
        "CELERY_BROKER_URL": broker,
        "CELERY_RESULT_BACKEND": broker,
        "CELERY_BROKER_READ_URL": broker,
        "CELERY_BROKER_WRITE_URL": broker,
        "IDENTITY_URL": "",
        "IDENTITY_RECOVERY_DATABASE_URL": "",
        "IDENTITY_RECOVERY_DATABASE_URL_FILE": "",
        "IDENTITY_MAIL_DATABASE_URL": "",
        "IDENTITY_MAIL_DATABASE_URL_FILE": "",
        "IDENTITY_MAIL_SETTINGS_DATABASE_URL": "",
        "IDENTITY_MAIL_SETTINGS_DATABASE_URL_FILE": "",
        "IDENTITY_RECOVERY_MAIL_KEY": key,
        "IDENTITY_RECOVERY_MAIL_KEY_FILE": "",
        "SECRET_KEY": secrets.token_urlsafe(32),
        "SENTRY_DSN": "",
        "SMTP_HOST": "127.0.0.1",
        "SMTP_PORT": str(inbox.port),
        "SMTP_FROM": "noreply@example.test",
        "SMTP_USER": "",
        "SMTP_PASSWORD": "",
    }
    try:
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.workers.celery_app:celery_app",
            "worker",
            "--pool=solo",
            "--concurrency=1",
            f"--queues={queue}",
            f"--hostname={hostname}",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel=WARNING",
            cwd=ROOT / "apps/api",
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        inspector = sender.control.inspect(destination=[hostname], timeout=1)
        task_name = "app.workers.identity_mail_delivery.relay_credential_mail"
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            assert worker.returncode is None, "Owned mail scheduler worker exited"
            registered = await asyncio.to_thread(inspector.registered)
            if registered:
                assert task_name in registered[hostname]
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Owned mail scheduler worker did not register")
        for index, operation_id in enumerate(operations):
            await RecoveryManager(
                RecoveryPostgresData(_dsn("RECOVERY", test_engine)),
                "celery-recovery-fixture",
                key,
            ).execute(
                decode_recovery_request(
                    command(
                        "request_password_reset",
                        {
                            "email": f"identity-live-{actor}@example.test",
                            "captcha_verified": True,
                        },
                        operation_id=operation_id,
                    )
                )
            )
            delivery = await asyncio.to_thread(sender.send_task, task_name, queue=queue)
            deliveries.append(delivery)
            result = await asyncio.to_thread(delivery.get, timeout=30)
            assert result == {"processed": 1, "state": "idle"}
            await inbox.wait(index + 1)
            assert (await outbox(maker, operation_id)).status == "delivered"
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE usename='legacy_runtime' AND state='idle in transaction'"
                    )
                )
                == 0
            )
        record_property(
            "credential_mail_scheduler",
            "registered-task,owned-broker,two-real-dispatches,separate-event-loops,no-idle-transactions",
        )
    finally:
        if worker is not None and worker.returncode is None:
            worker.terminate()
            try:
                await asyncio.wait_for(worker.wait(), timeout=15)
            except TimeoutError:
                worker.kill()
                await worker.wait()
        log.close()
        _verify_redis()
        for delivery in deliveries:
            await asyncio.to_thread(delivery.forget)
        with sender.connection_for_write() as connection:
            connection.default_channel.queue_delete(queue)
        sender.close()
        inbox.close()
        await cleanup(maker, [actor], operations)
