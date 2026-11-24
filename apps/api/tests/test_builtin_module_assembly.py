"""Executable invariants for declarative built-in backend module assembly."""

from __future__ import annotations

import ast
import re
import tomllib
from collections import defaultdict
from pathlib import Path

import pytest
import yaml
from fastapi import APIRouter, FastAPI

from app.assembly import (
    BUILTIN_MODULES,
    BeatScheduleSpec,
    BuiltinModuleManifest,
    ComposeOverlaySpec,
    MigrationSurface,
    RouterMount,
    build_beat_schedule,
    collect_celery_includes,
    collect_native_managed_relations,
    collect_native_managed_tables,
    collect_orm_model_modules,
    list_compose_overlays,
    load_declared_orm_models,
    mount_api_v1_routers,
    mount_root_routers,
    validate_builtin_module_manifests,
)
from app.config import Settings, settings
from app.db.native_schema import NATIVE_MANAGED_RELATIONS, NATIVE_MANAGED_TABLES
from app.workers.celery_app import celery_app
from yoao_jobs.queue_contract import load_queue_configuration

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
ASSEMBLY_DIR = API_DIR / "app" / "assembly"
API_ROOT_DIR = API_DIR / "app" / "api"
V1_DIR = API_ROOT_DIR / "v1"
WORKERS_DIR = API_DIR / "app" / "workers"
MODELS_DIR = API_DIR / "app" / "db" / "models"

_SUBROUTER_ONLY_V1_MODULES = frozenset({"_test_seed_backfill_image"})
_RETIRED_QUEUE_ROUTE_MODULES = frozenset(
    {
        "app.workers.identity_mail_delivery",
        "app.workers.identity_mail_v2_delivery",
    }
)
_FORBIDDEN_MANIFEST_IMPORT_PREFIXES = (
    "fastapi",
    "sqlalchemy",
    "celery",
    "app.api",
    "app.workers",
    "app.db",
    "app.services",
    "app.application",
    "app.config",
)
_PARAM_SEGMENT_RE = re.compile(r"\{[^}]+\}")


class _ComposeYamlLoader(yaml.SafeLoader):
    pass


_ComposeYamlLoader.add_constructor("!reset", lambda loader, node: None)
_ComposeYamlLoader.add_constructor(
    "!override", lambda loader, node: loader.construct_object(node)
)


def _top_level_routers(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    attrs: list[str] = []
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and "APIRouter" in ast.unparse(node.value.func)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    attrs.append(target.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Call)
            and "APIRouter" in ast.unparse(node.value.func)
        ):
            attrs.append(node.target.id)
    return tuple(attrs)


def _has_celery_task_decorator(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                rendered = ast.unparse(decorator)
                if "celery_app.task" in rendered or "shared_task" in rendered:
                    return True
    return False


def _defines_orm_table(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "__tablename__"
                    for target in stmt.targets
                ):
                    return True
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.target.id == "__tablename__"
                ):
                    return True
        elif isinstance(node, ast.Call):
            callee = ast.unparse(node.func)
            if callee == "Table" or callee.endswith(".Table"):
                return True
    return False


def _is_forbidden_import(module_name: str) -> bool:
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in _FORBIDDEN_MANIFEST_IMPORT_PREFIXES
    )


def _segment_kind(segment: str) -> tuple[str, str]:
    if segment.startswith("{") and segment.endswith(":path}"):
        return ("path_catchall", "")
    if segment.startswith("{") and segment.endswith("}"):
        return ("param", "")
    if _PARAM_SEGMENT_RE.search(segment):
        return ("partial_param", _PARAM_SEGMENT_RE.sub("{}", segment))
    return ("static", segment)


def _route_a_shadows_b(parts_a: list[str], parts_b: list[str]) -> bool:
    more_general = False
    for idx, seg_a in enumerate(parts_a):
        kind_a, val_a = _segment_kind(seg_a)
        if kind_a == "path_catchall":
            return idx < len(parts_b) and (
                len(parts_b) > len(parts_a)
                or _segment_kind(parts_b[idx])[0] != "path_catchall"
            )
        if idx >= len(parts_b):
            return False
        kind_b, val_b = _segment_kind(parts_b[idx])
        if kind_a == "param":
            if kind_b != "param":
                more_general = True
            continue
        if kind_a == "partial_param":
            if kind_b == "static":
                regex = "^" + re.escape(val_a).replace(r"\{\}", r"[^/]+") + "$"
                if not re.match(regex, val_b):
                    return False
                more_general = True
            elif kind_b == "partial_param" and val_a == val_b:
                continue
            else:
                return False
            continue
        if kind_b != "static" or val_a != val_b:
            return False
    return len(parts_a) == len(parts_b) and more_general


