"""Owned backup, restore, replay, and protected downgrade checks."""

from __future__ import annotations

import os
import json
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_workspace.configuration import ConfigurationManager
from aap_workspace.configuration_contract import configuration_request
from aap_workspace.configuration_postgres import ConfigurationPostgresData
from aap_workspace.template_queries import TemplateQueryManager
from aap_workspace.template_query_contract import template_query_request
from aap_workspace.template_query_postgres import TemplateQueryPostgresData
from aap_workspace.config import RuntimeSettings
from aap_workspace.project_queries import ProjectQueryManager
from aap_workspace_contract.project_query_contract import project_query_request
from aap_workspace.project_query_postgres import ProjectQueryPostgresData
from aap_workspace.project_query_profiles import ProjectQueryIdentityProfiles
from aap_workspace.directory_queries import DirectoryQueryManager
from aap_workspace.directory_query_contract import directory_query_request
from aap_workspace.directory_query_postgres import DirectoryQueryPostgresData
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from tests.factory import create_user
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_recovery import _alembic, _postgres_tool
from tests.workspace_configuration_helpers import configuration_dsn
from tests.workspace_template_query_helpers import template_query_dsn
from tests.workspace_project_query_helpers import (
    project_query_briefs_dsn,
    project_query_dsn,
)
from tests.workspace_directory_query_helpers import directory_query_dsn


def _project_reader(test_engine, database=None):
    return ProjectQueryManager(
        ProjectQueryPostgresData(project_query_dsn(test_engine, database)),
        ProjectQueryIdentityProfiles(
            RuntimeSettings(
                project_query_identity_briefs_database_url=project_query_briefs_dsn(
                    test_engine, database
                )
            )
        ),
    )


def _directory_reader(test_engine, database=None):
    return DirectoryQueryManager(
        DirectoryQueryPostgresData(directory_query_dsn(test_engine, database)),
        ProjectQueryIdentityProfiles(
            RuntimeSettings(
                project_query_identity_briefs_database_url=project_query_briefs_dsn(
                    test_engine, database
                )
            )
        ),
    )


async def _project_counter_facts(maker, project_id):
    async with maker() as db:
        await db.execute(text("SET LOCAL ROLE workspace_code"))
        rows = await db.execute(
            text(
                "SELECT * FROM aap_labeling_v1.workspace_project_query_counters("
                "CAST(:ids AS uuid[]))"
            ),
            {"ids": [project_id]},
        )
        return [dict(row) for row in rows.mappings()]


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable Workspace configuration verifier",
    ),
]


def _request(operation, actor_id, target_id, payload, operation_id):
    return configuration_request(
        operation=operation,
        actor_id=actor_id,
        target_id=target_id,
        payload=payload,
        operation_id=operation_id,
    )


async def _execute(manager, operation, actor_id, target_id, payload, operation_id):
    request = _request(operation, actor_id, target_id, payload, operation_id)
    reply = await manager.execute(request)
    assert reply.ok is True, reply.model_dump(mode="json", by_alias=True)
    return request, reply


async def _assert_receipt_set(db, operation_ids: list[uuid.UUID]) -> None:
    for operation_id in operation_ids:
        params = {"operation_id": str(operation_id)}
        for table, expected in (
            ("workspace_configuration_operation_receipts", 1),
            ("workspace_configuration_operation_states", 0),
            ("workspace_configuration_operation_attempts", 0),
        ):
            count = await db.scalar(
                text(
                    f"SELECT count(*) FROM public.{table} "
                    "WHERE operation_id=CAST(:operation_id AS uuid)"
                ),
                params,
            )
            assert count == expected, (table, operation_id, count)


async def _cleanup_source(
    test_engine, actor_id: uuid.UUID, operation_ids: list[uuid.UUID]
) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        for operation_id in operation_ids:
            params = {"operation_id": str(operation_id)}
            for table in (
                "workspace_configuration_operation_states",
                "workspace_configuration_operation_attempts",
            ):
                await db.execute(
                    text(
                        f"DELETE FROM public.{table} "
                        "WHERE operation_id=CAST(:operation_id AS uuid)"
                    ),
                    params,
                )
            await db.execute(
                text(
                    "DELETE FROM public.workspace_configuration_operation_receipts "
                    "WHERE operation_id=CAST(:operation_id AS uuid)"
                ),
                params,
            )
        await db.execute(
            delete(ProjectTemplate).where(ProjectTemplate.created_by == actor_id)
        )
        await db.execute(delete(Project).where(Project.owner_id == actor_id))
        await db.execute(delete(User).where(User.id == actor_id))


