"""Static checks for the frozen accepted-input migration and capability profiles."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
SQL_DIR = ROOT / "apps/api/alembic/sql"
MIGRATION = ROOT / "apps/api/alembic/versions/0221_accepted_inputs.py"
SCHEMA = SQL_DIR / "0221_accepted_inputs.schema.json"
ARTIFACTS = (
    "0221_accepted_inputs_state.sql",
    "0221_accepted_inputs_authorities.sql",
    "0221_accepted_inputs_control.sql",
    "0221_accepted_inputs_execution.sql",
    "0221_accepted_inputs_retention.sql",
    "0221_accepted_inputs_garbage.sql",
)
OPERATIONS = {
    "find_accepted_image_input": ("control", "uuid,uuid,uuid"),
    "reserve_accepted_image_input": ("control", "uuid,uuid,uuid,text,jsonb"),
    "read_accepted_image_input": ("control", "uuid,uuid,uuid"),
    "read_accepted_image_input_work": ("control", "uuid,uuid,uuid"),
    "claim_accepted_image_input": ("worker", "uuid,uuid,integer"),
    "heartbeat_accepted_image_input": ("worker", "uuid,bigint,uuid,integer"),
    "finish_accepted_image_input": ("worker", "uuid,bigint,uuid,jsonb"),
    "fail_accepted_image_input": ("worker", "uuid,bigint,uuid,text"),
    "lock_accepted_image_input": ("labeling", "uuid,uuid,uuid"),
    "sweep_accepted_image_inputs": ("system", "integer"),
    "claim_accepted_input_garbage": ("cleanup", "uuid,integer,integer"),
    "complete_accepted_input_garbage": ("cleanup", "uuid,uuid"),
}


def _constants(path: Path) -> dict[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        result[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        continue
    return result


def test_0221_installer_orders_new_artifacts_and_preserves_0220():
    migration = _constants(MIGRATION)

    assert migration["_UPGRADE_ARTIFACTS"] == ARTIFACTS
    assert migration["_SCHEMA_ARTIFACTS"] == ("0221_accepted_inputs.schema.json",)
    assert all((SQL_DIR / name).is_file() for name in ARTIFACTS)


def test_0221_schema_publishes_exactly_the_twelve_native_capabilities():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["$id"] == "aap-assets-inputs/v1"
    assert set(schema["x-native"]) == set(OPERATIONS)
    for name, (role, arguments) in OPERATIONS.items():
        operation = schema["x-native"][name]
        assert operation["role"] == role
        assert operation["signature"] == (f"aap_assets_v1.{name}({arguments})")


def test_0221_every_new_function_is_revoked_from_public():
    for filename in ARTIFACTS:
        source = (SQL_DIR / filename).read_text(encoding="utf-8")
        created = re.findall(
            r"(?im)^CREATE\s+FUNCTION\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\s*\(",
            source,
        )
        revoked = re.findall(
            r"(?im)^REVOKE\s+ALL\s+ON\s+FUNCTION\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\s*\(",
            source,
        )
        assert set(created) <= set(revoked), (filename, set(created) - set(revoked))


def test_0221_native_functions_are_granted_only_to_frozen_profiles():
    media = (ROOT / "infra/docker/assets-media-grants.sql").read_text()
    image = (ROOT / "infra/docker/assets-image-grants.sql").read_text()
    modules = (
        (ROOT / "infra/docker/module-data-grants.sql").read_text().replace(", ", ",")
    )
    expected = {
        name: f"aap_assets_v1.{name}({arguments})"
        for name, (_role, arguments) in OPERATIONS.items()
    }
    for name in (
        "find_accepted_image_input",
        "reserve_accepted_image_input",
        "read_accepted_image_input",
        "read_accepted_image_input_work",
    ):
        assert expected[name] in media
        assert expected[name] in modules
    for name in (
        "claim_accepted_image_input",
        "heartbeat_accepted_image_input",
        "finish_accepted_image_input",
        "fail_accepted_image_input",
    ):
        assert expected[name] in image
    assert expected["lock_accepted_image_input"] in modules
    assert "aap_labeling_v1.attach_annotation_input_source(uuid,uuid,text)" in modules
    assert "aap_labeling_v1.copy_annotation_input_sources(uuid,uuid)" in modules
    assert 'ON public.annotation_input_sources TO :"workflow_role"' not in modules
    for name in ("claim_accepted_input_garbage", "complete_accepted_input_garbage"):
        assert expected[name] in modules
    assert (
        'ON public.asset_accepted_image_inputs TO :"asset_cleanup_role"' not in modules
    )
    assert (
        'ON public.asset_accepted_image_input_attempts TO :"asset_image_worker_role"'
        not in image
    )
