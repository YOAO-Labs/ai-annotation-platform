"""Legacy Workspace reads retain authorization without Identity private ACLs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import shutil
import subprocess
import uuid

import httpx
import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.security import create_access_token
from app.db.models.organization import Organization, OrganizationMember
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from app.deps import get_db
from app.services.identity_gateway import IdentityGateway, get_identity_gateway
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_project_templates import _seed_template, _seed_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified disposable module-data database",
    ),
]


async def test_legacy_template_visibility_uses_only_public_identity_capabilities(
    test_engine, app_module, record_property
):
    _verify_postgres(test_engine.url)
    urls = {}
    for name, role in (
        ("IDENTITY_API_CONSUMER", "identity_api_consumer"),
        ("IDENTITY_AUTH", "identity_auth_client"),
    ):
        url = make_url(os.environ[f"AAP_MODULE_DATA_{name}_URL"])
        assert url.username == role
        assert (url.host, url.port, url.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
        urls[name] = url
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    user_ids, organization_ids, template_ids = [], [], []
    api_engine = create_async_engine(
        urls["IDENTITY_API_CONSUMER"], hide_parameters=True
    )
    api_sessions = async_sessionmaker(api_engine, expire_on_commit=False)
    previous = dict(app_module.dependency_overrides)
    try:
        async with sessions.begin() as db:
            actor = await _seed_user(db, "employee", "identity-adoption-actor")
            creator = await _seed_user(db, "employee", "identity-adoption-creator")
            user_ids = [actor.id, creator.id]
            organizations = []
            for label in ("member", "removed", "foreign"):
                organization = Organization(
                    id=uuid.uuid4(),
                    name=label,
                    slug=f"adoption-{uuid.uuid4().hex}",
                    created_by=creator.id,
                )
                db.add(organization)
                organizations.append(organization)
            await db.flush()
            organization_ids = [row.id for row in organizations]
            for index, organization in enumerate(organizations):
                db.add(
                    OrganizationMember(
                        organization_id=organization.id,
                        user_id=actor.id if index < 2 else creator.id,
                        deleted_at=datetime.now(timezone.utc) if index == 1 else None,
                    )
                )
            templates = [
                await _seed_template(
                    db,
                    creator.id,
                    name=f"organization-{index}",
                    scope="organization",
                    organization_id=organization.id,
                )
                for index, organization in enumerate(organizations)
            ]
            public = await _seed_template(db, creator.id, name="public", scope="public")
            private = await _seed_template(db, actor.id, name="private")
            template_ids = [row.id for row in templates] + [public.id, private.id]
            expected = {str(templates[0].id), str(public.id), str(private.id)}
            expected_names = {str(row.id): creator.name for row in templates + [public]}
            expected_names[str(private.id)] = actor.name
            token = create_access_token(str(actor.id), actor.role)

        async def api_db():
            async with api_sessions() as db:
                yield db

        gateway = IdentityGateway(
            auth_database_url=urls["IDENTITY_AUTH"].render_as_string(
                hide_password=False
            ),
            signing_secret=settings.secret_key,
        )
        app_module.dependency_overrides[get_db] = api_db
        app_module.dependency_overrides[get_identity_gateway] = lambda: gateway
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/project-templates",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200, response.text
        rows = response.json()
        assert {row["id"] for row in rows} == expected
        assert all(row["created_by_name"] == expected_names[row["id"]] for row in rows)

        for relation in ("users", "organizations", "organization_members"):
            async with api_sessions() as db:
                with pytest.raises(DBAPIError) as failure:
                    await db.execute(text(f"SELECT * FROM public.{relation} LIMIT 1"))
                assert failure.value.orig.sqlstate == "42501"
                await db.rollback()
        async with api_sessions() as db:
            count = await db.scalar(
                text(
                    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$' "
                    "AND has_function_privilege(current_user,p.oid,'EXECUTE')"
                )
            )
            assert count == 6
        node = shutil.which("node")
        assert node, "Node is required for the non-Python capability caller"
        container = os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"]
        program = r"""
const fs = require('node:fs'), cp = require('node:child_process');
const d = JSON.parse(fs.readFileSync(0, 'utf8'));
if (!/^[0-9a-f-]{36}$/.test(d.actor)) throw new Error('Invalid actor fixture');
const sql = `SELECT aap_identity_v1.workspace_template_query_org_ids('${d.actor}'::uuid)::text`;
const r = cp.spawnSync('docker', ['exec', '-e', 'PGPASSWORD='+d.password, d.container,
  'psql', '-X', '-h', '127.0.0.1', '-U', d.username, '-d', d.database,
  '-At', '-v', 'ON_ERROR_STOP=1', '-c', sql], {encoding:'utf8'});
if(r.status !== 0) throw new Error('Capability call failed');
const reply = JSON.parse(r.stdout);
if(reply.schema !== 'aap-identity-workspace-template-query/v1' || reply.actor_id !== d.actor)
  throw new Error('Invalid capability contract');
process.stdout.write(JSON.stringify(reply.organization_ids));
"""
        result = await asyncio.to_thread(
            subprocess.run,
            [node, "-e", program],
            input=json.dumps(
                {
                    "actor": str(actor.id),
                    "container": container,
                    "username": urls["IDENTITY_API_CONSUMER"].username,
                    "password": urls["IDENTITY_API_CONSUMER"].password,
                    "database": urls["IDENTITY_API_CONSUMER"].database,
                }
            ),
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [str(organization_ids[0])]
        record_property(
            "identity_primary_compatibility",
            "ordinary API role; six public functions; private tables denied; real HTTP and Node calls",
        )
    finally:
        app_module.dependency_overrides.clear()
        app_module.dependency_overrides.update(previous)
        await api_engine.dispose()
        async with sessions.begin() as db:
            await db.execute(
                delete(ProjectTemplate).where(ProjectTemplate.id.in_(template_ids))
            )
            await db.execute(
                delete(OrganizationMember).where(
                    OrganizationMember.organization_id.in_(organization_ids)
                )
            )
            await db.execute(
                delete(Organization).where(Organization.id.in_(organization_ids))
            )
            await db.execute(delete(User).where(User.id.in_(user_ids)))
