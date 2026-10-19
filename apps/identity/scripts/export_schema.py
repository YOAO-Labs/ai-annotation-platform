"""Generate the distributable Identity wire schema from the closed models."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pydantic import TypeAdapter  # noqa: E402
from aap_identity.contract import (  # noqa: E402
    AuthenticateRequest,
    CandidatesPage,
    FailureReply,
    PrincipalReply,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    schema = TypeAdapter(
        AuthenticateRequest | PrincipalReply | FailureReply | CandidatesPage
    ).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "aap-identity/v1"
    schema["$defs"]["Principal"]["allOf"] = [
        {
            "oneOf": [
                {
                    "properties": {
                        "credential_type": {"const": "jwt"},
                        "generation": {
                            "type": "integer",
                            "minimum": 0,
                            "exclusiveMaximum": 2**63,
                        },
                        "scopes": {"type": "null"},
                        "expires_at": {"type": "string", "format": "date-time"},
                    }
                },
                {
                    "properties": {
                        "credential_type": {"const": "api_key"},
                        "generation": {"type": "null"},
                        "scopes": {"type": "array", "items": {"type": "string"}},
                    }
                },
            ]
        }
    ]
    content = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    target = ROOT / "src/aap_identity/protocol.schema.json"
    if args.check:
        if not target.exists() or json.loads(target.read_text()) != schema:
            raise SystemExit("Identity schema differs from its frozen models")
    else:
        target.write_text(content)


if __name__ == "__main__":
    main()
