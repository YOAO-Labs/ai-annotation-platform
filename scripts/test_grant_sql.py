from pathlib import Path

import pytest

from grant_sql import read_psql_grant_script, read_runtime_grant_statements


def _sql(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def test_psql_owner_replay_expands_in_order_without_touching_sql(tmp_path):
    body = (
        "DO $sql$\nBEGIN\n PERFORM 'path\\_fragment';\nEND;\n$sql$;\n"
        "/* Outer /* nested */ comment\n\\ir ignored.sql\n*/\n"
        "SELECT $text$\n\\ir ignored.sql\n$text$;\n"
    )
    _sql(tmp_path, "permissions.sql", body.rstrip("\n"))
    _sql(tmp_path, "owners.sql", "SELECT 1;\n\\ir permissions.sql")
    entry = _sql(
        tmp_path,
        "install.sql",
        "\\set ON_ERROR_STOP on\nBEGIN;\n"
        "\\if :{?owner}\n\\ir owners.sql\n\\endif\nCOMMIT;\n",
    )
    expected = (
        "\\set ON_ERROR_STOP on\nBEGIN;\n"
        "\\if :{?owner}\nSELECT 1;\n" + body + "\\endif\nCOMMIT;\n"
    )
    assert read_psql_grant_script(entry) == expected
    assert read_psql_grant_script(entry) == expected


@pytest.mark.parametrize(
    "directive",
    [
        r"\ir ../other.sql",
        r"\ir sub/other.sql",
        r"\ir /tmp/other.sql",
        r"\ir :filename",
        r"\ir 'other.sql'",
        r"\ir other.sql \! anything",
        r"\include_relative other.sql",
        r"\i other.sql",
        r"\set ON_ERROR_STOP off",
        r"SELECT 1; \ir other.sql",
    ],
)
def test_psql_rejects_non_literal_includes_and_unexpected_controls(tmp_path, directive):
    path = _sql(tmp_path, "entry.sql", directive)
    with pytest.raises(ValueError, match="psql command|grant SQL directive"):
        read_psql_grant_script(path)


def test_psql_requires_every_include_even_if_psql_would_skip_it(tmp_path):
    entry = _sql(tmp_path, "entry.sql", "\\if false\n\\ir missing.sql\n\\endif\n")
    with pytest.raises(ValueError, match="missing grant SQL file.*missing.sql"):
        read_psql_grant_script(entry)


def test_psql_rejects_linked_include(tmp_path):
    regular = _sql(tmp_path, "owner.sql", "SELECT 1;")
    (tmp_path / "linked.sql").symlink_to(regular)
    entry = _sql(tmp_path, "entry.sql", r"\ir linked.sql")
    with pytest.raises(
        ValueError, match="Invalid or missing grant SQL file.*linked.sql"
    ):
        read_psql_grant_script(entry)


def test_psql_rejects_include_cycle(tmp_path):
    entry = _sql(tmp_path, "entry.sql", r"\ir owner.sql")
    _sql(tmp_path, "owner.sql", r"\ir entry.sql")
    with pytest.raises(
        ValueError, match="include cycle: entry.sql -> owner.sql -> entry.sql"
    ):
        read_psql_grant_script(entry)


def test_psql_rejects_duplicate_nested_include(tmp_path):
    _sql(tmp_path, "common.sql", "SELECT 1;")
    _sql(tmp_path, "owner.sql", r"\ir common.sql")
    entry = _sql(tmp_path, "entry.sql", "\\ir owner.sql\n\\ir common.sql\n")
    with pytest.raises(ValueError, match="Duplicate grant SQL include: common.sql"):
        read_psql_grant_script(entry)


def test_driver_executes_one_grant_at_a_time_with_one_quoted_role(tmp_path):
    entry = _sql(tmp_path, "entry.sql", r"\ir capabilities.sql")
    _sql(
        tmp_path,
        "capabilities.sql",
        'GRANT USAGE ON SCHEMA yoao_drill_v1 TO :"platform_runtime";\n'
        "GRANT EXECUTE ON FUNCTION yoao_drill_v1.one(uuid[], pg_catalog.jsonb),\n"
        '    yoao_drill_v1.two(double precision) TO :"application_role";\n'
        'GRANT SELECT ON yoao_drill_v1.facts TO :"platform_runtime";\n'
        'GRANT SELECT, INSERT ON yoao_jobs_v1.async_job_creation TO :"platform_runtime";',
    )
    assert read_runtime_grant_statements(
        entry, role='login"; DROP SCHEMA public;--'
    ) == [
        'GRANT USAGE ON SCHEMA yoao_drill_v1 TO "login""; DROP SCHEMA public;--";',
        "GRANT EXECUTE ON FUNCTION yoao_drill_v1.one(uuid[], pg_catalog.jsonb),\n"
        '    yoao_drill_v1.two(double precision) TO "login""; DROP SCHEMA public;--";',
        'GRANT SELECT ON yoao_drill_v1.facts TO "login""; DROP SCHEMA public;--";',
        "GRANT SELECT, INSERT ON yoao_jobs_v1.async_job_creation "
        'TO "login""; DROP SCHEMA public;--";',
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "",
        r"\set ON_ERROR_STOP on",
        'GRANT USAGE ON SCHEMA yoao_drill_v1 TO :"platform_runtime"',
        'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA yoao_drill_v1 TO :"platform_runtime";',
        'GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"platform_runtime";',
        'GRANT USAGE ON SEQUENCE public.private_seq TO :"platform_runtime";',
        'GRANT SELECT, INSERT ON public.async_jobs TO :"platform_runtime";',
        'GRANT EXECUTE ON FUNCTION yoao_drill_v1.one() TO :"source_role";',
        'GRANT USAGE ON SCHEMA yoao_drill_v1 TO :"PLATFORM_RUNTIME";',
        'GRANT USAGE ON SCHEMA yoao_drill_v1 TO :"platform_runtime" WITH GRANT OPTION;',
        'GRANT USAGE ON SCHEMA yoao_drill_v1 TO :"platform_runtime"; SELECT 1;',
    ],
)
def test_driver_refuses_other_sql_and_psql_commands(tmp_path, sql):
    path = _sql(tmp_path, "entry.sql", sql)
    with pytest.raises(
        ValueError, match="grant SQL directive|runtime GRANT|runtime grant"
    ):
        read_runtime_grant_statements(path, role="runtime")


