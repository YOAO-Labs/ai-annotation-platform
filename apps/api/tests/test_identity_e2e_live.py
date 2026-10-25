"""Cold installed-API qualification for guarded Identity and Workspace fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from aap_workspace.e2e_fixture_contract import e2e_fixture_schema

from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.workspace_import_cli_helpers import CliRun, WorkspaceImportCli
from tests.test_workspace_consumer_profile_native import _PRIVATE as _WORKSPACE_PRIVATE

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_E2E_FIXTURE_LIVE") != "1",
        reason="requires the explicitly enabled owned Identity E2E verifier",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
_FIXTURE_MEDIA = ROOT / "docs-site/public/home/sam-tools/smart-point.webm"
_FIXTURE_SCHEMA = "aap_identity_fixture_v1"
_FIXTURE_VIEW = f"{_FIXTURE_SCHEMA}.account_profiles"
_ORDINARY_FUNCTIONS = {
    "read_actor_facts(uuid)",
    "lock_accounts_share(uuid[],boolean)",
    "lock_accounts_update(uuid[],boolean)",
    "lock_accounts_key_share(uuid[],boolean)",
    "check_accepted_api_key(uuid,uuid,text[])",
}
_ORDINARY_VIEWS = {
    "aap_identity_v1.account_profiles",
    "aap_identity_v1.invitation_audit_targets",
}
_FIXTURE_FUNCTIONS = (
    "aap_identity_fixture_v1.create_account(uuid,text,text,text,text)",
    "aap_identity_fixture_v1.mark_manual_inactive(uuid,timestamptz,uuid)",
    "aap_identity_fixture_v1.create_invitations(jsonb)",
    "aap_identity_fixture_v1.delete_reset_tokens(uuid[])",
    "aap_identity_fixture_v1.delete_invitations(uuid[])",
    "aap_identity_fixture_v1.delete_organization_members(uuid[])",
    "aap_identity_fixture_v1.delete_accounts(boolean,text[])",
)
_PRIVATE_TABLES = (
    "public.users",
    "public.user_invitations",
    "public.password_reset_tokens",
    "public.organization_members",
)
_USER_OUT_FIELDS = {
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
_USER_OUT_COLUMN_ORDER = (
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
)


async def _function_set(db, role: str, schema: str) -> set[str]:
    return set(
        (
            await db.scalars(
                text(
                    "SELECT proc.proname || '(' || "
                    "replace(pg_catalog.oidvectortypes(proc.proargtypes), ' ', '') || ')' "
                    "FROM pg_catalog.pg_proc AS proc "
                    "JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace "
                    "WHERE ns.nspname=:schema "
                    "AND pg_catalog.has_function_privilege(:role,proc.oid,'EXECUTE')"
                ),
                {"role": role, "schema": schema},
            )
        ).all()
    )


async def _view_set(db, role: str) -> set[str]:
    return set(
        (
            await db.scalars(
                text(
                    "SELECT ns.nspname || '.' || relation.relname "
                    "FROM pg_catalog.pg_class AS relation "
                    "JOIN pg_catalog.pg_namespace AS ns ON ns.oid=relation.relnamespace "
                    "WHERE ns.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$' "
                    "AND relation.relkind='v' "
                    "AND pg_catalog.has_table_privilege(:role,relation.oid,'SELECT')"
                ),
                {"role": role},
            )
        ).all()
    )


async def _identity_schema_set(db, role: str) -> set[str]:
    return set(
        (
            await db.scalars(
                text(
                    "SELECT namespace.nspname FROM pg_catalog.pg_namespace AS namespace "
                    "WHERE namespace.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$' "
                    "AND pg_catalog.has_schema_privilege(:role,namespace.oid,'USAGE')"
                ),
                {"role": role},
            )
        ).all()
    )


async def _identity_function_count(db, role: str) -> int:
    return int(
        await db.scalar(
            text(
                "SELECT count(*) FROM pg_catalog.pg_proc AS proc "
                "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=proc.pronamespace "
                "WHERE namespace.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$' "
                "AND pg_catalog.has_function_privilege(:role,proc.oid,'EXECUTE')"
            ),
            {"role": role},
        )
    )


async def _assert_identity_profile(test_engine, *, create_allowed: bool = True) -> None:
    _verify_postgres(test_engine.url)
    assert _USER_OUT_FIELDS == set(_USER_OUT_COLUMN_ORDER)
    async with test_engine.connect() as db:
        for role, fixture_count in (
            ("legacy_runtime", 7 if create_allowed else 6),
            ("identity_api_consumer", 0),
        ):
            expected_schemas = {"aap_identity_v1"}
            if role == "legacy_runtime":
                expected_schemas.add(_FIXTURE_SCHEMA)
            assert await _identity_schema_set(db, role) == expected_schemas
            assert await _identity_function_count(db, role) == 5 + fixture_count
            assert (
                await _function_set(db, role, "aap_identity_v1") == _ORDINARY_FUNCTIONS
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_proc AS proc "
                        "JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace "
                        "WHERE ns.nspname=:schema "
                        "AND pg_catalog.has_function_privilege(:role,proc.oid,'EXECUTE')"
                    ),
                    {"role": role, "schema": _FIXTURE_SCHEMA},
                )
                == fixture_count
            )
            for signature in _FIXTURE_FUNCTIONS:
                assert await db.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege(CAST(:role AS name),"
                        "CAST(:signature AS regprocedure),'EXECUTE')"
                    ),
                    {"role": role, "signature": signature},
                ) is (
                    role == "legacy_runtime"
                    and (create_allowed or signature != _FIXTURE_FUNCTIONS[0])
                )

        columns = await db.scalar(
            text(
                "SELECT pg_catalog.array_agg(attribute.attname::text "
                "ORDER BY attribute.attnum) "
                "FROM pg_catalog.pg_attribute AS attribute "
                "WHERE attribute.attrelid=CAST(:view AS regclass) "
                "AND attribute.attnum>0 AND NOT attribute.attisdropped"
            ),
            {"view": _FIXTURE_VIEW},
        )
        assert tuple(columns) == _USER_OUT_COLUMN_ORDER

        for role in ("legacy_runtime", "identity_api_consumer"):
            expected_views = set(_ORDINARY_VIEWS)
            if role == "legacy_runtime":
                expected_views.add(_FIXTURE_VIEW)
            assert await _view_set(db, role) == expected_views
            assert await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege(CAST(:role AS name),"
                    "CAST(:view AS regclass),'SELECT')"
                ),
                {"role": role, "view": _FIXTURE_VIEW},
            ) is (role == "legacy_runtime")
            assert await db.scalar(
                text(
                    "SELECT pg_catalog.has_schema_privilege(CAST(:role AS name),"
                    "CAST(:schema AS name),'USAGE')"
                ),
                {"role": role, "schema": _FIXTURE_SCHEMA},
            ) is (role == "legacy_runtime")
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege(CAST(:role AS name),"
                    "CAST(:view AS regclass),"
                    "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                ),
                {"role": role, "view": _FIXTURE_VIEW},
            )
            for relation in _PRIVATE_TABLES:
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(CAST(:role AS name),"
                        "CAST(:relation AS regclass),"
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                    ),
                    {"role": role, "relation": relation},
                ), (role, relation)
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_any_column_privilege(CAST(:role AS name),"
                        "CAST(:relation AS regclass),'SELECT,INSERT,UPDATE,REFERENCES')"
                    ),
                    {"role": role, "relation": relation},
                ), (role, relation)

        assert (
            await db.scalar(
                text(
                    "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname='legacy_runtime'"
                )
            )
            is False
        )
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM pg_catalog.pg_auth_members "
                    "WHERE member=(SELECT oid FROM pg_catalog.pg_roles "
                    "WHERE rolname='legacy_runtime')"
                )
            )
            == 0
        )


def _write_probe(source_root: Path) -> None:
    source_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    probe = source_root / "identity_e2e_probe.py"
    probe.write_text(_PROBE_SOURCE, encoding="utf-8")
    probe.chmod(0o400)


def _probe_command(mode: str, *arguments: str) -> tuple[str, ...]:
    return ("python", "/fixture/identity_e2e_probe.py", mode, *arguments)


async def _run_probe(
    cli: WorkspaceImportCli,
    command: tuple[str, ...],
    record_property,
    key: str,
) -> tuple[CliRun, dict]:
    run = await asyncio.to_thread(cli.run_command, command)
    record_property(key, run.evidence)
    assert not run.timed_out, run.evidence
    assert run.exit_code == 0, run.evidence
    for line in reversed(run.stdout.splitlines()):
        if line.startswith("AAP_IDENTITY_E2E_RESULT="):
            return run, json.loads(line.partition("=")[2])
    raise AssertionError(f"installed HTTP probe emitted no marker: {run.evidence}")


def _namespace_emails(namespace: str) -> list[str]:
    locals_ = (
        "admin",
        "anno",
        "rev",
        "owner-a",
        "owner-b",
        "owner-c",
        "employee",
        "peer",
        "spare",
        "viewer",
        "viewer-unassigned",
        "solo",
        "filter-active",
        "filter-inactive",
    )
    return [f"{local}-{namespace}@e2e.test" for local in locals_]


async def _assert_namespace_absent(test_engine, namespace: str) -> None:
    project_names = [
        f"E2E Owned {namespace}",
        f"E2E Lidar {namespace}",
        *(f"E2E Project Roles {key} {namespace}" for key in "ABCD"),
    ]
    project_display_ids = [
        *(f"P-PR-{key}-{namespace}" for key in "ABCD"),
        *(f"P-FI-{key}-{namespace}" for key in ("I", "V", "P", "L", "OA", "OB")),
    ]
    async with test_engine.connect() as db:
        assert (
            await db.scalar(
                text("SELECT count(*) FROM public.users WHERE email=ANY(:emails)"),
                {"emails": _namespace_emails(namespace)},
            )
            == 0
        )
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.projects "
                    "WHERE name=ANY(:names) OR display_id=ANY(:display_ids)"
                ),
                {"names": project_names, "display_ids": project_display_ids},
            )
            == 0
        )


async def _assert_namespace_user_count(test_engine, namespace: str, expected: int):
    async with test_engine.connect() as db:
        assert (
            await db.scalar(
                text("SELECT count(*) FROM public.users WHERE email=ANY(:emails)"),
                {"emails": _namespace_emails(namespace)},
            )
            == expected
        )


async def _insert_reset_token(test_engine, *, user_id: str, email: str) -> str:
    token_id = uuid.uuid4()
    token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    async with test_engine.begin() as db:
        await db.execute(
            text(
                "INSERT INTO public.password_reset_tokens "
                "(id,user_id,token_hash,issued_email,expires_at) "
                "VALUES (:id,:user_id,:token_hash,:email,"
                "pg_catalog.clock_timestamp()+interval '1 hour')"
            ),
            {
                "id": token_id,
                "user_id": uuid.UUID(user_id),
                "token_hash": token_hash,
                "email": email,
            },
        )
    return str(token_id)


async def _assert_reset_token_exists(test_engine, token_id: str, expected: bool):
    async with test_engine.connect() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM public.password_reset_tokens "
                    "WHERE id=CAST(:token_id AS uuid))"
                ),
                {"token_id": token_id},
            )
            is expected
        )


async def _insert_invitation_count(test_engine, ids: list[str], expected: int):
    async with test_engine.connect() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.user_invitations "
                    "WHERE id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [uuid.UUID(value) for value in ids]},
            )
            == expected
        )


def _object_keys(owner_s3, bucket: str, prefix: str) -> set[str]:
    continuation_token = None
    keys: set[str] = set()
    while True:
        params = {"Bucket": bucket, "Prefix": prefix}
        if continuation_token:
            params["ContinuationToken"] = continuation_token
        response = owner_s3.list_objects_v2(**params)
        keys.update(item["Key"] for item in response.get("Contents", []))
        if not response.get("IsTruncated"):
            return keys
        continuation_token = response.get("NextContinuationToken")
        if not continuation_token:
            raise RuntimeError(
                "Owned E2E object listing omitted its continuation token"
            )


def _delete_owned_objects(owner_s3, bucket: str, prefixes: list[str]) -> None:
    for prefix in prefixes:
        versions = _object_versions(owner_s3, bucket, prefix)
        for offset in range(0, len(versions), 1000):
            response = owner_s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": versions[offset : offset + 1000]},
            )
            if response.get("Errors"):
                raise RuntimeError("Owned E2E object-version cleanup failed")


def _object_versions(owner_s3, bucket: str, prefix: str) -> list[dict[str, str]]:
    versions = []
    paginator = owner_s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        versions.extend(
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for item in page.get("Versions", [])
        )
        versions.extend(
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for item in page.get("DeleteMarkers", [])
        )
    return versions


async def _install_create_account_gate(
    test_engine, *, email: str, application_name: str
) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:12]
    function = f"public.identity_e2e_create_pause_{suffix}"
    trigger = f"identity_e2e_create_pause_{suffix}"
    body = f"""CREATE FUNCTION {function}() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $identity_e2e_pause$
