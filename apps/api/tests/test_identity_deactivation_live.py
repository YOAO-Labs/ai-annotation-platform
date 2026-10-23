"""Independent Node consumer and live-owner checks for Identity deactivation."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
import pytest
import redis.asyncio as redis
from sqlalchemy import delete, select, text, update

from aap_identity.security import decode_access_token
from app.db.models.notification import Notification
from app.db.models.user import User
from tests.factory import create_user
from tests.test_identity_admission_live import (
    ROOT,
    _dsn,
    _start_container_identity,
    _start_identity,
    _token,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_DEACTIVATION_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity deactivation verifier",
    ),
]


def _command(operation, token, payload=None, *, operation_id=None):
    return {
        "schema": "aap-identity-deactivation/v1",
        "operation": operation,
        "operation_id": str(operation_id or uuid.uuid4()),
        "token": token,
        "audit_context": {
            "method": "POST",
            "path": "/acceptance/identity-deactivation",
            "ip": "127.0.0.1",
            "request_id": str(uuid.uuid4()),
        },
        "payload": payload or {},
    }


def _due_command(*, operation_id=None, limit=256):
    return {
        "schema": "aap-identity-deactivation-due/v1",
        "operation_id": str(operation_id or uuid.uuid4()),
        "limit": limit,
    }


def _node(identity, control, request):
    result = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/identity_deactivation_peer.mjs")],
        input=json.dumps(
            {"url": identity.origin, "control_token": control, "request": request}
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 0, "Independent Node deactivation peer failed"
    secrets_to_hide = [control]
    if isinstance(request.get("token"), str):
        secrets_to_hide.append(request["token"])
    reason = request.get("payload", {}).get("reason")
    if isinstance(reason, str) and len(reason) >= 8:
        secrets_to_hide.append(reason)
    for secret in secrets_to_hide:
        assert secret not in result.stdout
        assert secret not in result.stderr
    return json.loads(result.stdout)


def _publisher_env(test_engine, directory, redis_url, label):
    database_path = directory / f"publisher-{label}-database-url"
    redis_path = directory / f"publisher-{label}-redis-url"
    database_path.write_text(_dsn("NOTIFICATION_PUBLISH", test_engine))
    redis_path.write_text(redis_url)
    database_path.chmod(0o600)
    redis_path.chmod(0o600)
    return {
        "PATH": os.environ.get("PATH", ""),
        "NOTIFICATION_PUBLISH_DATABASE_URL_FILE": str(database_path),
        "NOTIFICATION_PUBLISH_REDIS_URL_FILE": str(redis_path),
    }


def _run_publisher(test_engine, directory, redis_url, label):
    result = subprocess.run(
        [
            os.environ["AAP_IDENTITY_RUNTIME_PYTHON"],
            "-I",
            "-m",
            "aap_identity_runtime.notification_worker",
            "--once",
        ],
        cwd=directory,
        env=_publisher_env(test_engine, directory, redis_url, label),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, "Owned notification publisher batch failed"
    return result


async def _cleanup_deactivation(
    maker,
    *,
    actor_ids,
    user_operation_ids=(),
    due_operation_ids=(),
    jtis=(),
):
    actor_ids = [uuid.UUID(str(value)) for value in actor_ids]
    user_operation_ids = [uuid.UUID(str(value)) for value in user_operation_ids]
    due_operation_ids = [uuid.UUID(str(value)) for value in due_operation_ids]
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
        await db.execute(
            text(
                "DELETE FROM public.identity_deactivation_operation_receipts "
                "WHERE actor_id=ANY(CAST(:actors AS uuid[])) "
                "OR operation_id=ANY(CAST(:operations AS uuid[]))"
            ),
            {"actors": actor_ids, "operations": user_operation_ids},
        )
        if due_operation_ids:
            await db.execute(
                text(
                    "DELETE FROM public.identity_deactivation_due_receipts "
                    "WHERE operation_id=ANY(CAST(:operations AS uuid[]))"
                ),
                {"operations": due_operation_ids},
            )
        await db.execute(
            text(
                "DELETE FROM public.identity_deactivation_requests "
                "WHERE user_id=ANY(CAST(:actors AS uuid[])) "
                "OR request_id=ANY(CAST(:operations AS uuid[]))"
            ),
            {"actors": actor_ids, "operations": user_operation_ids},
        )
        await db.execute(
            delete(Notification).where(
                (Notification.user_id.in_(actor_ids))
                | (Notification.target_id.in_(actor_ids))
            )
        )
        await db.execute(
            text(
                "DELETE FROM public.audit_logs WHERE actor_id=ANY(CAST(:actors AS uuid[])) "
                "OR target_id=ANY(CAST(:targets AS text[]))"
            ),
            {
                "actors": actor_ids,
                "targets": [str(value) for value in actor_ids],
            },
        )
        if jtis:
            await db.execute(
                text(
                    "DELETE FROM public.identity_revoked_jtis "
                    "WHERE jti=ANY(CAST(:jtis AS uuid[]))"
                ),
                {"jtis": [uuid.UUID(str(value)) for value in jtis]},
            )
        await db.execute(delete(User).where(User.id.in_(actor_ids)))


async def _state(maker, user_id):
    async with maker() as db:
        return await db.get(User, user_id)


async def _operation_count(maker, operation_id):
    async with maker() as db:
        return await db.scalar(
            text(
                "SELECT count(*) FROM public.identity_deactivation_operation_receipts "
                "WHERE operation_id=:operation_id"
            ),
            {"operation_id": operation_id},
        )


async def _event_count(maker, actor_id, action):
    async with maker() as db:
        return await db.scalar(
            text(
                "SELECT count(*) FROM public.audit_logs "
                "WHERE actor_id=:actor AND action=:action"
            ),
            {"actor": actor_id, "action": action},
        )


async def test_installed_owner_node_replay_current_revocation_and_no_reason_echo(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    actor_ids = [actor]
    operation_ids = []
    jtis = []
    identity = None
    try:
        async with maker.begin() as db:
            other_admin = await create_user(
                db,
                "super_admin",
                f"deactivation-peer-{suffix}@example.test",
                "Second active administrator",
            )
        actor_ids.append(other_admin.id)
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_deactivation=True,
        )
        token = _token(actor, secret)
        jtis.append(uuid.UUID(decode_access_token(token, secret=secret)["jti"]))

        first_reason = f"private-reason-first-{suffix}"
        first_request = _command(
            "request_self_deactivation", token, {"reason": f"  {first_reason}  "}
        )
        first_id = uuid.UUID(first_request["operation_id"])
        operation_ids.append(first_id)
        accepted = await asyncio.to_thread(_node, identity, control, first_request)
        assert accepted["ok"] and accepted["operation_id"] == str(first_id)
        profile = accepted["data"]["account"]
        assert profile["id"] == str(actor)
        assert profile["deactivation_requested_at"] is not None
        assert profile["deactivation_scheduled_at"] is not None
        assert profile["disabled_reason"] is None
        assert "deactivation_reason" not in profile
        assert first_reason not in json.dumps(accepted, ensure_ascii=False)
        state = await _state(maker, actor)
        assert state.deactivation_reason == first_reason
        assert state.deactivation_requested_at is not None
        assert (
            state.deactivation_scheduled_at - state.deactivation_requested_at
            == timedelta(days=7)
        )
        assert await _operation_count(maker, first_id) == 1
        async with maker() as db:
            audit_details = list(
                (
                    await db.execute(
                        text(
                            "SELECT detail_json FROM public.audit_logs "
                            "WHERE actor_id=:actor AND action='user.deactivation_request'"
                        ),
                        {"actor": actor},
                    )
                ).scalars()
            )
        assert len(audit_details) == 1
        assert set(audit_details[0]) == {"scheduled_at", "reason_present"}
        assert audit_details[0]["reason_present"] is True
        assert first_reason not in json.dumps(audit_details[0], ensure_ascii=False)

        replay_first = await asyncio.to_thread(_node, identity, control, first_request)
        assert replay_first == accepted
        assert await _operation_count(maker, first_id) == 1
        assert await _event_count(maker, actor, "user.deactivation_request") == 1

        cancel = _command("cancel_self_deactivation", token, {})
        cancel_id = uuid.UUID(cancel["operation_id"])
        operation_ids.append(cancel_id)
        cancelled = await asyncio.to_thread(_node, identity, control, cancel)
        assert cancelled["ok"]
        assert cancelled["data"]["account"]["deactivation_requested_at"] is None
        assert cancelled["data"]["account"]["deactivation_scheduled_at"] is None
        replay_cancel = await asyncio.to_thread(_node, identity, control, cancel)
        assert replay_cancel == cancelled
        assert await _operation_count(maker, cancel_id) == 1
        assert await _event_count(maker, actor, "user.deactivation_cancel") == 1
        cancelled_state = await _state(maker, actor)
        assert cancelled_state.deactivation_requested_at is None
        assert cancelled_state.deactivation_reason is None
        assert cancelled_state.deactivation_scheduled_at is None

        second_reason = f"private-reason-second-{suffix}"
        second_request = _command(
            "request_self_deactivation", token, {"reason": second_reason}
        )
        second_id = uuid.UUID(second_request["operation_id"])
        operation_ids.append(second_id)
        second_accepted = await asyncio.to_thread(
            _node, identity, control, second_request
        )
        assert second_accepted["ok"]
        second_scheduled_at = second_accepted["data"]["account"][
            "deactivation_scheduled_at"
        ]

        # A frozen earlier success must never rewind a later accepted request.
        frozen_first = await asyncio.to_thread(_node, identity, control, first_request)
        frozen_cancel = await asyncio.to_thread(_node, identity, control, cancel)
        assert frozen_first == accepted and frozen_cancel == cancelled
        current = await _state(maker, actor)
        assert current.deactivation_reason == second_reason
        assert current.deactivation_scheduled_at.isoformat().replace("+00:00", "Z") == (
            second_scheduled_at.replace("+00:00", "Z")
        )
        assert await _operation_count(maker, first_id) == 1
        assert await _operation_count(maker, cancel_id) == 1

        # Current credential revocation is checked before receipt replay.
        async with maker.begin() as db:
            await db.execute(
                text(
                    "SELECT aap_identity_v1.revoke_jti(CAST(:jti AS uuid), :retain_until)"
                ),
                {"jti": jtis[0], "retain_until": datetime.now(UTC) + timedelta(days=8)},
            )
        revoked_replay = await asyncio.to_thread(
            _node, identity, control, second_request
        )
        assert revoked_replay["ok"] is False
        assert revoked_replay["error"]["code"] == "credential_revoked"
        current = await _state(maker, actor)
        assert current.deactivation_reason == second_reason
        record_property(
            "deactivation_node_user_owner",
            "request-cancel,operation-replay,no-state-rewind,current-jti-revocation,reason-not-returned",
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup_deactivation(
            maker,
            actor_ids=actor_ids,
            user_operation_ids=operation_ids,
            jtis=jtis,
        )


async def test_super_admin_requests_race_and_due_never_removes_final_admin(
    test_engine, tmp_path, record_property
):
    maker, first_admin = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    actor_ids = [first_admin]
    operation_ids = []
    due_operation_ids = []
    identity = None
    try:
        async with maker.begin() as db:
            second_admin = await create_user(
                db,
                "super_admin",
                f"deactivation-race-{suffix}@example.test",
                "Concurrent administrator",
            )
        actor_ids.append(second_admin.id)
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_deactivation=True,
            enable_deactivation_operator=True,
        )
        first_token = _token(first_admin, secret)
        second_token = _token(second_admin.id, secret)
        first_request = _command(
            "request_self_deactivation", first_token, {"reason": None}
        )
        second_request = _command(
            "request_self_deactivation", second_token, {"reason": None}
        )
        operation_ids.extend(
            [
                uuid.UUID(first_request["operation_id"]),
                uuid.UUID(second_request["operation_id"]),
            ]
        )
        first_result, second_result = await asyncio.gather(
            asyncio.to_thread(_node, identity, control, first_request),
            asyncio.to_thread(_node, identity, control, second_request),
        )
        results = [first_result, second_result]
        accepted = [result for result in results if result["ok"]]
        assert len(accepted) == 1
        rejected = results[0] if not results[0]["ok"] else results[1]
        if rejected["error"]["code"] == "invariant_busy":
            # Lock contention has no receipt; retry after the successful owner commits.
            request = first_request if not first_result["ok"] else second_request
            retried = await asyncio.to_thread(_node, identity, control, request)
            assert retried["ok"] is False
            assert retried["error"] == {
                "code": "policy_rejected",
                "status_code": 400,
                "detail": "其余超级管理员均已申请注销，请先让其他管理员撤销申请或指定继任者",
            }
        else:
            assert rejected["error"]["code"] == "policy_rejected"
            assert rejected["error"]["detail"] == (
                "其余超级管理员均已申请注销，请先让其他管理员撤销申请或指定继任者"
            )

        # Also seed a legacy pending row with only the historical three fields.
        # Due processing must reconcile it and process at most one final SA.
        past = datetime.now(UTC) - timedelta(days=8)
        due_at = datetime.now(UTC) - timedelta(seconds=1)
        async with maker.begin() as db:
            await db.execute(
                update(User)
                .where(User.id.in_(actor_ids))
                .values(
                    deactivation_requested_at=past,
                    deactivation_reason="legacy seeded pending request",
                    deactivation_scheduled_at=due_at,
                )
            )
        due_request = _due_command(limit=16)
        due_id = uuid.UUID(due_request["operation_id"])
        due_operation_ids.append(due_id)
        due_result = await asyncio.to_thread(_node, identity, control, due_request)
        assert due_result == {
            "schema": "aap-identity-deactivation-due/v1",
            "ok": True,
            "operation_id": str(due_id),
            "processed": 1,
            "examined": 2,
            "blocked_last_super_admin": 1,
            "has_more": False,
        }
        assert set(due_result) == {
            "schema",
            "ok",
            "operation_id",
            "processed",
            "examined",
            "blocked_last_super_admin",
            "has_more",
        }
        replay_due = await asyncio.to_thread(_node, identity, control, due_request)
        assert replay_due == due_result

        async with maker() as db:
            users = list(
                (await db.scalars(select(User).where(User.id.in_(actor_ids)))).all()
            )
            assert (
                sum(user.role == "super_admin" and user.is_active for user in users)
                == 1
            )
            completed = [user for user in users if not user.is_active]
            pending = [user for user in users if user.is_active]
            assert len(completed) == len(pending) == 1
            assert completed[0].disabled_kind == "deleted"
            assert completed[0].disabled_by == completed[0].id
            assert completed[0].status == "offline"
            assert completed[0].disabled_reason == "legacy seeded pending request"
            assert pending[0].deactivation_requested_at is not None
            assert pending[0].deactivation_scheduled_at is not None
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND action='user.deactivation_approve'"
                    ),
                    {"actor": completed[0].id},
                )
                == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND action='user.deactivation_approve'"
                    ),
                    {"actor": pending[0].id},
                )
                == 0
            )
            assert (
                await db.scalar(
                    select(Notification.id).where(
                        Notification.target_id == pending[0].id,
                        Notification.type == "user.deactivation_completed",
                    )
                )
                is None
            )
            history = list(
                (
                    await db.execute(
                        text(
                            "SELECT actor_email,actor_role,detail_json FROM public.audit_logs "
                            "WHERE actor_id=:actor"
                        ),
                        {"actor": completed[0].id},
                    )
                ).mappings()
            )
            assert history and all(
                row["actor_email"] is None and row["actor_role"] is None
                for row in history
            )
            assert any(row["detail_json"] is not None for row in history)
        record_property(
            "deactivation_super_admin_fence",
            "concurrent-request-fence,legacy-pending-reconcile,due-last-admin-block,replayed-page",
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup_deactivation(
            maker,
            actor_ids=actor_ids,
            user_operation_ids=operation_ids,
            due_operation_ids=due_operation_ids,
        )


async def _collect_notifications(pubsub, expected_ids, timeout=15):
    expected_ids = {str(value) for value in expected_ids}
    received = {}
    deadline = time.monotonic() + timeout
    while expected_ids - received.keys():
        if time.monotonic() >= deadline:
            pytest.fail(
                "Redis did not receive all committed deactivation notifications"
            )
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.25)
        if message and message.get("type") == "message":
            body = json.loads(message["data"])
            if body.get("id") in expected_ids:
                received[body["id"]] = (message["channel"], body)
        await asyncio.sleep(0.02)
    return received


async def _notification_rows(maker, operation_id):
    async with maker() as db:
        return list(
            (
                await db.execute(
                    text(
                        "SELECT n.id,n.user_id,n.type,n.target_type,n.target_id,n.payload,n.created_at,"
                        "o.state,o.worker_id,o.lease_id,o.lease_expires_at,o.attempt "
                        "FROM public.notifications AS n "
                        "JOIN public.notification_delivery_outbox AS o "
                        "ON o.notification_id=n.id WHERE o.operation_id=:operation_id "
                        "ORDER BY n.user_id,n.id"
                    ),
                    {"operation_id": operation_id},
                )
            ).mappings()
        )


async def _wait_claimed(maker, operation_id, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with maker() as db:
            count = await db.scalar(
                text(
                    "SELECT count(*) FROM public.notification_delivery_outbox "
                    "WHERE operation_id=:operation_id AND worker_id IS NOT NULL "
                    "AND lease_id IS NOT NULL"
                ),
                {"operation_id": operation_id},
            )
        if count:
            return
        await asyncio.sleep(0.02)
    pytest.fail("Publisher did not commit a lease before attempting Redis")


async def _park_redis_connection(reader, writer):
    try:
        await reader.read()
    finally:
        writer.close()
        await writer.wait_closed()


async def test_deactivation_outbox_commits_and_publisher_death_is_recovered(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    actor_ids = [actor]
    operation_ids = []
    identity = None
    redis_client = None
    pubsub = None
    blackhole = None
    publisher = None
    try:
        async with maker.begin() as db:
            other_admin = await create_user(
                db,
                "super_admin",
                f"deactivation-notify-{suffix}@example.test",
                "Notification recipient",
            )
        actor_ids.append(other_admin.id)
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_deactivation=True,
        )
        redis_client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        pubsub = redis_client.pubsub()
        channels = [f"notify:{user_id}" for user_id in actor_ids]
        await pubsub.subscribe(*channels)
        acknowledged = set()
        deadline = time.monotonic() + 5
        while acknowledged != set(channels) and time.monotonic() < deadline:
            message = await pubsub.get_message(timeout=0.2)
            if message and message.get("type") == "subscribe":
                acknowledged.add(message["channel"])
        assert acknowledged == set(channels)

        first_reason = f"private-notification-reason-{suffix}"
        first_request = _command(
            "request_self_deactivation", _token(actor, secret), {"reason": first_reason}
        )
        first_id = uuid.UUID(first_request["operation_id"])
        operation_ids.append(first_id)
        accepted = await asyncio.to_thread(_node, identity, control, first_request)
        assert accepted["ok"]
        assert first_reason not in json.dumps(accepted, ensure_ascii=False)
        first_rows = await _notification_rows(maker, first_id)
        assert len(first_rows) == 2
        assert {row["user_id"] for row in first_rows} == set(actor_ids)
        assert all(row["state"] == "pending" for row in first_rows)
        assert all(row["type"] == "user.deactivation_requested" for row in first_rows)
        assert all(
            row["target_type"] == "user" and row["target_id"] == actor
            for row in first_rows
        )
        assert all(
            set(row["payload"]) == {"email", "name", "role", "scheduled_at"}
            for row in first_rows
        )
        first_notification_ids = [row["id"] for row in first_rows]

        await asyncio.to_thread(
            _run_publisher,
            test_engine,
            tmp_path,
            os.environ["REDIS_URL"],
            "successful-batch",
        )
        first_messages = await _collect_notifications(pubsub, first_notification_ids)
        for notification_id, (channel, body) in first_messages.items():
            assert set(body) == {
                "id",
                "type",
                "target_type",
                "target_id",
                "payload",
                "created_at",
            }
            assert body["id"] == notification_id
            assert body["type"] == "user.deactivation_requested"
            assert body["target_type"] == "user" and body["target_id"] == str(actor)
            assert "user_id" not in body
            assert set(body["payload"]) == {"email", "name", "role", "scheduled_at"}
            assert first_reason not in json.dumps(body, ensure_ascii=False)
            assert channel in channels
        assert all(
            row["state"] == "done" for row in await _notification_rows(maker, first_id)
        )

        cancel = _command("cancel_self_deactivation", _token(actor, secret), {})
        operation_ids.append(uuid.UUID(cancel["operation_id"]))
        assert (await asyncio.to_thread(_node, identity, control, cancel))["ok"]
        second_reason = f"private-takeover-reason-{suffix}"
        second_request = _command(
            "request_self_deactivation",
            _token(actor, secret),
            {"reason": second_reason},
        )
        second_id = uuid.UUID(second_request["operation_id"])
        operation_ids.append(second_id)
        second_accepted = await asyncio.to_thread(
            _node, identity, control, second_request
        )
        assert second_accepted["ok"]
        second_rows = await _notification_rows(maker, second_id)
        assert len(second_rows) == 2 and all(
            row["state"] == "pending" for row in second_rows
        )
        second_notification_ids = [row["id"] for row in second_rows]

        # Let the real publisher commit a lease, then kill it while its Redis
        # handshake is blocked. Expire only those test-owned leases to model
        # the dead publisher's abandoned lease without waiting sixty seconds.
        async def _serve_blackhole():
            nonlocal blackhole
            blackhole = await asyncio.start_server(
                _park_redis_connection, "127.0.0.1", 0
            )
            port = blackhole.sockets[0].getsockname()[1]
            return f"redis://127.0.0.1:{port}/0"

        blocked_redis_url = await _serve_blackhole()
        publisher = subprocess.Popen(
            [
                os.environ["AAP_IDENTITY_RUNTIME_PYTHON"],
                "-I",
                "-m",
                "aap_identity_runtime.notification_worker",
                "--once",
            ],
            cwd=tmp_path,
            env=_publisher_env(
                test_engine, tmp_path, blocked_redis_url, "blocked-batch"
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        await _wait_claimed(maker, second_id)
        assert publisher.poll() is None
        publisher.terminate()
        await asyncio.to_thread(publisher.wait, 5)
        stdout, stderr = publisher.communicate(timeout=2)
        assert second_reason not in stdout and second_reason not in stderr
        blackhole.close()
        await blackhole.wait_closed()
        blackhole = None

        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.notification_delivery_outbox "
                    "SET lease_expires_at=now()-interval '1 second',"
                    "available_at=now()-interval '1 second' "
                    "WHERE operation_id=:operation_id AND lease_id IS NOT NULL"
                ),
                {"operation_id": second_id},
            )
        await asyncio.to_thread(
            _run_publisher,
            test_engine,
            tmp_path,
            os.environ["REDIS_URL"],
            "takeover-batch",
        )
        second_messages = await _collect_notifications(pubsub, second_notification_ids)
        assert set(second_messages) == {str(value) for value in second_notification_ids}
        for notification_id, (channel, body) in second_messages.items():
            assert body["id"] == notification_id
            assert body["type"] == "user.deactivation_requested"
            assert "user_id" not in body
            assert set(body["payload"]) == {"email", "name", "role", "scheduled_at"}
            assert second_reason not in json.dumps(body, ensure_ascii=False)
            assert channel in channels
        final_rows = await _notification_rows(maker, second_id)
        assert all(row["state"] == "done" for row in final_rows)
        assert all(row["attempt"] >= 2 for row in final_rows)
        record_property(
            "deactivation_notification_delivery",
            "transactional-outbox,real-redis-publish,dead-lease-takeover,same-message-id,no-reason-payload",
        )
    finally:
        if publisher is not None and publisher.poll() is None:
            publisher.terminate()
            await asyncio.to_thread(publisher.wait, 5)
        if blackhole is not None:
            blackhole.close()
            await blackhole.wait_closed()
        if pubsub is not None:
            await pubsub.unsubscribe()
            await pubsub.aclose()
        if redis_client is not None:
            await redis_client.aclose()
        if identity is not None:
            identity.stop()
        await _cleanup_deactivation(
            maker,
            actor_ids=actor_ids,
            user_operation_ids=operation_ids,
        )


async def test_container_due_pages_stay_bounded_and_owner_grants_are_narrow(
    test_engine, tmp_path, record_property
):
    from tests.test_asset_storage_cleanup_runtime import _inspect_docker

    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex[:12]
    actor_ids = [actor]
    due_operation_ids = []
    identity = None
    total = 70
    try:
        due_time = datetime.now(UTC) - timedelta(seconds=1)
        requested_at = due_time - timedelta(days=8)
        async with maker.begin() as db:
            workers = [
                await create_user(
                    db,
                    "employee",
                    f"deactivation-page-{suffix}-{index}@example.test",
                    f"Due account {index}",
                )
                for index in range(total)
            ]
            actor_ids.extend(user.id for user in workers)
            await db.execute(
                update(User)
                .where(User.id.in_([user.id for user in workers]))
                .values(
                    deactivation_requested_at=requested_at,
                    deactivation_reason=None,
                    deactivation_scheduled_at=due_time,
                )
            )
        identity = await _start_container_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_deactivation=True,
            enable_deactivation_operator=True,
        )
        pages = []
        for _ in range(4):
            request = _due_command(limit=32)
            due_operation_ids.append(uuid.UUID(request["operation_id"]))
            page = await asyncio.to_thread(_node, identity, control, request)
            assert page["ok"]
            assert page["examined"] <= 32
            assert (
                page["processed"] + page["blocked_last_super_admin"] == page["examined"]
            )
            assert not any(key in page for key in ("users", "items", "email", "name"))
            pages.append(page)
            if not page["has_more"]:
                break
        assert [page["processed"] for page in pages] == [32, 32, 6]
        assert sum(page["examined"] for page in pages) == total
        assert pages[-1]["has_more"] is False
        async with maker() as db:
            assert (
                await db.scalar(
                    select(text("count(*)"))
                    .select_from(User)
                    .where(
                        User.id.in_([user.id for user in workers]),
                        User.is_active.is_(True),
                    )
                )
                == 0
            )
        details = _inspect_docker(identity.container_id)
        assert details["HostConfig"]["ReadonlyRootfs"] is True
        assert details["Config"]["User"] == "10003:10003"
        assert 0 < details["HostConfig"]["Memory"] <= 256 * 1024 * 1024
        async with maker.begin() as db:
            for role, expected in (
                (
                    "identity_deactivation_client",
                    {"deactivation_policy", "execute_deactivation_command"},
                ),
                (
                    "identity_deactivation_operator",
                    {"due_policy", "process_due_deactivations"},
                ),
            ):
                actual = set(
                    (
                        await db.execute(
                            text(
                                "SELECT proc.proname FROM pg_catalog.pg_proc AS proc "
                                "JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace "
                                "WHERE ns.nspname='aap_identity_deactivation_v1' "
                                "AND pg_catalog.has_function_privilege(:role,proc.oid,'EXECUTE')"
                            ),
                            {"role": role},
                        )
                    ).scalars()
                )
                assert actual == expected
                for table in (
                    "public.users",
                    "public.notifications",
                    "public.notification_delivery_outbox",
                ):
                    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        assert not await db.scalar(
                            text(
                                "SELECT pg_catalog.has_table_privilege(:role,:table_name,:privilege)"
                            ),
                            {"role": role, "table_name": table, "privilege": privilege},
                        )
        record_property(
            "deactivation_bounded_container",
            json.dumps(
                {
                    "users": total,
                    "pages": [page["processed"] for page in pages],
                    "memory": details["HostConfig"]["Memory"],
                    "readonly_rootfs": details["HostConfig"]["ReadonlyRootfs"],
                    "user": details["Config"]["User"],
                }
            ),
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup_deactivation(
            maker,
            actor_ids=actor_ids,
            due_operation_ids=due_operation_ids,
        )
