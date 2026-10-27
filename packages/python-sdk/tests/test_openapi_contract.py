"""Contract checks between SDK call sites and the monorepo OpenAPI snapshot."""

from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

import pytest

from ai_annotation import __aap_target_version__
from ai_annotation._http import API_PREFIX

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CLIENT_SOURCE = PACKAGE_ROOT / "src" / "ai_annotation" / "client.py"
ADDITIONAL_SOURCES = tuple(
    path for path in sorted(CLIENT_SOURCE.parent.glob("*.py")) if path != CLIENT_SOURCE
)
COVERAGE_MANIFEST = PACKAGE_ROOT / "api-coverage.toml"
SNAPSHOT = (
    PACKAGE_ROOT / ".." / ".." / "apps" / "api" / "openapi.snapshot.json"
).resolve()

_HTTP_METHODS = {"DELETE", "GET", "PATCH", "POST", "PUT"}
_OPENAPI_METHODS = {method.lower() for method in _HTTP_METHODS}
_COVERAGE_STATUSES = {"covered", "excluded", "planned", "uncovered"}
_EXCLUSION_REASONS = {"executor-only"}

_PLANNED_RELEASES: set[str] = set()


def _is_http_call(node: ast.Call, method: str) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == method
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "_http"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "self"
    )


def _is_presigned_url(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "url" or node.id.endswith("_url")
    if isinstance(node, ast.Attribute):
        return node.attr == "url" or node.attr.endswith("_url")
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value in {
            "url",
            "download_url",
            "artifact_url",
        }
    return False


def _literal_string(node: ast.expr, *, label: str, line: int) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise AssertionError(f"line {line}: SDK HTTP {label} must be a string literal")


def _placeholder(node: ast.expr, *, line: int) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        index = node.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, str):
            return index.value
    raise AssertionError(
        f"line {line}: SDK HTTP f-string placeholders must be names, attributes, "
        "or string-key lookups"
    )


def _path_template(node: ast.expr, *, line: int) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        raise AssertionError(
            f"line {line}: SDK HTTP path must be a literal or simple f-string"
        )
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            parts.append("{" + _placeholder(value.value, line=line) + "}")
        else:
            raise AssertionError(f"line {line}: unsupported SDK HTTP path expression")
    return "".join(parts)


def extract_client_operations(source: str) -> list[tuple[str, str, int]]:
    operations: list[tuple[str, str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if _is_http_call(node, "request"):
            if len(node.args) < 2:
                raise AssertionError(
                    f"line {node.lineno}: request requires method and path"
                )
            method = _literal_string(
                node.args[0], label="method", line=node.lineno
            ).upper()
            if method not in _HTTP_METHODS:
                raise AssertionError(
                    f"line {node.lineno}: unsupported HTTP method {method}"
                )
            path = _path_template(node.args[1], line=node.lineno)
            if not path.startswith("/"):
                raise AssertionError(
                    f"line {node.lineno}: SDK HTTP path must start with /"
                )
            root_option = next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "root_path"
                ),
                None,
            )
            if root_option is not None:
                assert (
                    isinstance(root_option, ast.Constant)
                    and type(root_option.value) is bool
                ), "root_path must be a literal boolean"
            is_root = root_option is not None and root_option.value is True
            full_path = (
                path
                if is_root or path.startswith(f"{API_PREFIX}/")
                else API_PREFIX + path
            )
            operations.append((method, full_path, node.lineno))
        elif _is_http_call(node, "stream_download"):
            method_node = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "method"),
                None,
            )
            method = (
                "GET"
                if method_node is None
                else _literal_string(
                    method_node, label="stream method", line=node.lineno
                ).upper()
            )
            if method not in _HTTP_METHODS:
                raise AssertionError(
                    f"line {node.lineno}: unsupported stream HTTP method {method}"
                )
            path_node = (
                node.args[0]
                if node.args
                else next(
                    (
                        keyword.value
                        for keyword in node.keywords
                        if keyword.arg == "url"
                    ),
                    None,
                )
            )
            if path_node is None:
                raise AssertionError(
                    f"line {node.lineno}: stream_download requires a URL"
                )
            if _is_presigned_url(path_node):
                continue
            path = _path_template(path_node, line=node.lineno)
            if path.startswith(("http://", "https://")):
                continue
            if not path.startswith(f"{API_PREFIX}/"):
                raise AssertionError(
                    f"line {node.lineno}: SDK stream download path must start with {API_PREFIX}/"
                )
            operations.append((method, path, node.lineno))
    return sorted(operations, key=lambda item: item[2])