async def test_workspace_configuration_receipts_survive_owned_restore_and_protect_downgrade(
    test_engine, tmp_path
):
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    dsn = configuration_dsn(test_engine)
    source_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    operation_ids = [uuid.uuid4() for _ in range(5)]
    actor_id = member_actor_id = template_id = deleted_template_id = project_id = None
    clone = f"aap_workspace_configuration_restore_{uuid.uuid4().hex[:10]}_test"
    ownership = f"workspace-configuration-restore:{uuid.uuid4()}"
    snapshot = tmp_path / "workspace-configuration.dump"
    database_created = False
    restored_owner = None
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )

    try:
        async with source_maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"workspace-configuration-restore-{suffix}@example.test",
                "Workspace configuration restore actor",
            )
            doomed = ProjectTemplate(
                id=uuid.uuid4(),
                display_id="PT-" + uuid.uuid4().hex[:8],
                name=f"Workspace configuration delete replay {suffix}",
                type_label="Image",
                type_key="image-det",
                created_by=actor.id,
                scope="private",
            )
            db.add(doomed)
            await db.flush()
            actor_id, deleted_template_id = actor.id, doomed.id

        manager = ConfigurationManager(ConfigurationPostgresData(dsn))
        create_template_request, create_template_reply = await _execute(
            manager,
            "create_template",
            actor_id,
            None,
            {
                "name": f"Workspace configuration accepted {suffix}",
                "type_label": "Image",
                "type_key": "image-det",
                "scope": "private",
                "box_threshold": 0.41,
            },
            operation_ids[0],
        )
        template_id = create_template_reply.result.resource_id
        update_request, update_reply = await _execute(
            manager,
            "update_template",
            actor_id,
            template_id,
            {"name": f"Workspace configuration accepted update {suffix}"},
            operation_ids[1],
        )
        later_name = f"Workspace configuration later edit {suffix}"
        await _execute(
            manager,
            "update_template",
            actor_id,
            template_id,
            {"name": later_name},
            operation_ids[2],
        )
        _, delete_template_reply = await _execute(
            manager,
            "delete_template",
            actor_id,
            deleted_template_id,
            {},
            operation_ids[3],
        )
        _, create_project_reply = await _execute(
            manager,
            "create_project",
            actor_id,
            None,
            {
                "name": f"Workspace configuration restored project {suffix}",
                "type_label": "Image",
                "type_key": "image-det",
            },
            operation_ids[4],
        )
        project_id = create_project_reply.result.resource_id
        original_project_counters = await _project_counter_facts(
            source_maker, project_id
        )
        assert len(original_project_counters) == 1
        assert original_project_counters[0]["project_id"] == project_id

        query_requests = [
            template_query_request(
                "list_templates",
                actor_id,
                "project_admin",
                filters={"scope": "private", "type_key": None, "search": None},
            ),
            template_query_request(
                "get_template", actor_id, "project_admin", target_id=template_id
            ),
            template_query_request(
                "get_template_projection",
                actor_id,
                "project_admin",
                target_id=template_id,
                configuration_receipt={
                    "operation_id": operation_ids[0],
                    "operation": "create_template",
                },
            ),
        ]
        query_data = TemplateQueryPostgresData(template_query_dsn(test_engine))
        original_query_policy = await query_data.template_query_policy()
        original_queries = [
            await TemplateQueryManager(query_data).execute(request)
            for request in query_requests
        ]
        list_filters = dict.fromkeys(
            (
                "status",
                "search",
                "type_key",
                "data_type",
                "member_id",
                "project_role",
                "created_from",
                "created_to",
            )
        )
        project_actor = (actor_id, "project_admin")
        project_requests = [
            project_query_request(
                "list_projects", *project_actor, filters=list_filters
            ),
            project_query_request(
                "query_projects",
                *project_actor,
                filters=list_filters,
                pagination={"page": "1", "page_size": 10},
            ),
            project_query_request("get_project", *project_actor, target_id=project_id),
            project_query_request("project_stats", *project_actor),
            project_query_request(
                "project_readiness", *project_actor, target_id=project_id
            ),
            project_query_request(
                "get_project_projection",
                *project_actor,
                target_id=project_id,
                projection_evidence={
                    "source": "configuration",
                    "operation_id": operation_ids[4],
                    "operation": "create_project",
                },
            ),
            project_query_request(
                "search_projects",
                *project_actor,
                filters={"q": "Workspace configuration restored project", "limit": 20},
            ),
        ]
        async with source_maker.begin() as db:
            member_actor = await create_user(
                db,
                "employee",
                f"workspace-access-restore-{suffix}@example.test",
                "Restored member",
            )
            member_actor_id = member_actor.id
            db.add(
                ProjectMember(
                    project_id=project_id,
                    user_id=actor_id,
                    assigned_by=actor_id,
                    role="annotator",
                )
            )
            db.add(
                ProjectMember(
                    project_id=project_id,
                    user_id=member_actor_id,
                    assigned_by=actor_id,
                    role="reviewer",
                    version=37,
                )
            )
        project_requests.extend(
            [
                project_query_request(
                    "get_project_access", *project_actor, target_id=project_id
                ),
                project_query_request(
                    "get_project_access",
                    member_actor_id,
                    "employee",
                    target_id=project_id,
                ),
            ]
        )
        original_project_reader = _project_reader(test_engine)
        original_projects = [
            await original_project_reader.execute(request)
            for request in project_requests
        ]
        assert all(reply.ok for reply in original_projects)
        member_access = json.loads(original_projects[-1].data_json)
        assert member_access["membership_version"] == 37
        assert member_access["project_role"] == "reviewer"

        directory_requests = [
            directory_query_request(operation, actor_id, project_id)
            for operation in ("list_members", "mention_candidates")
        ]
        original_directory = [
            await _directory_reader(test_engine).execute(request)
            for request in directory_requests
        ]
        assert all(reply.ok for reply in original_directory)

        async with source_maker() as db:
            await _assert_receipt_set(db, operation_ids)
            assert await db.get(ProjectTemplate, deleted_template_id) is None
            assert (await db.get(ProjectTemplate, template_id)).name == later_name
            assert await db.get(Project, project_id) is not None
            frozen_scope, frozen_creator = (
                await db.execute(
                    text(
                        "SELECT frozen_scope,frozen_creator_id "
                        "FROM public.workspace_configuration_operation_receipts "
                        "WHERE operation_id=CAST(:operation_id AS uuid)"
                    ),
                    {"operation_id": str(operation_ids[3])},
                )
            ).one()
            assert frozen_scope == "private" and frozen_creator == actor_id

        refused = _alembic(test_engine.url, "downgrade", "0256")
        assert refused.returncode != 0
        assert b"Cannot remove Workspace configuration while private state exists" in (
            refused.stdout + refused.stderr
        )
        async with source_maker() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM public.alembic_version"))
                == "0276"
            )
            assert await db.scalar(
                text(
                    "SELECT to_regnamespace('aap_workspace_configuration_v1') IS NOT NULL"
                )
            )
            assert (await db.get(ProjectTemplate, template_id)).name == later_name
            await _assert_receipt_set(db, operation_ids)

        snapshot.touch(mode=0o600)
        snapshot.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database],
            )
        )
        assert snapshot.read_bytes().startswith(b"PGDMP")

        _verify_postgres(test_engine.url)
        async with admin.connect() as db:
            await db.execute(text(f'CREATE DATABASE "{clone}" TEMPLATE template0'))
            database_created = True
            await db.execute(text(f"COMMENT ON DATABASE \"{clone}\" IS '{ownership}'"))
            async with test_engine.connect() as source:
                acl = (
                    await source.execute(
                        text(
                            "SELECT COALESCE(role.rolname,'PUBLIC'), "
                            "entry.privilege_type, entry.is_grantable "
                            "FROM pg_database AS database CROSS JOIN LATERAL "
                            "aclexplode(COALESCE(database.datacl, "
                            "acldefault('d',database.datdba))) AS entry "
                            "LEFT JOIN pg_roles AS role ON role.oid=entry.grantee "
                            "WHERE database.datname=current_database()"
                        )
                    )
                ).all()
            quote = db.dialect.identifier_preparer.quote_identifier
            await db.execute(text(f'REVOKE ALL ON DATABASE "{clone}" FROM PUBLIC'))
            for grantee, privilege, grantable in acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote(grantee)
                suffix_sql = " WITH GRANT OPTION" if grantable else ""
                await db.execute(
                    text(
                        f'GRANT {privilege} ON DATABASE "{clone}" TO {role}{suffix_sql}'
                    )
                )
        _verify_postgres(test_engine.url)
        _postgres_tool(
            test_engine,
            ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", clone],
            body=snapshot.read_bytes(),
        )

        clone_dsn = configuration_dsn(test_engine, clone)
        _verify_postgres(make_url(clone_dsn))
        restored_owner = create_async_engine(
            test_engine.url.set(database=clone), hide_parameters=True
        )
        restored_maker = async_sessionmaker(restored_owner, expire_on_commit=False)
        assert (
            await _project_counter_facts(restored_maker, project_id)
            == original_project_counters
        )
        restored_data = ConfigurationPostgresData(clone_dsn)
        restored_manager = ConfigurationManager(restored_data)
        await restored_data.configuration_policy()
        restored_query_data = TemplateQueryPostgresData(
            template_query_dsn(test_engine, clone)
        )
        assert (
            await restored_query_data.template_query_policy() == original_query_policy
        )
        restored_query_manager = TemplateQueryManager(restored_query_data)
        assert [
            await restored_query_manager.execute(request) for request in query_requests
        ] == original_queries
        restored_project_reader = _project_reader(test_engine, clone)
        await restored_project_reader.data.project_query_policy()
        async with restored_project_reader.data.transaction() as transaction:
            assert (
                await transaction.connection.fetchval("SHOW transaction_read_only")
                == "on"
            )
        assert [
            await restored_project_reader.execute(request)
            for request in project_requests
        ] == original_projects
        restored_directory_reader = _directory_reader(test_engine, clone)
        async with restored_directory_reader.data.transaction() as transaction:
            assert (
                await transaction.connection.fetchval("SHOW transaction_read_only")
                == "on"
            )
        assert [
            await restored_directory_reader.execute(request)
            for request in directory_requests
        ] == original_directory
        async with restored_maker() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM public.alembic_version"))
                == "0276"
            )
            template = await db.get(ProjectTemplate, template_id)
            project = await db.get(Project, project_id)
            assert template is not None and template.name == later_name
            assert project is not None
            assert await db.get(ProjectTemplate, deleted_template_id) is None
            await _assert_receipt_set(db, operation_ids)

        assert (
            await restored_manager.execute(create_template_request)
            == create_template_reply
        )
        assert await restored_manager.execute(update_request) == update_reply
        assert (
            await restored_manager.execute(
                _request(
                    "delete_template",
                    actor_id,
                    deleted_template_id,
                    {},
                    operation_ids[3],
                )
            )
            == delete_template_reply
        )

        async with restored_maker() as db:
            assert (await db.get(ProjectTemplate, template_id)).name == later_name
            assert await db.get(ProjectTemplate, deleted_template_id) is None
            assert await db.get(Project, project_id) is not None
            await _assert_receipt_set(db, operation_ids)

        async with restored_owner.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET is_active=false "
                    "WHERE id=CAST(:actor_id AS uuid)"
                ),
                {"actor_id": str(actor_id)},
            )
        revoked = await restored_manager.execute(create_template_request)
        assert revoked.ok is False
        assert revoked.error.status_code == 401
        assert revoked.error.headers == {"WWW-Authenticate": "Bearer"}
        revoked_directory = await restored_directory_reader.execute(
            directory_requests[0]
        )
        assert not revoked_directory.ok and revoked_directory.error.status_code == 401
        # A previously admitted response readback retains its actor snapshot;
        # it does not adopt the command owner's new current-account fence.
        assert (
            await restored_query_manager.execute(query_requests[2])
            == original_queries[2]
        )
        assert (
            await restored_project_reader.execute(project_requests[5])
            == original_projects[5]
        )
        async with restored_maker() as db:
            assert (await db.get(ProjectTemplate, template_id)).name == later_name
            await _assert_receipt_set(db, operation_ids)
    finally:
        if restored_owner is not None:
            await restored_owner.dispose()
        if database_created:
            _verify_postgres(test_engine.url)
            async with admin.connect() as db:
                marker = await db.scalar(
                    text(
                        "SELECT shobj_description(oid,'pg_database') "
                        "FROM pg_database WHERE datname=:name"
                    ),
                    {"name": clone},
                )
                assert marker == ownership
                assert not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                        "WHERE datname=:name "
                        "AND backend_type IS DISTINCT FROM 'autovacuum worker')"
                    ),
                    {"name": clone},
                )
                await db.execute(text(f'DROP DATABASE "{clone}"'))
        await admin.dispose()
        if actor_id is not None:
            await _cleanup_source(test_engine, actor_id, operation_ids)
        if member_actor_id is not None:
            async with source_maker.begin() as db:
                await db.execute(delete(User).where(User.id == member_actor_id))
        snapshot.unlink(missing_ok=True)
