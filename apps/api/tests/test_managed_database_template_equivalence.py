"""Prove head-template managed databases match fresh head migrations."""

from pathlib import Path
import re

from sqlalchemy import text

from tests.managed_image_inference_fixtures import (
    DATABASE_BOUND_REBIND_TABLES,
    _migrated_database,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION_SQL_ROOTS = (
    _REPO_ROOT / "apps" / "api" / "alembic" / "sql",
    *sorted((_REPO_ROOT / "apps").glob("*/src")),
)
_KNOWN_EMPTY_SOURCE_DB_BOUND_BACKFILLS = frozenset(
    {"yoao_inference_routing_v1.registry_state"}
)
_KNOWN_DATABASE_LEVEL_GRANTS = frozenset(
    {"apps/api/alembic/sql/0487_insights_storage_owner.sql"}
)
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_INSERT_DB_BOUND = re.compile(
    r"\bINSERT\s+INTO\s+([A-Za-z0-9_.\"]+)\b.*?"
    r"\b(?:gen_random_bytes|current_database|pg_database|pg_control_system)\b",
    re.IGNORECASE | re.DOTALL,
)


def _strip_dollar_quoted_bodies(sql: str) -> str:
    """Remove dollar-quoted routine and DO bodies so top-level DDL/DML stays visible."""
    out: list[str] = []
    pos = 0
    length = len(sql)
    while pos < length:
        match = _DOLLAR_TAG.search(sql, pos)
        if match is None:
            out.append(sql[pos:])
            break
        out.append(sql[pos : match.start()])
        tag = match.group(0)
        end = sql.find(tag, match.end())
        if end == -1:
            out.append(sql[match.start() :])
            break
        out.append("''")
        pos = end + len(tag)
    return "".join(out)


def _normalize_schema_table(name: str) -> str:
    cleaned = name.replace('"', "").lower()
    if cleaned.startswith("aap_"):
        return "yoao_" + cleaned[4:]
    return cleaned


async def _catalog_snapshot(engine) -> dict:
    async with engine.connect() as conn:
        schemas = [
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT nspname FROM pg_namespace "
                        "WHERE nspname NOT IN ('pg_catalog', 'information_schema') "
                        "  AND nspname NOT LIKE 'pg_toast%' "
                        "  AND nspname NOT LIKE 'pg_temp%' "
                        "ORDER BY 1"
                    )
                )
            ).all()
        ]
        relations = [
            tuple(row)
            for row in (
                await conn.execute(
                    text(
                        "SELECT n.nspname, c.relname, c.relkind "
                        "FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S') "
                        "  AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
                        "ORDER BY 1, 2, 3"
                    )
                )
            ).all()
        ]
        columns = [
            tuple(row)
            for row in (
                await conn.execute(
                    text(
                        "SELECT table_schema, table_name, column_name, data_type, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
                        "ORDER BY 1, 2, ordinal_position"
                    )
                )
            ).all()
        ]
        routines = [
            tuple(row)
            for row in (
                await conn.execute(
                    text(
                        "SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), "
                        "       pg_get_functiondef(p.oid) "
                        "FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                        "  AND p.prokind IN ('f', 'p') "
                        "  AND NOT EXISTS ("
                        "      SELECT 1 FROM pg_depend d "
                        "      WHERE d.classid = 'pg_proc'::regclass "
                        "        AND d.objid = p.oid AND d.deptype = 'e'"
                        "  ) "
                        "ORDER BY 1, 2, 3"
                    )
                )
            ).all()
        ]
        triggers = [
            tuple(row)
            for row in (
                await conn.execute(
                    text(
                        "SELECT n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid) "
                        "FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE NOT t.tgisinternal "
                        "  AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
                        "ORDER BY 1, 2, 3"
                    )
                )
            ).all()
        ]
        datacl = await conn.scalar(
            text(
                "SELECT datacl::text FROM pg_database "
                "WHERE datname = current_database()"
            )
        )
        non_empty_tables: list[str] = []
        for schema, relname, relkind in relations:
            if relkind not in ("r", "p"):
                continue
            count = await conn.scalar(
                text(f'SELECT count(*) FROM "{schema}"."{relname}"')
            )
            if count and count > 0:
                non_empty_tables.append(f"{schema}.{relname}")
        static_rows = {}
        for table in non_empty_tables:
            if table in DATABASE_BOUND_REBIND_TABLES:
                continue
            schema, relname = table.split(".", 1)
            rows = [
                tuple(row)
                for row in (
                    await conn.execute(
                        text(f'SELECT * FROM "{schema}"."{relname}" ORDER BY 1')
                    )
                ).all()
            ]
            static_rows[table] = rows
        secrets = {}
        for table in sorted(DATABASE_BOUND_REBIND_TABLES):
            secret = await conn.scalar(
                text(f"SELECT secret FROM {table} WHERE singleton")
            )
            secrets[table] = bytes(secret)
        routing_row = (
            await conn.execute(
                text(
                    "SELECT realm_database_oid, realm_database_name "
                    "FROM yoao_inference_routing_v1.authority_secret WHERE singleton"
                )
            )
        ).one()
        execution_row = (
            await conn.execute(
                text(
                    "SELECT database_oid, database_name, cluster_identifier "
                    "FROM yoao_jobs_v1.video_execution_authority WHERE singleton"
                )
            )
        ).one()
        db_oid, db_name, cluster_id = (
            await conn.execute(
                text(
                    "SELECT d.oid, d.datname, c.system_identifier "
                    "FROM pg_database d CROSS JOIN pg_control_system() c "
                    "WHERE d.datname = current_database()"
                )
            )
        ).one()
        routing_secret = bytes(
            await conn.scalar(
                text("SELECT yoao_inference_routing_v1._routing_secret()")
            )
        )
        execution_secret = bytes(
            await conn.scalar(text("SELECT yoao_jobs_v1._video_execution_secret()"))
        )
    return {
        "schemas": schemas,
        "relations": relations,
        "columns": columns,
        "routines": routines,
        "triggers": triggers,
        "datacl": datacl,
        "non_empty_tables": non_empty_tables,
        "static_rows": static_rows,
        "secrets": secrets,
        "routing_row": tuple(routing_row),
        "execution_row": tuple(execution_row),
        "db_identity": (db_oid, db_name, cluster_id),
        "routing_secret": routing_secret,
        "execution_secret": execution_secret,
    }