def extract_sdk_operations() -> list[tuple[str, str, int]]:
    sources = (CLIENT_SOURCE, *(path for path in ADDITIONAL_SOURCES if path.is_file()))
    operations = [
        operation
        for path in sources
        for operation in extract_client_operations(path.read_text(encoding="utf-8"))
    ]
    assert operations, "no SDK HTTP call sites found"
    return sorted(operations, key=lambda item: item[2])


def _snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "prefix", ["/api/v1/videos/{dataset_item_id}", "/api/v1/tasks/{task_id}/video"]
)
@pytest.mark.parametrize(
    "resource,model",
    [("chunks/{chunk_id}", "VideoChunkOut"), ("frames/{frame_index}", "VideoFrameOut")],
)
def test_pending_media_is_documented_as_json(prefix, resource, model):
    pending = _snapshot()["paths"][f"{prefix}/{resource}"]["get"]["responses"]["202"]
    assert pending["content"]["application/json"]["schema"]["$ref"].endswith(
        f"/{model}"
    )
    assert "Retry-After" in pending["headers"]


def _watched_operations(spec: dict, watched_tags: set[str]) -> set[str]:
    operations: set[str] = set()
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method not in _OPENAPI_METHODS:
                continue
            if "*" in watched_tags or watched_tags.intersection(
                operation.get("tags", [])
            ):
                operations.add(f"{method.upper()} {path}")
    return operations


def test_extract_client_operations_supports_simple_f_strings():
    source = """
class Example:
    def load(self, dataset_id, init):
        self._http.request(
            "POST",
            f"/datasets/{dataset_id}/items/{init['item_id']}",
        )
"""
    assert extract_client_operations(source) == [
        (
            "POST",
            "/api/v1/datasets/{dataset_id}/items/{item_id}",
            4,
        )
    ]


def test_extract_client_operations_distinguishes_root_and_versioned_paths():
    source = """
class Health:
    def check(self):
        self._http.request("GET", "/health", root_path=True)
        self._http.request("GET", "/health", root_path=False)
"""
    assert [
        (method, path) for method, path, _ in extract_client_operations(source)
    ] == [("GET", "/health"), ("GET", "/api/v1/health")]
    with pytest.raises(AssertionError, match="literal boolean"):
        extract_client_operations(source.replace("root_path=True", "root_path=enabled"))


def test_extract_client_operations_rejects_dynamic_paths():
    source = """
class Example:
    def load(self, path):
        self._http.request("GET", path)
"""
    with pytest.raises(AssertionError, match="literal or simple f-string"):
        extract_client_operations(source)


def test_extract_client_operations_includes_fixed_stream_downloads_and_skips_urls():
    source = """
class Example:
    def load(self, item_id, download_url, dest):
        self._http.stream_download(
            f"/api/v1/projects/{item_id}/items/media", dest
        )
        self._http.stream_download(
            f"/api/v1/projects/{item_id}/items/export", dest, method="POST"
        )
        self._http.stream_download(download_url, dest)
"""
    operations = extract_client_operations(source)
    assert [(method, path) for method, path, _line in operations] == [
        ("GET", "/api/v1/projects/{item_id}/items/media"),
        ("POST", "/api/v1/projects/{item_id}/items/export"),
    ]


def test_extract_client_operations_rejects_dynamic_stream_paths():
    source = """
class Example:
    def load(self, path, dest):
        self._http.stream_download(path, dest)
"""
    with pytest.raises(AssertionError, match="literal or simple f-string"):
        extract_client_operations(source)


def test_extract_client_operations_rejects_dynamic_stream_methods():
    source = """
class Example:
    def load(self, method, dest):
        self._http.stream_download("/api/v1/files/export", dest, method=method)
"""
    with pytest.raises(AssertionError, match="stream method must be a string literal"):
        extract_client_operations(source)