BEGIN
    IF NEW.email = '{email}' THEN
        PERFORM pg_catalog.set_config('application_name','{application_name}',true);
        PERFORM pg_catalog.pg_sleep(10);
    END IF;
    RETURN NEW;
END
$identity_e2e_pause$"""
    async with test_engine.begin() as db:
        await db.execute(text(body))
        await db.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE INSERT ON public.users "
                f"FOR EACH ROW EXECUTE FUNCTION {function}()"
            )
        )
    return function, trigger


async def _drop_create_account_gate(test_engine, gate: tuple[str, str]) -> None:
    function, trigger = gate
    async with test_engine.begin() as db:
        await db.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON public.users"))
        await db.execute(text(f"DROP FUNCTION IF EXISTS {function}()"))


async def _create_account_activity(test_engine, application_name: str) -> dict | None:
    async with test_engine.connect() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT pid,usename,application_name,host(client_addr) AS client_host,"
                        "state,wait_event_type,wait_event,"
                        "position('aap_identity_fixture_v1.create_account' in query)>0 "
                        "AS native_call FROM pg_catalog.pg_stat_activity "
                        "WHERE datname=current_database() AND usename='legacy_runtime' "
                        "AND application_name=:tag"
                    ),
                    {"tag": application_name},
                )
            )
            .mappings()
            .first()
        )
    return None if row is None else dict(row)


async def _wait_for_create_account_gate(
    test_engine, cli: WorkspaceImportCli, pending: asyncio.Task, application_name: str
) -> dict:
    deadline = asyncio.get_running_loop().time() + 30
    while cli.container_id is None and not pending.done():
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("owned API probe did not create its verified container")
        await asyncio.sleep(0.03)
    if pending.done():
        pytest.fail(
            f"API probe exited before the Identity INSERT gate: {pending.result()}"
        )

    while asyncio.get_running_loop().time() < deadline:
        activity = await _create_account_activity(test_engine, application_name)
        if (
            activity is not None
            and activity["state"] == "active"
            and activity["wait_event_type"] == "Timeout"
            and activity["wait_event"] == "PgSleep"
            and activity["native_call"] is True
        ):
            address = await asyncio.to_thread(cli.running_database_client_address)
            assert activity["client_host"] == address
            return activity
        if pending.done():
            pytest.fail(
                f"API probe exited before the Identity INSERT gate: {pending.result()}"
            )
        await asyncio.sleep(0.05)
    pytest.fail("installed API did not reach the restricted create_account call")


async def _wait_for_backend_exit(test_engine, backend_pid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        async with test_engine.connect() as db:
            alive = await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
                    "WHERE pid=:pid)"
                ),
                {"pid": backend_pid},
            )
        if not alive:
            return
        await asyncio.sleep(0.05)
    pytest.fail("stopped API container left its transaction in PostgreSQL")


async def _assert_workspace_profile(test_engine):
    functions = e2e_fixture_schema()["x-native-capabilities"]["functions"]
    signatures = {function["signature"].replace(" ", "") for function in functions}
    async with test_engine.connect() as db:
        for role, operator in (
            ("legacy_runtime", True),
            ("identity_api_consumer", False),
        ):
            for relation in _WORKSPACE_PRIVATE:
                assert not await db.scalar(
                    text(
                        "SELECT has_table_privilege(:role,:table,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR has_any_column_privilege(:role,:table,'SELECT,INSERT,UPDATE,REFERENCES')"
                    ),
                    {"role": role, "table": "public." + relation},
                ), (role, relation)
            assert not await db.scalar(
                text(
                    "SELECT has_schema_privilege(:role,'aap_workspace_fixture_v1','CREATE')"
                ),
                {"role": role},
            )
            assert (
                bool(
                    await db.scalar(
                        text(
                            "SELECT has_table_privilege(:role,'aap_workspace_fixture_v1.templates','SELECT')"
                        ),
                        {"role": role},
                    )
                )
                is operator
            )
            expected = (
                {signature.partition(".")[2] for signature in signatures}
                if operator
                else set()
            )
            assert await _function_set(db, role, "aap_workspace_fixture_v1") == expected
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT p.oid::regprocedure::text AS signature,r.rolname AS owner,p.prosecdef "
                        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner "
                        "WHERE n.nspname='aap_workspace_fixture_v1'"
                    )
                )
            )
            .mappings()
            .all()
        )
        assert {row["signature"].replace(" ", "") for row in rows} == signatures
        assert all(
            row["owner"] == "workspace_code" and row["prosecdef"] for row in rows
        )


async def test_installed_e2e_routes_use_fenced_identity_fixture_capabilities(
    test_engine, tmp_path, record_property
):
    _verify_postgres(test_engine.url)
    await _assert_identity_profile(test_engine)
    await _assert_workspace_profile(test_engine)
    role_url = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    assert role_url.drivername == "postgresql+asyncpg"
    assert role_url.username == "legacy_runtime"
    assert (role_url.host, role_url.port, role_url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert str(role_url.database).lower().endswith(("_test", "_e2e"))

    resources, _main_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    datasets_bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    comment_bucket = resources["buckets"]["MINIO_BUCKET"]
    assert not _object_keys(owner_s3, comment_bucket, "comment-attachments/"), (
        "owned comment-attachment prefix must be empty before cold acceptance"
    )
    minio_url = urlsplit(endpoint_url())
    assert (
        minio_url.scheme in {"http", "https"}
        and minio_url.hostname
        and minio_url.username is None
        and minio_url.password is None
        and minio_url.path in {"", "/"}
        and not minio_url.query
        and not minio_url.fragment
    )
    namespaces = [uuid.uuid4().hex[:8] for _ in range(3)]
    denied_ns, cancel_ns, neighbor_ns = namespaces
    source_root = tmp_path / "identity-e2e-probe"
    _write_probe(source_root)
    host = (
        f"[{minio_url.hostname}]" if ":" in minio_url.hostname else minio_url.hostname
    )
    minio_authority = f"{host}:{minio_url.port or 9000}"
    api_s3_access_key = os.environ.get("AAP_MODULE_DATA_API_BOOTSTRAP_S3_ACCESS_KEY")
    api_s3_secret_key = os.environ.get("AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY")
    assert api_s3_access_key and api_s3_secret_key, (
        "verifier did not provide its owned API storage principal"
    )
    assert api_s3_access_key != os.environ.get("MINIO_ACCESS_KEY")
    assert api_s3_secret_key != os.environ.get("MINIO_SECRET_KEY")
    cli: WorkspaceImportCli | None = None
    gate: tuple[str, str] | None = None
    pending_cancel: asyncio.Task | None = None
    create_signature = _FIXTURE_FUNCTIONS[0]
    capability_revoked = False
    prefixes = [
        f"e2e/owned/{namespace}/{suffix}/"
        for namespace in namespaces
        for suffix in ("image", "project-roles", "filtering")
    ] + [f"e2e/lidar/{namespace}/" for namespace in namespaces]
    mask_content_prefix = "raster-masks/sha256/"
    assert not _object_versions(owner_s3, comment_bucket, mask_content_prefix), (
        "owned raster-mask prefix must be empty before cold acceptance"
    )
    for prefix in prefixes:
        assert not _object_versions(owner_s3, datasets_bucket, prefix), (
            "owned fixture prefix must be empty before cold acceptance",
            prefix,
        )

    try:
        cli = WorkspaceImportCli(
            test_engine,
            tmp_path,
            source_root=source_root,
            buckets=resources["buckets"],
            minio_authority=minio_authority,
            minio_use_ssl=minio_url.scheme == "https",
            s3_access_key=api_s3_access_key,
            s3_secret_key=api_s3_secret_key,
        )
        await asyncio.to_thread(cli.start)
        await _assert_identity_profile(test_engine)

        disabled, _ = await _run_probe(
            cli,
            _probe_command("disabled"),
            record_property,
            "identity_e2e_routes_disabled",
        )
        assert "disabled routes absent" in disabled.stdout

        try:
            async with test_engine.begin() as owner:
                assert (
                    await owner.scalar(
                        text(
                            "SELECT pg_catalog.has_function_privilege('legacy_runtime',"
                            "CAST(:signature AS regprocedure),'EXECUTE')"
                        ),
                        {"signature": create_signature},
                    )
                    is True
                )
                await owner.execute(
                    text(
                        f"REVOKE EXECUTE ON FUNCTION {create_signature} FROM legacy_runtime"
                    )
                )
                capability_revoked = True
            await _assert_identity_profile(test_engine, create_allowed=False)
            denied, denied_result = await _run_probe(
                cli,
                _probe_command("denied", denied_ns),
                record_property,
                "identity_e2e_missing_create_capability",
            )
            assert denied_result["status_code"] == 500, denied.evidence
            assert (
                "permission denied for function create_account" in denied.stderr.lower()
            ), denied.evidence
            await _assert_namespace_absent(test_engine, denied_ns)
        finally:
            if capability_revoked:
                async with test_engine.begin() as owner:
                    await owner.execute(
                        text(
                            f"GRANT EXECUTE ON FUNCTION {create_signature} TO legacy_runtime"
                        )
                    )
                capability_revoked = False

        await _assert_identity_profile(test_engine)

        workspace_create = "aap_workspace_fixture_v1.create_project(jsonb)"
        try:
            async with test_engine.begin() as owner:
                await owner.execute(
                    text(
                        f"REVOKE EXECUTE ON FUNCTION {workspace_create} FROM legacy_runtime"
                    )
                )
            denied, denied_result = await _run_probe(
                cli,
                _probe_command("denied", denied_ns),
                record_property,
                "workspace_e2e_missing_create_capability",
            )
            assert denied_result["status_code"] == 500, denied.evidence
            assert (
                "permission denied for function create_project" in denied.stderr.lower()
            ), denied.evidence
            await _assert_namespace_absent(test_engine, denied_ns)
        finally:
            async with test_engine.begin() as owner:
                await owner.execute(
                    text(
                        f"GRANT EXECUTE ON FUNCTION {workspace_create} TO legacy_runtime"
                    )
                )
        await _run_probe(
            cli,
            _probe_command("recover", denied_ns),
            record_property,
            "workspace_e2e_create_recovered",
        )
        await _assert_namespace_absent(test_engine, denied_ns)
        await _assert_workspace_profile(test_engine)

        application_name = f"aap-id-e2e-{uuid.uuid4().hex[:20]}"
        gate = await _install_create_account_gate(
            test_engine,
            email=f"admin-{cancel_ns}@e2e.test",
            application_name=application_name,
        )
        pending_cancel = asyncio.create_task(
            asyncio.to_thread(cli.run_command, _probe_command("cancel", cancel_ns))
        )
        try:
            activity = await _wait_for_create_account_gate(
                test_engine, cli, pending_cancel, application_name
            )
            backend_pid = activity["pid"]
            assert activity["usename"] == "legacy_runtime"
            assert activity["application_name"] == application_name
            assert activity["native_call"] is True
            record_property(
                "identity_e2e_physical_cancellation_gate",
                {
                    "backend_pid": backend_pid,
                    "role": activity["usename"],
                    "application_name": activity["application_name"],
                    "client_host": activity["client_host"],
                    "wait_event": activity["wait_event"],
                    "native_call": activity["native_call"],
                },
            )
            await _assert_namespace_absent(test_engine, cancel_ns)
            assert await asyncio.to_thread(cli.stop_current_container) is True
            cancelled = await asyncio.wait_for(
                asyncio.shield(pending_cancel), timeout=35
            )
            record_property("identity_e2e_physical_cancellation", cancelled.evidence)
            assert cancelled.exit_code in {137, 143}, cancelled.evidence
            assert cancelled.externally_stopped is True
            assert cancelled.timed_out is False
            await _wait_for_backend_exit(test_engine, backend_pid)
            await _assert_namespace_absent(test_engine, cancel_ns)
        finally:
            if pending_cancel is not None and not pending_cancel.done():
                await asyncio.to_thread(cli.stop_current_container)
            if pending_cancel is not None:
                await asyncio.wait_for(
                    asyncio.gather(pending_cancel, return_exceptions=True), timeout=35
                )
            if gate is not None:
                await _drop_create_account_gate(test_engine, gate)
                gate = None

        recovered, _ = await _run_probe(
            cli,
            _probe_command("recover", cancel_ns),
            record_property,
            "identity_e2e_cancellation_recovery",
        )
        assert "recovered and cleaned" in recovered.stdout
        await _assert_namespace_absent(test_engine, cancel_ns)

        expected_media_sha = hashlib.sha256(_FIXTURE_MEDIA.read_bytes()).hexdigest()
        _, setup = await _run_probe(
            cli,
            _probe_command("setup", expected_media_sha, denied_ns, neighbor_ns),
            record_property,
            "identity_e2e_actual_http_consumers",
        )
        await _assert_identity_profile(test_engine)
        await _assert_namespace_user_count(test_engine, denied_ns, 5)
        await _assert_namespace_user_count(test_engine, neighbor_ns, 3)
        invitation_ids = setup["filtering"]["operations"]["invitation_ids"]
        assert len(invitation_ids) == 4
        await _insert_invitation_count(test_engine, invitation_ids, 4)
        mask_key = setup["raster"]["mask"]["object_key"]
        assert mask_key.startswith(mask_content_prefix)
        mask_versions = _object_versions(owner_s3, comment_bucket, mask_key)
        assert len(mask_versions) == 1
        assert mask_versions[0]["Key"] == mask_key
        async with test_engine.connect() as db:
            raster_annotation = (
                await db.execute(
                    text(
                        "SELECT user_id,geometry->'mask' AS mask "
                        "FROM public.annotations WHERE id=CAST(:annotation_id AS uuid)"
                    ),
                    {"annotation_id": setup["raster"]["annotation_id"]},
                )
            ).one()
        assert raster_annotation.user_id == uuid.UUID(
            setup["filtering"]["users"]["anno"]
        )
        assert raster_annotation.mask == setup["raster"]["mask"]

        token_a = await _insert_reset_token(
            test_engine,
            user_id=setup["filtering"]["users"]["admin"],
            email=setup["filtering"]["user_emails"]["admin"],
        )
        token_b = await _insert_reset_token(
            test_engine,
            user_id=setup["neighbor_admin_id"],
            email=setup["neighbor_admin_email"],
        )
        await _assert_reset_token_exists(test_engine, token_a, True)
        await _assert_reset_token_exists(test_engine, token_b, True)

        cleaned_a, cleanup_a = await _run_probe(
            cli,
            _probe_command("cleanup", denied_ns, neighbor_ns),
            record_property,
            "identity_e2e_owned_cleanup_and_neighbor_isolation",
        )
        assert "neighbor login succeeded" in cleaned_a.stdout
        assert cleanup_a["cleanup_statuses"] == [200, 200]
        await _assert_namespace_absent(test_engine, denied_ns)
        await _assert_namespace_user_count(test_engine, neighbor_ns, 3)
        await _insert_invitation_count(test_engine, invitation_ids, 0)
        await _assert_reset_token_exists(test_engine, token_a, False)
        await _assert_reset_token_exists(test_engine, token_b, True)

        cleaned_b, cleanup_b = await _run_probe(
            cli,
            _probe_command("cleanup_one", neighbor_ns),
            record_property,
            "identity_e2e_neighbor_cleanup",
        )
        assert cleanup_b["cleanup_status"] == 200
        assert "cleaned" in cleaned_b.stdout
        await _assert_namespace_absent(test_engine, neighbor_ns)
        await _assert_reset_token_exists(test_engine, token_b, False)

        _delete_owned_objects(owner_s3, datasets_bucket, prefixes)
        _delete_owned_objects(owner_s3, comment_bucket, [mask_content_prefix])
        assert not any(
            _object_versions(owner_s3, datasets_bucket, prefix) for prefix in prefixes
        )
        assert not _object_versions(owner_s3, comment_bucket, mask_content_prefix)
        assert not _object_keys(owner_s3, comment_bucket, "comment-attachments/")
    finally:
        try:
            if pending_cancel is not None and not pending_cancel.done():
                await asyncio.to_thread(cli.stop_current_container)
                await asyncio.wait_for(
                    asyncio.gather(pending_cancel, return_exceptions=True), timeout=35
                )
            if gate is not None:
                await _drop_create_account_gate(test_engine, gate)
            if capability_revoked:
                async with test_engine.begin() as owner:
                    await owner.execute(
                        text(
                            f"GRANT EXECUTE ON FUNCTION {create_signature} TO legacy_runtime"
                        )
                    )
            if cli is not None and cli.network_id is not None:
                try:
                    await _run_probe(
                        cli,
                        _probe_command("cleanup_all", *namespaces),
                        record_property,
                        "identity_e2e_final_fixture_cleanup",
                    )
                except Exception as exc:
                    record_property(
                        "identity_e2e_final_fixture_cleanup_error", str(exc)
                    )
            if cli is not None:
                await asyncio.to_thread(cli.close)
        finally:
            _delete_owned_objects(
                owner_s3,
                datasets_bucket,
                prefixes,
            )


_PROBE_SOURCE = r"""from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

