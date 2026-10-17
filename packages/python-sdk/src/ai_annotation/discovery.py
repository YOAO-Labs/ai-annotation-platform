"""Offline discovery for API operations exposed by the Python SDK and CLI."""

from __future__ import annotations

import copy
import importlib
import json
from functools import lru_cache
from importlib.resources import files
from typing import Any, get_args, get_origin, get_type_hints

from pydantic import BaseModel, TypeAdapter

_STATUSES = {"covered", "excluded", "planned", "uncovered"}
_MAX_LIMIT = 200
_CAS_FIELDS = {
    "expected_revision",
    "expected_head_revision",
    "expected_generation",
    "expected_version",
    "expected_fingerprint",
}


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    data = json.loads(
        files("ai_annotation")
        .joinpath("_operation_catalog.generated.json")
        .read_text(encoding="utf-8")
    )
    if data.get("schema_version") != 1:
        raise RuntimeError("unsupported operation catalog schema version")
    return data


def _operation_names(operation: dict[str, Any]) -> set[str]:
    names = {operation["name"]}
    if operation.get("operation_id"):
        names.add(operation["operation_id"])
    names.update(operation.get("sdk_methods", []))
    names.update(operation.get("cli_aliases", []))
    return names


def _parameter_schema(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for item in get_args(annotation):
        model = _parameter_schema(item)
        if model is not None:
            return model
    return None


def _sdk_schemas(method: dict[str, Any]) -> dict[str, Any]:
    module = importlib.import_module(method["module"])
    owner = getattr(module, method["class"])
    function = getattr(owner, method["method"])
    try:
        annotations = get_type_hints(function)
    except (NameError, TypeError):
        annotations = getattr(function, "__annotations__", {})
    result = []
    raw_schemas = []
    for parameter in method.get("parameters", []):
        annotation = annotations.get(parameter["name"])
        model = _parameter_schema(annotation)
        if model is not None:
            result.append(
                {
                    "parameter": parameter["name"],
                    "model": model.__name__,
                    "schema": TypeAdapter(annotation).json_schema(
                        mode="validation", by_alias=True
                    )
                    if get_origin(annotation) in {dict, list}
                    else model.model_json_schema(mode="validation", by_alias=True),
                }
            )
        elif get_origin(annotation) in {dict, list}:
            raw_schemas.append(
                {
                    "parameter": parameter["name"],
                    "model": str(annotation),
                    "schema": TypeAdapter(annotation).json_schema(
                        mode="validation", by_alias=True
                    ),
                }
            )
    result.extend(raw_schemas)
    for name in method.get("constructed_models", []):
        model = _parameter_schema(getattr(module, name, None))
        if (
            model is not None
            and model.model_config.get("extra") == "forbid"
            and not any(item["model"] == name for item in result)
        ):
            result.append(
                {
                    "parameter": None,
                    "model": name,
                    "schema": model.model_json_schema(mode="validation", by_alias=True),
                    "usage": "Validated internally from the method arguments.",
                }
            )
    schemas: dict[str, Any] = {"request_schemas": result} if result else {}
    response_type = annotations.get("return")
    if not method["name"].startswith("streams.") and (
        _parameter_schema(response_type) is not None
        or get_origin(response_type)
        in {
            dict,
            list,
        }
    ):
        schemas["response_schema"] = TypeAdapter(response_type).json_schema(
            mode="serialization", by_alias=True
        )
    return schemas


def _field_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for child in value.values():
            names.update(_field_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_field_names(child))
    return names


def _method_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {method["name"]: method for method in catalog["sdk_methods"]}


def _command_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {command["alias"]: command for command in catalog["cli_commands"]}


def _enrich_method(method: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(method)
    result.update(_sdk_schemas(result))
    result.pop("constructed_models", None)
    if result.get("python_only"):
        result["invocation_note"] = (
            "This SDK method has opaque or Python-only parameters; the description is not a generic JSON tool signature."
        )
    return result


def _describe_row(
    operation: dict[str, Any],
    catalog: dict[str, Any],
    methods: dict[str, dict[str, Any]],
    selected_method: str | None = None,
) -> dict[str, Any]:
    result = copy.deepcopy(operation)
    command_index = _command_index(catalog)
    sdk_names = [selected_method] if selected_method else result.get("sdk_methods", [])
    sdk = [_enrich_method(methods[name]) for name in sdk_names]
    result["sdk_methods"] = sdk_names
    commands = [
        copy.deepcopy(command_index[alias])
        for alias in result.get("cli_aliases", [])
        if alias in command_index
    ]
    result["sdk"] = sdk
    result["cli"] = commands
    schemas = [
        item["schema"] for method in sdk for item in method.get("request_schemas", [])
    ]

    fields = (
        set().union(*(_field_names(schema) for schema in schemas)) if schemas else set()
    )
    params = {
        parameter["name"]
        for method in sdk
        for parameter in method.get("parameters", [])
    }
    constraints = result.setdefault("constraints", [])
    examples = result.setdefault("examples", [])
    domain_effects = result.setdefault("domain_effects", [])
    for method in sdk:
        for field in ("constraints", "examples", "domain_effects"):
            target = {
                "constraints": constraints,
                "examples": examples,
                "domain_effects": domain_effects,
            }[field]
            for note in method.get(field, []):
                if note not in target:
                    target.append(note)
    if any(command.get("confirmation") for command in commands):
        note = "The listed CLI command uses its confirmation helper; Python SDK calls do not prompt, so Python callers own their confirmation policy."
        if note not in constraints:
            constraints.append(note)
    if "request_id" in fields and not any("request_id" in item for item in constraints):
        constraints.append(
            "Keep the caller-supplied request_id stable for retries or reconciliation; after an ambiguous submission inspect server state before resubmitting, and do not generate a replacement ID automatically."
        )
    compare_and_set = sorted((fields | params) & _CAS_FIELDS)
    if compare_and_set and not any(
        field in note for field in compare_and_set for note in constraints
    ):
        constraint = (
            f"{', '.join(compare_and_set)} is a compare-and-set precondition; "
            "reread the resource after a conflict before retrying."
        )
        if constraint not in constraints:
            constraints.append(constraint)
    result["authority"] = catalog["authority"]
    generic_note = catalog["invocation_note"]
    if result.get("invocation_note"):
        result["invocation_note"] += " " + generic_note
    else:
        result["invocation_note"] = generic_note
    return result


def _describe_sdk_method(
    method: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, Any]:
    result = _enrich_method(method)
    operations = {operation["name"]: operation for operation in catalog["operations"]}
    method_index = _method_index(catalog)
    result["kind"] = "sdk_method"
    if method["name"].startswith("streams."):
        result["channel"] = next(
            copy.deepcopy(channel)
            for channel in catalog["stream_channels"]
            if channel["name"] == method["stream_channel"]
        )
    result["api_operations"] = [
        _describe_row(operations[name], catalog, method_index)
        for name in method.get("api_operations", [])
        if name in operations
    ]
    commands = _command_index(catalog)
    result["cli"] = [
        copy.deepcopy(commands[alias])
        for alias in method.get("cli_aliases", [])
        if alias in commands
    ]
    result["authority"] = catalog["authority"]
    generic_note = catalog["invocation_note"]
    if result.get("invocation_note"):
        result["invocation_note"] += " " + generic_note
    else:
        result["invocation_note"] = generic_note
    schema_fields = (
        set().union(
            *(
                _field_names(item["schema"])
                for item in result.get("request_schemas", [])
            )
        )
        if result.get("request_schemas")
        else set()
    )
    constraints = result.setdefault("constraints", [])
    if "request_id" in schema_fields and not any(
        "request_id" in item for item in constraints
    ):
        constraints.append(
            "Keep the caller-supplied request_id stable for retries or reconciliation; after an ambiguous submission inspect server state before resubmitting, and do not generate a replacement ID automatically."
        )
    compare_and_set = sorted(
        (schema_fields | {item["name"] for item in result.get("parameters", [])})
        & _CAS_FIELDS
    )
    if compare_and_set:
        note = (
            f"{', '.join(compare_and_set)} is a compare-and-set precondition; "
            "reread the resource after a conflict before retrying."
        )
        if note not in constraints and not any(
            field in existing for field in compare_and_set for existing in constraints
        ):
            constraints.append(note)
    return result


def list_capabilities(
    *,
    tag: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Return a bounded page of API operations and their SDK/CLI coverage."""
    if status is not None and not isinstance(status, str):
        raise ValueError("status must be a string")
    if status is not None and status not in _STATUSES:
        raise ValueError(
            f"unknown status {status!r}; choose from {', '.join(sorted(_STATUSES))}"
        )
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= _MAX_LIMIT
    ):
        raise ValueError(f"limit must be an integer from 1 to {_MAX_LIMIT}")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    catalog = _catalog()
    tags = {item for operation in catalog["operations"] for item in operation["tags"]}
    if tag is not None and not isinstance(tag, str):
        raise ValueError("tag must be a string")
    if tag is not None and tag not in tags:
        raise ValueError(
            f"unknown tag {tag!r}; available tags: {', '.join(sorted(tags))}"
        )
    matched = [
        operation
        for operation in catalog["operations"]
        if (status is None or operation["status"] == status)
        and (tag is None or tag in operation["tags"])
    ]
    page = matched[offset : offset + limit]
    coverage_counts = {
        value: sum(operation["status"] == value for operation in catalog["operations"])
        for value in sorted(_STATUSES)
    }
    return {
        "openapi_version": catalog["openapi_version"],
        "guide_resource": catalog["guide_resource"],
        "authority": catalog["authority"],
        "coverage_counts": coverage_counts,
        "total": len(matched),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + len(page)
        if offset + len(page) < len(matched)
        else None,
        "operations": [
            {
                "name": operation["name"],
                "operation_id": operation.get("operation_id"),
                "method": operation["method"],
                "path": operation["path"],
                "tags": list(operation["tags"]),
                "status": operation["status"],
                **({"reason": operation["reason"]} if operation.get("reason") else {}),
                "sdk_methods": list(operation.get("sdk_methods", [])),
                "cli_paths": [
                    command["path"]
                    for command in catalog["cli_commands"]
                    if command["alias"] in operation.get("cli_aliases", [])
                ],
                **(
                    {"summary": operation["summary"]}
                    if operation.get("summary")
                    else {}
                ),
            }
            for operation in page
        ],
    }


def _describe_cli_command(
    command: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, Any]:
    methods = _method_index(catalog)
    result = copy.deepcopy(command)
    result["kind"] = "cli_command"
    result["sdk"] = [
        _describe_sdk_method(methods[name], catalog)
        for name in command.get("sdk_methods", [])
        if name in methods
    ]
    operation_names = sorted(
        {
            operation["name"]
            for method in result["sdk"]
            for operation in method.get("api_operations", [])
        }
    )
    operations = {operation["name"]: operation for operation in catalog["operations"]}
    result["api_operations"] = [
        _describe_row(operations[operation], catalog, methods)
        for operation in operation_names
        if operation in operations
    ]
    result["authority"] = catalog["authority"]
    result["invocation_note"] = catalog["invocation_note"]
    return result


def _all_names(catalog: dict[str, Any]) -> set[str]:
    names = {
        name
        for operation in catalog["operations"]
        for name in _operation_names(operation)
    }
    names.update(method["name"] for method in catalog["sdk_methods"])
    names.update(command["alias"] for command in catalog["cli_commands"])
    return names


def describe_operation(name: str) -> dict[str, Any]:
    """Describe a route, SDK method, or CLI command without making a request."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("operation name must be a non-empty string")
    catalog = _catalog()
    operations = catalog["operations"]
    methods = _method_index(catalog)
    commands = _command_index(catalog)
    route_matches = [
        operation
        for operation in operations
        if name == operation["name"] or name == operation.get("operation_id")
    ]
    if route_matches:
        return _describe_row(route_matches[0], catalog, methods)

    if name in methods:
        method = methods[name]
        routes = method.get("api_operations", [])
        direct = method.get("direct_api_operations", [])
        if len(routes) == 1 and routes == direct and not method.get("calls"):
            route = next(
                (item for item in operations if item["name"] == routes[0]), None
            )
            if route is not None:
                result = _describe_row(route, catalog, methods, selected_method=name)
                result["matched_sdk_method"] = name
                return result
        return _describe_sdk_method(method, catalog)

    cli_matches = [
        operation
        for operation in operations
        if name in operation.get("cli_aliases", [])
    ]
    if len(cli_matches) == 1:
        result = _describe_row(cli_matches[0], catalog, methods)
        result["matched_cli_alias"] = name
        return result
    if len(cli_matches) > 1:
        choices = []
        for operation in cli_matches:
            sdk_names = ", ".join(operation.get("sdk_methods", [])) or "no SDK method"
            choices.append(f"{operation['name']} ({sdk_names})")
        raise ValueError(
            f"ambiguous CLI alias {name!r}; choose a canonical SDK method or route: "
            + "; ".join(choices)
        )
    if name in commands:
        return _describe_cli_command(commands[name], catalog)

    candidates = sorted(
        candidate
        for candidate in _all_names(catalog)
        if name.lower() in candidate.lower()
    )[:5]
    hint = f" Similar names: {', '.join(candidates)}." if candidates else ""
    raise ValueError(f"unknown operation {name!r}.{hint}")