def test_builtin_module_manifests_validate_and_reject_conflicts() -> None:
    validate_builtin_module_manifests(BUILTIN_MODULES)

    with pytest.raises(ValueError, match="Duplicate or empty module_id"):
        validate_builtin_module_manifests(
            (
                BuiltinModuleManifest(module_id="dup"),
                BuiltinModuleManifest(module_id="dup"),
            )
        )

    with pytest.raises(ValueError, match="Duplicate or empty owner_package"):
        validate_builtin_module_manifests(
            (
                BuiltinModuleManifest(module_id="a", owner_packages=("yoao-jobs",)),
                BuiltinModuleManifest(module_id="b", owner_packages=("yoao-jobs",)),
            )
        )

    with pytest.raises(ValueError, match="Invalid router surface"):
        validate_builtin_module_manifests(
            (
                BuiltinModuleManifest(
                    module_id="a",
                    routers=(
                        RouterMount(
                            module_path="app.api.v1.tasks",
                            surface="invalid",  # type: ignore[arg-type]
                        ),
                    ),
                ),
            )
        )

    with pytest.raises(ValueError, match="Duplicate router mount"):
        validate_builtin_module_manifests(
            (
                BuiltinModuleManifest(
                    module_id="a",
                    routers=(
                        RouterMount(module_path="app.api.v1.tasks", surface="api_v1"),
                    ),
                ),
                BuiltinModuleManifest(
                    module_id="b",
                    routers=(
                        RouterMount(module_path="app.api.v1.tasks", surface="root"),
                    ),
                ),
            )
        )

    with pytest.raises(ValueError, match="undeclared worker module"):
        validate_builtin_module_manifests(
            (
                BuiltinModuleManifest(
                    module_id="a",
                    beat_schedules=(
                        BeatScheduleSpec(
                            name="orphan-beat",
                            task="app.workers.missing_mod.run",
                            schedule=60.0,
                        ),
                    ),
                ),
            )
        )

    with pytest.raises(ValueError, match="Invalid compose tier"):
        validate_builtin_module_manifests(
            (
                BuiltinModuleManifest(
                    module_id="a",
                    compose_overlays=(
                        ComposeOverlaySpec(
                            file="docker-compose.fake.yml",
                            tier="invalid",  # type: ignore[arg-type]
                        ),
                    ),
                ),
            )
        )

    with pytest.raises(ValueError, match="both services and overrides"):
        validate_builtin_module_manifests(
            (
                BuiltinModuleManifest(
                    module_id="a",
                    compose_overlays=(
                        ComposeOverlaySpec(
                            file="docker-compose.fake.yml",
                            tier="optional_split",
                            services=("svc",),
                            overrides=("svc",),
                        ),
                    ),
                ),
            )
        )

    with pytest.raises(ValueError, match="Empty schema or table"):
        validate_builtin_module_manifests(
            (
                BuiltinModuleManifest(
                    module_id="a",
                    migrations=MigrationSurface(native_managed_tables=(("", "tbl"),)),
                ),
            )
        )

    with pytest.raises(ValueError, match="Duplicate ORM model"):
        validate_builtin_module_manifests(
            (
                BuiltinModuleManifest(
                    module_id="a",
                    migrations=MigrationSurface(orm_models=("task",)),
                ),
                BuiltinModuleManifest(
                    module_id="b",
                    migrations=MigrationSurface(orm_models=("task",)),
                ),
            )
        )


def test_assembly_manifests_have_zero_heavy_runtime_imports() -> None:
    manifest_files = [
        ASSEMBLY_DIR / "__init__.py",
        ASSEMBLY_DIR / "contracts.py",
        ASSEMBLY_DIR / "registry.py",
        *sorted((ASSEMBLY_DIR / "modules").glob("*.py")),
    ]
    violations: list[str] = []
    for path in manifest_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_forbidden_import(alias.name):
                        violations.append(
                            f"{path.relative_to(REPO_ROOT)} imports {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)} uses relative import level={node.level}"
                    )
                elif node.module and _is_forbidden_import(node.module):
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)} imports {node.module}"
                    )

    builders_tree = ast.parse(
        (ASSEMBLY_DIR / "builders.py").read_text(encoding="utf-8")
    )
    for stmt in builders_tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if _is_forbidden_import(alias.name):
                    violations.append(f"builders.py top-level imports {alias.name}")
        elif isinstance(stmt, ast.ImportFrom):
            if stmt.level > 0:
                violations.append("builders.py top-level uses relative import")
            elif stmt.module and _is_forbidden_import(stmt.module):
                violations.append(f"builders.py top-level imports {stmt.module}")

    assert not violations, "\n".join(violations)


