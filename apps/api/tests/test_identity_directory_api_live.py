"""AUTH-only API routes consume the selected Identity directory owner."""

from __future__ import annotations

import csv
import io
import os
import secrets
import uuid

import httpx
import pytest
from sqlalchemy import text, update
from sqlalchemy.engine import make_url

from aap_identity.security import create_access_token
from app.db.models.user import User
from tests.factory import create_user
from tests.test_identity_admission_live import (
    _dsn,
    _start_api,
    _start_container_identity,
    _token,
    _world,
)
from tests.test_identity_directory_live import (
    _add_large_directory_catalog,
    _auth_token,
    _cleanup_directory,
    _seed_directory_world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_DIRECTORY_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity directory verifier",
    ),
]


def _auth_dsn(test_engine):
    return (
        make_url(_dsn("AUTH", test_engine))
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )


@pytest.mark.parametrize("binding", ["remote_container", "dedicated_dsn"])
async def test_auth_only_api_routes_use_directory_owner(
    test_engine, tmp_path, binding, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    world = await _seed_directory_world(maker, actor, suffix)
    actor_ids = world["actor_ids"]
    project_ids = world["project_ids"]
    group_ids = world["group_ids"]
    identity = api = None
    async with maker() as db:
        actor_email = (await db.get(User, actor)).email
    try:
        async with maker.begin() as db:
            formula_user = await create_user(
                db,
                "employee",
                f"=formula-{suffix}@example.test",
                "=SUM(1,1)",
            )
        actor_ids.append(formula_user.id)
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
                enable_directory=True,
            )
            identity_origin = identity.origin
        else:
            identity_origin = ""
            configuration = {
                "IDENTITY_AUTH_DATABASE_URL": _dsn("AUTH", test_engine),
                "IDENTITY_DIRECTORY_DATABASE_URL": _dsn("DIRECTORY", test_engine),
            }
        api = _start_api(
            tmp_path,
            secret,
            control,
            identity_origin,
            database_url=_auth_dsn(test_engine),
            configuration=configuration,
        )

        super_headers = {"Authorization": f"Bearer {_token(actor, secret)}"}
        pa_token = _auth_token(world["pa"], secret)
        pa_headers = {"Authorization": f"Bearer {pa_token}"}
        async with (
            httpx.AsyncClient(
                base_url=api.origin,
                trust_env=False,
                timeout=120,
                headers=super_headers,
            ) as super_admin,
            httpx.AsyncClient(
                base_url=api.origin,
                trust_env=False,
                timeout=90,
                headers=pa_headers,
            ) as project_admin,
        ):
            # Legacy list and the query/stat/export family intentionally keep
            # their distinct project-admin visibility policies.
            pa_list = await project_admin.get(
                "/api/v1/users",
                params={
                    "status": "active",
                    "project_id": str(world["foreign_project"].id),
                },
            )
            assert pa_list.status_code == 200, pa_list.text
            pa_list_ids = {row["id"] for row in pa_list.json()}
            assert str(world["pa"].id) in pa_list_ids
            assert str(world["users"]["both_member"].id) in pa_list_ids
            assert str(world["users"]["foreign_member"].id) not in pa_list_ids
            assert str(world["users"]["external"].id) not in pa_list_ids
            assert str(actor) not in pa_list_ids

            candidates = await project_admin.get(
                "/api/v1/users",
                params={
                    "role": "employee",
                    "status": "active",
                    "project_id": str(world["foreign_project"].id),
                },
            )
            assert candidates.status_code == 200, candidates.text
            candidate_ids = {row["id"] for row in candidates.json()}
            assert str(world["users"]["external"].id) in candidate_ids
            assert str(world["users"]["foreign_member"].id) in candidate_ids
            assert str(world["users"]["inactive_outside"].id) not in candidate_ids

            pa_query = await project_admin.get(
                "/api/v1/users/query",
                params={"page": 1, "page_size": 500, "status": "all"},
            )
            assert pa_query.status_code == 200, pa_query.text
            pa_query_body = pa_query.json()
            pa_items = {item["id"]: item for item in pa_query_body["items"]}
            assert str(world["users"]["own_member"].id) in pa_items
            assert pa_items[str(world["users"]["own_member"].id)]["is_managed"] is True
            assert (
                pa_items[str(world["users"]["own_member"].id)]["is_lifecycle_managed"]
                is True
            )
            assert (
                pa_items[str(world["users"]["both_member"].id)]["is_lifecycle_managed"]
                is False
            )
            assert pa_items[str(world["users"]["external"].id)]["is_managed"] is True
            assert (
                pa_items[str(world["users"]["external"].id)]["is_lifecycle_managed"]
                is True
            )
            assert pa_items[str(actor)]["is_managed"] is False
            assert str(world["users"]["inactive_outside"].id) not in pa_items
            assert (
                pa_items[str(world["users"]["task_lock_user"].id)]["is_managed"] is True
            )
            assert (
                pa_items[str(world["users"]["task_lock_user"].id)][
                    "is_lifecycle_managed"
                ]
                is False
            )

            intersection = await project_admin.get(
                "/api/v1/users/query",
                params={
                    "page": 1,
                    "page_size": 50,
                    "status": "all",
                    "project_id": str(world["foreign_project"].id),
                },
            )
            assert intersection.status_code == 200, intersection.text
            assert {item["id"] for item in intersection.json()["items"]} == {
                str(world["users"]["both_member"].id)
            }

            unicode_search = await project_admin.get(
                "/api/v1/users/query",
                params={
                    "page": 1,
                    "page_size": 50,
                    "status": "all",
                    "search": "\u3000ali_e%\u00a0",
                },
            )
            assert unicode_search.status_code == 200, unicode_search.text
            assert str(world["users"]["external"].id) in {
                item["id"] for item in unicode_search.json()["items"]
            }

            legacy_role = await project_admin.get(
                "/api/v1/users", params={"role": "annotator", "status": "all"}
            )
            assert legacy_role.status_code == 400
            assert legacy_role.json()["detail"] == "非法平台角色: annotator"

            sa_project_list = await super_admin.get(
                "/api/v1/users",
                params={
                    "status": "all",
                    "project_id": str(world["own_project"].id),
                },
            )
            assert sa_project_list.status_code == 200, sa_project_list.text
            assert {row["id"] for row in sa_project_list.json()} == {
                str(world["users"][key].id)
                for key in (
                    "own_member",
                    "both_member",
                    "inactive_own",
                    "legacy_member",
                    "task_lock_user",
                )
            }

            pa_stats = await project_admin.get(
                "/api/v1/users/stats", params={"status": "all"}
            )
            assert pa_stats.status_code == 200, pa_stats.text
            assert pa_stats.json()["total"] == pa_query_body["total"]

            await _add_large_directory_catalog(maker, actor_ids, suffix, count=540)

            # The super-admin surface must return an uncapped list and page.
            listed = await super_admin.get("/api/v1/users", params={"status": "all"})
            assert listed.status_code == 200, listed.text
            assert len(listed.content) > 64 * 1024
            listed_rows = listed.json()
            assert len(listed_rows) > 500
            public_user_fields = {
                "id",
                "email",
                "name",
                "role",
                "group_name",
                "group_id",
                "status",
                "is_active",
                "disabled_kind",
                "disabled_at",
                "disabled_by",
                "disabled_reason",
                "email_verified_at",
                "last_login_at",
                "last_seen_at",
                "password_admin_reset_at",
                "deactivation_requested_at",
                "deactivation_scheduled_at",
                "preferences",
                "avatar_ref",
                "created_at",
            }
            assert all(set(row) == public_user_fields for row in listed_rows)
            assert all(
                "password_hash" not in row and "credential_generation" not in row
                for row in listed_rows
            )
            assert "9007199254740993" in listed.text
            assert "0.12345678901234568" in listed.text

            page_one = await super_admin.get(
                "/api/v1/users/query",
                params={"page": 1, "page_size": 500, "status": "all"},
            )
            assert page_one.status_code == 200, page_one.text
            assert len(page_one.json()["items"]) == 500
            assert page_one.json()["pages"] >= 2
            page_two = await super_admin.get(
                "/api/v1/users/query",
                params={"page": 2, "page_size": 500, "status": "all"},
            )
            assert page_two.status_code == 200, page_two.text
            assert len(page_two.json()["items"]) > 0
            assert page_two.json()["total"] == page_one.json()["total"]
            sa_flags = await super_admin.get(
                "/api/v1/users/query",
                params={
                    "page": 1,
                    "page_size": 50,
                    "status": "all",
                    "role": "super_admin",
                },
            )
            assert sa_flags.status_code == 200, sa_flags.text
            actor_item = next(
                item for item in sa_flags.json()["items"] if item["id"] == str(actor)
            )
            assert actor_item["is_managed"] is True
            assert actor_item["is_lifecycle_managed"] is True

            json_export_request_id = str(uuid.uuid4())
            json_export = await super_admin.get(
                "/api/v1/users/export",
                params={"format": "json", "status": "all"},
                headers={"X-Request-ID": json_export_request_id},
            )
            assert json_export.status_code == 200, json_export.text
            assert json_export.headers["content-type"].startswith("application/json")
            assert (
                'attachment; filename="users_'
                in json_export.headers["content-disposition"]
            )
            assert len(json_export.content) > 64 * 1024
            json_body = json_export.json()
            assert json_body["_export_meta"]["count"] == len(json_body["users"])
            assert len(json_body["users"]) > 500
            assert json_body["_export_meta"]["exported_by"] == actor_email
            assert json_body["_export_meta"]["request_id"] == json_export_request_id
            assert all(
                set(row)
                == {
                    "id",
                    "email",
                    "name",
                    "role",
                    "group_name",
                    "group_id",
                    "status",
                    "created_at",
                }
                for row in json_body["users"]
            )

            csv_export = await super_admin.get(
                "/api/v1/users/export",
                params={"format": "csv", "status": "all"},
            )
            assert csv_export.status_code == 200, csv_export.text
            assert csv_export.headers["content-type"].startswith("text/csv")
            assert (
                'attachment; filename="users_'
                in csv_export.headers["content-disposition"]
            )
            assert csv_export.content.startswith(b"\xef\xbb\xbf")
            csv_body = csv_export.content.decode("utf-8-sig")
            csv_lines = csv_body.splitlines()
            assert csv_lines[0].startswith("# Exported by:")
            assert csv_lines[2].startswith("# Request ID:")
            csv_rows = list(csv.reader(io.StringIO(csv_body)))
            assert len(csv_rows) > 504
            assert csv_rows[3] == [
                "id",
                "email",
                "name",
                "role",
                "group_name",
                "group_id",
                "status",
                "created_at",
            ]
            assert any(row[1].startswith("'=formula-") for row in csv_rows[4:])
            assert any(row[2] == "'=SUM(1,1)" for row in csv_rows[4:])

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
                            "'identity_auth_client','public.projects','SELECT')"
                        )
                    )
                    is False
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT pg_catalog.has_table_privilege("
                            "'identity_directory_client','public.users','SELECT')"
                        )
                    )
                    is False
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM public.identity_directory_export_receipts "
                            "WHERE actor_id=:actor"
                        ),
                        {"actor": actor},
                    )
                    == 2
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM public.audit_logs "
                            "WHERE actor_id=:actor AND action='user.export'"
                        ),
                        {"actor": actor},
                    )
                    == 2
                )

            async with maker.begin() as db:
                await db.execute(
                    update(User)
                    .where(User.id == world["pa"].id)
                    .values(credential_generation=1)
                )
            stale_pa = await project_admin.get("/api/v1/users/query")
            assert stale_pa.status_code == 401, stale_pa.text
            current_pa_token = create_access_token(
                str(world["pa"].id),
                "project_admin",
                secret=secret,
                gen=1,
                expire_minutes=15,
            )
            current_pa = await super_admin.get(
                "/api/v1/users/query",
                headers={"Authorization": f"Bearer {current_pa_token}"},
                params={"page": 1, "page_size": 10},
            )
            assert current_pa.status_code == 200, current_pa.text

        record_property(
            "directory_api_boundary",
            binding + ",AUTH-only API DB,4 REST reads,legacy+query PA scopes,"
            "CSV+JSON+audit,>500 rows,large pages,credential generation fence",
        )
    finally:
        if api is not None:
            api.stop()
        if identity is not None:
            identity.stop()
        await _cleanup_directory(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            group_ids=group_ids,
        )
