"""Qualify the complete existing Identity deployment on verifier-owned resources."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

from cryptography.fernet import Fernet
from alembic.script import ScriptDirectory
import httpx
from jose.exceptions import JWTError
import pytest
import redis.asyncio as redis
from redis.exceptions import ResponseError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
import websockets

from yoao_identity.security import create_access_token, decode_access_token
from tests.factory import create_project, create_user
from tests.identity_module_assembly_helpers import IdentityModuleStack
from tests.identity_recovery_live_helpers import SMTPInbox
from tests.test_identity_admission_live import ROOT, _dsn, _world
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_briefs_api_live import _caller_world
from tests.test_identity_deactivation_live import (
    _collect_notifications,
    _due_command,
    _notification_rows,
)
from tests.test_identity_groups_live import _command, _node
from tests.test_identity_invitations_live import _command as _invitation_command
from tests.test_identity_recovery import _postgres_tool
from tests.test_identity_registration_status_live import _node_policy_read
from tests.test_identity_sessions_live import _login_redis, _verify_redis

MIGRATION_HEAD = ScriptDirectory(str(ROOT / "apps/api/alembic")).get_current_head()


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_LIVE_ACCEPTANCE") != "1"
        or not os.getenv("AAP_IDENTITY_API_RUNTIME_IMAGE"),
        reason="requires the owned verifier and current complete Identity artifacts",
    ),
]


@asynccontextmanager
async def _publisher_redis(*, retain=None):
    url = _verify_redis()
    name, password = (
        "identity-publisher-" + uuid.uuid4().hex[:12],
        secrets.token_urlsafe(32),
    )
    owner = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    scoped = None
    created = False
    try:
        await owner.execute_command(
            "ACL",
            "SETUSER",
            name,
            "reset",
            "on",
            ">" + password,
            "resetchannels",
            "&notify:*",
            "-@all",
            "+publish",
            "+ping",
            "+select",
            "+client|setinfo",
            "+client|setname",
        )
        created = True
        dsn = url.set(username=name, password=password).render_as_string(
            hide_password=False
        )
        scoped = redis.from_url(dsn, decode_responses=True)
        assert await scoped.ping()
        with pytest.raises(ResponseError):
            await scoped.get("login_failed:assembly-private")
        with pytest.raises(ResponseError):
            await scoped.publish("outside-identity", "denied")
        yield dsn, owner
    finally:
        primary = sys.exception()
        errors = []
        try:
            if scoped is not None:
                await scoped.aclose()
        except BaseException as exc:
            errors.append(exc)
        try:
            if created and not errors and not (retain is not None and retain()):
                _verify_redis()
                assert await owner.execute_command("ACL", "DELUSER", name) == 1
        except BaseException as exc:
            errors.append(exc)
        try:
            await owner.aclose()
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise BaseExceptionGroup(
                "Identity publisher Redis cleanup failed",
                ([primary] if primary is not None else []) + errors,
            ) from None


async def _run_hosted_notification_relay(test_engine, publisher_url, database_name):
    database_url = (
        make_url(_dsn("NOTIFICATION_PUBLISH", test_engine))
        .set(database=database_name)
        .render_as_string(hide_password=False)
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from app.workers.notification_delivery import relay_identity_notifications; "
        "relay_identity_notifications()",
        cwd=ROOT / "apps/api",
        env={
            "PATH": os.environ.get("PATH", ""),
            "DATABASE_URL": os.environ["AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "IDENTITY_URL": "",
            "IDENTITY_TOKEN": "",
            "IDENTITY_EMBEDDED_SIGNING_SECRET": "",
            "IDENTITY_EMBEDDED_SIGNING_SECRET_FILE": "",
            "NOTIFICATION_PUBLISH_MODE": "embedded",
            "NOTIFICATION_PUBLISH_DATABASE_URL": database_url,
            "NOTIFICATION_PUBLISH_DATABASE_URL_FILE": "",
            "NOTIFICATION_PUBLISH_REDIS_URL": publisher_url,
            "NOTIFICATION_PUBLISH_REDIS_URL_FILE": "",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0, "Hosted Notifications relay failed"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def _assert_consumer_is_restricted(test_engine, actor):
    from scripts.verify_module_data_boundary import (
        IDENTITY_AVATAR_FUNCTIONS,
        IDENTITY_ISSUER_FACT_FUNCTIONS,
    )

    url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL"])
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert url.username == "identity_api_consumer" and url.database.endswith("_test")
    engine = create_async_engine(url, hide_parameters=True)
    try:
        for query in (
            "SELECT password_hash FROM public.users LIMIT 1",
            "SELECT key_hash FROM public.api_keys LIMIT 1",
            "SELECT * FROM public.identity_recovery_operation_receipts LIMIT 1",
            "UPDATE public.users SET name=name WHERE id=:id",
        ):
            async with engine.connect() as connection:
                with pytest.raises(DBAPIError) as denied:
                    await connection.execute(text(query), {"id": actor})
                assert denied.value.orig.sqlstate == "42501"
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT current_user")) == url.username
            assert (
                await connection.scalar(
                    text(
                        "SELECT id FROM yoao_identity_v1.account_profiles WHERE id=:id"
                    ),
                    {"id": actor},
                )
                == actor
            )
            functions = await connection.scalars(
                text(
                    "SELECT n.nspname || '.' || p.proname || '(' || "
                    "replace(oidvectortypes(p.proargtypes), ', ', ',') || ')' "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname ~ '^(yoao|aap)_identity(_[a-z0-9]+)*_v[0-9]+$' "
                    "AND has_function_privilege(current_user,p.oid,'EXECUTE')"
                )
            )
            expected = set(IDENTITY_ISSUER_FACT_FUNCTIONS)
            if os.environ.get("AAP_MODULE_DATA_IDENTITY_AVATAR_RUNTIME") == "1":
                expected.update(IDENTITY_AVATAR_FUNCTIONS)
            assert set(functions) == expected
    finally:
        await engine.dispose()


async def _briefs_consumer_world(maker, actor, suffix):
    world = await _caller_world(maker, actor, suffix)
    async with maker.begin() as db:
        await db.execute(
            text(
                "UPDATE projects SET type_key='lidar',type_label='点云',data_type='lidar' WHERE id=:id"
            ),
            {"id": world["project_id"]},
        )
        await db.execute(
            text(
                "UPDATE tasks SET file_type='point_cloud',file_name='brief.pcd',file_path=:path WHERE id=:id"
            ),
            {"id": world["task_id"], "path": f"brief/{suffix}.pcd"},
        )
    return world


@asynccontextmanager
async def _restored_database(test_engine, snapshot, *, retain=None):
    """Copy database ACLs as well as the full dump; retain global LOGIN identities."""
    clone = f"aap_identity_module_restore_{uuid.uuid4().hex[:12]}_test"
    marker = "identity-module-restore:" + str(uuid.uuid4())
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    created = False
    try:
        async with admin.connect() as db:
            await db.execute(text(f'CREATE DATABASE "{clone}" TEMPLATE template0'))
            created = True
            await db.execute(text(f"COMMENT ON DATABASE \"{clone}\" IS '{marker}'"))
            async with test_engine.connect() as source:
                acl = (
                    await source.execute(
                        text(
                            "SELECT COALESCE(role.rolname,'PUBLIC'),entry.privilege_type,entry.is_grantable "
                            "FROM pg_database d CROSS JOIN LATERAL "
                            "aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) entry "
                            "LEFT JOIN pg_roles role ON role.oid=entry.grantee "
                            "WHERE d.datname=current_database()"
                        )
                    )
                ).all()
            await db.execute(text(f'REVOKE ALL ON DATABASE "{clone}" FROM PUBLIC'))
            for grantee, privilege, grantable in acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = (
                    "PUBLIC"
                    if grantee == "PUBLIC"
                    else db.dialect.identifier_preparer.quote_identifier(grantee)
                )
                suffix = " WITH GRANT OPTION" if grantable else ""
                await db.execute(
                    text(f'GRANT {privilege} ON DATABASE "{clone}" TO {role}{suffix}')
                )
        _postgres_tool(
            test_engine,
            [
                "pg_restore",
                "-U",
                "owner",
                "--exit-on-error",
                "--single-transaction",
                "--dbname",
                clone,
            ],
            body=snapshot.read_bytes(),
        )
        yield clone
    finally:
        primary = sys.exception()
        errors = []
        try:
            if created and not (retain is not None and retain()):
                async with admin.connect() as db:
                    assert (
                        await db.scalar(
                            text(
                                "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=:name"
                            ),
                            {"name": clone},
                        )
                        == marker
                    )
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity WHERE datname=:name "
                                "AND backend_type IS DISTINCT FROM 'autovacuum worker'"
                            ),
                            {"name": clone},
                        )
                        == 0
                    )
                    await db.execute(text(f'DROP DATABASE "{clone}"'))
        except BaseException as exc:
            errors.append(exc)
        try:
            await admin.dispose()
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise BaseExceptionGroup(
                "Identity restored database cleanup failed",
                ([primary] if primary is not None else []) + errors,
            ) from None


async def test_complete_compose_remote_api_workers_restart_and_restore(
    test_engine,
    tmp_path,
    record_property,
):
    """Select this case on its own fresh DB; the verifier drops all fixture state."""
    _verify_postgres(test_engine.url)
    async with test_engine.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM users")) == 0
    password = "IdentityModuleStrong123!"
    maker, actor = await _world(test_engine, password=password)
    secret, api_secret, control = [secrets.token_urlsafe(32) for _ in range(3)]
    assert len({secret, api_secret, control}) == 3
    suffix = uuid.uuid4().hex
    async with maker.begin() as db:
        successor = await create_user(
            db,
            "super_admin",
            f"module-successor-{suffix}@example.test",
            "Module successor",
        )
        owner_receiver = await create_user(
            db,
            "project_admin",
            f"module-receiver-{suffix}@example.test",
            "Project receiver",
        )
        target = await create_user(
            db,
            "employee",
            f"module-target-{suffix}@example.test",
            "Module target",
            password,
        )
        disposable = await create_user(
            db,
            "employee",
            f"module-delete-{suffix}@example.test",
            "Disposable module target",
        )
        leaver = await create_user(
            db, "employee", f"module-leaver-{suffix}@example.test", "Module leaver"
        )
        due_target = await create_user(
            db, "employee", f"module-due-{suffix}@example.test", "Due module account"
        )
        project = await create_project(db, owner_id=leaver.id, name="Module handoff")
    briefs = await _briefs_consumer_world(maker, actor, suffix)
    await _assert_consumer_is_restricted(test_engine, actor)
    inbox = SMTPInbox.for_docker()
    snapshot = tmp_path / "identity-module.dump"
    stack = None
    pubsub = None
    calls = []
    try:
        async with (
            _login_redis() as (login_url, _),
            _publisher_redis() as (publisher_url, redis_owner),
        ):
            stack = IdentityModuleStack(
                test_engine,
                tmp_path,
                secret=secret,
                api_secret=api_secret,
                control=control,
                mail_key=Fernet.generate_key().decode(),
                login_redis_url=login_url,
                smtp_port=inbox.port,
            )
            await stack.start()
            async with httpx.AsyncClient(
                base_url=stack.api_origin, trust_env=False, timeout=40
            ) as client:

                async def call(method, path, expected=200, **kwargs):
                    response = await client.request(method, path, **kwargs)
                    if response.status_code != expected:
                        record_property(
                            "runtime_diagnostics", stack.capture_diagnostics()
                        )
                        record_property("failed_response", stack.redact(response.text))
                    assert response.status_code == expected, (
                        f"{method} {path}: {response.status_code}"
                    )
                    calls.append(
                        {
                            "method": method,
                            "path": path.split("?")[0],
                            "status": expected,
                        }
                    )
                    return response.json() if response.content else None

                login = await call(
                    "POST",
                    "/api/v1/auth/login",
                    json={
                        "email": f"identity-live-{actor}@example.test",
                        "password": password,
                    },
                )
                token = login["access_token"]
                assert decode_access_token(token, secret=secret)["sub"] == str(actor)
                with pytest.raises(JWTError):
                    decode_access_token(token, secret=api_secret)
                client.headers["Authorization"] = "Bearer " + token
                assert (await call("GET", "/api/v1/auth/me"))["id"] == str(actor)
                assert await call("GET", "/api/v1/audit-logs/archives") == []
                wrong_token = create_access_token(
                    str(actor), "super_admin", secret=api_secret, expire_minutes=15
                )
                await call(
                    "GET",
                    "/api/v1/auth/me",
                    401,
                    headers={"Authorization": "Bearer " + wrong_token},
                )
                async with websockets.connect(
                    stack.api_origin.replace("http://", "ws://")
                    + "/ws/notifications?token="
                    + token
                ) as socket:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if (await redis_owner.pubsub_numsub(f"notify:{actor}"))[0][1]:
                            break
                        await asyncio.sleep(0.05)
                    await redis_owner.publish(
                        f"notify:{actor}",
                        json.dumps(
                            {"type": "notifications.sync", "marker": "cold-assembly"}
                        ),
                    )
                    assert (
                        json.loads(await asyncio.wait_for(socket.recv(), 5))["marker"]
                        == "cold-assembly"
                    )
                await call("POST", "/api/v1/auth/refresh")
                await call(
                    "PATCH", "/api/v1/auth/me", json={"name": "Assembled owner account"}
                )
                await call("POST", "/api/v1/auth/me/heartbeat", 204)
                key = await call(
                    "POST",
                    "/api/v1/me/api-keys",
                    201,
                    json={"name": "Module key", "scopes": ["*"]},
                )
                await call("GET", "/api/v1/me/api-keys")
                key_id = key["id"]
                await call("DELETE", f"/api/v1/me/api-keys/{key_id}", 204)
                await call("GET", "/api/v1/users", params={"status": "all"})
                await call(
                    "GET", "/api/v1/users/query", params={"search": target.email}
                )
                await call("GET", "/api/v1/users/stats")

                group = await call(
                    "POST",
                    "/api/v1/groups",
                    201,
                    json={"name": "Module " + suffix, "description": None},
                )
                await call("GET", "/api/v1/groups")
                await call(
                    "PATCH",
                    f"/api/v1/users/{target.id}/group",
                    json={"group_id": group["id"]},
                )
                await call(
                    "PATCH", f"/api/v1/users/{target.id}/group", json={"group_id": None}
                )
                await call("DELETE", "/api/v1/groups/" + group["id"], 204)

                assert (
                    await asyncio.to_thread(
                        _node_policy_read, stack.identity_origin, control, "read_status"
                    )
                    is True
                )
                status = await call("GET", "/api/v1/auth/registration-status")
                assert status == {"open_registration_enabled": True}
                settings = await call("GET", "/api/v1/settings/system")
                assert (
                    settings["metadata"]["allow_open_registration"][
                        "deployment_default"
                    ]
                    is True
                )
                changed = await call(
                    "PATCH",
                    "/api/v1/settings/system",
                    json={
                        "allow_open_registration": False,
                        "expected_version": settings["version"],
                    },
                )
                assert (await call("GET", "/api/v1/auth/registration-status"))[
                    "open_registration_enabled"
                ] is False
                await call(
                    "POST",
                    "/api/v1/settings/system/reset",
                    json={
                        "keys": ["allow_open_registration"],
                        "expected_version": changed["version"],
                    },
                )
                registered = await call(
                    "POST",
                    "/api/v1/auth/register-open",
                    201,
                    json={
                        "email": f"module-open-{suffix}@example.test",
                        "name": "Module registration",
                        "password": password,
                    },
                )
                assert (
                    decode_access_token(registered["access_token"], secret=secret)[
                        "sub"
                    ]
                    == registered["user"]["id"]
                )

                reset = await call(
                    "POST", f"/api/v1/users/{target.id}/admin-reset-password"
                )
                assert reset["target_email"] == target.email
                await call(
                    "GET",
                    f"/api/v1/users/{target.id}/role/preview",
                    params={"role": "viewer"},
                )
                assert (
                    await call(
                        "PATCH",
                        f"/api/v1/users/{target.id}/role",
                        json={"role": "viewer"},
                    )
                )["role"] == "viewer"
                async with maker() as db:
                    generation = await db.scalar(
                        text("SELECT credential_generation FROM users WHERE id=:id"),
                        {"id": target.id},
                    )
                old_target_token = create_access_token(
                    str(target.id),
                    "viewer",
                    secret=secret,
                    gen=generation,
                    expire_minutes=15,
                )
                await call(
                    "GET",
                    "/api/v1/auth/me",
                    headers={"Authorization": "Bearer " + old_target_token},
                )
                await call("POST", f"/api/v1/users/{target.id}/deactivate")
                await call(
                    "GET",
                    "/api/v1/auth/me",
                    401,
                    headers={"Authorization": "Bearer " + old_target_token},
                )
                await call(
                    "POST",
                    f"/api/v1/users/{target.id}/reactivate",
                    json={"reason": "Local module acceptance"},
                )
                await call(
                    "GET",
                    "/api/v1/auth/me",
                    401,
                    headers={"Authorization": "Bearer " + old_target_token},
                )
                await call(
                    "DELETE",
                    f"/api/v1/users/{disposable.id}",
                    json={"transfer_to_user_id": None},
                )

                preview = await call(
                    "GET", f"/api/v1/users/{leaver.id}/offboarding-preview"
                )
                transferred = await call(
                    "POST",
                    f"/api/v1/users/{leaver.id}/offboarding",
                    json={
                        "preview_version": preview["preview_version"],
                        "reason": "",
                        "projects": [
                            {
                                "project_id": str(project.id),
                                "owner_receiver_id": str(owner_receiver.id),
                                "annotator_receiver_id": None,
                                "reviewer_receiver_id": None,
                            }
                        ],
                    },
                )
                assert transferred["status"] == "suspended"
                caller_headers = {
                    "Authorization": "Bearer "
                    + create_access_token(
                        str(briefs["caller_id"]),
                        "employee",
                        secret=secret,
                        expire_minutes=15,
                    )
                }
                await call(
                    "GET", f"/api/v1/tasks/{briefs['task_id']}", headers=caller_headers
                )
                await call(
                    "GET",
                    f"/api/v1/projects/{briefs['project_id']}/batches/{briefs['batch_id']}",
                    headers=caller_headers,
                )

                await stack.stop_service("identity-mail-v2")
                await call(
                    "POST",
                    "/api/v1/auth/forgot-password",
                    202,
                    json={"email": target.email},
                )
                assert not inbox.messages
                await stack.start_service("identity-mail-v2")
                await inbox.wait(1)
                reset_token = inbox.tokens()[0]
                await call(
                    "POST",
                    "/api/v1/auth/reset-password",
                    json={"token": reset_token, "new_password": password},
                )
                await call(
                    "POST",
                    "/api/v1/auth/reset-password",
                    400,
                    json={"token": reset_token, "new_password": password},
                )
                invitation = await call(
                    "POST",
                    "/api/v1/users/invite",
                    201,
                    json={
                        "email": f"module-invite-{suffix}@example.test",
                        "role": "employee",
                        "group_name": None,
                        "project_id": str(briefs["project_id"]),
                        "project_member_role": "reviewer",
                    },
                )
                async with maker() as db:
                    invitation_id = await db.scalar(
                        text("SELECT id FROM user_invitations WHERE token=:token"),
                        {"token": invitation["token"]},
                    )
                await call("POST", f"/api/v1/invitations/{invitation_id}/send-email")
                await inbox.wait(2)
                assert inbox.tokens()[1] == invitation["token"]
                resolved = await client.get(
                    "/api/v1/auth/invitations/" + invitation["token"]
                )
                assert resolved.status_code == 200
                invited = await call(
                    "POST",
                    "/api/v1/auth/register",
                    201,
                    json={
                        "token": invitation["token"],
                        "name": "Assembled invitee",
                        "password": password,
                    },
                )
                assert invited["acceptance"]["project_member_role"] == "reviewer"

                slots = asyncio.Semaphore(4)
                elapsed = []

                async def bounded_read(path):
                    async with slots:
                        started = time.monotonic()
                        await call("GET", path)
                        elapsed.append(time.monotonic() - started)

                started = time.monotonic()
                await asyncio.gather(
                    *[
                        bounded_read(path)
                        for path in (
                            "/api/v1/auth/me",
                            "/api/v1/auth/registration-status",
                            "/api/v1/users/query",
                            "/api/v1/groups",
                        )
                        * 4
                    ]
                )
                record_property(
                    "local_capacity",
                    json.dumps(
                        {
                            "requests": 16,
                            "concurrency": 4,
                            "seconds": round(time.monotonic() - started, 3),
                            "observed_p95_seconds": round(sorted(elapsed)[15], 3),
                            "identity_cpu": 1,
                            "identity_memory_mib": 256,
                            "api_cpu": 1,
                            "api_memory_mib": 512,
                        }
                    ),
                )

                command = _command(
                    "create_group",
                    token,
                    {"name": "Replay " + suffix, "description": None},
                )
                first = await asyncio.to_thread(
                    _node,
                    SimpleNamespace(origin=stack.identity_origin),
                    control,
                    command,
                )
                assert first["ok"] is True
                due_headers = {
                    "Authorization": "Bearer "
                    + create_access_token(
                        str(due_target.id), "employee", secret=secret, expire_minutes=15
                    )
                }
                await call(
                    "POST",
                    "/api/v1/auth/me/deactivation-request",
                    json={},
                    headers=due_headers,
                )
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "UPDATE users SET deactivation_requested_at=CURRENT_TIMESTAMP-interval '8 days', "
                            "deactivation_scheduled_at=CURRENT_TIMESTAMP-interval '1 day' WHERE id=:id"
                        ),
                        {"id": due_target.id},
                    )
                due = await client.post(
                    stack.identity_origin + "/v1/identity/deactivation/process-due",
                    headers={"Authorization": "Bearer " + control},
                    json=_due_command(limit=8),
                )
                assert due.status_code == 200 and due.json()["ok"] is True
                assert due.json()["processed"] == due.json()["examined"] == 1
                await call("GET", "/api/v1/auth/me", 401, headers=due_headers)
                await stack.stop_service("identity-issuer")
                await call("GET", "/api/v1/auth/me", 503)
                await call("GET", "/api/v1/auth/registration-status", 503)
                await stack.start_service("identity-issuer")
                replay = await asyncio.to_thread(
                    _node,
                    SimpleNamespace(origin=stack.identity_origin),
                    control,
                    command,
                )
                assert replay == first
                await call("GET", "/api/v1/auth/me")

                pubsub = redis_owner.pubsub()
                channels = [f"notify:{value}" for value in (actor, successor.id)]
                await pubsub.subscribe(*channels)
                acknowledged = set()
                deadline = time.monotonic() + 5
                while acknowledged != set(channels) and time.monotonic() < deadline:
                    message = await pubsub.get_message(timeout=0.2)
                    if message and message["type"] == "subscribe":
                        acknowledged.add(message["channel"])
                assert acknowledged == set(channels)
                await call(
                    "POST",
                    "/api/v1/auth/me/deactivation-request",
                    json={"reason": "Private assembly reason"},
                )
                async with maker() as db:
                    operation = await db.scalar(
                        text(
                            "SELECT operation_id FROM identity_deactivation_operation_receipts WHERE actor_id=:id AND operation='request_self_deactivation' ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"id": actor},
                    )
                notifications = await _notification_rows(maker, operation)
                assert len(notifications) == 2 and all(
                    row["state"] == "pending" for row in notifications
                )
                await _run_hosted_notification_relay(
                    test_engine, publisher_url, test_engine.url.database
                )
                await _collect_notifications(
                    pubsub, [row["id"] for row in notifications]
                )
                await call("DELETE", "/api/v1/auth/me/deactivation-request")
                await stack.stop_service("identity-mail-v2")
                await call("POST", "/api/v1/auth/me/deactivation-request", json={})
                async with maker() as db:
                    restore_operation = await db.scalar(
                        text(
                            "SELECT operation_id FROM identity_deactivation_operation_receipts "
                            "WHERE actor_id=:id AND operation='request_self_deactivation' "
                            "ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"id": actor},
                    )
                restore_notifications = await _notification_rows(
                    maker, restore_operation
                )
                assert len(restore_notifications) == 2
                assert all(row["state"] == "pending" for row in restore_notifications)
                pending = await call(
                    "POST",
                    "/api/v1/users/invite",
                    201,
                    json={
                        "email": f"module-restored-mail-{suffix}@example.test",
                        "role": "employee",
                    },
                )
                async with maker() as db:
                    pending_id = await db.scalar(
                        text("SELECT id FROM user_invitations WHERE token=:token"),
                        {"token": pending["token"]},
                    )
                queued = await client.post(
                    stack.identity_origin + "/v1/identity/invitations/execute",
                    headers={"Authorization": "Bearer " + control},
                    json=_invitation_command(
                        "send_invitation_email",
                        token,
                        {"invitation_id": str(pending_id)},
                    ),
                )
                assert queued.status_code == 200
                assert queued.json()["ok"] is True
                assert queued.json()["data"]["mail_status"] == "pending"

            await stack.stop()
            snapshot.touch(mode=0o600)
            snapshot.write_bytes(
                _postgres_tool(
                    test_engine,
                    ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database],
                )
            )
            assert snapshot.read_bytes().startswith(b"PGDMP")
            record_property(
                "backup_sha256", hashlib.sha256(snapshot.read_bytes()).hexdigest()
            )
            async with _restored_database(test_engine, snapshot) as clone:
                try:
                    await stack.switch_database(clone)
                    async with httpx.AsyncClient(
                        base_url=stack.api_origin,
                        trust_env=False,
                        timeout=40,
                        headers={"Authorization": "Bearer " + token},
                    ) as client:
                        restored_me = await client.get("/api/v1/auth/me")
                        if restored_me.status_code != 200:
                            record_property(
                                "runtime_diagnostics", stack.capture_diagnostics()
                            )
                            record_property(
                                "failed_response", stack.redact(restored_me.text)
                            )
                        assert restored_me.status_code == 200
                        assert (
                            await client.get(
                                "/api/v1/auth/me",
                                headers={"Authorization": "Bearer " + old_target_token},
                            )
                        ).status_code == 401
                        assert (
                            await client.get("/api/v1/auth/registration-status")
                        ).json() == {"open_registration_enabled": True}
                        assert (
                            await asyncio.to_thread(
                                _node,
                                SimpleNamespace(origin=stack.identity_origin),
                                control,
                                command,
                            )
                        ) == first
                        await inbox.wait(3)
                        assert inbox.tokens()[2] == pending["token"]
                        await _run_hosted_notification_relay(
                            test_engine, publisher_url, clone
                        )
                        await _collect_notifications(
                            pubsub, [row["id"] for row in restore_notifications]
                        )
                        restored = create_async_engine(
                            test_engine.url.set(database=clone), hide_parameters=True
                        )
                        try:
                            async with restored.connect() as db:
                                assert (
                                    await db.scalar(
                                        text(
                                            "SELECT revoked_at IS NOT NULL FROM api_keys WHERE id=:id"
                                        ),
                                        {"id": uuid.UUID(key_id)},
                                    )
                                    is True
                                )
                                assert (
                                    await db.scalar(
                                        text(
                                            "SELECT count(*) FROM identity_group_operation_receipts WHERE operation_id=:id"
                                        ),
                                        {"id": uuid.UUID(command["operation_id"])},
                                    )
                                    == 1
                                )
                                assert (
                                    await db.scalar(
                                        text(
                                            "SELECT has_table_privilege('identity_api_consumer','users','SELECT')"
                                        )
                                    )
                                    is False
                                )
                                assert (
                                    await db.scalar(
                                        text("SELECT version_num FROM alembic_version")
                                    )
                                    == MIGRATION_HEAD
                                )
                        finally:
                            await restored.dispose()
                finally:
                    await stack.stop()
            record_property("assembled_public_operations", json.dumps(calls))
            record_property("container_assembly", json.dumps(stack.inspection_evidence))
            record_property(
                "assembly",
                "cold opt-in issuer and API; distinct JWT/media keys; API and Redis ACLs; one V2 sender; hosted relay; Node replay; owner outage; dump/restore",
            )
    finally:
        if pubsub is not None:
            await pubsub.aclose()
        if stack is not None:
            await stack.stop()
        snapshot.unlink(missing_ok=True)
        inbox.close()


async def test_embedded_identity_with_isolated_mail_on_cold_api(
    test_engine, tmp_path, record_property
):
    password = "IdentityEmbeddedStrong123!"
    maker, actor = await _world(test_engine, password=password)
    api_role = make_url(
        os.environ["AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL"]
    )
    restricted = create_async_engine(api_role, hide_parameters=True)
    try:
        for command in (
            "SELECT token_ciphertext FROM public.identity_credential_mail_outbox",
            "SELECT yoao_identity_recovery_v1.claim_mail('{}'::jsonb)",
            "SELECT yoao_identity_mail_v2.claim_mail('{}'::jsonb)",
            "SELECT yoao_settings_v1.mail_delivery_policy('{}'::jsonb)",
            "SELECT yoao_settings_v1.claim_smtp_test()",
            "SELECT yoao_settings_v1.finish_smtp_test(NULL,NULL,NULL,NULL,NULL,NULL)",
        ):
            async with restricted.connect() as connection:
                with pytest.raises(DBAPIError) as denied:
                    await connection.execute(text(command))
                assert denied.value.orig.sqlstate == "42501"
    finally:
        await restricted.dispose()

    signer, edge_secret, control = [secrets.token_urlsafe(32) for _ in range(3)]
    inbox = SMTPInbox.for_docker()
    stack = None
    try:
        async with _login_redis() as (login_url, redis_owner):
            stack = IdentityModuleStack(
                test_engine,
                tmp_path,
                secret=signer,
                api_secret=edge_secret,
                control=control,
                mail_key=Fernet.generate_key().decode(),
                login_redis_url=login_url,
                smtp_port=inbox.port,
                embedded=True,
            )
            await stack.start()
            assert set(stack.service_ids) == {"identity-api", "identity-mail-v2"}
            async with httpx.AsyncClient(
                base_url=stack.api_origin, trust_env=False, timeout=30
            ) as client:
                email = f"identity-live-{actor}@example.test"
                login = await client.post(
                    "/api/v1/auth/login", json={"email": email, "password": password}
                )
                assert login.status_code == 200, login.text
                jwt = login.json()["access_token"]
                assert decode_access_token(jwt, secret=signer)["sub"] == str(actor)
                with pytest.raises(JWTError):
                    decode_access_token(jwt, secret=edge_secret)
                client.headers["Authorization"] = "Bearer " + jwt
                refreshed = await client.post("/api/v1/auth/refresh")
                assert refreshed.status_code == 200, refreshed.text
                fresh_jwt = refreshed.json()["access_token"]
                client.headers["Authorization"] = "Bearer " + fresh_jwt
                key = await client.post(
                    "/api/v1/me/api-keys",
                    json={"name": "Embedded key", "scopes": ["*"]},
                )
                assert key.status_code == 201, key.text
                api_key = {"Authorization": "Bearer " + key.json()["plaintext"]}
                admitted = await client.get("/api/v1/auth/me", headers=api_key)
                assert admitted.status_code == 200 and admitted.json()["id"] == str(
                    actor
                )
                async with websockets.connect(
                    stack.api_origin.replace("http://", "ws://")
                    + "/ws/notifications?token="
                    + fresh_jwt
                ) as socket:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if (await redis_owner.pubsub_numsub(f"notify:{actor}"))[0][1]:
                            break
                        await asyncio.sleep(0.05)
                    await redis_owner.publish(
                        f"notify:{actor}",
                        '{"type":"notifications.sync","marker":"embedded"}',
                    )
                    frame = json.loads(await asyncio.wait_for(socket.recv(), 5))
                    assert frame["marker"] == "embedded"

                await stack.stop_service("identity-mail-v2")
                recovery = await client.post(
                    "/api/v1/auth/forgot-password", json={"email": email}
                )
                assert recovery.status_code == 202
                async with maker() as db:
                    status = await db.scalar(
                        text(
                            "SELECT status FROM identity_credential_mail_outbox "
                            "WHERE actor_id=:actor AND purpose='password_reset'"
                        ),
                        {"actor": actor},
                    )
                    assert status == "pending"
                assert not inbox.messages
                await stack.start_service("identity-mail-v2")
                await inbox.wait(1)

                invitation = await client.post(
                    "/api/v1/users/invite",
                    json={
                        "email": f"embedded-{actor}@example.test",
                        "role": "employee",
                    },
                )
                assert invitation.status_code == 201, invitation.text
                invitation_token = invitation.json()["token"]
                async with maker() as db:
                    invitation_id = await db.scalar(
                        text("SELECT id FROM user_invitations WHERE token=:token"),
                        {"token": invitation_token},
                    )
                sent = await client.post(
                    f"/api/v1/invitations/{invitation_id}/send-email"
                )
                assert sent.status_code == 200, sent.text
                await inbox.wait(2)
                assert inbox.tokens()[1] == invitation_token
                reset = await client.post(
                    "/api/v1/auth/reset-password",
                    json={"token": inbox.tokens()[0], "new_password": password},
                )
                assert reset.status_code == 200
                assert (await client.get("/api/v1/auth/me")).status_code == 401
            record_property(
                "embedded_container_assembly", json.dumps(stack.inspection_evidence)
            )
    finally:
        if stack is not None:
            await stack.stop()
        inbox.close()


@pytest.mark.parametrize("trace", [True, False], ids=["traced", "untraced"])
async def test_host_forwarded_identity_original_sequence(
    test_engine, tmp_path, record_property, monkeypatch, trace
):
    """Replay the original full consumer order rather than a smaller RPC sample."""
    from tests.identity_forwarding_diagnostic_helpers import ForwardedIdentityStack

    evidence = {"trace_enabled": trace, "status": "failed", "traces": []}
    receipt = (
        ROOT / ".worktree" / f"identity-forwarded-sequence-{uuid.uuid4().hex}.json"
    )

    class ObservedStack(ForwardedIdentityStack):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, trace=trace, **kwargs)
            self.trace_offsets = {}
            self.trace_stage = "original"
            evidence["project_name"] = self.project_name

        def retain_traces(self):
            counts = {}
            ids = self.service_ids
            for row in self.trace_records():
                key = (row["service"], ids[row["service"]])
                counts[key] = counts.get(key, 0) + 1
                if counts[key] > self.trace_offsets.get(key, 0):
                    evidence["traces"].append(
                        {"stage": self.trace_stage, "container_id": key[1], **row}
                    )
            self.trace_offsets.update(counts)

        async def stop(self):
            self.retain_traces()
            await super().stop()

        async def switch_database(self, database_name):
            self.retain_traces()
            self.trace_stage = "restored"
            await super().switch_database(database_name)

    monkeypatch.setattr(sys.modules[__name__], "IdentityModuleStack", ObservedStack)
    try:
        await test_complete_compose_remote_api_workers_restart_and_restore(
            test_engine, tmp_path, record_property
        )
        evidence["status"] = "passed"
    finally:
        receipt.touch(mode=0o600)
        receipt.write_text(json.dumps(evidence, indent=2) + "\n")
        record_property("original_forwarded_sequence_evidence", str(receipt))


@pytest.mark.parametrize("trace", [True, False], ids=["traced", "untraced"])
async def test_host_forwarded_identity_rpc_failures(
    test_engine, tmp_path, record_property, trace
):
    """Diagnose actual RPC consumers on the former topology, including restore."""
    from tests.identity_forwarding_diagnostic_helpers import ForwardedIdentityStack

    _verify_postgres(test_engine.url)
    async with test_engine.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM users")) == 0
    password = "IdentityForwardingStrong123!"
    maker, actor = await _world(test_engine, password=password)
    suffix = uuid.uuid4().hex
    async with maker.begin() as db:
        receiver = await create_user(
            db, "project_admin", f"forward-receiver-{suffix}@example.test", "Receiver"
        )
        invitation_project = await create_project(db, owner_id=actor)
        leavers = []
        for index in range(8):
            leaver = await create_user(
                db,
                "employee",
                f"forward-leaver-{index}-{suffix}@example.test",
                "Leaver",
            )
            project = await create_project(db, owner_id=leaver.id)
            leavers.append((str(leaver.id), str(project.id)))
    await _assert_consumer_is_restricted(test_engine, actor)
    secret, api_secret, control = [secrets.token_urlsafe(32) for _ in range(3)]
    stack = None
    inbox = SMTPInbox()
    snapshot = tmp_path / "identity-forwarding.dump"
    evidence = {
        "route": "host.docker.internal",
        "trace_enabled": trace,
        "calls": [],
        "traces": [],
    }
    receipt = ROOT / ".worktree" / f"identity-forwarded-rpc-{suffix}.json"
    trace_offsets = {}

    async def call(client, operation, method, path, expected=200, **kwargs):
        started = time.monotonic()
        row = {"operation": operation, "expected": expected}
        try:
            response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            row.update(status=None, error=type(error).__name__)
            return None
        else:
            row["status"] = response.status_code
            return response.json() if response.status_code == expected else None
        finally:
            row["seconds"] = round(time.monotonic() - started, 6)
            evidence["calls"].append(row)

    def collect_traces(stage):
        counts = {}
        for row in stack.trace_records():
            service = row["service"]
            counts[service] = counts.get(service, 0) + 1
            if counts[service] > trace_offsets.get(service, 0):
                evidence["traces"].append({"stage": stage, **row})
        trace_offsets.update(counts)

    try:
        async with _login_redis() as (login_url, _):
            stack = ForwardedIdentityStack(
                test_engine,
                tmp_path,
                trace=trace,
                secret=secret,
                api_secret=api_secret,
                control=control,
                mail_key=Fernet.generate_key().decode(),
                login_redis_url=login_url,
                smtp_port=inbox.port,
            )
            await stack.start()
            try:
                async with httpx.AsyncClient(
                    base_url=stack.api_origin, trust_env=False, timeout=40
                ) as client:
                    login = await call(
                        client,
                        "login",
                        "POST",
                        "/api/v1/auth/login",
                        json={
                            "email": f"identity-live-{actor}@example.test",
                            "password": password,
                        },
                    )
                    assert login is not None
                    token = login["access_token"]
                    client.headers["Authorization"] = "Bearer " + token
                    for index, (leaver_id, project_id) in enumerate(leavers):
                        await call(client, "profile", "GET", "/api/v1/auth/me")
                        await call(
                            client,
                            "invitation",
                            "POST",
                            "/api/v1/users/invite",
                            201,
                            json={
                                "email": f"forward-invite-{index}-{suffix}@example.test",
                                "role": "employee",
                                "group_name": None,
                                "project_id": str(invitation_project.id),
                                "project_member_role": "reviewer",
                            },
                        )
                        preview = await call(
                            client,
                            "offboarding_preview",
                            "GET",
                            f"/api/v1/users/{leaver_id}/offboarding-preview",
                        )
                        if preview is not None:
                            await call(
                                client,
                                "offboarding",
                                "POST",
                                f"/api/v1/users/{leaver_id}/offboarding",
                                json={
                                    "preview_version": preview["preview_version"],
                                    "reason": "",
                                    "projects": [
                                        {
                                            "project_id": project_id,
                                            "owner_receiver_id": str(receiver.id),
                                            "annotator_receiver_id": None,
                                            "reviewer_receiver_id": None,
                                        }
                                    ],
                                },
                            )
                    collect_traces("original")
                    async with test_engine.begin() as db:
                        limit = await db.scalar(
                            text(
                                "SELECT rolconnlimit FROM pg_roles "
                                "WHERE rolname='yoao_identity_invitation_client' AND NOT rolsuper"
                            )
                        )
                        assert isinstance(limit, int) and limit != 0
                        await db.execute(
                            text(
                                "ALTER ROLE yoao_identity_invitation_client CONNECTION LIMIT 0"
                            )
                        )
                    try:
                        await call(
                            client,
                            "controlled_connection_denial",
                            "POST",
                            "/api/v1/users/invite",
                            503,
                            json={
                                "email": f"forward-control-{suffix}@example.test",
                                "role": "employee",
                            },
                        )
                    finally:
                        async with test_engine.begin() as db:
                            await db.execute(
                                text(
                                    f"ALTER ROLE yoao_identity_invitation_client CONNECTION LIMIT {limit}"
                                )
                            )
                    async with test_engine.connect() as db:
                        assert (
                            await db.scalar(
                                text(
                                    "SELECT count(*) FROM user_invitations WHERE email=:email"
                                ),
                                {"email": f"forward-control-{suffix}@example.test"},
                            )
                            == 0
                        )
                    await call(
                        client,
                        "controlled_connection_recovery",
                        "POST",
                        "/api/v1/users/invite",
                        201,
                        json={
                            "email": f"forward-control-{suffix}@example.test",
                            "role": "employee",
                        },
                    )
            finally:
                collect_traces("control")
                await stack.stop()
                trace_offsets.clear()
            snapshot.touch(mode=0o600)
            snapshot.write_bytes(
                _postgres_tool(
                    test_engine,
                    [
                        "pg_dump",
                        "-U",
                        "owner",
                        "-Fc",
                        "--dbname",
                        str(test_engine.url.database),
                    ],
                )
            )
            assert snapshot.read_bytes().startswith(b"PGDMP")
            evidence["backup_sha256"] = hashlib.sha256(
                snapshot.read_bytes()
            ).hexdigest()
            async with _restored_database(test_engine, snapshot) as clone:
                try:
                    await stack.start(clone)
                    async with httpx.AsyncClient(
                        base_url=stack.api_origin,
                        trust_env=False,
                        timeout=40,
                        headers={"Authorization": "Bearer " + token},
                    ) as client:
                        for _ in range(32):
                            await call(
                                client, "restored_profile", "GET", "/api/v1/auth/me"
                            )
                finally:
                    collect_traces("restored")
                    await stack.stop()
        failures = [
            row for row in evidence["calls"] if row["status"] != row["expected"]
        ]
        assert not failures, failures
        for rpc in (
            "/v1/identity/authenticate",
            "/v1/identity/accounts/execute",
            "/v1/identity/invitations/execute",
            "/v1/identity/offboarding/execute",
        ):
            assert not trace or any(
                row.get("rpc") == rpc and row["phase"] == "api_http"
                for row in evidence["traces"]
            ), rpc
        if trace:
            assert any(
                row["stage"] == "control"
                and row["phase"] == "connect"
                and row.get("sqlstate") == "53300"
                for row in evidence["traces"]
            )
            for phase in ("owner_http", "api_http"):
                assert any(
                    row["stage"] == "control"
                    and row["phase"] == phase
                    and row.get("status") == 503
                    for row in evidence["traces"]
                )
    finally:
        receipt.touch(mode=0o600)
        receipt.write_text(json.dumps(evidence, indent=2) + "\n")
        record_property("forwarded_rpc_evidence", str(receipt))
        if stack is not None:
            await stack.stop()
        snapshot.unlink(missing_ok=True)
        inbox.close()


async def test_complete_identity_migration_roundtrip(test_engine):
    _verify_postgres(test_engine.url)
    assert test_engine.url.database.endswith("_test")
    environment = dict(os.environ)
    environment["MIGRATION_DATABASE_URL"] = test_engine.url.render_as_string(
        hide_password=False
    )
    environment.pop("YOAO_WORKTREE_MODE", None)
    environment["AAP_MIGRATION_VALIDATION_OWNER"] = "identity-module-validator:" + str(
        uuid.uuid4()
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "rtk",
            "proxy",
            sys.executable,
            str(ROOT / "scripts/validate_migrations.py"),
            "--json",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
    )
    report = json.loads(result.stdout)
    (ROOT / ".worktree/identity-module-migration-validation.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    assert result.returncode == 0 and report["result"] == "passed", report
    assert report["head"] == MIGRATION_HEAD
    assert report["cleanup_errors"] == report["leftover_owned_databases"] == []