def test_every_api_router_is_declared_once() -> None:
    discovered: set[tuple[str, str]] = set()
    for path in sorted(API_ROOT_DIR.glob("*.py")):
        if path.stem == "__init__":
            continue
        for attr in _top_level_routers(path):
            discovered.add((f"app.api.{path.stem}", attr))
    for path in sorted(V1_DIR.glob("*.py")):
        if (
            path.stem in {"__init__", "router"}
            or path.stem in _SUBROUTER_ONLY_V1_MODULES
        ):
            continue
        for attr in _top_level_routers(path):
            discovered.add((f"app.api.v1.{path.stem}", attr))
    for pkg_init in sorted(V1_DIR.glob("*/__init__.py")):
        pkg_name = pkg_init.parent.name
        if pkg_name == "__pycache__":
            continue
        for attr in _top_level_routers(pkg_init):
            discovered.add((f"app.api.v1.{pkg_name}", attr))

    declared_list = [
        (mount.module_path, mount.attr)
        for manifest in BUILTIN_MODULES
        for mount in manifest.routers
    ]
    assert len(declared_list) == len(set(declared_list))
    assert set(declared_list) == discovered


def test_every_celery_task_module_beat_task_and_queue_route_matches_manifest() -> None:
    declared_includes = collect_celery_includes()
    assert celery_app.conf.include == declared_includes

    discovered_task_modules = {
        f"app.workers.{path.stem}"
        for path in sorted(WORKERS_DIR.glob("*.py"))
        if path.stem not in {"__init__", "celery_app"}
        and _has_celery_task_decorator(path)
    }
    declared_set = set(declared_includes)
    assert discovered_task_modules.issubset(declared_set)
    assert declared_set - discovered_task_modules == {
        "app.workers.heartbeat",
        "app.workers.signals",
    }

    queue_cfg = load_queue_configuration()
    routed_modules = {
        task_name.rsplit(".", 1)[0] for task_name in queue_cfg["task_routes"]
    }
    undeclared_routed = routed_modules - declared_set - _RETIRED_QUEUE_ROUTE_MODULES
    assert not undeclared_routed

    celery_app.loader.import_default_modules()
    missing_beat_tasks = [
        spec.task
        for manifest in BUILTIN_MODULES
        for spec in manifest.beat_schedules
        if spec.task not in celery_app.tasks
    ]
    assert not missing_beat_tasks
    assert celery_app.conf.beat_schedule == build_beat_schedule(settings)


def test_beat_schedule_applies_dynamic_intervals_and_split_selectors() -> None:
    base = Settings(
        _env_file=None,
        assets_video_executor="celery",
        assets_image_executor="celery",
        notification_publication_url="",
        notification_channel_sweep_interval_seconds=17,
    )
    schedule = build_beat_schedule(base)
    assert len(schedule) == 41
    assert (
        schedule["relay-notification-channel-deliveries"]["schedule"].total_seconds()
        == 17
    )

    split = Settings(
        _env_file=None,
        assets_video_executor="split",
        assets_image_executor="split",
        notification_publication_url="http://notifications-publisher:8181",
    )
    split_schedule = build_beat_schedule(split)
    assert set(schedule) - set(split_schedule) == {
        "reconcile-accepted-video-media",
        "execute-video-media-recovery",
        "reconcile-image-pyramids",
        "relay-notification-deliveries",
    }


