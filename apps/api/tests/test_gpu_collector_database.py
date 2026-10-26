from __future__ import annotations

from pathlib import Path

import pytest

from aap_inference.gpu.collector_database import (
    GPUCollectorDatabaseConfigError,
    load_gpu_collector_database_url,
    validate_gpu_collector_role_boundary,
)
from aap_inference.gpu.settings import GPUArbiterMode, GPUAuthoritySettings


def _config(path: Path | str) -> GPUAuthoritySettings:
    return GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.OFF,
        gpu_arbiter_collector_database_url_file=str(path),
    )


def _role_boundaries() -> tuple[dict[str, object], dict[str, object]]:
    application = {
        "role_name": "annotation_app",
        "is_superuser": False,
        "can_create_role": False,
        "can_create_database": False,
        "can_replicate": False,
        "can_bypass_rls": False,
        "can_set_other_role": False,
        "can_delete_memberships": False,
        "can_delete_fences": False,
    }
    collector = {
        "role_name": "annotation_gpu_collector",
        "is_superuser": False,
        "can_create_role": False,
        "can_create_database": False,
        "can_replicate": False,
        "can_bypass_rls": False,
        "can_set_other_role": False,
        "can_select_registry": True,
        "can_lock_registry": False,
        "can_insert_registry": False,
        "can_update_registry": False,
        "can_delete_registry": False,
        "can_select_memberships": True,
        "can_lock_memberships": True,
        "can_insert_memberships": False,
        "can_update_memberships": False,
        "can_delete_memberships": True,
        "can_select_fences": True,
        "can_lock_fences": True,
        "can_insert_fences": False,
        "can_update_fences": False,
        "can_delete_fences": True,
    }
    return application, collector


def test_collector_url_file_accepts_one_complete_asyncpg_url(tmp_path: Path) -> None:
    path = tmp_path / "collector-url"
    path.write_text(
        "postgresql+asyncpg://gpu_collector:secret@postgres/annotation\n",
        encoding="utf-8",
    )

    assert load_gpu_collector_database_url(_config(path)) == (
        "postgresql+asyncpg://gpu_collector:secret@postgres/annotation"
    )


@pytest.mark.parametrize(
    "contents",
    (
        "",
        "not-a-url",
        "postgresql://gpu_collector:secret@postgres/annotation",
        "postgresql+asyncpg://gpu_collector@postgres/annotation",
        "postgresql+asyncpg://gpu_collector:secret@postgres/annotation\n"
        "postgresql+asyncpg://other:secret@postgres/annotation",
    ),
)
def test_collector_url_file_rejects_unsafe_contents(
    tmp_path: Path,
    contents: str,
) -> None:
    path = tmp_path / "collector-url"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(GPUCollectorDatabaseConfigError):
        load_gpu_collector_database_url(_config(path))


def test_collector_url_file_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(GPUCollectorDatabaseConfigError):
        load_gpu_collector_database_url(_config(tmp_path / "missing"))


def test_collector_role_boundary_accepts_exact_least_privilege_roles() -> None:
    application, collector = _role_boundaries()

    assert validate_gpu_collector_role_boundary(application, collector) == (
        "annotation_app",
        "annotation_gpu_collector",
    )


@pytest.mark.parametrize(
    ("target", "field", "value"),
    (
        ("application", "role_name", "annotation_gpu_collector"),
        ("application", "can_delete_memberships", True),
        ("application", "can_delete_fences", True),
        ("application", "is_superuser", True),
        ("application", "can_create_role", True),
        ("application", "can_set_other_role", True),
        ("collector", "can_insert_registry", True),
        ("collector", "can_set_other_role", True),
        ("collector", "can_lock_registry", True),
        ("collector", "can_update_memberships", True),
        ("collector", "can_update_fences", True),
        ("collector", "can_delete_memberships", False),
        ("collector", "can_lock_fences", False),
        ("collector", "can_select_registry", None),
    ),
)
def test_collector_role_boundary_rejects_missing_or_excess_privilege(
    target: str,
    field: str,
    value: object,
) -> None:
    application, collector = _role_boundaries()
    (application if target == "application" else collector)[field] = value

    with pytest.raises(GPUCollectorDatabaseConfigError):
        validate_gpu_collector_role_boundary(application, collector)
