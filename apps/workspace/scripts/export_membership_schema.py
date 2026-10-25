#!/usr/bin/env python3
"""Generate and verify the portable Workspace membership wire schema."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pydantic import TypeAdapter  # noqa: E402

from aap_workspace.contract import (  # noqa: E402
    MEMBERSHIP_NATIVE_TEXT_CODEC,
    MEMBERSHIP_NATIVE_TEXT_FIELDS,
    MembershipFailure,
    MembershipPolicyReply,
    MembershipRequest,
    MembershipSuccess,
    NativeMembershipApplyRequest,
    NativeMembershipApplySuccess,
    NativeMembershipFailure,
    NativeMembershipPrepareSuccess,
)


def render_schema() -> dict:
    schema = TypeAdapter(
        MembershipRequest
        | MembershipSuccess
        | MembershipFailure
        | MembershipPolicyReply
        | NativeMembershipApplyRequest
        | NativeMembershipPrepareSuccess
        | NativeMembershipApplySuccess
        | NativeMembershipFailure
    ).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "aap-workspace-membership/v1"
    schema["x-postgresql-text-codec"] = {
        "name": MEMBERSHIP_NATIVE_TEXT_CODEC,
        "operation": "change_role",
        "payload_fields": list(MEMBERSHIP_NATIVE_TEXT_FIELDS),
        "representation": "ASCII JSON string literal",
        "http_values": "unchanged logical strings",
    }
    return schema


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    relative_path = Path("src/aap_workspace/membership_protocol.schema.json")
    target = ROOT / relative_path
    schema = render_schema()
    if args.check:
        if not target.exists() or json.loads(target.read_text()) != schema:
            raise SystemExit(
                f"Workspace membership schema differs from its frozen models: {relative_path}"
            )
    else:
        target.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
