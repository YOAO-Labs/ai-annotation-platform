"""Owner-process fixtures and restricted Identity business HTTP consumers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import inspect
import json
import os
import subprocess
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_identity.e2e_fixture_contract import ACCOUNT_PROFILE_FIELDS
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.project import Project
from app.db.models.user import User
from app.services.identity_e2e_fixtures import IdentityE2EFixtures
from app.config import settings
from tests import seed_owned_scenarios as owned_cases
from tests import seed_router_scenarios as seed_cases
from tests.identity_e2e_support import (
    ROOT,
    IDS,
    STAMP,
    _committed_owner,
    accounts,
    snapshot,
    committed_owner as committed_owner,
    e2e_consumer as e2e_consumer,
    fenced_http as fenced_http,
    fixture_and_fenced_http as fixture_and_fenced_http,
    scenario_rle_bindings as scenario_rle_bindings,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_training_dataset_references import (
    committed_rle_bindings as committed_rle_bindings,
)
from tests.test_workspace_consumer_profile_native import _grants, _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data verifier",
    ),
]


async def test_owned_cleanup_removes_preference_receipt_without_touching_audit(
    test_engine, fixture_and_fenced_http, fenced_http, monkeypatch
):
    account_url = os.environ["AAP_MODULE_DATA_IDENTITY_ACCOUNTS_URL"]
    monkeypatch.setattr(settings, "identity_account_database_url_file", "")
    monkeypatch.setattr(settings, "identity_account_database_url", account_url)
    namespace = "receiptcase"
    keeper_id = uuid.uuid4()
    keeper_email = f"cleanup-audit-{keeper_id.hex}@example.test"
    async with test_engine.begin() as db:
        await db.execute(
            text(
                "INSERT INTO users(id,email,name,password_hash,role,is_active,preferences) "
                "VALUES (:id,:email,'Audit keeper','opaque','employee',true,'{}'::jsonb)"
            ),
            {"id": keeper_id, "email": keeper_email},
        )
        audit_id = await db.scalar(
            text(
                "INSERT INTO audit_logs(actor_id,actor_email,actor_role,action) "
                "VALUES (:id,:email,'employee','fixture.keep') RETURNING id"
            ),
            {"id": keeper_id, "email": keeper_email},
        )
    try:
        created = await fixture_and_fenced_http.post(
            "/api/v1/__test/seed/project-roles", json={"namespace": namespace}
        )
        assert created.status_code == 200, created.text
        email = created.json()["owner_email_a"]
        login = await fixture_and_fenced_http.post(
            "/api/v1/__test/seed/login", json={"email": email}
        )
        assert login.status_code == 200, login.text
        changed = await fenced_http.patch(
            "/api/v1/auth/me/preferences",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
            json={"ui": {"theme": "dark"}},
        )
        assert changed.status_code == 200, changed.text
        async with test_engine.connect() as db:
            actor_id = await db.scalar(
                text("SELECT id FROM users WHERE email=:email"), {"email": email}
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM identity_account_operation_receipts WHERE actor_id=:id"
                    ),
                    {"id": actor_id},
                )
                == 1
            )
        cleaned = await fixture_and_fenced_http.post(
            "/api/v1/__test/seed/owned-cleanup", json={"namespace": namespace}
        )
        assert cleaned.status_code == 200, cleaned.text
        async with test_engine.connect() as db:
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM users WHERE id=:id"), {"id": actor_id}
                )
                == 0
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM identity_account_operation_receipts WHERE actor_id=:id"
                    ),
                    {"id": actor_id},
                )
                == 0
            )
            assert (
                await db.execute(
                    text(
                        "SELECT actor_id,actor_email,actor_role,action FROM audit_logs WHERE id=:id"
                    ),
                    {"id": audit_id},
                )
            ).one() == (keeper_id, keeper_email, "employee", "fixture.keep")
    finally:
        async with test_engine.begin() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(
                text("DELETE FROM audit_logs WHERE id=:id"), {"id": audit_id}
            )
            await db.execute(text("DELETE FROM users WHERE id=:id"), {"id": keeper_id})


async def test_owned_cleanup_rolls_back_when_an_external_dataset_references_actor(
    test_engine, fixture_and_fenced_http, fenced_http, e2e_consumer
):
    namespace = "refcase"
    role = e2e_consumer.url.username
    assert role.startswith("workspace_consumer_") and role.replace("_", "").isalnum()
    async with test_engine.begin() as db:
        await db.execute(
            text(
                "GRANT EXECUTE ON FUNCTION "
                "aap_assets_v1.create_catalog_dataset(uuid,uuid,jsonb) "
                f'TO "{role}"'
            )
        )
        assert not await db.scalar(
            text(
                "SELECT has_table_privilege(:role,'public.users','SELECT') OR "
                "has_any_column_privilege(:role,'public.users','SELECT')"
            ),
            {"role": role},
        )
    dataset_id = None
    dataset_name = f"External cleanup reference {uuid.uuid4().hex}"
    try:
        created = await fixture_and_fenced_http.post(
            "/api/v1/__test/seed/project-roles", json={"namespace": namespace}
        )
        assert created.status_code == 200, created.text
        email = created.json()["owner_email_a"]
        login = await fixture_and_fenced_http.post(
            "/api/v1/__test/seed/login", json={"email": email}
        )
        assert login.status_code == 200, login.text
        dataset = await fenced_http.post(
            "/api/v1/datasets",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
            json={"name": dataset_name, "data_type": "image"},
        )
        assert dataset.status_code == 201, dataset.text
        dataset_id = uuid.UUID(dataset.json()["id"])
        async with test_engine.connect() as db:
            actor_id = await db.scalar(
                text("SELECT id FROM users WHERE email=:email"), {"email": email}
            )
            assert (
                await db.scalar(
                    text("SELECT created_by FROM datasets WHERE id=:id"),
                    {"id": dataset_id},
                )
                == actor_id
            )
            projects_before = await db.scalar(
                text("SELECT count(*) FROM projects WHERE name LIKE :name"),
                {"name": f"E2E Project Roles % {namespace}"},
            )
            assert projects_before == 4
        blocked = await fixture_and_fenced_http.post(
            "/api/v1/__test/seed/owned-cleanup", json={"namespace": namespace}
        )
        assert blocked.status_code == 409, blocked.text
        async with test_engine.connect() as db:
            assert (
                await db.scalar(
                    text("SELECT created_by FROM datasets WHERE id=:id"),
                    {"id": dataset_id},
                )
                == actor_id
            )
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM users WHERE email LIKE :email"),
                    {"email": f"%-{namespace}@e2e.test"},
                )
                == 9
            )
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM projects WHERE name LIKE :name"),
                    {"name": f"E2E Project Roles % {namespace}"},
                )
                == projects_before
            )
    finally:
        async with test_engine.begin() as db:
            await db.execute(
                text("DELETE FROM datasets WHERE name=:name"), {"name": dataset_name}
            )
        cleaned = await fixture_and_fenced_http.post(
            "/api/v1/__test/seed/owned-cleanup", json={"namespace": namespace}
        )
        assert cleaned.status_code == 200, cleaned.text


@pytest.mark.parametrize(
    "grant_file",
    [
        "identity-e2e-fixture-grants.sql",
        "identity-recording-fixture-grants.sql",
        "identity-scale-seed-grants.sql",
        "identity-seed-operator-grants.sql",
    ],
)
async def test_fixture_grants_reject_direct_presence_access(test_engine, grant_file):
    async with _world(test_engine, apply=False) as (_owner, _consumer, role, _url):
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    f'GRANT UPDATE (status) ON public.identity_account_presence TO "{role}"'
                )
            )
        result = await _grants(test_engine, ROOT / "infra/docker" / grant_file, role)
        assert result.returncode != 0
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    f'REVOKE UPDATE (status) ON public.identity_account_presence FROM "{role}"'
                )
            )
        result = await _grants(test_engine, ROOT / "infra/docker" / grant_file, role)
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "scenario",
    [
        seed_cases.seed_reset_returns_fixture_payload,
        seed_cases.project_roles_seed_builds_multi_project_employee_matrix,
        seed_cases.project_roles_seed_is_idempotent,
        seed_cases.seed_reset_is_idempotent_with_singleton_pool,
        seed_cases.seed_login_after_reset_returns_jwt,
        seed_cases.seed_login_unknown_email_404,
        seed_cases.seed_raster_mask_project_opt_in_and_content_fixtures,
        seed_cases.seed_raster_mask_media_canvas_preserves_item_dimensions,
        seed_cases.seed_reset_preserves_dev_data,
        owned_cases.owned_cleanup_is_exact_idempotent_and_neighbour_safe,
        owned_cases.owned_rebuild_is_retry_safe,
        owned_cases.owned_filtering_fixture_is_namespaced_and_converges,
        owned_cases.owned_lidar_fixture_is_namespaced,
        owned_cases.owned_project_roles_fixture_is_namespaced,
        owned_cases.owned_cleanup_removes_own_takeover_actor_and_keeps_neighbours,
    ],
    ids=lambda scenario: "test_" + scenario.__name__,
)
async def test_owner_fixture_scenarios_with_fenced_identity_business_http(
    scenario,
    scenario_rle_bindings,
    fixture_and_fenced_http,
    committed_owner,
    record_property,
):
    values = {
        "httpx_client": fixture_and_fenced_http,
        "db_session": committed_owner,
    }
    arguments = inspect.signature(scenario).parameters
    assert set(arguments).issubset(values)
    await scenario(**{name: values[name] for name in arguments})
    record_property(
        "identity_owner_fixture_fenced_business_case", "test_" + scenario.__name__
    )


@pytest.mark.parametrize("case_error", [False, True], ids=["normal", "case-error"])
async def test_committed_owner_cleans_only_its_committed_keepers(
    test_engine, fixture_and_fenced_http, case_error
):
    """Existing, concurrent and rollback-reused identities are not owned."""
    models = (User, Project, MLBackendRegistry)
    all_ids = {model: set() for model in models}
    suffix = uuid.uuid4().hex

    async def make_rows(db, label, user_id=None):
        user = User(
            id=user_id,
            email=f"{label}-{suffix}@example.com",
            name=label,
            password_hash="x",
        )
        db.add(user)
        await db.flush()
        all_ids[User].add(user.id)
        project = Project(
            display_id=f"P-{uuid.uuid4().hex[:16]}",
            name=label,
            type_label="image-det",
            type_key="image-det",
            owner_id=user.id,
        )
        backend = MLBackendRegistry(
            name=label, url=f"http://{label}-{suffix}.test:9999"
        )
        db.add_all([project, backend])
        await db.flush()
        all_ids[Project].add(project.id)
        all_ids[MLBackendRegistry].add(backend.id)
        return {User: user.id, Project: project.id, MLBackendRegistry: backend.id}

    class CaseError(Exception):
        pass

    try:
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as peer:
            existing = await make_rows(peer, "existing")
            await peer.commit()
        owned = concurrent = uncommitted_id = None

        async def run_case():
            nonlocal owned, concurrent, uncommitted_id
            async with _committed_owner(test_engine, fixture_and_fenced_http) as owner:
                # Loading and modifying existing rows must not claim them.
                for model, id_ in existing.items():
                    row = await owner.get(model, id_)
                    row.name = "updated"
                owned = await make_rows(owner, "owned")
                await owner.commit()

                rolled_back = User(
                    email=f"rolled-back-{suffix}@example.com",
                    name="rolled back",
                    password_hash="x",
                )
                owner.add(rolled_back)
                await owner.flush()
                rolled_back_id = rolled_back.id
                await owner.rollback()

                # This independent session commits after tracking started and
                # reuses an ID whose owner-session insertion was rolled back.
                async with async_sessionmaker(
                    test_engine, expire_on_commit=False
                )() as peer:
                    concurrent = await make_rows(peer, "concurrent", rolled_back_id)
                    await peer.commit()

                uncommitted = User(
                    email=f"uncommitted-{suffix}@example.com",
                    name="uncommitted",
                    password_hash="x",
                )
                owner.add(uncommitted)
                await owner.flush()
                uncommitted_id = uncommitted.id
                all_ids[User].add(uncommitted_id)
                if case_error:
                    raise CaseError

        if case_error:
            # The same cleanup must run when a scenario raises.
            with pytest.raises(CaseError):
                await run_case()
        else:
            await run_case()
        async with async_sessionmaker(test_engine)() as observer:
            for model in models:
                assert await observer.get(model, owned[model]) is None
                assert (await observer.get(model, existing[model])).name == "updated"
                assert await observer.get(model, concurrent[model]) is not None
            assert await observer.get(User, uncommitted_id) is None
    finally:
        async with test_engine.begin() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(delete(Project).where(Project.id.in_(all_ids[Project])))
            await db.execute(
                text(
                    "DELETE FROM ml_backend_service_pools WHERE legacy_instance_id=ANY(:ids)"
                ),
                {"ids": list(all_ids[MLBackendRegistry])},
            )
            await db.execute(
                delete(MLBackendRegistry).where(
                    MLBackendRegistry.id.in_(all_ids[MLBackendRegistry])
                )
            )
            await db.execute(delete(User).where(User.id.in_(all_ids[User])))


async def test_node_fixture_contract_and_borrowed_rollback(
    test_engine, e2e_consumer, record_property
):
    async with (
        accounts(test_engine),
        organization_member(test_engine) as organization_id,
    ):
        before = await snapshot(test_engine)
        result = await asyncio.to_thread(
            subprocess.run,
            ["node", str(ROOT / "apps/api/tests/fixtures/identity_e2e_peer.mjs")],
            input=json.dumps(
                {
                    "database_url": e2e_consumer.url.set(
                        drivername="postgresql"
                    ).render_as_string(hide_password=False),
                    "target_id": str(IDS["target"]),
                    "peer_id": str(uuid.uuid4()),
                    "invitation_id": str(uuid.uuid4()),
                    "fields": ACCOUNT_PROFILE_FIELDS,
                }
            ),
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert output == {
            "ok": True,
            "private_identity_denied": True,
            "duplicate_preserved": True,
            "full_profile_fields": 21,
            "rolled_back": True,
        }
        assert await snapshot(test_engine) == before
        async with test_engine.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM organization_members WHERE organization_id=:id AND user_id=:user_id"
                    ),
                    {"id": organization_id, "user_id": IDS["target"]},
                )
                == 1
            )
        async with e2e_consumer.connect() as db:
            transaction = await db.begin()
            assert (
                await db.scalar(
                    text(
                        "SELECT aap_identity_fixture_v1.mark_manual_inactive(:id,:at,:by)"
                    ),
                    {"id": IDS["target"], "at": STAMP, "by": IDS["neighbor"]},
                )
                == 1
            )
            await transaction.rollback()
        assert await snapshot(test_engine) == before
        async with test_engine.connect() as db:
            signatures = (
                "aap_identity_v1.read_accepted_api_key_facts(uuid,uuid)",
                "aap_identity_v1.read_actor_facts(uuid)",
                "aap_identity_v1.lock_accounts_share(uuid[],boolean)",
                "aap_identity_v1.lock_accounts_update(uuid[],boolean)",
                "aap_identity_v1.lock_accounts_key_share(uuid[],boolean)",
                "aap_identity_v1.check_accepted_api_key(uuid,uuid,text[])",
                "aap_identity_v1.is_jti_revoked(uuid)",
                "aap_identity_v1.get_credential_generation(uuid)",
            )
            expected = {
                await db.scalar(
                    text("SELECT CAST(:signature AS regprocedure)::oid"),
                    {"signature": signature},
                )
                for signature in signatures
            }
            actual = set(
                (
                    await db.scalars(
                        text(
                            "SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                            "WHERE n.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$' "
                            "AND has_function_privilege('identity_api_consumer',p.oid,'EXECUTE')"
                        )
                    )
                ).all()
            )
            assert actual == expected
            for relation in (
                "users",
                "identity_account_presence",
                "user_invitations",
                "password_reset_tokens",
                "organization_members",
            ):
                assert not await db.scalar(
                    text(
                        "SELECT has_table_privilege('identity_api_consumer', :relation, "
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
                        "OR has_any_column_privilege('identity_api_consumer', :relation, "
                        "'SELECT,INSERT,UPDATE,REFERENCES')"
                    ),
                    {"relation": "public." + relation},
                )
            views = set(
                (
                    await db.scalars(
                        text(
                            "SELECT n.nspname || '.' || c.relname FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$' "
                            "AND c.relkind='v' AND has_table_privilege('identity_api_consumer',c.oid,'SELECT')"
                        )
                    )
                ).all()
            )
            assert views == {
                "aap_identity_v1.account_profiles",
                "aap_identity_v1.invitation_audit_targets",
            }
        record_property("identity_e2e_node", output)


@asynccontextmanager
async def organization_member(engine):
    organization_id = uuid.uuid4()
    try:
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO organizations(id,name,slug,created_by) VALUES (:id,'Owned fixture org',:slug,:by)"
                ),
                {
                    "id": organization_id,
                    "slug": "fixture-" + organization_id.hex,
                    "by": IDS["dev"],
                },
            )
            await db.execute(
                text(
                    "INSERT INTO organization_members(id,organization_id,user_id,role) VALUES (:id,:org,:user_id,'member')"
                ),
                {"id": uuid.uuid4(), "org": organization_id, "user_id": IDS["target"]},
            )
        yield organization_id
    finally:
        async with engine.begin() as db:
            await db.execute(
                text("DELETE FROM organization_members WHERE organization_id=:id"),
                {"id": organization_id},
            )
            await db.execute(
                text("DELETE FROM organizations WHERE id=:id"), {"id": organization_id}
            )


async def test_python_fixture_write_waits_and_borrows_transaction(
    test_engine, e2e_consumer, record_property
):
    async with accounts(test_engine):
        async with test_engine.begin() as db:
            await db.execute(
                text("UPDATE users SET is_active=true WHERE id=:id"),
                {"id": IDS["target"]},
            )
            await db.execute(
                text(
                    "UPDATE identity_account_presence SET status='online' WHERE user_id=:id"
                ),
                {"id": IDS["target"]},
            )
        before = await snapshot(test_engine)
        async with async_sessionmaker(e2e_consumer, expire_on_commit=False)() as db:
            service = IdentityE2EFixtures(db)
            user = await service.find("admin-probea@e2e.test")
            assert user.is_active is True
            await service.mark_manual_inactive(
                user, disabled_at=STAMP, disabled_by=IDS["dev"]
            )
            assert user.is_active is False and not db.is_modified(user)
            await db.rollback()
        assert await snapshot(test_engine) == before

        async def write():
            async with async_sessionmaker(e2e_consumer, expire_on_commit=False)() as db:
                service = IdentityE2EFixtures(db)
                user = await service.find("admin-probea@e2e.test")
                assert (
                    await service.mark_manual_inactive(
                        user, disabled_at=STAMP, disabled_by=IDS["dev"]
                    )
                    == 1
                )
                await db.commit()

        pending = None
        async with test_engine.connect() as db:
            transaction = await db.begin()
            await db.execute(
                text("SELECT id FROM users WHERE id=:id FOR UPDATE"),
                {"id": IDS["target"]},
            )
            try:
                pending = asyncio.create_task(write())
                waiting = None
                deadline = asyncio.get_running_loop().time() + 15
                while asyncio.get_running_loop().time() < deadline:
                    assert not pending.done(), (
                        pending.result() if pending.done() else None
                    )
                    async with test_engine.connect() as observer:
                        waiting = (
                            (
                                await observer.execute(
                                    text(
                                        "SELECT pid,wait_event_type,wait_event FROM pg_stat_activity WHERE datname=current_database() AND usename=:role AND state='active' AND wait_event_type='Lock' AND position('mark_manual_inactive' in query)>0"
                                    ),
                                    {"role": e2e_consumer.url.username},
                                )
                            )
                            .mappings()
                            .first()
                        )
                    if waiting:
                        break
                    await asyncio.sleep(0.03)
                assert waiting is not None
                record_property("identity_e2e_row_lock", dict(waiting))
            finally:
                await transaction.rollback()
                if pending is not None:
                    await asyncio.wait_for(asyncio.shield(pending), 15)
        async with test_engine.connect() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT is_active,status,disabled_by FROM users JOIN identity_account_presence ON user_id=users.id WHERE users.id=:id"
                    ),
                    {"id": IDS["target"]},
                )
            ).one()
            assert tuple(row) == (False, "offline", IDS["dev"])


@pytest.mark.parametrize("suffix", ["_TEST", "_dev"])
async def test_actual_fixture_function_database_guard(
    test_engine, suffix, record_property
):
    _verify_postgres(test_engine.url)
    name = "aap_e2e_guard_" + uuid.uuid4().hex[:12] + suffix
    marker = "e2e-guard:" + uuid.uuid4().hex
    async with test_engine.connect() as raw:
        db = await raw.execution_options(isolation_level="AUTOCOMMIT")
        await db.execute(text(f'CREATE DATABASE "{name}"'))
        await db.execute(text(f"COMMENT ON DATABASE \"{name}\" IS '{marker}'"))
        oid = await db.scalar(
            text("SELECT oid FROM pg_database WHERE datname=:name"), {"name": name}
        )
    engine = create_async_engine(
        test_engine.url.set(database=name), hide_parameters=True
    )
    try:
        async with engine.begin() as db:
            await db.execute(text("CREATE SCHEMA aap_identity_fixture_v1"))
            await db.execute(
                text(
                    "CREATE TABLE users(id uuid PRIMARY KEY,email text,name text,password_hash text,credential_generation bigint,role text,status text,is_active boolean,preferences jsonb)"
                )
            )
            statements = (
                (ROOT / "apps/api/alembic/sql/0305_identity_e2e_fixtures.sql")
                .read_text()
                .split("-- aap-statement-break")
            )
            function = next(
                statement
                for statement in statements
                if statement.strip().startswith(
                    "CREATE FUNCTION aap_identity_fixture_v1.create_account("
                )
            )
            await db.execute(text(function))
        async with engine.begin() as db:
            call = text(
                "SELECT aap_identity_fixture_v1.create_account(:id,'guard@e2e.test','Guard','opaque','employee')"
            )
            identifier = uuid.uuid4()
            if suffix == "_TEST":
                assert await db.scalar(call, {"id": identifier}) == identifier
            else:
                with pytest.raises(DBAPIError) as error:
                    await db.scalar(call, {"id": identifier})
                assert error.value.orig.sqlstate == "42501"
        record_property(
            "identity_e2e_database_guard",
            {
                "database": name,
                "allowed": suffix == "_TEST",
                "scope": "exact frozen create_account function; minimal fixture table, not a full migration",
            },
        )
    finally:
        await engine.dispose()
        async with test_engine.connect() as raw:
            db = await raw.execution_options(isolation_level="AUTOCOMMIT")
            row = (
                await db.execute(
                    text(
                        "SELECT oid,shobj_description(oid,'pg_database') FROM pg_database WHERE datname=:name"
                    ),
                    {"name": name},
                )
            ).one()
            assert tuple(row) == (oid, marker)
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM pg_stat_activity WHERE datname=:name"),
                    {"name": name},
                )
                == 0
            )
            await db.execute(text(f'DROP DATABASE "{name}"'))
