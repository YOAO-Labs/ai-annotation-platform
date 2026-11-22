"""Shared structural validation and traversal for Data Manager filter trees."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import HTTPException


_MAX_FILTER_DEPTH = 32
_MAX_FILTER_NODES = 4096
_MAX_IN_VALUES = 200

MAX_FILTER_DEPTH = _MAX_FILTER_DEPTH
MAX_FILTER_NODES = _MAX_FILTER_NODES


def _filter_error(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def validate_in_value(value: Any, *, max_values: int = _MAX_IN_VALUES) -> None:
    if isinstance(value, list) and len(value) > max_values:
        raise _filter_error(f"in value too long (max {max_values})")


def validate_filter_tree(filter_json: Any) -> None:
    """Validate node shape and bounded tree size without compiling SQL.

    The empty object is the existing unfiltered representation. Every non-empty
    root is either a rule or a group; group children must be objects so all
    compiler entrypoints produce the same 422 instead of skipping or crashing.
    """

    if not isinstance(filter_json, dict):
        raise _filter_error("Filter must be an object")
    if not filter_json:
        return

    stack: list[tuple[dict[str, Any], int]] = [(filter_json, 1)]
    node_count = 0
    while stack:
        node, depth = stack.pop()
        node_count += 1
        if node_count > _MAX_FILTER_NODES:
            raise _filter_error(
                f"Filter node count exceeds maximum ({_MAX_FILTER_NODES})"
            )
        if depth > _MAX_FILTER_DEPTH:
            raise _filter_error(
                f"Filter nesting depth exceeds maximum ({_MAX_FILTER_DEPTH})"
            )

        if "rules" not in node:
            if not isinstance(node.get("field"), str) or not isinstance(
                node.get("op"), str
            ):
                raise _filter_error("Filter rule needs field and op")
            if node["op"] == "in":
                validate_in_value(node.get("value"))
            continue

        op = node.get("op", "and")
        if not isinstance(op, str) or op not in {"and", "or"}:
            raise _filter_error("Filter group op must be and/or")
        rules = node.get("rules")
        if not isinstance(rules, list):
            raise _filter_error("Filter group rules must be a list")
        for child in reversed(rules):
            if not isinstance(child, dict):
                raise _filter_error("Filter group children must be objects")
            stack.append((child, depth + 1))


def iter_filter_nodes(filter_json: Any) -> Iterator[dict[str, Any]]:
    """Yield groups and rules in source order after structural validation."""

    validate_filter_tree(filter_json)
    if not filter_json:
        return

    stack: list[dict[str, Any]] = [filter_json]
    while stack:
        node = stack.pop()
        yield node
        rules = node.get("rules")
        if isinstance(rules, list):
            stack.extend(reversed(rules))


def iter_filter_rules(filter_json: Any) -> Iterator[dict[str, Any]]:
    for node in iter_filter_nodes(filter_json):
        if "rules" not in node:
            yield node


def collect_filter_fields(filter_json: Any) -> list[str]:
    """Return leaf field names in source order, preserving duplicates."""

    return [
        field
        for rule in iter_filter_rules(filter_json)
        if isinstance((field := rule.get("field")), str)
    ]