def test_extract_sdk_operations_scans_optional_domain_modules(monkeypatch, tmp_path):
    module = __import__(__name__, fromlist=["CLIENT_SOURCE"])
    client_source = tmp_path / "client.py"
    training_source = tmp_path / "training.py"
    client_source.write_text(
        'class Example:\n    def load(self):\n        self._http.request("GET", "/projects")\n',
        encoding="utf-8",
    )
    training_source.write_text(
        "class Example:\n"
        "    def download(self, project_id, dest):\n"
        "        self._http.stream_download(\n"
        '            f"/api/v1/projects/{project_id}/training-runs", dest\n'
        "        )\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "CLIENT_SOURCE", client_source)
    monkeypatch.setattr(
        module,
        "ADDITIONAL_SOURCES",
        (
            training_source,
            tmp_path / "model_management.py",
            tmp_path / "learning_automation.py",
        ),
    )

    assert {(method, path) for method, path, _line in extract_sdk_operations()} == {
        ("GET", "/api/v1/projects"),
        ("GET", "/api/v1/projects/{project_id}/training-runs"),
    }


@pytest.mark.skipif(not SNAPSHOT.is_file(), reason="OpenAPI snapshot is monorepo-only")
def test_client_call_sites_exist_in_snapshot():
    paths = _snapshot()["paths"]
    missing = [
        (method, path, line)
        for method, path, line in extract_sdk_operations()
        if path not in paths or method.lower() not in paths[path]
    ]
    assert not missing, f"SDK call sites missing from OpenAPI snapshot: {missing}"


@pytest.mark.skipif(not SNAPSHOT.is_file(), reason="OpenAPI snapshot is monorepo-only")
def test_api_coverage_manifest_classifies_watched_operations():
    spec = _snapshot()
    manifest = tomllib.loads(COVERAGE_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["snapshot"] == __aap_target_version__

    watched_tags = set(manifest["watched_tags"])
    assert watched_tags == {"*"}, "all OpenAPI tags must remain covered by drift checks"
    actual = _watched_operations(spec, watched_tags)
    declared = manifest["operations"]
    declared_keys = set(declared)
    assert declared_keys == actual, (
        f"unclassified={sorted(actual - declared_keys)}; "
        f"removed={sorted(declared_keys - actual)}"
    )

    for operation, entry in declared.items():
        status = entry.get("status")
        assert status in _COVERAGE_STATUSES, f"{operation}: invalid status {status!r}"
        if status == "planned":
            assert entry.get("release") in _PLANNED_RELEASES, (
                f"{operation}: planned operation needs a known release"
            )
        elif status == "excluded":
            assert entry.get("reason") in _EXCLUSION_REASONS, (
                f"{operation}: excluded operation needs a known reason"
            )
            assert operation.startswith("POST /api/v1/internal/training/"), (
                f"{operation}: public REST operations belong in the SDK scope"
            )
        elif status == "uncovered":
            assert entry.get("reason") == "not-implemented", (
                f"{operation}: uncovered operation needs the not-implemented reason"
            )

    covered = {
        operation
        for operation, entry in declared.items()
        if entry["status"] == "covered"
    }
    client_operations = {
        f"{method} {path}" for method, path, _line in extract_sdk_operations()
    }
    assert client_operations <= declared_keys, (
        f"unclassified SDK call sites={sorted(client_operations - declared_keys)}"
    )
    assert covered == client_operations.intersection(actual), (
        f"coverage drift: missing={sorted(client_operations.intersection(actual) - covered)}; "
        f"stale={sorted(covered - client_operations)}"
    )


@pytest.mark.skipif(not SNAPSHOT.is_file(), reason="WebSocket router is monorepo-only")
def test_public_stream_channels_match_root_mounted_router():
    from ai_annotation.discovery import _catalog

    api = SNAPSHOT.parent
    tree = ast.parse((api / "app/api/v1/ws.py").read_text())
    paths = {
        decorator.args[0].value
        for function in tree.body
        if isinstance(function, ast.AsyncFunctionDef)
        for decorator in function.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "websocket"
    }
    channels = _catalog()["stream_channels"]
    assert len(channels) == 6
    assert {channel["path"] for channel in channels} == paths
    main = ast.parse((api / "app/main.py").read_text())
    mounts = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "include_router"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "ws_router"
    ]
    assert len(mounts) == 1
    assert not any(keyword.arg == "prefix" for keyword in mounts[0].keywords)
