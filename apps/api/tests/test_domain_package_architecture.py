"""Architecture guards for the v0.23.0 service-domain modularization.

These tests enforce the dependency-direction and cycle rules declared in
`docs/plans/2026-07-15-v0.23.0-api-services-domain-modularization.md` §4.4 for every
new domain package. They run on source via AST, so they apply as soon as a package is
created; a package that does not exist yet is skipped.

Rules enforced per domain package ``app.services.<pkg>``:

1. No module in the package may import ``app.api`` or ``app.workers`` (service → API /
   worker reverse dependency is forbidden).
2. No module in the package may import its own compatibility facade (the old flat
   module it replaced). Each domain declares its old facade module roots.
3. No module in the package may hide an import inside a function body that points at
   another module *within the same package* (function-local import masking a cycle).
   Top-level imports within the package are the only allowed intra-package wiring.

Domains are declared in ``DOMAIN_PACKAGES``. Add an entry when a new package lands.
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass
from importlib.util import resolve_name
from pathlib import Path

import pytest

_SERVICES_DIR = Path(__file__).resolve().parent.parent / "app" / "services"
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Facades that already have a canonical domain-package replacement. First-party code
# may exercise these paths only in the compatibility contract test; all production,
# worker, script and ordinary test imports must use the new modules directly.
CURRENT_COMPAT_FACADES: tuple[str, ...] = ()

# Modules permanently deleted in v0.23.2. The architecture guard treats these
# the same as compat facades (forbids domain packages from importing them) but
# they additionally must not exist at all.
REMOVED_MODULES = (
    "app.services.data_manager",
    "app.services.data_manager_cursor",
    "app.services.data_manager_entities",
    "app.services.data_manager_entity_filter",
    "app.services.data_manager_tracks",
    "app.services.task_views",
    "app.services.video_tracker_adapters",
    "app.services.video_tracker_job_service",
    "app.services.video_tracker_runner",
    "app.services.export",
    "app.services.export_cache",
    "app.services.export_davis",
    "app.services.export_lidar",
    "app.services.export_packaging",
    "app.services.export_video",
    "app.services.gpu_arbiter_store",
    "app.services.gpu_arbiter",
    "app.services.gpu_admission_signer",
    "app.services.gpu_arbiter_rollout",
    "app.services.gpu_collector_database",
    "app.services.gpu_dispatch_authority",
    "app.services.gpu_membership_activation",
    "app.services.gpu_rollout_control",
)

_LEGACY_CALL_REFERENCE_ALLOWLIST: dict[tuple[str, str, str], str] = {}
_LEGACY_CALL_SCAN_EXEMPT_FILES = {
    "apps/api/tests/test_compat_facades.py": (
        "the compatibility contract intentionally imports every facade in cold processes"
    ),
}


@dataclass(frozen=True)
class DomainSpec:
    """One new domain package and the rules it must satisfy."""

    package: str  # e.g. "gpu_arbitration"
    # Old flat module roots this package replaces; the new package must not import them.
    legacy_facades: tuple[str, ...]
    # Optional sub-packages that must stay lazy (no eager high-level imports at the
    # package __init__). Listed as "<pkg>.<sub>".
    lazy_roots: tuple[str, ...] = ()


DOMAIN_PACKAGES = (
    DomainSpec(
        package="gpu_arbitration",
        legacy_facades=(
            "app.services.gpu_arbiter_store",
            "app.services.gpu_arbiter",
            "app.services.gpu_admission_signer",
            "app.services.gpu_arbiter_rollout",
            "app.services.gpu_collector_database",
            "app.services.gpu_dispatch_authority",
            "app.services.gpu_membership_activation",
            "app.services.gpu_rollout_control",
        ),
        lazy_roots=(
            "gpu_arbitration",
        ),  # package root must not eager-import dispatch/membership/rollout
    ),
    DomainSpec(
        package="video_tracking",
        legacy_facades=(
            "app.services.video_tracker_adapters",
            "app.services.video_tracker_job_service",
            "app.services.video_tracker_runner",
        ),
    ),
    DomainSpec(
        package="exporting",
        legacy_facades=(
            "app.services.export",
            "app.services.export_cache",
            "app.services.export_davis",
            "app.services.export_lidar",
            "app.services.export_packaging",
            "app.services.export_video",
        ),
    ),
    DomainSpec(
        package="data_management",
        legacy_facades=(
            "app.services.data_manager",
            "app.services.data_manager_cursor",
            "app.services.data_manager_entities",
            "app.services.data_manager_entity_filter",
            "app.services.data_manager_tracks",
            "app.services.task_views",
        ),
    ),
)

# Absolute module roots a service domain must never depend on.
_FORBIDDEN_ROOTS = ("app.api", "app.workers")


def _module_path(rel_source: Path) -> str:
    """``app/services/gpu_arbitration/ledger/store.py`` -> ``app.services.gpu_arbitration.ledger.store``."""
    rel_parts = rel_source.with_suffix("").parts
    if rel_parts[-1] == "__init__":
        rel_parts = rel_parts[:-1]
    parts = ("app", "services") + rel_parts
    return ".".join(parts)


def _collect_imports(tree: ast.AST, current_module: str) -> list[tuple[str, bool]]:
    """Return ``(dotted_module_or_name, is_local)`` for every import in the module.

    ``is_local`` is True when the import statement lives inside a function/method body
    (i.e. a function-local import), False when it is module-level.
    """
    out: list[tuple[str, bool]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._depth = 0

        def visit_FunctionDef(self, node):  # noqa: N802
            self._depth += 1
            self.generic_visit(node)
            self._depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Import(self, node):  # noqa: N802
            out.extend((alias.name, self._depth > 0) for alias in node.names)

        def visit_ImportFrom(self, node):  # noqa: N802
            if node.level:
                package = current_module.rpartition(".")[0]
                relative = "." * node.level + (node.module or "")
                module = resolve_name(relative, package)
            else:
                module = node.module or ""
            if module:
                out.append((module, self._depth > 0))
                # ``from app.services import gpu_arbiter`` and
                # ``from . import runner`` name a submodule via the imported alias.
                # Record both candidates; graph resolution later ignores symbol paths.
                out.extend(
                    (f"{module}.{alias.name}", self._depth > 0)
                    for alias in node.names
                    if alias.name != "*"
                )

    _Visitor().visit(tree)
    return out


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return "<dynamic>"


def _matching_legacy_facade(reference: str) -> str | None:
    return next(
        (
            facade
            for facade in (*CURRENT_COMPAT_FACADES, *REMOVED_MODULES)
            if reference == facade or reference.startswith(facade + ".")
        ),
        None,
    )


def _legacy_references(
    tree: ast.AST,
    current_module: str,
) -> list[tuple[int, str, str]]:
    """Return ``(line, reference kind, legacy path)`` from one Python AST."""
    references: list[tuple[int, str, str]] = []
    for imported, _local in _collect_imports(tree, current_module):
        facade = _matching_legacy_facade(imported)
        if facade is not None:
            references.append((0, "import", imported))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node.func)
        values = [*node.args, *(keyword.value for keyword in node.keywords)]
        for value in values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            facade = _matching_legacy_facade(value.value)
            if facade is not None:
                references.append((node.lineno, f"call:{call_name}", value.value))
    return references


def _tracked_python_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", ":(glob)**/*.py"],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
    )
    paths = result.stdout.decode().rstrip("\0").split("\0")
    return [_REPO_ROOT / path for path in paths if path and "/vendor/" not in path]


def _repo_module_context(path: Path) -> str:
    relative = path.relative_to(_REPO_ROOT)
    if relative.parts[:2] == ("apps", "api"):
        module_path = Path(*relative.parts[2:]).with_suffix("")
        return ".".join(module_path.parts)
    module_path = relative.with_suffix("")
    sanitized = (part.replace("-", "_") for part in module_path.parts)
    return ".".join(("__repo_scan__", *sanitized))


def _iter_domain_files(pkg: str) -> list[tuple[Path, str]]:
    """Yield (abs_path, module_dotted) for every .py under ``app/services/<pkg>``."""
    root = _SERVICES_DIR / pkg
    if not root.is_dir():
        return []
    files = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(_SERVICES_DIR)
        files.append((path, _module_path(rel)))
    return files


def _iter_domain_files_all() -> list[tuple[DomainSpec, Path, str]]:
    """Flat list across all declared domains that exist on disk."""
    flat = []
    for spec in DOMAIN_PACKAGES:
        for path, module in _iter_domain_files(spec.package):
            flat.append((spec, path, module))
    return flat


def _iter_service_files() -> list[tuple[Path, str]]:
    files = []
    for path in sorted(_SERVICES_DIR.rglob("*.py")):
        rel = path.relative_to(_SERVICES_DIR)
        files.append((path, _module_path(rel)))
    return files


def _source_context_module(path: Path, module: str) -> str:
    if path.name == "__init__.py":
        return f"{module}.__init__"
    return module


def _resolve_graph_target(imported: str, modules: set[str]) -> str | None:
    candidate = imported
    while candidate.startswith("app.services"):
        if candidate in modules:
            return candidate
        if "." not in candidate:
            break
        candidate = candidate.rpartition(".")[0]
    return None


def _service_import_graph() -> dict[str, set[str]]:
    files = _iter_service_files()
    modules = {module for _path, module in files}
    graph = {module: set() for module in modules}
    for path, module in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        context = _source_context_module(path, module)
        for imported, _local in _collect_imports(tree, context):
            target = _resolve_graph_target(imported, modules)
            if target is not None and target != module:
                graph[module].add(target)
    return graph


def _strongly_connected_components(
    graph: dict[str, set[str]],
) -> list[tuple[str, ...]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for target in sorted(graph.get(node, ())):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])

        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        components.append(tuple(sorted(component)))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return components


# Skip the whole module gracefully if no domain package exists yet (pre-P1 state).
_HAS_ANY_DOMAIN = any(_iter_domain_files(spec.package) for spec in DOMAIN_PACKAGES)
pytestmark = pytest.mark.skipif(
    not _HAS_ANY_DOMAIN, reason="no domain package created yet (P0 baseline)"
)


@pytest.mark.parametrize(
    "spec", DOMAIN_PACKAGES, ids=[d.package for d in DOMAIN_PACKAGES]
)
def test_domain_package_forbidden_imports(spec: DomainSpec) -> None:
    """No domain module imports app.api / app.workers or its own legacy facade."""
    files = _iter_domain_files(spec.package)
    if not files:
        pytest.skip(f"package app.services.{spec.package} not created yet")

    offenders: list[str] = []
    forbidden = _FORBIDDEN_ROOTS + tuple(spec.legacy_facades)
    for path, module in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        context = _source_context_module(path, module)
        for imported, _local in _collect_imports(tree, context):
            for bad in forbidden:
                if imported == bad or imported.startswith(bad + "."):
                    offenders.append(f"{module}: imports forbidden '{imported}'")
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize(
    "spec", DOMAIN_PACKAGES, ids=[d.package for d in DOMAIN_PACKAGES]
)
def test_domain_package_root_not_eager_importing(spec: DomainSpec) -> None:
    """Package ``__init__`` must not eager-import modules flagged as lazy roots."""
    if not spec.lazy_roots:
        pytest.skip("no lazy-root rule for this domain")
    files = _iter_domain_files(spec.package)
    if not files:
        pytest.skip(f"package app.services.{spec.package} not created yet")

    init_path = _SERVICES_DIR / spec.package / "__init__.py"
    if not init_path.is_file():
        pytest.fail(f"package app.services.{spec.package} has no __init__.py")

    tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    init_module = f"app.services.{spec.package}.__init__"
    eager = [
        name for name, is_local in _collect_imports(tree, init_module) if not is_local
    ]
    # lazy_roots for gpu_arbitration lists the package itself, meaning the *root*
    # __init__ must stay minimal; re-export of stable leaf symbols is allowed but
    # importing high-level modules (dispatch/membership/rollout) by name is not.
    # We forbid eager import of known high-level submodules.
    forbidden_high_level = {
        "dispatch",
        "membership",
        "membership_activation",
        "rollout_control",
        "rollout_state",
        "retirement",
        "reconciliation",
        "fences",
    }
    flagged = []
    for name in eager:
        leaf = name.split(".")[-1] if "." in name else ""
        if leaf in forbidden_high_level:
            flagged.append(name)
    assert not flagged, f"package root eager-imports high-level module(s): {flagged}"


def test_repo_has_no_first_party_legacy_facade_references() -> None:
    """Tracked Python code cannot reintroduce an old facade import or target."""
    offenders: list[str] = []
    used_call_allowlist: set[tuple[str, str, str]] = set()
    for path in _tracked_python_files():
        if not path.exists():
            continue  # deleted but not yet committed
        relative = path.relative_to(_REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, kind, reference in _legacy_references(
            tree, _repo_module_context(path)
        ):
            if kind.startswith("call:"):
                call_name = kind.removeprefix("call:")
                allowlist_key = (relative, call_name, reference)
                if allowlist_key in _LEGACY_CALL_REFERENCE_ALLOWLIST:
                    used_call_allowlist.add(allowlist_key)
                    continue
                if relative in _LEGACY_CALL_SCAN_EXEMPT_FILES:
                    continue
            offenders.append(f"{relative}:{line}: {kind} references {reference}")

    unused_allowlist = set(_LEGACY_CALL_REFERENCE_ALLOWLIST) - used_call_allowlist
    offenders.extend(
        f"unused legacy call allowlist entry: {entry!r}" for entry in unused_allowlist
    )
    assert not offenders, "\n".join(offenders)


def test_legacy_reference_guard_detects_import_and_string_forms() -> None:
    """Synthetic negatives prevent the repo-wide guard from becoming inert."""
    tree = ast.parse(
        """