import httpx
import uvicorn


def require(condition, message):
    if not condition:
        raise AssertionError(message)


mode = sys.argv[1]
os.environ["E2E_SEED_ENABLED"] = "false" if mode == "disabled" else "true"
from app.main import app  # noqa: E402

owned_path = "/api/v1/__test/seed/owned"
present = any(getattr(route, "path", None) == owned_path for route in app.routes)
require(
    present is (mode != "disabled"),
    "E2E router mount does not match its in-process flag",
)
require(
    not any("/__test/seed/" in path for path in app.openapi()["paths"]),
    "fixture route leaked into OpenAPI",
)

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
server = uvicorn.Server(
    uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
deadline = time.monotonic() + 30
while not server.started and thread.is_alive() and time.monotonic() < deadline:
    time.sleep(0.03)
require(server.started, "installed API server did not start")


def request(client, method, path, **kwargs):
    return client.request(method, path, **kwargs)


def login(client, email):
    response = request(
        client, "POST", "/api/v1/__test/seed/login", json={"email": email}
    )
    require(
        response.status_code == 200,
        f"seed/login {email}: {response.status_code} {response.text}",
    )
    body = response.json()
    require(
        set(body["user"])
        == {
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
        },
        "seed/login UserOut fields changed",
    )
    require(
        body["access_token"] and body["token_type"] == "bearer",
        "seed/login omitted its JWT",
    )
    return body


result = {}
try:
    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=150
    ) as client:
        if mode == "disabled":
            response = request(client, "GET", "/api/v1/__test/seed/peek")
            require(
                response.status_code == 404,
                f"disabled route returned {response.status_code}",
            )
            require(
                "/api/v1/__test/seed/peek"
                not in client.get("/openapi.json").json()["paths"],
                "disabled route appeared in OpenAPI",
            )
            result = {
                "status_code": response.status_code,
                "note": "disabled routes absent",
            }
        elif mode == "denied":
            namespace = sys.argv[2]
            response = request(
                client, "POST", owned_path, json={"namespace": namespace}
            )
            require(
                response.status_code == 500,
                f"missing create capability returned {response.status_code}: {response.text}",
            )
            result = {"status_code": response.status_code, "body": response.text}
        elif mode == "cancel":
            namespace = sys.argv[2]
            response = request(
                client, "POST", owned_path, json={"namespace": namespace}
            )
            raise AssertionError(
                f"owned route completed before physical cancellation: {response.status_code} {response.text}"
            )
        elif mode == "recover":
            namespace = sys.argv[2]
            created = request(client, "POST", owned_path, json={"namespace": namespace})
            require(
                created.status_code == 200,
                f"recovery owned returned {created.status_code}: {created.text}",
            )
            cleaned = request(
                client,
                "POST",
                "/api/v1/__test/seed/owned-cleanup",
                json={"namespace": namespace},
            )
            require(
                cleaned.status_code == 200,
                f"recovery cleanup returned {cleaned.status_code}: {cleaned.text}",
            )
            result = {
                "cleanup_status": cleaned.status_code,
                "note": "recovered and cleaned",
            }
        elif mode == "setup":
            expected_sha, namespace_a, namespace_b = sys.argv[2:5]
            media = Path("/app/fixture-media/smart-point.webm")
            require(media.is_file(), "installed API image omitted fixture media")
            observed_sha = hashlib.sha256(media.read_bytes()).hexdigest()
            require(
                observed_sha == expected_sha,
                "installed filtering video asset differs from source",
            )

            peek_before = request(client, "GET", "/api/v1/__test/seed/peek")
            require(
                peek_before.status_code == 200,
                f"initial seed/peek returned {peek_before.status_code}",
            )
            first = request(client, "POST", owned_path, json={"namespace": namespace_a})
            require(
                first.status_code == 200,
                f"seed/owned returned {first.status_code}: {first.text}",
            )
            admin_email = f"admin-{namespace_a}@e2e.test"
            admin_login = login(client, admin_email)
            peek = request(client, "GET", "/api/v1/__test/seed/peek")
            previous_admin = peek_before.json()["admin_email"]
            expected_admin = (
                previous_admin
                if previous_admin and not previous_admin.endswith("@e2e.test")
                else admin_email
            )
            require(
                peek.status_code == 200
                and peek.json()["admin_email"] == expected_admin,
                f"seed/peek selection changed: {peek.status_code} {peek.text}",
            )

            roles = request(
                client,
                "POST",
                "/api/v1/__test/seed/project-roles",
                json={"namespace": namespace_a},
            )
            require(
                roles.status_code == 200,
                f"seed/project-roles returned {roles.status_code}: {roles.text}",
            )
            role_data = roles.json()
            require(len(role_data["users"]) == 9, "project-roles user matrix changed")
            employee_login = login(client, role_data["employee_email"])
            require(
                employee_login["user"]["role"] == "employee",
                "project role account changed",
            )

            filtering = request(
                client,
                "POST",
                "/api/v1/__test/seed/filtering",
                json={"namespace": namespace_a},
            )
            require(
                filtering.status_code == 200,
                f"seed/filtering returned {filtering.status_code}: {filtering.text}",
            )
            filtering_data = filtering.json()
            require(
                len(filtering_data["operations"]["invitation_ids"]) == 4,
                "filtering invitation fixture changed",
            )
            inactive_email = filtering_data["operations"]["user_emails"][1]
            inactive_login = login(client, inactive_email)
            inactive_user = inactive_login["user"]
            require(
                inactive_user["is_active"] is False,
                "seed/login added an active-account gate",
            )
            require(
                inactive_user["status"] == "offline"
                and inactive_user["disabled_kind"] == "manual",
                "inactive fixture state changed",
            )
            require(
                inactive_user["disabled_at"] is not None, "inactive timestamp was lost"
            )
            require(
                inactive_user["disabled_by"] == filtering_data["users"]["admin"],
                "inactive actor changed",
            )
            require(
                inactive_user["preferences"] == {},
                "inactive profile preferences changed",
            )
            require(
                filtering_data["user_emails"]["admin"] == admin_email,
                "filtering admin identity changed",
            )

            lidar = request(
                client,
                "POST",
                "/api/v1/__test/seed/lidar",
                json={"namespace": namespace_a},
            )
            require(
                lidar.status_code == 200,
                f"seed/lidar returned {lidar.status_code}: {lidar.text}",
            )
            tracker = request(
                client,
                "POST",
                "/api/v1/__test/seed/tracker-review",
                json={
                    "task_id": filtering_data["video"]["task_ids"]["both"],
                    "user_email": filtering_data["user_emails"]["anno"],
                },
            )
            require(
                tracker.status_code == 200,
                f"seed/tracker-review returned {tracker.status_code}: {tracker.text}",
            )
            image_task_id = filtering_data["image"]["task_ids"]["same_object"]
            raster = request(
                client,
                "POST",
                "/api/v1/__test/seed/inject-raster-mask",
                json={
                    "task_id": image_task_id,
                    "user_email": filtering_data["user_emails"]["anno"],
                    "variant": "single",
                },
            )
            require(
                raster.status_code == 200,
                f"seed/inject-raster-mask returned {raster.status_code}: {raster.text}",
            )
            advanced = request(
                client,
                "POST",
                "/api/v1/__test/seed/advance_task",
                json={
                    "task_id": image_task_id,
                    "to_status": "submitted",
                    "annotator_email": filtering_data["user_emails"]["anno"],
                    "reviewer_email": filtering_data["user_emails"]["rev"],
                },
            )
            require(
                advanced.status_code == 200
                and advanced.json()["status"] == "submitted",
                f"seed/advance_task returned {advanced.status_code}: {advanced.text}",
            )

            neighbor = request(
                client, "POST", owned_path, json={"namespace": namespace_b}
            )
            require(
                neighbor.status_code == 200,
                f"neighbor seed/owned returned {neighbor.status_code}: {neighbor.text}",
            )
            neighbor_admin = login(client, neighbor.json()["admin_email"])
            result = {
                "media_sha256": observed_sha,
                "admin_user": admin_login["user"],
                "inactive_user": inactive_user,
                "project_roles_user_count": len(role_data["users"]),
                "filtering": filtering_data,
                "lidar": lidar.json(),
                "tracker": tracker.json(),
                "raster": raster.json(),
                "advance": advanced.json(),
                "neighbor_admin_id": neighbor_admin["user"]["id"],
                "neighbor_admin_email": neighbor.json()["admin_email"],
            }
        elif mode == "cleanup":
            namespace_a, namespace_b = sys.argv[2:4]
            statuses = []
            for _ in range(2):
                response = request(
                    client,
                    "POST",
                    "/api/v1/__test/seed/owned-cleanup",
                    json={"namespace": namespace_a},
                )
                statuses.append(response.status_code)
                require(
                    response.status_code == 200,
                    f"owned cleanup returned {response.status_code}: {response.text}",
                )
            neighbor = login(client, f"admin-{namespace_b}@e2e.test")
            require(neighbor["user"]["is_active"] is True, "neighbor account changed")
            result = {
                "cleanup_statuses": statuses,
                "neighbor_user_id": neighbor["user"]["id"],
                "note": "neighbor login succeeded",
            }
        elif mode == "cleanup_one":
            namespace = sys.argv[2]
            response = request(
                client,
                "POST",
                "/api/v1/__test/seed/owned-cleanup",
                json={"namespace": namespace},
            )
            require(
                response.status_code == 200,
                f"owned cleanup returned {response.status_code}: {response.text}",
            )
            result = {"cleanup_status": response.status_code, "note": "cleaned"}
        elif mode == "cleanup_all":
            statuses = []
            for namespace in sys.argv[2:]:
                response = request(
                    client,
                    "POST",
                    "/api/v1/__test/seed/owned-cleanup",
                    json={"namespace": namespace},
                )
                statuses.append(response.status_code)
                require(
                    response.status_code == 200,
                    f"final cleanup returned {response.status_code}: {response.text}",
                )
            result = {"cleanup_statuses": statuses, "note": "final fixtures cleaned"}
        else:
            raise AssertionError(f"unknown probe mode: {mode}")
    print(
        "AAP_IDENTITY_E2E_RESULT="
        + json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    )
finally:
    server.should_exit = True
    thread.join(timeout=15)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=2)
        raise RuntimeError("installed API server failed to stop")
"""
