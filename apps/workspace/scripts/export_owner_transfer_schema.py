#!/usr/bin/env python3
"""Generate and verify the portable Workspace owner-transfer schema."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pydantic import TypeAdapter  # noqa: E402

from aap_workspace.owner_transfer_contract import (  # noqa: E402
    OwnerTransferFailure,
    OwnerTransferPolicy,
    OwnerTransferRequest,
    OwnerTransferSuccess,
)


def render_schema() -> dict:
    schema = TypeAdapter(
        OwnerTransferRequest
        | OwnerTransferPolicy
        | OwnerTransferSuccess
        | OwnerTransferFailure
    ).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "aap-workspace-owner-transfer/v1"
    return schema


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    relative_path = Path("src/aap_workspace/owner_transfer_protocol.schema.json")
    target = ROOT / relative_path
    schema = render_schema()
    if args.check:
        if not target.exists() or json.loads(target.read_text()) != schema:
            raise SystemExit(
                "Workspace owner-transfer schema differs from its frozen models: "
                f"{relative_path}"
            )
    else:
        target.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
