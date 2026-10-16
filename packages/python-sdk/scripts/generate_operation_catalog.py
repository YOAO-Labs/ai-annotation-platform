#!/usr/bin/env python3
"""Generate the installed SDK's compact, offline API operation catalog."""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "packages" / "python-sdk"
SOURCE = PACKAGE / "src" / "ai_annotation"
SNAPSHOT = ROOT / "apps" / "api" / "openapi.snapshot.json"
MANIFEST = PACKAGE / "api-coverage.toml"
OUTPUT = SOURCE / "_operation_catalog.generated.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
_MISSING = object()
METHOD_NOTES: dict[str, dict[str, Any]] = {
    "training_studies.create": {
        "constraints": [
            "Creation requires preview_digest even though the shared preview model permits omission; submit the digest for the unchanged preview request.",
            "Keep base.request_id and the exact request stable when reconciling an uncertain submission.",
        ],
    },
    "exports.create": {
        "constraints": [
            "scope and lidar belong in body=ExportRequestBody(...), not query kwargs.",
            "This method returns an async job ID; use exports.download_voc for synchronous VOC ZIP bytes.",
        ],
    },
    "batches.export": {
        "constraints": [
            "scope and lidar belong in body=ExportRequestBody(...), not query kwargs.",
            "This method returns an async job ID; use batches.download_voc for synchronous VOC ZIP bytes.",
        ],
    },
    "exports.download_voc": {
        "constraints": [
            "Streams synchronous VOC ZIP bytes using authenticated POST without mutation retries; no async job ID is returned."
        ],
    },
    "batches.download_voc": {
        "constraints": [
            "Streams synchronous VOC ZIP bytes using authenticated POST without mutation retries; no async job ID is returned."
        ],
    },
    "training_runs.preflight": {
        "constraints": [
            "Use the returned digest for the same request body when creating the run; changing inputs invalidates the preflight result."
        ],
        "examples": [
            {
                "kind": "workflow",
                "text": "Run `aap learning runs preflight --project <project-id> --body run.json --json`, copy its digest into the same request body, then call `aap learning runs create`.",
            }
        ],
    },
    "training_runs.create": {
        "constraints": [
            "The SDK rejects a missing preflight_digest locally; submit the fresh digest returned by preflight for these request values.",
            "Keep request_id stable across reconciliation of an ambiguous submission. The SDK does not generate or replace it.",
        ],
    },
    "training_runs.logs": {
        "constraints": [
            "Use `after` to page forward or `before` to page backward; the SDK rejects requests that provide both.",
            "Resume from the returned next_cursor and preserve stream_id; inspect gaps before treating the read as complete.",
        ],
    },
    "training_runs.telemetry": {
        "constraints": [
            "Resume forward from the returned next_cursor for the same stream_id; inspect gaps before treating the read as complete."
        ],
    },
    "training_runs.events": {
        "constraints": [
            "`after` is the last event sequence already consumed; continue with the returned sequence values."
        ],
    },
    "training_runs.list": {
        "domain_effects": [
            "This GET may reconcile stale or lost training execution state on the server. Treat it as a state-aware read."
        ],
    },
    "training.runs": {
        "domain_effects": [
            "This GET may reconcile stale or lost training execution state on the server. Treat it as a state-aware read."
        ],
    },
    "training_recipes.archive": {
        "constraints": [
            "expected_head_revision is a compare-and-set precondition; reread the recipe after a conflict before retrying."
        ],
    },
    "learning_policies.disable": {
        "constraints": [
            "expected_revision is a compare-and-set precondition; reread the policy after a conflict before retrying."
        ],
    },
}