async def test_rebound_head_template_matches_fresh_head_catalog_and_rotates_authorities(
    test_db_url, managed_database
):
    """A cloned and rebound head template matches a fresh migration catalog and rotates secrets."""
    async with _migrated_database(test_db_url, "head", template="template0") as (
        _,
        fresh_engine,
        _,
    ):
        fresh = await _catalog_snapshot(fresh_engine)
    async with managed_database() as cloned_a_engine:
        cloned_a = await _catalog_snapshot(cloned_a_engine)
    async with managed_database() as cloned_b_engine:
        cloned_b = await _catalog_snapshot(cloned_b_engine)

    for key in (
        "schemas",
        "relations",
        "columns",
        "routines",
        "triggers",
        "datacl",
        "non_empty_tables",
        "static_rows",
    ):
        assert cloned_a[key] == fresh[key], key

    for table in sorted(DATABASE_BOUND_REBIND_TABLES):
        assert len(cloned_a["secrets"][table]) == 32, table
        assert len(cloned_b["secrets"][table]) == 32, table
        assert cloned_a["secrets"][table] != cloned_b["secrets"][table], table
        assert cloned_a["secrets"][table] != fresh["secrets"][table], table

    assert cloned_a["routing_row"] == cloned_a["db_identity"][:2]
    assert cloned_b["routing_row"] == cloned_b["db_identity"][:2]
    assert cloned_a["routing_row"] != cloned_b["routing_row"]
    assert cloned_a["execution_row"] == cloned_a["db_identity"]
    assert cloned_b["execution_row"] == cloned_b["db_identity"]
    assert cloned_a["execution_row"] != cloned_b["execution_row"]
    assert (
        cloned_a["routing_secret"]
        == cloned_a["secrets"]["yoao_inference_routing_v1.authority_secret"]
    )
    assert (
        cloned_a["execution_secret"]
        == cloned_a["secrets"]["yoao_jobs_v1.video_execution_authority"]
    )


def test_migration_sql_database_bound_inserts_match_rebind_registry():
    """Fail when a new migration seeds a database-bound row or DB grant outside the rebind list."""
    discovered_tables: set[str] = set()
    database_grant_files: set[str] = set()
    for root in _MIGRATION_SQL_ROOTS:
        if not root.exists():
            continue
        for sql_path in sorted(root.rglob("*.sql")):
            if sql_path.name.endswith("_down.sql"):
                continue
            raw = sql_path.read_text(encoding="utf-8")
            rel = sql_path.relative_to(_REPO_ROOT).as_posix()
            if re.search(
                r"\b(?:GRANT|REVOKE)\b[^;]*\bON\s+DATABASE\b", raw, re.IGNORECASE
            ):
                database_grant_files.add(rel)
            stripped = _strip_dollar_quoted_bodies(raw)
            for statement in stripped.split(";"):
                match = _INSERT_DB_BOUND.search(statement)
                if match is not None:
                    discovered_tables.add(_normalize_schema_table(match.group(1)))

    unexpected_tables = (
        discovered_tables
        - DATABASE_BOUND_REBIND_TABLES
        - _KNOWN_EMPTY_SOURCE_DB_BOUND_BACKFILLS
    )
    assert not unexpected_tables, (
        f"Unregistered database-bound migration seed tables: {sorted(unexpected_tables)}"
    )
    missing_tables = DATABASE_BOUND_REBIND_TABLES - discovered_tables
    assert not missing_tables, (
        f"Stale entries in DATABASE_BOUND_REBIND_TABLES: {sorted(missing_tables)}"
    )
    assert database_grant_files == _KNOWN_DATABASE_LEVEL_GRANTS, (
        f"Unexpected database-level GRANT migration files: {sorted(database_grant_files)}"
    )

    versions_dir = _REPO_ROOT / "apps" / "api" / "alembic" / "versions"
    py_hits = [
        path.name
        for path in sorted(versions_dir.glob("*.py"))
        if re.search(
            r"\b(?:gen_random_bytes|pg_control_system)\b",
            path.read_text(encoding="utf-8"),
        )
    ]
    assert not py_hits, (
        f"Python migrations seeding DB-bound secrets need audit: {py_hits}"
    )