def test_orm_models_and_native_schema_relations_match_manifests() -> None:
    discovered_models = {
        path.stem
        for path in sorted(MODELS_DIR.glob("*.py"))
        if path.stem != "__init__" and _defines_orm_table(path)
    }
    declared_models = set(collect_orm_model_modules())
    assert declared_models == discovered_models
    assert len(load_declared_orm_models()) == len(discovered_models)

    init_tree = ast.parse((MODELS_DIR / "__init__.py").read_text(encoding="utf-8"))
    init_imported_stems = {
        node.module.removeprefix("app.db.models.")
        for node in ast.walk(init_tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("app.db.models.")
    }
    assert init_imported_stems == declared_models

    assert NATIVE_MANAGED_TABLES == collect_native_managed_tables()
    assert NATIVE_MANAGED_RELATIONS == collect_native_managed_relations()
    assert len(NATIVE_MANAGED_TABLES) == 53
    assert len(NATIVE_MANAGED_RELATIONS) == 78


def test_every_compose_file_and_service_matches_module_declarations() -> None:
    overlays = list_compose_overlays()
    declared_by_file = {overlay.file: overlay for overlay in overlays}
    disk_files = {path.name for path in REPO_ROOT.glob("docker-compose*.yml")}
    assert set(declared_by_file) == disk_files

    all_defined_services = {svc for overlay in overlays for svc in overlay.services}
    base_dev_services = set(declared_by_file["docker-compose.yml"].services)

    for file_name, overlay in sorted(declared_by_file.items()):
        parsed = (
            yaml.load(
                (REPO_ROOT / file_name).read_text(encoding="utf-8"),
                Loader=_ComposeYamlLoader,
            )
            or {}
        )
        actual_services = set((parsed.get("services") or {}).keys())
        declared_services = set(overlay.services) | set(overlay.overrides)
        assert declared_services == actual_services, (
            f"{file_name}: declared={sorted(declared_services)} actual={sorted(actual_services)}"
        )
        assert set(overlay.overrides).issubset(all_defined_services), (
            f"{file_name}: overrides reference unknown services "
            f"{sorted(set(overlay.overrides) - all_defined_services)}"
        )
        if overlay.tier != "base":
            assert set(overlay.services).isdisjoint(base_dev_services), (
                f"{file_name}: non-base overlay declares base service under services instead of overrides"
            )


def test_owner_packages_match_api_pyproject_workspace_sources() -> None:
    pyproject = tomllib.loads((API_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    uv_sources = pyproject["tool"]["uv"]["sources"]
    workspace_packages = {
        pkg
        for pkg, spec in uv_sources.items()
        if isinstance(spec, dict)
        and isinstance(spec.get("path"), str)
        and spec["path"].startswith("../")
        and spec.get("editable") is True
    }
    declared_list = [
        pkg for manifest in BUILTIN_MODULES for pkg in manifest.owner_packages
    ]
    assert len(declared_list) == len(set(declared_list))
    assert set(declared_list) == workspace_packages


def test_mounted_http_and_ws_routes_have_no_collisions_or_shadows() -> None:
    dev_settings = Settings(
        _env_file=None,
        environment="development",
        e2e_seed_enabled=True,
    )
    full_app = FastAPI()
    v1_router = APIRouter()
    mount_api_v1_routers(v1_router, dev_settings)
    full_app.include_router(v1_router, prefix="/api/v1")
    mount_root_routers(full_app, dev_settings)

    by_pattern: dict[tuple[str, str], list[str]] = defaultdict(list)
    entries: list[tuple[str, str, list[str]]] = []
    for route in full_app.routes:
        path = getattr(route, "path", "")
        if not path:
            continue
        methods = tuple(sorted(getattr(route, "methods", ()) or ()))
        keys = methods if methods else ("WS",)
        norm = _PARAM_SEGMENT_RE.sub("{}", path)
        parts = path.strip("/").split("/")
        for key in keys:
            by_pattern[(key, norm)].append(path)
            entries.append((key, path, parts))

    assert any(key == "WS" for key, _, _ in entries)
    assert any(path.startswith("/api/v1/__test") for _, path, _ in entries)

    collisions = {k: v for k, v in by_pattern.items() if len(v) > 1}
    assert not collisions, f"Duplicate normalized routes: {collisions}"

    shadows: list[tuple[str, str, str]] = []
    for idx, (key_a, path_a, parts_a) in enumerate(entries):
        for key_b, path_b, parts_b in entries[idx + 1 :]:
            if key_a != key_b:
                continue
            if _route_a_shadows_b(parts_a, parts_b):
                shadows.append((key_a, path_a, path_b))
    assert not shadows, f"General route shadows more specific route: {shadows}"


def test_e2e_seed_router_mounts_only_when_non_prod_and_enabled() -> None:
    disabled = APIRouter()
    mount_api_v1_routers(
        disabled,
        Settings(_env_file=None, environment="development", e2e_seed_enabled=False),
    )
    assert not any(
        getattr(r, "path", "").startswith("/__test") for r in disabled.routes
    )

    prod_blocked = APIRouter()
    mount_api_v1_routers(
        prod_blocked,
        Settings(
            _env_file=None,
            environment="production",
            cors_allow_origins=["https://yoao.example.com"],
            e2e_seed_enabled=True,
        ),
    )
    assert not any(
        getattr(r, "path", "").startswith("/__test") for r in prod_blocked.routes
    )

    enabled = APIRouter()
    mount_api_v1_routers(
        enabled,
        Settings(_env_file=None, environment="development", e2e_seed_enabled=True),
    )
    assert any(getattr(r, "path", "").startswith("/__test") for r in enabled.routes)