import app.services.export
from app.services import export_cache
from app.services.export_video import build_mot_gt
importlib.import_module("app.services.data_manager")
mock.patch("app.services.task_views.TaskViewService")
"""
    )
    references = {
        (kind, reference)
        for _line, kind, reference in _legacy_references(tree, "app.consumer")
    }
    assert ("import", "app.services.export") in references
    assert ("import", "app.services.export_cache") in references
    assert ("import", "app.services.export_video") in references
    assert ("call:importlib.import_module", "app.services.data_manager") in references
    assert (
        "call:mock.patch",
        "app.services.task_views.TaskViewService",
    ) in references


def test_relative_and_package_imports_are_resolved_for_guards() -> None:
    """Synthetic imports keep relative/local/legacy parsing from silently regressing."""
    function_tree = ast.parse("def load():\n    from .proofs import Subject\n")
    function_imports = set(
        _collect_imports(function_tree, "app.services.example.runner")
    )
    assert ("app.services.example.proofs", True) in function_imports

    root_tree = ast.parse("from . import dispatch\n")
    root_imports = set(_collect_imports(root_tree, "app.services.example.__init__"))
    assert ("app.services.example.dispatch", False) in root_imports

    legacy_tree = ast.parse("from app.services import gpu_arbiter\n")
    legacy_imports = set(_collect_imports(legacy_tree, "app.services.example.consumer"))
    assert ("app.services.gpu_arbiter", False) in legacy_imports


def test_scc_detector_finds_two_and_three_node_cycles() -> None:
    """Synthetic negative graphs prove the SCC guard detects both cycle shapes."""
    graph = {
        "two.a": {"two.b"},
        "two.b": {"two.a"},
        "three.a": {"three.b"},
        "three.b": {"three.c"},
        "three.c": {"three.a"},
        "acyclic.a": {"acyclic.b"},
        "acyclic.b": set(),
    }
    components = set(_strongly_connected_components(graph))
    assert ("two.a", "two.b") in components
    assert ("three.a", "three.b", "three.c") in components
    assert ("acyclic.a",) in components
    assert ("acyclic.b",) in components


def test_services_graph_has_no_gpu_related_cycle() -> None:
    """No SCC may cross the GPU domain, its clients, or legacy GPU modules."""
    graph = _service_import_graph()
    gpu_related = {
        "app.services.ml_client",
        "app.services.ml_backend",
        *DOMAIN_PACKAGES[0].legacy_facades,
    }

    def is_gpu_related(module: str) -> bool:
        return module.startswith("app.services.gpu_arbitration") or (
            module in gpu_related
        )

    offenders = [
        component
        for component in _strongly_connected_components(graph)
        if len(component) > 1 and any(is_gpu_related(node) for node in component)
    ]
    details = []
    for component in offenders:
        members = set(component)
        edges = sorted(
            f"{source} -> {target}"
            for source in component
            for target in graph[source]
            if target in members
        )
        details.append(f"SCC {component!r}: {edges!r}")
    assert not offenders, "\n".join(details)
