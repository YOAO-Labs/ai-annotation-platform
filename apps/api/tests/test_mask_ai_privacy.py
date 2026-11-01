from __future__ import annotations

import ast
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
LOG_SOURCES = (
    WORKSPACE / "api/app/services/video_tracking/runner.py",
    WORKSPACE / "api/app/workers/cleanup.py",
)
SENSITIVE_NAMES = {
    "caption",
    "counts",
    "discovery_text",
    "logits",
    "mask_input",
    "rle",
    "scribbles",
    "text",
    "trimmed_text",
}
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}


def _names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def test_mask_ai_logs_do_not_receive_prompt_or_mask_payload_variables() -> None:
    violations: list[str] = []
    for path in LOG_SOURCES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr not in LOG_METHODS:
                continue
            leaked = set().union(*(_names(arg) for arg in node.args)) & SENSITIVE_NAMES
            if leaked:
                violations.append(f"{path.name}:{node.lineno}:{sorted(leaked)}")

    assert violations == []
