"""Exercise frozen downgrade guards without downgrading restored current data."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text

from tests.identity_role_audit_helpers import historical_audit_database


_VERSIONS = Path(__file__).resolve().parents[1] / "alembic/versions"
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_NAMESPACES = {
    "workspace_membership_operation_receipts": "aap_workspace_membership_v1",
    "identity_suspension_operation_receipts": "aap_identity_suspensions_v1",
    "identity_deletion_operation_receipts": "aap_identity_deletions_v1",
    "identity_offboarding_operation_receipts": "aap_identity_offboarding_v1",
    "workspace_owner_transfer_operation_receipts": "aap_workspace_owner_transfer_v1",
}


def _invoke_downgrade(connection, filename, revision):
    spec = importlib.util.spec_from_file_location(
        "frozen_identity_guard", _VERSIONS / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == revision
    with Operations.context(MigrationContext.configure(connection)):
        module.downgrade()


async def assert_historical_receipt_guards(test_engine, source, alembic, cases):
    """Copy exact restored evidence into an owned DB at each frozen boundary.

    Cases are ordered newest first: (revision, predecessor, filename, table,
    key column, key value). A shared clone is valid only for one revision chain.
    """
    assert cases
    values = []
    async with source.connect() as db:
        for _, _, _, table, key, value in cases:
            assert _IDENTIFIER.fullmatch(table) and _IDENTIFIER.fullmatch(key)
            receipt = await db.scalar(
                text(
                    f"SELECT to_jsonb(receipt)::text FROM public.{table} "
                    f"AS receipt WHERE {key}=:key"
                ),
                {"key": value},
            )
            assert receipt is not None
            values.append(receipt)

    async with historical_audit_database(test_engine, initialize=False) as (owned, _):
        result = alembic(owned.url, "upgrade", cases[0][0])
        assert result.returncode == 0, "owned historical migration setup failed"
        current = cases[0][0]
        for (revision, predecessor, filename, table, key, value), receipt in zip(
            cases, values, strict=True
        ):
            if current != revision:
                result = alembic(owned.url, "downgrade", revision)
                assert result.returncode == 0, (
                    "owned historical migration traversal failed"
                )
            async with owned.begin() as db:
                assert (
                    await db.scalar(
                        text("SELECT version_num FROM public.alembic_version")
                    )
                    == revision
                )
                columns = set(
                    await db.scalars(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name=:table"
                        ),
                        {"table": table},
                    )
                )
                assert columns == set(json.loads(receipt))
                assert (
                    await db.scalar(text(f"SELECT count(*) FROM public.{table}")) == 0
                )
                await db.execute(
                    text(
                        f"INSERT INTO public.{table} SELECT "
                        f"(jsonb_populate_record(NULL::public.{table}, CAST(:receipt AS jsonb))).*"
                    ),
                    {"receipt": receipt},
                )
                select_receipt = text(
                    f"SELECT to_jsonb(receipt)::text FROM public.{table} "
                    f"AS receipt WHERE {key}=:key"
                )
                assert await db.scalar(select_receipt, {"key": value}) == receipt
                with pytest.raises(RuntimeError) as refusal:
                    async with db.begin_nested():
                        await db.run_sync(_invoke_downgrade, filename, revision)
                if table == "identity_revoked_jtis":
                    evidence = "revoked_jtis=1"
                elif table == "identity_deactivation_operation_receipts":
                    evidence = "user_receipts=1"
                elif table == "workspace_configuration_operation_receipts":
                    evidence = f"'{table}': 1"
                else:
                    evidence = "receipts=1"
                assert evidence in str(refusal.value)
                assert await db.scalar(select_receipt, {"key": value}) == receipt
                assert (
                    await db.scalar(
                        text("SELECT version_num FROM public.alembic_version")
                    )
                    == revision
                )
                await db.execute(
                    text(f"DELETE FROM public.{table} WHERE {key}=:key"),
                    {"key": value},
                )
            result = alembic(owned.url, "downgrade", predecessor)
            assert result.returncode == 0, "owned empty historical downgrade failed"
            async with owned.connect() as db:
                assert (
                    await db.scalar(
                        text("SELECT version_num FROM public.alembic_version")
                    )
                    == predecessor
                )
                assert (
                    await db.scalar(
                        text("SELECT to_regclass(:name)"), {"name": f"public.{table}"}
                    )
                    is None
                )
                if table in _NAMESPACES:
                    assert (
                        await db.scalar(
                            text("SELECT to_regnamespace(:name)"),
                            {"name": _NAMESPACES[table]},
                        )
                        is None
                    )
            current = predecessor
