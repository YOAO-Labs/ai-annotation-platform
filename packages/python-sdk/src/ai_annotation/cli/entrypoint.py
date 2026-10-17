"""Console-script shim that keeps core-only installs readable."""

from __future__ import annotations

import json
import sys


def _json_error_requested(args: list[str]) -> bool:
    error_format = "text"
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--error-format" and index + 1 < len(args):
            error_format = args[index + 1]
            index += 2
            continue
        if arg.startswith("--error-format="):
            error_format = arg.partition("=")[2]
            index += 1
            continue
        if arg in {"-h", "--help", "--version"}:
            index += 1
            continue
        break
    return error_format == "json"


def main() -> None:
    try:
        from ai_annotation.cli.main import app
    except ModuleNotFoundError as exc:
        if exc.name not in {"rich", "typer"}:
            raise
        message = "aap CLI 依赖未安装；请运行: pip install 'ai-annotation-sdk[cli]'"
        if _json_error_requested(sys.argv[1:]):
            print(
                json.dumps(
                    {"kind": "dependency_error", "detail": message},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
        else:
            print(message, file=sys.stderr)
        raise SystemExit(1) from None
    app()
