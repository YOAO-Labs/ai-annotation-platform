"""Least-privilege contract for the isolated Labeling Mask GC database role."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GRANTS = ROOT / "infra/docker/labeling-mask-gc-data-grants.sql"
MODULE_GRANTS = ROOT / "infra/docker/module-data-grants.sql"

EXPECTED_GC_FUNCTIONS = {
    "aap_labeling_v1.claim_mask_gc_sweep(jsonb)",
    "aap_labeling_v1.stage_mask_gc_page(jsonb)",
    "aap_labeling_v1.claim_mask_gc_deletes(jsonb)",
    "aap_labeling_v1.finish_mask_gc_delete(jsonb)",
    "aap_labeling_v1.read_mask_gc_progress(jsonb)",
    "aap_labeling_v1.inspect_mask_gc_page(jsonb)",
}


def test_gc_login_receives_only_the_six_gc_operation_functions():
    script = GRANTS.read_text(encoding="utf-8")
    grant = re.search(
        r"(?is)GRANT EXECUTE ON FUNCTION\s+(.*?)\s+TO\s+:\"mask_gc_role\";",
        script,
    )
    assert grant is not None
    granted = set(
        re.findall(r"aap_labeling_v1\.[a-z_]+\([^)]*\)", grant.group(1).lower())
    )

    assert granted == EXPECTED_GC_FUNCTIONS
    assert "GRANT SELECT" not in script
    assert "GRANT INSERT" not in script
    assert "GRANT UPDATE" not in script
    assert "GRANT DELETE" not in script


def test_gc_login_is_revoked_from_raw_tables_schemas_and_other_functions():
    script = GRANTS.read_text(encoding="utf-8")

    assert 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"mask_gc_role";' in script
    assert (
        'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"mask_gc_role";' in script
    )
    assert (
        "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public, aap_labeling_v1 "
        'FROM :"mask_gc_role";'
    ) in script
    assert (
        "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_labeling_v1 FROM PUBLIC;"
    ) in script
    assert "The Labeling Mask GC role must have no role memberships" in script
    assert "v_callable <> 6" in script
    assert "raw table privileges" in script
    for write_gate in (
        "read_mask_write_epochs",
        "begin_mask_content_write",
        "lock_mask_write_epochs",
    ):
        assert write_gate in script


def test_module_grants_validate_gc_role_and_assign_native_functions_to_labeling():
    script = MODULE_GRANTS.read_text(encoding="utf-8")

    assert "mask_gc_role=labeling_mask_gc_client" in script
    assert "current_setting('aap.mask_gc_role')" in script
    assert ") <> 16 THEN" in script
    assert "The Labeling Mask GC role must not have inheriting members" in script
    assert "proc.proname LIKE '%mask_gc%'" in script
    assert "public.mask_gc_intents" in script
    assert "public.mask_gc_write_permit_secrets" in script