def test_checked_in_installers_resolve_for_actual_consumers():
    directory = Path(__file__).resolve().parents[1] / "infra/docker"
    owner_sql = read_psql_grant_script(directory / "module-data-grants.sql")
    assert "SELECT yoao_assets_storage_v1._adopt_installed_owners();" in owner_sql
    assert owner_sql.rstrip().endswith("COMMIT;")
    lan_sql = read_psql_grant_script(directory / "lan-production-grants.sql")
    assert lan_sql.rstrip().endswith("COMMIT;")
    assert read_psql_grant_script(directory / "identity-consumer-grants.sql")
    assert read_runtime_grant_statements(
        directory / "m2b-runtime-grants.sql", role="ordinary"
    )


def test_capability_section_extraction_handles_nested_conditionals_and_rejects_unknown(
    tmp_path,
):
    installer = _sql(
        tmp_path,
        "module-grants.sql",
        "\\set ON_ERROR_STOP on\nBEGIN;\n"
        "SELECT set_config('yoao.grant_section_applied', '0', true);\n"
        "\\if :{?grant_alpha}\n"
        "SELECT set_config('yoao.grant_section_applied', '1', true);\n"
        "\\if :{?opt}\nSELECT 1;\n\\else\nSELECT 2;\n\\endif\n"
        "\\endif\n"
        "\\if :{?grant_beta}\n"
        "SELECT set_config('yoao.grant_section_applied', '1', true);\n"
        "SELECT 3;\n"
        "\\endif\n"
        "COMMIT;\n",
    )
    assert read_psql_grant_script(installer, capability="alpha") == (
        "\\set ON_ERROR_STOP on\nBEGIN;\n"
        "\\if :{?opt}\nSELECT 1;\n\\else\nSELECT 2;\n\\endif\nCOMMIT;\n"
    )
    assert read_psql_grant_script(installer, capability="beta") == (
        "\\set ON_ERROR_STOP on\nBEGIN;\nSELECT 3;\nCOMMIT;\n"
    )
    with pytest.raises(ValueError, match="expected once"):
        read_psql_grant_script(installer, capability="missing")
    with pytest.raises(ValueError, match="Invalid grant capability"):
        read_psql_grant_script(installer, capability="bad-name")


def test_merged_module_installers_extract_every_capability_section():
    directory = Path(__file__).resolve().parents[1] / "infra/docker"
    expected_modules = {
        "assets-grants.sql": (
            "catalog",
            "image",
            "import",
            "media",
            "operator",
            "source",
        ),
        "audit-grants.sql": ("append", "maintenance", "query", "read"),
        "identity-grants.sql": (
            "accounts",
            "actor_status",
            "admin_passwords",
            "auth",
            "briefs",
            "deactivation",
            "deletions",
            "directory",
            "e2e_fixture",
            "groups",
            "invitations",
            "keys",
            "offboarding",
            "platform_roles",
            "provisioning",
            "recovery",
            "role_audit",
            "scale_seed",
            "seed_operator",
            "sessions",
            "suspensions",
        ),
        "inference-grants.sql": (
            "backend_probe",
            "health_data",
            "health_observation",
            "health_origin",
            "owner",
            "pipelines",
            "pool_reference",
            "registry_ingress",
            "routing",
            "video_restore",
        ),
        "jobs-grants.sql": (
            "dispatch",
            "lifecycle",
            "list_query",
            "notification_scope",
            "recovery",
            "video_execution",
        ),
        "labeling-grants.sql": (
            "annotation_maintenance",
            "annotation_transfer",
            "annotation_writes",
            "batch_control",
            "class_query",
            "core_read",
            "cross_frame",
            "discussion",
            "editing",
            "mask_edit",
            "mask_gc_data",
            "mask_quality",
            "preannotation_result_data",
            "prediction",
            "role_audit",
            "scene_read",
            "scene_track_commands",
            "spatial_quality",
            "task_control",
            "task_maintenance",
            "video_control",
            "video_writer",
        ),
        "notifications-grants.sql": (
            "append",
            "delivery_read",
            "inbox",
            "preferences",
            "read",
        ),
        "workspace-grants.sql": (
            "configuration",
            "consumer",
            "dataset_unlink",
            "directory_query",
            "e2e_fixture",
            "membership",
            "owner_transfer",
            "project_deletion",
            "project_maintenance",
            "project_query",
            "role_audit",
            "scale_seed",
            "seed_operator",
            "template_query",
        ),
        "platform-grants.sql": (
            "insights_refresh",
            "module_execution",
            "storage_migration_control",
        ),
    }
    for filename, capabilities in expected_modules.items():
        path = directory / filename
        full = read_psql_grant_script(path)
        assert full.rstrip().endswith("COMMIT;")
        for capability in capabilities:
            section = read_psql_grant_script(path, capability=capability)
            assert section.startswith("\\set ON_ERROR_STOP on\nBEGIN;\n")
            assert section.endswith("\nCOMMIT;\n")