def _attribute_path(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return list(reversed(parts))


def _placeholder(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        value = node.slice
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    raise ValueError(f"unsupported path placeholder: {ast.dump(node)}")


def _path_template(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        raise ValueError(
            f"HTTP path must be a literal or simple f-string: {ast.dump(node)}"
        )
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            parts.append("{" + _placeholder(value.value) + "}")
        else:
            raise ValueError(f"unsupported HTTP path expression: {ast.dump(node)}")
    return "".join(parts)


def _literal(node: ast.expr) -> str:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        raise ValueError(f"HTTP method must be a string literal: {ast.dump(node)}")
    return node.value.upper()


def _source_default(node: ast.expr) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return ast.unparse(node)


def _signature(function: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    defaults = [_MISSING] * (len(positional) - len(args.defaults)) + list(args.defaults)
    parameters: list[dict[str, Any]] = []
    for arg, default in zip(positional, defaults, strict=True):
        if arg.arg == "self":
            continue
        parameters.append(
            {
                "name": arg.arg,
                "kind": "positional_only"
                if arg in args.posonlyargs
                else "positional_or_keyword",
                "annotation": ast.unparse(arg.annotation) if arg.annotation else None,
                "required": default is _MISSING,
                "default": _source_default(default)
                if default is not _MISSING
                else None,
            }
        )
    if args.vararg:
        parameters.append(
            {
                "name": args.vararg.arg,
                "kind": "var_positional",
                "annotation": ast.unparse(args.vararg.annotation)
                if args.vararg.annotation
                else None,
                "required": False,
                "default": None,
                "opaque": True,
            }
        )
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        parameters.append(
            {
                "name": arg.arg,
                "kind": "keyword_only",
                "annotation": ast.unparse(arg.annotation) if arg.annotation else None,
                "required": default is None,
                "default": _source_default(default) if default is not None else None,
            }
        )
    if args.kwarg:
        parameters.append(
            {
                "name": args.kwarg.arg,
                "kind": "var_keyword",
                "annotation": ast.unparse(args.kwarg.annotation)
                if args.kwarg.annotation
                else None,
                "required": False,
                "default": None,
                "opaque": True,
            }
        )
    return {
        "parameters": parameters,
        "return_type": ast.unparse(function.returns) if function.returns else None,
        "python_only": any(
            param.get("opaque")
            or (param.get("annotation") and "Callable" in param["annotation"])
            for param in parameters
        ),
    }


def _client_namespaces() -> dict[str, str]:
    tree = ast.parse((SOURCE / "client.py").read_text(encoding="utf-8"))
    client = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Client"
    )
    init = next(
        node
        for node in client.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    mapping: dict[str, str] = {}
    for node in ast.walk(init):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name):
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                mapping[value.func.id] = target.attr
    return mapping


def _is_http_call(node: ast.Call, method: str) -> bool:
    path = _attribute_path(node.func)
    return path == ["self", "_http", method]


def _is_external_url(node: ast.expr) -> bool:
    path = _attribute_path(node)
    return bool(path and (path[-1] == "url" or path[-1].endswith("_url")))


def _method_target(
    chain: list[str] | None,
    namespace: str,
    namespaces: set[str],
    injected_namespaces: dict[str, str],
) -> str | None:
    if not chain or chain[0] != "self" or len(chain) < 2 or chain[1] == "_http":
        return None
    if chain[1] in namespaces and len(chain) >= 3:
        return ".".join(chain[1:])
    if chain[1] in injected_namespaces and len(chain) >= 3:
        return ".".join((injected_namespaces[chain[1]], *chain[2:]))
    if namespace:
        return f"{namespace}.{chain[1]}"
    return chain[1]


def _sdk_declarations() -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    namespace_by_class = _client_namespaces()
    namespaces = set(namespace_by_class.values())
    injected_by_class: dict[str, dict[str, str]] = {}
    methods: dict[str, dict[str, Any]] = {}
    function_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    calls: dict[str, set[str]] = defaultdict(set)
    operation_methods: dict[str, set[str]] = defaultdict(set)

    for source_path in sorted(SOURCE.glob("*.py")):
        if source_path.name.startswith("_") and source_path.name != "__init__.py":
            continue
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"), filename=str(source_path)
        )
        module = f"ai_annotation.{source_path.stem}"
        for class_node in (
            node for node in tree.body if isinstance(node, ast.ClassDef)
        ):
            if (
                class_node.name != "Client"
                and class_node.name not in namespace_by_class
            ):
                continue
            namespace = namespace_by_class.get(class_node.name, "")
            init = next(
                (
                    node
                    for node in class_node.body
                    if isinstance(node, ast.FunctionDef) and node.name == "__init__"
                ),
                None,
            )
            if init is not None:
                argument_names = {
                    argument.arg: namespace_by_class.get(
                        argument.annotation.id
                        if isinstance(argument.annotation, ast.Name)
                        else ""
                    )
                    for argument in init.args.args
                    if argument.annotation is not None
                }
                for assignment in ast.walk(init):
                    if not isinstance(assignment, ast.Assign) or not isinstance(
                        assignment.value, ast.Name
                    ):
                        continue
                    injected = argument_names.get(assignment.value.id)
                    if not injected:
                        continue
                    for target in assignment.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            injected_by_class.setdefault(class_node.name, {})[
                                target.attr
                            ] = injected
            for function in class_node.body:
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if function.name.startswith("_"):
                    continue
                if class_node.name == "Client" and function.name != "me":
                    continue
                name = f"{namespace}.{function.name}" if namespace else function.name
                methods[name] = {
                    "name": name,
                    "module": module,
                    "class": class_node.name,
                    "method": function.name,
                    **_signature(function),
                    "direct_api_operations": [],
                    "api_operations": [],
                    "calls": [],
                    "external_http": False,
                }
                function_nodes[name] = function

    for name, function in function_nodes.items():
        namespace = name.rsplit(".", 1)[0] if "." in name else ""
        class_name = methods[name]["class"]
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            method = None
            path_node: ast.expr | None = None
            if _is_http_call(call, "request"):
                if len(call.args) < 2:
                    raise ValueError(f"{name}: request needs method and path")
                method = _literal(call.args[0])
                path_node = call.args[1]
            elif _is_http_call(call, "stream_download"):
                path_node = (
                    call.args[0]
                    if call.args
                    else next(
                        (
                            keyword.value
                            for keyword in call.keywords
                            if keyword.arg == "url"
                        ),
                        None,
                    )
                )
                if path_node is None:
                    continue
                if _is_external_url(path_node):
                    methods[name]["external_http"] = True
                    continue
                method_node = next(
                    (
                        keyword.value
                        for keyword in call.keywords
                        if keyword.arg == "method"
                    ),
                    None,
                )
                method = _literal(method_node) if method_node is not None else "GET"
            if method is not None and path_node is not None:
                path = _path_template(path_node)
                if path.startswith(("http://", "https://")):
                    methods[name]["external_http"] = True
                    continue
                if not path.startswith("/"):
                    raise ValueError(f"{name}: HTTP path must start with /")
                if not path.startswith("/api/v1/"):
                    path = "/api/v1" + path
                operation = f"{method} {path}"
                methods[name]["direct_api_operations"].append(operation)
                operation_methods[operation].add(name)
                continue
            target = _method_target(
                _attribute_path(call.func),
                namespace,
                namespaces,
                injected_by_class.get(class_name, {}),
            )
            if target and target != name:
                calls[name].add(target)

    for name, method in methods.items():
        method["direct_api_operations"] = sorted(set(method["direct_api_operations"]))
        method["calls"] = sorted(target for target in calls[name] if target in methods)
        method.update(METHOD_NOTES.get(name, {}))

    def resolve(name: str, visiting: set[str]) -> set[str]:
        if name in visiting:
            return set()
        method = methods[name]
        operations = set(method["direct_api_operations"])
        for callee in method["calls"]:
            operations.update(resolve(callee, {*visiting, name}))
        return operations

    for name, method in methods.items():
        method["api_operations"] = sorted(resolve(name, set()))
    methods = {
        name: method
        for name, method in methods.items()
        if method["api_operations"] or method["external_http"]
    }
    operation_sdk = {
        operation: sorted(names) for operation, names in operation_methods.items()
    }
    return operation_sdk, methods


def _click_commands() -> list[dict[str, Any]]:
    sys.path.insert(0, str(PACKAGE / "src"))
    from ai_annotation.cli.main import app

    commands: list[dict[str, Any]] = []

    def add_command(info: Any, path: tuple[str, ...]) -> None:
        callback = getattr(info, "callback", None)
        if callback is None:
            return
        source_file = inspect.getsourcefile(callback)
        callback_name = getattr(callback, "__name__", None)
        callback_tree = None
        function_nodes = {}
        if source_file and callback_name:
            module_tree = ast.parse(Path(source_file).read_text(encoding="utf-8"))
            function_nodes = {
                node.name: node
                for node in module_tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            callback_tree = function_nodes.get(callback_name)
        if callback_tree is None:
            return
        sdk_calls: set[str] = set()
        confirmation = False

        def collect_calls(
            function: ast.FunctionDef | ast.AsyncFunctionDef, visited: set[str]
        ) -> None:
            nonlocal confirmation
            if function.name in visited:
                return
            visited.add(function.name)
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                chain = _attribute_path(node.func)
                if chain and chain[0] == "client" and len(chain) >= 2:
                    sdk_calls.add(".".join(chain[1:]))
                elif isinstance(node.func, ast.Name) and node.func.id in function_nodes:
                    collect_calls(function_nodes[node.func.id], visited)
                if chain and chain[-1] == "confirm_destructive":
                    confirmation = True

        collect_calls(callback_tree, set())
        params = []
        for parameter in inspect.signature(callback).parameters.values():
            declaration = parameter.default
            is_typer_declaration = hasattr(declaration, "param_decls")
            options = list(getattr(declaration, "param_decls", ()) or ())
            options = [
                option
                for option in options
                if isinstance(option, str) and option.startswith("-")
            ]
            if not is_typer_declaration:
                options = ["--" + parameter.name.replace("_", "-")]
            default = getattr(declaration, "default", declaration)
            if default is ...:
                default = None
            if not isinstance(default, (str, int, float, bool, type(None), list, dict)):
                default = str(default)
            params.append(
                {
                    "name": parameter.name,
                    "kind": "option" if options else "argument",
                    "options": options,
                    "required": getattr(declaration, "default", declaration) is ...,
                    "default": default,
                    "type": str(parameter.annotation)
                    if parameter.annotation is not inspect.Parameter.empty
                    else None,
                    "description": getattr(declaration, "help", None),
                }
            )
        command_name = info.name or callback_name.removesuffix("_").replace("_", "-")
        commands.append(
            {
                "path": " ".join(("aap", *path, command_name)),
                "alias": ".".join((*path, command_name)),
                "parameters": params,
                "sdk_methods": sorted(sdk_calls),
                "confirmation": confirmation,
            }
        )

    def visit(app_info: Any, path: tuple[str, ...]) -> None:
        for info in getattr(app_info, "registered_commands", ()):
            add_command(info, path)
        for group in getattr(app_info, "registered_groups", ()):
            if group.name:
                visit(group.typer_instance, (*path, group.name))

    visit(app, ())
    return commands


def _schema_descriptor(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if not schema:
        return None
    allowed = (
        "$ref",
        "title",
        "type",
        "format",
        "enum",
        "default",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "description",
    )
    result = {key: schema[key] for key in allowed if key in schema}
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in schema:
            result[keyword] = [_schema_descriptor(item) for item in schema[keyword]]
    if isinstance(schema.get("items"), dict):
        result["items"] = _schema_descriptor(schema["items"])
    if isinstance(schema.get("additionalProperties"), dict):
        result["additionalProperties"] = _schema_descriptor(
            schema["additionalProperties"]
        )
    elif "additionalProperties" in schema:
        result["additionalProperties"] = schema["additionalProperties"]
    return result


def generate_catalog() -> dict[str, Any]:
    spec = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    coverage = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    operations = coverage["operations"]
    sdk_by_operation, sdk_methods = _sdk_declarations()
    cli_commands = _click_commands()
    cli_by_operation: dict[str, set[str]] = defaultdict(set)
    for command in cli_commands:
        for sdk_method in command["sdk_methods"]:
            if sdk_method in sdk_methods:
                sdk_methods[sdk_method].setdefault("cli_aliases", []).append(
                    command["alias"]
                )
                for operation in sdk_methods[sdk_method]["api_operations"]:
                    cli_by_operation[operation].add(command["alias"])
    for method in sdk_methods.values():
        if method.get("external_http"):
            method["indirect_note"] = (
                "This method also streams from a returned or presigned URL; that URL has no fixed route in the catalog."
            )
        if "cli_aliases" in method:
            method["cli_aliases"] = sorted(set(method["cli_aliases"]))

    openapi_keys = {
        f"{method.upper()} {path}"
        for path, path_item in spec["paths"].items()
        for method in path_item
        if method.lower() in HTTP_METHODS
    }
    if set(operations) != openapi_keys:
        raise ValueError(
            "coverage manifest differs from OpenAPI snapshot: "
            f"missing={sorted(openapi_keys - set(operations))}; "
            f"extra={sorted(set(operations) - openapi_keys)}"
        )

    result: list[dict[str, Any]] = []
    for path, path_item in spec["paths"].items():
        for method, operation_data in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            key = f"{method.upper()} {path}"
            entry = operations[key]
            request_body = operation_data.get("requestBody")
            request_content = request_body.get("content", {}) if request_body else {}
            responses = {}
            for code, response in sorted(operation_data.get("responses", {}).items()):
                content = {}
                for media_type, media in sorted(response.get("content", {}).items()):
                    descriptor = _schema_descriptor(media.get("schema"))
                    if descriptor:
                        content[media_type] = {"schema": descriptor}
                responses[code] = {
                    "description": response.get("description"),
                    **({"content": content} if content else {}),
                }
            sdk_names = sdk_by_operation.get(key, [])
            cli_aliases = sorted(cli_by_operation.get(key, set()))
            row: dict[str, Any] = {
                "name": key,
                "method": method.upper(),
                "path": path,
                "operation_id": operation_data.get("operationId"),
                "summary": operation_data.get("summary"),
                "description": operation_data.get("description"),
                "tags": sorted(operation_data.get("tags", [])),
                "status": entry["status"],
                "sdk_methods": sdk_names,
                "cli_aliases": cli_aliases,
                "parameters": [
                    {
                        "name": parameter["name"],
                        "in": parameter["in"],
                        "required": parameter.get("required", False),
                        **(
                            {"description": parameter["description"]}
                            if parameter.get("description")
                            else {}
                        ),
                        **(
                            {"schema": _schema_descriptor(parameter.get("schema"))}
                            if parameter.get("schema")
                            else {}
                        ),
                    }
                    for parameter in operation_data.get("parameters", [])
                ],
                "responses": responses,
            }
            if entry.get("reason"):
                row["reason"] = entry["reason"]
            if entry.get("release"):
                row["release"] = entry["release"]
            if request_body:
                row["request_body"] = {
                    "required": request_body.get("required", False),
                    "content": {
                        media_type: {"schema": _schema_descriptor(media.get("schema"))}
                        for media_type, media in sorted(request_content.items())
                        if _schema_descriptor(media.get("schema"))
                    },
                }
            result.append(row)
    result.sort(key=lambda item: item["name"])
    cli_commands = sorted(cli_commands, key=lambda item: item["alias"])
    for command in cli_commands:
        command["sdk_methods"] = sorted(
            name for name in command["sdk_methods"] if name in sdk_methods
        )
    return {
        "schema_version": 1,
        "openapi_version": spec["info"].get("version"),
        "guide_resource": "ai_annotation/AGENT_GUIDE.md",
        "authority": "Descriptions do not grant permission or prove runtime availability; the server remains authoritative.",
        "invocation_note": "This catalog describes concrete SDK and CLI interfaces; it does not provide a generic JSON request executor.",
        "operation_count": len(result),
        "operations": result,
        "sdk_methods": [sdk_methods[name] for name in sorted(sdk_methods)],
        "cli_commands": cli_commands,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="fail if generated catalog is stale"
    )
    args = parser.parse_args()
    generated = (
        json.dumps(generate_catalog(), ensure_ascii=False, indent=2) + "\n"
    ).encode()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != generated:
            print(
                f"{OUTPUT.relative_to(ROOT)} is stale; run {Path(__file__).relative_to(ROOT)}"
            )
            return 1
        print(f"{OUTPUT.relative_to(ROOT)} is current")
        return 0
    OUTPUT.write_bytes(generated)
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
