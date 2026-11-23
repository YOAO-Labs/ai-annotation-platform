"""Class reads use only the approved functions of the ordinary LOGIN."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from yoao_labeling.class_query_postgres import CLASS_QUERY_FUNCTIONS, CLASS_QUERY_SCHEMA
from tests.labeling_class_query_helpers import (
    assert_owned_test_database,
    bound_api_client,
    cleanup_world,
    fixture_data,
    request_sample,
    seed_legacy_world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


@pytest.mark.parametrize("login", ["worktree", "lan"])
async def test_class_query_last_function_denial_never_returns_partial_counts(
    test_engine, app_module, monkeypatch, tmp_path, login
):
    await assert_owned_test_database(test_engine)
    baseline = next(
        sample
        for sample in fixture_data()["samples"]
        if sample["label"] == "target_project_owner_aggregation"
    )
    world = await seed_legacy_world(test_engine, app_module, monkeypatch, tmp_path)
    try:
        async with bound_api_client(
            test_engine, app_module, monkeypatch, login=login
        ) as (client, runtime, rpc):
            async with runtime.connect() as db:
                expected_role = {
                    "worktree": "legacy_runtime",
                    "lan": "identity_api_consumer",
                }[login]
                assert tuple(
                    (await db.execute(text("SELECT session_user,current_user"))).one()
                ) == (expected_role, expected_role)
                functions = await db.execute(
                    text(
                        "SELECT p.proname,pg_catalog.oidvectortypes(p.proargtypes) "
                        "FROM pg_catalog.pg_proc p "
                        "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname=:schema "
                        "AND pg_catalog.has_function_privilege(current_user,p.oid,'EXECUTE')"
                    ),
                    {"schema": CLASS_QUERY_SCHEMA},
                )
                assert {tuple(row) for row in functions} == CLASS_QUERY_FUNCTIONS
                for denied in (
                    "SELECT class_name,attributes FROM public.annotations LIMIT 1",
                    "UPDATE public.annotations SET class_name=class_name WHERE false",
                    "SELECT yoao_labeling_v1._video_writer_exact_keys('{}',ARRAY[]::text[])",
                    "SELECT yoao_jobs_v1.video_writer_authority('policy','{}')",
                    "SELECT * FROM yoao_labeling_v1.video_writer_receipts LIMIT 1",
                ):
                    with pytest.raises(DBAPIError) as error:
                        await db.exec_driver_sql(denied)
                    assert error.value.orig.sqlstate == "42501"
                    await db.rollback()

            role = runtime.dialect.identifier_preparer.quote_identifier(expected_role)
            function = f"{CLASS_QUERY_SCHEMA}.attribute_rows(uuid)"
            try:
                async with test_engine.begin() as db:
                    await db.exec_driver_sql(
                        f"REVOKE EXECUTE ON FUNCTION {function} FROM {role}"
                    )
                refused = await request_sample(client, world, baseline, native_rpc=rpc)
                assert refused["borrowed_labeling_rpc_phases"] == [
                    "class_query_policy",
                    "class_counts",
                    "attribute_rows",
                ]
                assert refused["response"] == {
                    "status": 503,
                    "headers": {"content-type": "application/json"},
                    "body": {"detail": "标注统计服务暂不可用"},
                }
            finally:
                async with test_engine.begin() as db:
                    await db.exec_driver_sql(
                        f"GRANT EXECUTE ON FUNCTION {function} TO {role}"
                    )
            recovered = await request_sample(client, world, baseline, native_rpc=rpc)
            assert recovered["response"] == {
                key: baseline["response"][key] for key in ("status", "headers", "body")
            }
    finally:
        await cleanup_world(test_engine, world)
