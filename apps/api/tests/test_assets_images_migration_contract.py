"""Static checks for the frozen 0220 image database boundary artifacts."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
SQL_DIR = ROOT / "apps/api/alembic/sql"
MIGRATION = ROOT / "apps/api/alembic/versions/0220_assets_images.py"
VERIFIER = ROOT / "scripts/verify_module_data_boundary.py"
IMAGE_CONTRACT = ROOT / "apps/assets-media/src/aap_assets_images/contract.py"
IMAGE_SCHEMA = SQL_DIR / "0220_assets_images.schema.json"


def _module_constants(path: Path) -> dict[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        values[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        continue
    return values


def _image_operations() -> dict[str, tuple[str, str]]:
    tree = ast.parse(IMAGE_CONTRACT.read_text(encoding="utf-8"))
    operations = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "OPERATIONS"
            for target in node.targets
        )
    )
    assert isinstance(operations, ast.Dict)
    result = {}
    for key, value in zip(operations.keys, operations.values, strict=True):
        name = ast.literal_eval(key)
        assert isinstance(value, ast.Call) and value.func.id == "NativeOperation"
        types = ast.literal_eval(value.args[3])
        role = ast.literal_eval(value.args[4])
        result[name] = (role, f"aap_assets_v1.{name}({','.join(types)})")
    return result


def _oid_array(source: str, name: str) -> list[str]:
    match = re.search(
        rf"{re.escape(name)}\s+oid\[\]\s*:=\s*ARRAY\[(.*?)\]\s*;",
        source,
        flags=re.DOTALL,
    )
    assert match is not None, name
    return re.findall(r"to_regprocedure\('([^']+)'\)", match.group(1))


def test_0220_installer_and_verifier_share_order_and_version_edge():
    migration = _module_constants(MIGRATION)
    verifier = _module_constants(VERIFIER)
    image_artifacts = (
        "0220_image_authorities.sql",
        "0220_assets_images_state.sql",
        "0220_assets_images_control.sql",
        "0220_assets_images_execution.sql",
        "0220_assets_images_garbage.sql",
        "0220_image_cleanup.sql",
        "0220_image_sources.sql",
    )
    assert migration["revision"] == "0220"
    assert migration["down_revision"] == "0219"
    assert migration["_UPGRADE_ARTIFACTS"] == image_artifacts
    assert verifier["SQL_ARTIFACTS"][-len(image_artifacts) :] == image_artifacts
    assert "0220_assets_images.schema.json" in verifier["SCHEMA_ARTIFACTS"]


def test_0220_schema_is_an_exact_copy_of_the_published_image_contract():
    package_schema = (
        ROOT / "apps/assets-media/src/aap_assets_images/protocol.schema.json"
    )
    assert IMAGE_SCHEMA.read_bytes() == package_schema.read_bytes()
    assert json.loads(IMAGE_SCHEMA.read_text(encoding="utf-8"))["$id"] == (
        "aap-assets-images/v1"
    )


def test_0220_grants_match_all_frozen_image_operation_roles():
    operations = _image_operations()
    media_grants = (ROOT / "infra/docker/assets-media-grants.sql").read_text()
    image_grants = (ROOT / "infra/docker/assets-image-grants.sql").read_text()
    cleanup_grants = (ROOT / "infra/docker/module-data-grants.sql").read_text()
    source_grants = (ROOT / "infra/docker/assets-source-grants.sql").read_text()
    import_grants = (ROOT / "infra/docker/assets-import-grants.sql").read_text()

    control_and_system = {
        name: signature
        for name, (role, signature) in operations.items()
        if role in {"control", "system"}
    }
    image_worker = {
        name: signature
        for name, (role, signature) in operations.items()
        if role == "worker"
    }
    image_cleanup = {
        name: signature
        for name, (role, signature) in operations.items()
        if role == "cleanup"
    }
    assert len(control_and_system) == 14
    assert len(image_worker) == 6
    assert len(image_cleanup) == 2
    for signature in control_and_system.values():
        assert signature in media_grants, signature
    for signature in image_worker.values():
        assert signature in image_grants, signature
    for signature in image_cleanup.values():
        assert signature in cleanup_grants, signature

    source_allowed = _oid_array(source_grants, "allowed_functions")
    import_allowed = _oid_array(import_grants, "allowed_functions")
    media_control_allowed = _oid_array(media_grants, "control_functions")
    media_video_worker_allowed = _oid_array(media_grants, "worker_functions")
    image_worker_allowed = _oid_array(image_grants, "worker_functions")
    assert len(source_allowed) == 26
    assert len(import_allowed) == 9
    assert len(media_control_allowed) == 32
    assert len(media_video_worker_allowed) == 10
    assert len(image_worker_allowed) == 6

    source_extras = {
        "aap_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb)",
        "aap_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean)",
        "aap_assets_v1.read_legacy_dataset_upload(uuid,uuid,uuid,uuid)",
    }
    import_extras = source_extras - {
        "aap_assets_v1.read_legacy_dataset_upload(uuid,uuid,uuid,uuid)"
    }
    assert source_extras <= set(
        re.findall(r"aap_assets_v1\.[a-z_]+\([^)]*\)", source_grants)
    )
    assert import_extras <= set(
        re.findall(r"aap_assets_v1\.[a-z_]+\([^)]*\)", import_grants)
    )
